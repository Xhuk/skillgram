"""Long-running surfaces: the MCP stdio server, the watcher, the daemon,
and autostart."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .drift import status, sync
from .harnesses import caller_identity
from .hub import VERSION, Hub, log
from .lifecycle import index_search, list_skills, touch_usage
from .llm import llm_config
from .scope import project_status, promote, resolve_project
from .select import missing, propose_skill

MCP_TOOLS = [
    {"name": "skillgram_search",
     "description": "Full-text search over the skill hub (FTS5). Use it before proposing a new skill.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"},
                                                     "limit": {"type": "integer", "default": 10}},
                     "required": ["query"]}},
    {"name": "skillgram_missing",
     "description": "Ask whether the hub covers a capability. found=false means: do not invent, tell the user.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "skillgram_use",
     "description": "Promote hub skills into the current project scope (idempotent). Records the usage with the harness that called it.",
     "inputSchema": {"type": "object", "properties": {"skills": {"type": "array", "items": {"type": "string"}},
                                                     "by": {"type": "string"},
                                                     "project": {"type": "string"}},
                     "required": ["skills"]}},
    {"name": "skillgram_status",
     "description": "Hub health and the project scope summary.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "skillgram_propose",
     "description": "Draft (and with apply=true publish) a new skill with the connected LLM. Requires approval.",
     "inputSchema": {"type": "object", "properties": {"need": {"type": "string"},
                                                     "apply": {"type": "boolean", "default": False},
                                                     "project": {"type": "string"}},
                     "required": ["need"]}},
    {"name": "skillgram_touch",
     "description": "Declare that you used these skills for the current task. This is the only usage signal skillgram can see; `by` defaults to the harness that called it.",
     "inputSchema": {"type": "object", "properties": {"skills": {"type": "array", "items": {"type": "string"}},
                                                     "by": {"type": "string"},
                                                     "project": {"type": "string"}},
                     "required": ["skills"]}},
    {"name": "skillgram_list",
     "description": "List the hub (optionally one category, optionally only used/unused in this project).",
     "inputSchema": {"type": "object", "properties": {"category": {"type": "string"},
                                                     "used": {"type": "string", "enum": ["used", "unused"]},
                                                     "project": {"type": "string"},
                                                     "limit": {"type": "integer"}}}},
]


def mcp_call(hub: Hub, name: str, args: dict) -> dict:
    """Despacho de una tool MCP. Devuelve el resultado como dict."""
    if name == "skillgram_search":
        return index_search(str(args.get("query", "")), int(args.get("limit", 10) or 10))
    if name == "skillgram_missing":
        return missing(hub, str(args.get("query", "")))
    if name == "skillgram_use":
        proj = resolve_project(args.get("project"))
        result = promote(hub, [str(s) for s in args.get("skills", [])], Path(proj["path"]))
        # the MCP server runs inside the harness, so the process tree names it: the
        # record says `claude`/`codex`/`reasonix`, not a constant
        by = str(args.get("by") or caller_identity("mcp"))
        result["uso"] = touch_usage(Path(proj["path"]),
                                    [str(s) for s in args.get("skills", [])], by=by).get("touched", [])
        result["by"] = by
        return result
    if name == "skillgram_status":
        proj = resolve_project(args.get("project"))
        return {"hub": status(hub), "project": project_status(hub, Path(proj["path"]))}
    if name == "skillgram_propose":
        proj = resolve_project(args.get("project"))
        return propose_skill(hub, Path(proj["path"]), str(args.get("need", "")),
                             llm_config(), apply=bool(args.get("apply")))
    if name == "skillgram_touch":
        proj = resolve_project(args.get("project"))
        return touch_usage(Path(proj["path"]), [str(s) for s in args.get("skills", [])],
                           by=str(args.get("by") or caller_identity("mcp")))
    if name == "skillgram_list":
        proj = resolve_project(args.get("project")) if args.get("project") else None
        return list_skills(hub, category=args.get("category"),
                           project=Path(proj["path"]) if proj else None,
                           used=args.get("used"), limit=args.get("limit"))
    return {"ok": False, "error": f"unknown tool: {name}"}


def mcp_serve(hub: Hub, stdin=None, stdout=None, read_only: bool = False) -> int:
    """Server MCP minimo: initialize, tools/list, tools/call, ping."""
    import sys as _sys

    fin = stdin or _sys.stdin
    fout = stdout or _sys.stdout

    def reply(payload):
        fout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fout.flush()

    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            reply({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "skillgram", "version": VERSION},
            }})
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "ping":
            reply({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            reply({"jsonrpc": "2.0", "id": mid, "result": {"tools": MCP_TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if read_only and name in ("skillgram_use", "skillgram_propose"):
                    out = {"ok": False, "error": "read-only mode: this tool cannot write"}
                else:
                    out = mcp_call(hub, name, args)
            except Exception as exc:  # noqa: BLE001 - el server no debe morir
                out = {"ok": False, "error": str(exc)}
            reply({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "isError": not out.get("ok", False),
            }})
        elif mid is not None:
            reply({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"metodo no soportado: {method}"}})
    return 0


def hub_fingerprint(hub: Hub) -> tuple:
    """Huella barata del hub: el conjunto de skills y el mtime de cada SKILL.md."""
    items = []
    for name, tipo in sorted(hub.skills().items()):
        f = hub.root / tipo / name / "SKILL.md"
        try:
            items.append((name, tipo, int(f.stat().st_mtime)))
        except OSError:
            items.append((name, tipo, 0))
    return tuple(items)


class Watcher:
    """Re-sincroniza cuando cambia el hub. Poll: sin dependencias."""

    def __init__(self, hub: Hub, interval: float = 5.0, quiet: bool = False):
        self.hub = hub
        self.interval = interval
        self.quiet = quiet
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sync: dict | None = None
        self.last_change: float | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="skills-sync-watch", daemon=True)
        self._thread.start()
        if not self.quiet:
            log(f"watch: vigilando {self.hub.root} cada {self.interval:g}s")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if not self.quiet:
            log("watch: detenido")

    def _loop(self) -> None:
        # La base se toma ANTES del primer sync y **no** se re-lee despues: si se releyera,
        # una skill que aparezca *durante* ese sync quedarÃ­a dentro de la base sin haberse
        # enlazado, y el vigilante no volverÃ­a a mirarla hasta el siguiente cambio no
        # relacionado (pasaba de verdad: una skill se quedaba missing links).
        # Con la base de antes, el primer sondeo ve el cambio y sincroniza otra vez;
        # repetir un sync es idempotente, asÃ­ que no cuesta nada.
        previous = hub_fingerprint(self.hub)
        self.last_sync = sync(self.hub, quiet=self.quiet)
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            current = hub_fingerprint(self.hub)
            if os.environ.get("SKILLGRAM_DEBUG") or os.environ.get("SKILLS_SYNC_DEBUG"):
                log(f"debug: huella {len(previous)} -> {len(current)} Â· iguales={current == previous}")
            if current != previous:
                changed = [n for n, _, _ in current if n not in {x[0] for x in previous}]
                gone = [n for n, _, _ in previous if n not in {x[0] for x in current}]
                detail = ", ".join(
                    filter(None, [f"+{len(changed)}" if changed else "", f"-{len(gone)}" if gone else ""])
                )
                self.last_change = time.time()
                if not self.quiet:
                    log(f"watch: cambio en el hub ({detail or 'mtime'}) -> sync")
                self.last_sync = sync(self.hub, quiet=self.quiet)
                previous = hub_fingerprint(self.hub)


def single_instance(name: str = "skillgram_watch") -> bool:
    """True si este proceso es el Ãºnico vigilante.

    Importa porque el vigilante es un **hijo** del proceso que lo lanza: si edgePass se cierra
    de golpe, en Windows el hijo sobrevive. Sin candado, el siguiente arranque levanta un
    segundo vigilante y quedan dos sincronizando (inofensivo, pero invisible).
    """
    if os.name != "nt":
        lock = Path(tempfile.gettempdir()) / f"{name}.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except OSError:
            return False
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, f"Local\\{name}")
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def daemon(hub: Hub, interval: float = 5.0, watch_at_start: bool = False) -> int:
    watcher = Watcher(hub, interval=interval, quiet=True)
    if watch_at_start:
        watcher.start()

    def reply(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    reply({"ok": True, "op": "hello", "version": VERSION, "hub": str(hub.root)})

    for raw in sys.stdin:
        raw = raw.lstrip("\ufeff").strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply({"ok": False, "error": "the line is not JSON"})
            continue
        op = str(msg.get("op", "")).strip()
        try:
            if op == "ping":
                reply({"ok": True, "op": "pong"})
            elif op == "status":
                reply(status(hub, watching=watcher.running))
            elif op in ("sync", "push"):
                done = sync(hub, quiet=True)
                watcher.last_sync = done
                reply(done)
            elif op == "watch.on":
                if "interval" in msg:
                    watcher.interval = float(msg["interval"])
                if not watcher.running and not single_instance():
                    reply({"ok": False, "error": "a watcher is already running"})
                    continue
                watcher.start()
                reply({"ok": True, "op": "watch.on", "watching": watcher.running})
            elif op == "watch.off":
                watcher.stop()
                reply({"ok": True, "op": "watch.off", "watching": watcher.running})
            elif op == "quit":
                reply({"ok": True, "op": "quit"})
                break
            else:
                reply({"ok": False, "error": f"unknown op: {op or '(empty)'}"})
        except Exception as exc:  # noqa: BLE001 - el daemon no debe morir por una op
            reply({"ok": False, "op": op, "error": f"{type(exc).__name__}: {exc}"})
    watcher.stop()
    return 0


TASK_NAME = "skills-sync-watch"


def autostart_script() -> Path:
    return Path(__file__).resolve()


def install_autostart(hub: Hub, interval: float, pythonw: str | None = None) -> dict:
    exe = pythonw or shutil.which("pythonw") or shutil.which("python") or sys.executable
    cmd = f'"{exe}" "{autostart_script()}" watch --hub "{hub.root}" --interval {interval:g}'
    proc = subprocess.run(
        ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", cmd],
        capture_output=True,
        text=True,
    )
    return {
        "ok": proc.returncode == 0,
        "op": "install-autostart",
        "task": TASK_NAME,
        "comando": cmd,
        "salida": (proc.stdout or proc.stderr or "").strip()[:400],
    }


def uninstall_autostart() -> dict:
    proc = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True
    )
    return {
        "ok": proc.returncode == 0,
        "op": "uninstall-autostart",
        "task": TASK_NAME,
        "salida": (proc.stdout or proc.stderr or "").strip()[:400],
    }

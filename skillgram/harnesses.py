"""The harnesses themselves: identity, detection, doctor, the ledger,
the MCP registration, and the machine-wide `install`/`propagate`."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .bundled import seed_hub
from .govern import seed_skills
from .hub import (
    PROJECT_CONFIG,
    VERSION,
    Hub,
    _first_lines,
    _project_name,
    _under,
    _which,
    detect_harnesses,
    home_dir,
    is_link,
    launch_argv,
    link_target,
    normalize_harness,
    project_dir,
    run_cli,
    shell_cmd,
)
from .llm import llm_config
from .scope import init_project, inject_protocol, mount_project, project_scope, resolve_project
from .select import bootstrap


def doctor(hub: Hub) -> dict:
    """Inventario read-only: links, dead, missingtes, native y collisions."""
    known = hub.skills()
    rows = []
    for agent_dir in hub.agents():
        links = dead = natives = 0
        collisions = []
        present = set()
        for entry in sorted(agent_dir.iterdir()):
            if is_link(entry):
                tgt = link_target(entry)
                if tgt and Path(tgt).exists() and _under(Path(tgt), hub.root):
                    links += 1
                    present.add(entry.name)
                else:
                    dead += 1
            elif entry.is_dir() and (entry / "SKILL.md").is_file():
                natives += 1
                present.add(entry.name)
                if entry.name in known:
                    collisions.append(entry.name)
        rows.append({
            "path": str(agent_dir),
            "app": agent_dir.parent.name,
            "hub_links": links,
            "dead": dead,
            "missing": len([n for n in known if n not in present]),
            "native": natives,
            "collisions": sorted(collisions),
        })

    recos = []
    if any(r["missing"] or r["dead"] for r in rows):
        recos.append("run `sync` to create missing links and drop dead ones")
    nat = sum(r["native"] for r in rows)
    if nat:
        recos.append(f"{nat} agent-native skills: left as they are, skillgram never touches them")
    col = sum(len(r["collisions"]) for r in rows)
    if col:
        recos.append(f"{col} name collisions: `sync` skips them (never overwrites)")
    if not known:
        recos.append(f"the hub {hub.root} is empty: add <type>/<skill>/SKILL.md")

    return {
        "ok": True,
        "op": "doctor",
        "version": VERSION,
        "hub": str(hub.root),
        "skills_hub": len(known),
        "categories": len(hub.tipos()),
        "agents": rows,
        "harnesses": detect_harnesses(),
        "recommendations": recos,
    }


MCP_SERVER = "skillgram"


HARNESS_ENV = "SKILLGRAM_HARNESS"


LEDGER_ENV = "SKILLGRAM_LEDGER"


MCP_CMD_ENV = "SKILLGRAM_MCP_CMD"


INTERACTIVE_ADD = {"hermes"}


HARNESS_MCP_CLI = {
    "reasonix": "reasonix",
    "claude-code": "claude",
    "codex": "codex",
    "hermes": "hermes",
    "gemini-cli": "gemini",
}


HARNESS_EFFECT = {
    "reasonix": "Reasonix: /reload (or restart the app) to connect the skillgram MCP server",
    "codex": "Codex: restart the session (`codex` again); MCP servers are read at startup",
    "claude-code": "Claude Code: restart the CLI (`claude` again) to load the new MCP server",
    "hermes": "Hermes: restart the Hermes session to connect the MCP server",
    "gemini-cli": "Gemini CLI: restart the CLI to load the new MCP server",
    "cursor": "Cursor: Reload Window, then enable the skillgram server in Settings > MCP",
}


GENERIC_EFFECT = "restart the harness session so it connects the skillgram MCP server"


PROCESS_HARNESS = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "cursor": "cursor",
    "hermes": "hermes",
    "hermes-agent": "hermes",
    "gemini": "gemini-cli",
    "qwen": "qwen-code",
    "kiro": "kiro",
    "opencode": "opencode",
    "aider": "aider",
    "windsurf": "windsurf",
    "reasonix": "reasonix",
    "reasonix-desktop": "reasonix",
    "reasonix-cli": "reasonix",
}


DISCOVERY_SKIP = {
    "node_modules", "venv", ".venv", "env", "dist", "build", "target", ".next", ".nuxt",
    "__pycache__", ".cache", ".turbo", ".parcel-cache", ".idea", ".vs", ".vscode", "vendor",
    "Pods", ".gradle", "bin", "obj", ".tox", ".mypy_cache", ".pytest_cache", ".terraform",
    ".angular", "coverage", "out", ".svelte-kit",
}


def _process_chain_win(limit: int = 6) -> list:
    script = (
        f"$cur = {os.getpid()}; $names = @();"
        f"for ($i = 0; $i -lt {limit}; $i++) {{"
        "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$cur\" -ErrorAction SilentlyContinue;"
        "  if (-not $p) { break };"
        "  $names += $p.Name; $cur = [int]$p.ParentProcessId; if ($cur -le 0) { break }"
        "};"
        "if ($names.Count -eq 0) { '[]' } else { $names | ConvertTo-Json -Compress }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=25, stdin=subprocess.DEVNULL,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    return [str(x) for x in (data if isinstance(data, list) else [data])]


def _process_chain_posix(limit: int = 6) -> list:
    names, pid = [], os.getpid()
    for _ in range(limit):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            start, end = stat.index("("), stat.rindex(")")
            names.append(stat[start + 1:end])
            pid = int(stat[end + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 0:
            break
    return names


def process_chain(limit: int = 6) -> list:
    """Ancestor process names of this process, nearest first. Best effort."""
    try:
        return _process_chain_win(limit) if os.name == "nt" else _process_chain_posix(limit)
    except Exception:  # noqa: BLE001 - an identity hint may never break install
        return []


def resolve_harness(explicit: str | None = None) -> dict:
    """SKILLGRAM_HARNESS -> --harness -> process tree -> unknown (never a guess)."""
    env = (os.environ.get(HARNESS_ENV) or "").strip()
    flag = (explicit or "").strip()
    raw, source, chain = "", "unknown", []
    if env:
        raw, source = env, "env"
    elif flag:
        raw, source = flag, "flag"
    else:
        chain = process_chain()
        for proc_name in chain:
            stem = Path(proc_name).stem.lower()
            if stem in PROCESS_HARNESS:
                raw, source = PROCESS_HARNESS[stem], "process"
                break
    name = normalize_harness(raw) if raw else "unknown"
    out = {"name": name or "unknown", "source": source, "process_chain": chain}
    if env and flag and normalize_harness(flag) != out["name"]:
        out["note"] = (f"{HARNESS_ENV}={env} wins over --harness {flag}; unset {HARNESS_ENV} "
                       f"to install for another harness from here")
    return out


def caller_identity(fallback: str = "agent") -> str:
    """Who is asking, for the usage record: the harness, never a guess.

    The harness is a mask, so the record has to say which mask was on: `use` and
    `touch` call this instead of writing a constant. When nothing knows (no env, no
    `--harness`, no harness in the process tree) it falls back to the caller's own
    default rather than inventing a name.
    """
    name = resolve_harness()["name"]
    return name if name and name != "unknown" else fallback


def ledger_path() -> Path:
    env = (os.environ.get(LEDGER_ENV) or "").strip()
    return Path(env) if env else home_dir() / ".agents" / "skillgram-ledger.json"


def _ledger_key(path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def read_ledger() -> dict:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    if not isinstance(data.get("repos"), dict):
        data["repos"] = {}
    return data


def write_ledger(data: dict) -> Path:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def discover_repos(root: Path, depth: int = 3, limit: int = 200) -> list:
    """Every git repo at or under `root`, bounded by depth and count."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    found: list = []
    stack = [(root, 0)]
    while stack and len(found) < limit:
        cur, level = stack.pop()
        if (cur / ".git").exists():
            found.append(cur.resolve())
        if level >= depth:
            continue
        try:
            entries = sorted(cur.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or is_link(entry):
                continue
            if entry.name.startswith(".") or entry.name in DISCOVERY_SKIP:
                continue
            stack.append((entry, level + 1))
    return sorted(found)


def _is_real_exe(path: str | None) -> bool:
    """A path a harness can spawn directly: a file with no shim wrapper.

    `shutil.which("skillgram")` also resolves `./skillgram.py` (PATHEXT has .PY
    and an empty PATH entry means the cwd) and, on Windows, matches plain
    directories. Neither can be the launch command of an MCP server.
    """
    if not path:
        return False
    try:
        if not Path(path).is_file():
            return False
    except OSError:
        return False
    return Path(path).suffix.lower() in ("", ".exe")


def mcp_launch_argv() -> list:
    """How a harness starts the MCP server: `[*launch_argv(), "mcp"]`."""
    override = (os.environ.get(MCP_CMD_ENV) or "").strip()
    if override:
        return override.split()
    exe = _which("skillgram")
    if _is_real_exe(exe):
        return [exe, "mcp"]
    return [*launch_argv(), "mcp"]


def mcp_env_pairs(hub: Hub, harness_name: str) -> list:
    return [f"{HARNESS_ENV}={harness_name}", f"SKILLGRAM_HUB={hub.root}"]


def harness_cli(harness_name: str, client: str) -> str | None:
    """The CLI to delegate to: SKILLGRAM_MCP_CLI_<HARNESS> wins over PATH.

    Some harnesses ship more than one launcher (a shared install plus an isolated
    home, or a venv shim next to a self-updating one). The override is the only
    supported way to say which one owns the config skillgram has to write.
    """
    key = "SKILLGRAM_MCP_CLI_" + harness_name.upper().replace("-", "_")
    override = (os.environ.get(key) or "").strip()
    if override:
        return override
    return _which(client)


def mcp_add_argv(client: str, name: str, launch: list, envs: list, binary: str | None = None) -> list:
    """The exact `mcp add` command for each harness CLI (verified against the real CLIs)."""
    exe = binary or client
    if client == "reasonix":
        return [exe, "mcp", "add", name, *[f"--env={e}" for e in envs], *launch]
    if client == "codex":
        return [exe, "mcp", "add", name,
                *[x for e in envs for x in ("--env", e)], "--", *launch]
    if client == "claude":
        return [exe, "mcp", "add", name, "-s", "user",
                *[x for e in envs for x in ("-e", e)], "--", *launch]
    if client == "hermes":
        return [exe, "mcp", "add", name, "--env", *envs,
                "--command", launch[0], "--args", *launch[1:]]
    if client == "gemini":
        return [exe, "mcp", "add", name, *[x for e in envs for x in ("-e", e)], *launch]
    raise ValueError(f"no mcp add form for {client}")


def mcp_probe_argv(client: str, name: str, binary: str | None = None) -> list:
    exe = binary or client
    if client == "hermes":
        return [exe, "mcp", "list"]
    return [exe, "mcp", "get", name]


def mcp_remove_argv(client: str, name: str, binary: str | None = None) -> list:
    exe = binary or client
    if client == "claude":
        return [exe, "mcp", "remove", name, "-s", "user"]
    return [exe, "mcp", "remove", name]


def _name_listed(text: str, name: str) -> bool:
    return bool(re.search(r"(?mi)^\s*(?:[-*]\s*)?[\"']?" + re.escape(name) + r"\b", text or ""))


def register_mcp(hub: Hub, harness_name: str, apply: bool = True, runner=None) -> dict:
    """Delegate the MCP registration to the harness CLI. Idempotent."""
    client = HARNESS_MCP_CLI.get(harness_name)
    out = {"harness": harness_name, "server": MCP_SERVER, "client": client, "supported": bool(client)}
    if not client:
        out["state"] = "unsupported"
        out["detail"] = (f"{harness_name} has no MCP CLI: register the server by hand once from the harness UI "
                         f"(Cursor: Settings > MCP), or run `skillgram install --harness <other>` there")
        return out
    binary = harness_cli(harness_name, client)
    if not binary:
        out["state"] = "not-installed"
        out["detail"] = (f"`{client}` is not on PATH, so the MCP server could not be registered "
                         f"(point at a launcher with SKILLGRAM_MCP_CLI_{harness_name.upper().replace('-', '_')})")
        return out
    out["cli"] = binary

    launch = mcp_launch_argv()
    marker = launch[1] if len(launch) > 1 else launch[0]
    call = runner or run_cli

    def probe():
        code, text = call(mcp_probe_argv(client, MCP_SERVER, binary))
        return code == 0 and (client != "hermes" or _name_listed(text, MCP_SERVER)), code, text

    registered, code, text = probe()
    if registered:
        out.update({"state": "already", "exit": code, "detail": _first_lines(text)})
        # `mcp get` prints the whole command; `hermes mcp list` truncates it, so the
        # "points elsewhere" check would fire on a merely cut-off path.
        if client != "hermes" and os.path.normcase(marker) not in os.path.normcase(text or ""):
            out["note"] = (f"a server named '{MCP_SERVER}' is already registered but it does not point at {marker}; "
                           f"remove it first: {shell_cmd(mcp_remove_argv(client, MCP_SERVER, binary))}")
        return out

    argv = mcp_add_argv(client, MCP_SERVER, launch, mcp_env_pairs(hub, harness_name), binary)
    out["command"] = argv
    if not apply:
        out["state"] = "planned"
        return out
    if runner is not None:
        code, text = runner(argv)
    else:
        code, text = run_cli(argv, stdin_text="y\n" if client in INTERACTIVE_ADD else None)
    out.update({"exit": code, "detail": _first_lines(text, 12)})
    # A CLI can exit 0 without writing anything (hermes cancels its own Y/n prompt
    # when stdin is closed). The registration is the config, so probe again.
    verified, _pcode, ptext = probe()
    out["verified"] = verified
    if code == 0 and verified:
        out["state"] = "registered"
        return out
    out["state"] = "failed"
    if code == 0:
        out["detail"] = (f"{_first_lines(text, 3).replace(chr(10), ' | ')} "
                         f"(cli exited 0 but the server is not registered)")
    elif ptext:
        out["detail"] = _first_lines(ptext, 3)
    return out


def propagate_repo(hub: Hub, repo: Path, harnesses: list | None = None, apply: bool = False,
                   cfg: dict | None = None, known: dict | None = None) -> dict:
    """mount --apply + protocol + registration for one repo. Never invents a set."""
    repo = Path(repo)
    row = {"repo": str(repo), "apply": apply}
    if not repo.is_dir():
        row.update({"status": "skipped", "reason": "not a directory"})
        return row

    init_state = "exists"
    if not (project_dir(repo) / PROJECT_CONFIG).is_file():
        if apply:
            created = init_project(_project_name(repo), str(repo))
            init_state = "created" if created.get("ok") else str(created.get("error"))
        else:
            init_state = "would create"
    row["init"] = init_state

    scope = project_scope(hub, repo, known)
    if not scope["skills"] and cfg:
        picked = bootstrap(hub, repo, cfg, apply=apply)
        row["bootstrap"] = [s.get("skill") for s in picked.get("selection", [])]
        scope = project_scope(hub, repo, known)
    if not scope["skills"]:
        row.update({"status": "skipped",
                    "reason": "no skills in scope: run `skillgram use <skill>...` here "
                              "(or pass --bootstrap with an LLM configured)"})
        return row

    try:
        mount = mount_project(hub, repo, apply=apply, only=harnesses, known=known)
    except Exception as exc:  # noqa: BLE001 - one odd repo may not abort a whole sweep
        row.update({"status": "skipped", "reason": f"mount failed: {type(exc).__name__}: {exc}"})
        return row
    if not mount.get("ok"):
        row.update({"status": "skipped", "reason": mount.get("error", "mount failed")})
        return row
    if not mount["harnesses"]:
        row.update({"status": "skipped",
                    "reason": f"no project skills dir for {', '.join(harnesses or ['this harness'])} "
                              f"(harness not installed?)"})
        return row

    proto = inject_protocol(hub, repo, apply=apply)
    created = sum(len(r["creados"]) for r in mount["harnesses"])
    kept = sum(len(r["already_present"]) for r in mount["harnesses"])
    row.update({
        "status": "planned" if not apply else ("mounted" if created else "already"),
        "skills": mount["skills"],
        "dirs": [r["dir"] for r in mount["harnesses"]],
        "created": created,
        "kept": kept,
        "native_skipped": sum(len(r["nativos_sin_tocar"]) for r in mount["harnesses"]),
        "protocol": [f["file"] for f in proto["files"] if f.get("changed")],
    })
    return row


def propagate(hub: Hub, harness: str | None = None, root: str | None = None, apply: bool = False,
              depth: int = 3, force: bool = False, single: bool = False,
              bootstrap_repos: bool = False) -> dict:
    """Propagate the project scope to the repos of one harness, skipping the ledger."""
    resolved = resolve_harness(harness)
    name = resolved["name"]
    harnesses = None if name == "unknown" else [name]
    start = Path(root).expanduser().resolve() if root else Path(resolve_project()["path"]).resolve()

    if single or not root:
        repos = [start] if (start / ".git").exists() else []
    else:
        repos = discover_repos(start, depth=depth)

    hub_resolved = hub.root.resolve()
    ledger = read_ledger()
    pending, ledger_skipped, ignored = [], 0, []
    for repo in repos:
        if repo == hub_resolved or _under(hub_resolved, repo):
            ignored.append(str(repo))
            continue
        if ledger["repos"].get(_ledger_key(repo)) and not force:
            ledger_skipped += 1
            continue
        pending.append(repo)

    cfg = llm_config() if bootstrap_repos else None
    known = hub.skills()
    rows = [propagate_repo(hub, repo, harnesses, apply=apply, cfg=cfg, known=known) for repo in pending]

    recorded = 0
    if apply:
        for row in rows:
            if row["status"] in {"mounted", "already"}:
                ledger["repos"][_ledger_key(row["repo"])] = {
                    "harness": name,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "mounted": row["created"],
                    "kept": row["kept"],
                    "protocol": bool(row["protocol"]),
                }
                recorded += 1
        if recorded:
            write_ledger(ledger)

    result = {
        "ok": True,
        "op": "propagate",
        "harness": {"name": name, "source": resolved["source"]},
        "root": str(start),
        "applied": apply,
        "repos_found": len(repos),
        "ledger_skipped": ledger_skipped,
        "rows": rows,
        "recorded": recorded,
        "ledger": str(ledger_path()),
    }
    if name == "unknown":
        result.update({"ok": False, "error": "harness unknown: pass --harness <name> or set SKILLGRAM_HARNESS"})
    elif not repos:
        result.update({"ok": False,
                       "error": f"no git repository at or under {start}" if root else
                                f"{start} is not a git repository: pass --root <dir> to scan for repos"})
    for key in ("note",):
        if resolved.get(key):
            result[key] = resolved[key]
    if ignored:
        result["ignored_hub"] = ignored
    return result


def _summarize_skips(rows: list) -> list:
    """One line per skip reason: sixty identical lines are not information."""
    groups: dict = {}
    for row in rows:
        if row["status"] == "skipped":
            groups.setdefault(row.get("reason", "skipped"), []).append(row["repo"])
    lines = []
    for reason, repos in groups.items():
        if len(repos) == 1:
            lines.append(f"skipped {repos[0]}: {reason}")
        else:
            preview = ", ".join(Path(r).name for r in repos[:3])
            more = f" and {len(repos) - 3} more" if len(repos) > 3 else ""
            lines.append(f"{len(repos)} repo(s) skipped ({preview}{more}): {reason}")
    return lines


def install(hub: Hub, harness: str | None = None, root: str | None = None, apply: bool = True,
            runner=None, bootstrap_repos: bool = False, depth: int = 2,
            bundled: bool = True) -> dict:
    """One idempotent command: harness -> hub -> MCP (delegated) -> propagate."""
    resolved = resolve_harness(harness)
    name = resolved["name"]
    done, already, blocked, effect = [], [], [], []
    failed = False

    # 1. hub
    hub_created = not hub.root.is_dir()
    if hub_created and apply:
        hub.root.mkdir(parents=True, exist_ok=True)
    seeded = seed_hub(hub, apply=apply) if bundled else {"rows": [], "created": []}
    for row in seeded["rows"]:
        line = f"base skill {row['skill']} -> {row['path']} ({row['state']})"
        (done if row["state"] in {"created", "would"} else already).append(line)
    known = hub.skills()
    if hub_created:
        done.append(f"hub {'created' if apply else 'would be created'}: {hub.root}")
    else:
        already.append(f"hub in place: {hub.root} ({len(known)} skills)")
    if seeded["created"] and apply:
        seed_skills(hub, seeded["created"], "always")
        done.append("base skills marked as seeds (always): " + ", ".join(seeded["created"]))
    # a dry run that would seed the base is not an empty hub: saying both would be a lie
    if not known and not any(row["state"] in {"created", "would"} for row in seeded["rows"]):
        blocked.append(f"hub is empty: add <category>/<skill>/SKILL.md or run `skillgram onboard` ({hub.root})")
        failed = True

    # 2. MCP server, delegated to the harness CLI
    mcp: dict = {"harness": name, "state": "blocked", "supported": False}
    if name == "unknown":
        blocked.append(f"harness unknown (checked {HARNESS_ENV}, --harness and the process tree); "
                       "run install from inside the harness or pass --harness <reasonix|claude-code|codex|hermes>")
        failed = True
    else:
        mcp = register_mcp(hub, name, apply=apply, runner=runner)
        state = mcp["state"]
        cmd = shell_cmd(mcp.get("command") or [])
        if state == "registered":
            done.append(f"MCP server '{MCP_SERVER}' registered in {mcp['client']} "
                        f"({HARNESS_ENV}={name}): {cmd}")
        elif state == "already":
            already.append(f"MCP server '{MCP_SERVER}' already registered in {mcp['client']}")
        elif state == "planned":
            done.append(f"would register the MCP server in {mcp['client']}: {cmd}")
        elif state in {"unsupported", "not-installed"}:
            blocked.append(mcp["detail"])
        else:
            detail = _first_lines(mcp.get("detail") or "no output", 1)
            blocked.append(f"registering the MCP server in {mcp['client']} failed "
                           f"(exit {mcp.get('exit')}): {detail}")
            failed = True
        if mcp.get("note"):
            blocked.append(mcp["note"])
        if state in {"registered", "planned"}:
            effect.append(HARNESS_EFFECT.get(name, GENERIC_EFFECT))

    # 3. propagate to the repos of this harness
    prop: dict = {"repos_found": 0, "ledger_skipped": 0, "rows": [], "root": None, "ledger": str(ledger_path())}
    if name != "unknown":
        prop = propagate(hub, harness=name, root=root, apply=apply, depth=depth,
                         single=root is None, bootstrap_repos=bootstrap_repos)
        for row in prop["rows"]:
            state = row["status"]
            if state == "mounted":
                extra = f"; protocol in {len(row['protocol'])} file(s)" if row["protocol"] else ""
                done.append(f"{row['repo']}: mounted {row['created']} link(s) in "
                            f"{', '.join(row['dirs'])}{extra}")
            elif state == "already":
                already.append(f"{row['repo']}: already mounted ({row['kept']} link(s) in place)")
            elif state == "planned":
                done.append(f"would mount {row['skills']} skill(s) of {row['repo']} in {', '.join(row['dirs'])}")
        blocked.extend(_summarize_skips(prop["rows"]))
        if prop["ledger_skipped"]:
            already.append(f"{prop['ledger_skipped']} repo(s) already in the ledger ({prop['ledger']})")
        if not prop["rows"] and not prop["ledger_skipped"]:
            blocked.append(prop.get("error") or
                           f"no git repository under {prop.get('root')}: pass --root <dir> (e.g. --root C:\\proyectos)")

    return {
        "ok": not failed,
        "op": "install",
        "version": VERSION,
        "applied": apply,
        "harness": resolved,
        "hub": {"path": str(hub.root), "skills": len(known), "created": hub_created},
        "mcp": mcp,
        "bundled": seeded,
        "propagate": prop,
        "done": done,
        "already": already,
        "blocked": blocked,
        "effect": effect,
    }


def install_report(result: dict) -> str:
    head = f"skillgram install  hub: {result['hub']['path']}"
    if not result["applied"]:
        head += "  [dry-run: nothing was written]"
    lines = [
        head,
        f"harness: {result['harness']['name']} (from {result['harness']['source']})"
        + (f"  note: {result['harness']['note']}" if result["harness"].get("note") else ""),
    ]
    blocks = (("done now", "done"), ("already in place", "already"),
              ("needs you", "blocked"), ("to take effect", "effect"))
    for title, key in blocks:
        items = result.get(key) or []
        if not items:
            continue
        if key == "done" and not result["applied"]:
            title = "would do"
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)


def propagate_report(result: dict) -> str:
    lines = [
        f"propagate  harness: {result['harness']['name']} ({result['harness']['source']})  root: {result['root']}",
        f"repos: {result['repos_found']} found, {result['ledger_skipped']} already in the ledger, "
        f"{len(result['rows'])} to look at",
    ]
    for row in result["rows"]:
        state = row["status"]
        if state == "mounted":
            lines.append(f"  mounted  {row['repo']}  {row['created']} link(s) -> {', '.join(row['dirs'])}")
        elif state == "already":
            lines.append(f"  already  {row['repo']}  {row['kept']} link(s) in place")
        elif state == "planned":
            lines.append(f"  would    {row['repo']}  {row['skills']} skill(s) -> {', '.join(row['dirs'])}")
    for skip in _summarize_skips(result["rows"]):
        lines.append(f"  {skip}")
    if result.get("error"):
        lines.append(f"\n{result['error']}")
    if not result["applied"] and result["rows"]:
        lines.append("\nplan only: re-run with --apply")
    else:
        lines.append(f"\nledger: {result['ledger']} ({result['recorded']} recorded now)")
    return "\n".join(lines)

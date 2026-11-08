"""Store and links: the hub, what a link is, and the content hash every
later module quotes. No sibling imports: this is the base."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.10.0"


HOME = Path(os.path.expanduser("~"))


def home_dir() -> Path:
    """The one home this process uses: tests move it, every module follows."""
    return HOME


def default_hub() -> Path:
    """El hub canonico. Se puede cambiar con --hub o SKILLS_SYNC_HUB."""
    env = os.environ.get("SKILLGRAM_HUB") or os.environ.get("SKILLS_SYNC_HUB")
    return Path(env) if env else home_dir() / ".agents" / "skills"


DEFAULT_EXCLUDE = {
    "_skill-library-migration",  # el registro de la migracion, no un agente
}


MISSING = object()


class Hub:
    """El estado deseado: que skills hay, en que tipo, y en que agentes."""

    def __init__(self, root: Path, exclude_agents: set[str] | None = None):
        self.root = Path(root)
        self.exclude_agents = DEFAULT_EXCLUDE | (exclude_agents or set())

    def skills(self) -> dict[str, str]:
        """{nombre_skill: tipo} leyendo <hub>/<tipo>/<skill>/SKILL.md (un nivel)."""
        found: dict[str, str] = {}
        if not self.root.is_dir():
            return found
        try:
            tipo_dirs = sorted(self.root.iterdir())
        except OSError:
            return found
        for tipo_dir in tipo_dirs:
            if not tipo_dir.is_dir() or tipo_dir.is_symlink() or self._is_junction(tipo_dir):
                continue
            if tipo_dir.name.startswith(".") or tipo_dir.name.startswith("_") or tipo_dir.name in self.exclude_agents:
                continue
            try:
                skill_dirs = sorted(tipo_dir.iterdir())
            except OSError:
                continue
            for skill_dir in skill_dirs:
                if not skill_dir.is_dir():
                    continue
                if (skill_dir / "SKILL.md").is_file():
                    found[skill_dir.name] = tipo_dir.name
        return found

    def tipos(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return []
        for d in entries:
            if d.is_dir() and not self._is_junction(d) and not d.name.startswith(".") and not d.name.startswith("_"):
                try:
                    children = list(d.iterdir())
                except OSError:
                    continue
                # un tipo es una carpeta cuyas hijas son skills
                if any((c / "SKILL.md").is_file() for c in children if c.is_dir()):
                    out.append(d.name)
        return out

    def nested(self) -> int:
        """Sub-skills que viven dentro de un paquete (mas de <tipo>/<skill>/SKILL.md).

        No se enlazan a proposito: el paquete las espera donde estan. Se cuentan para
        que el numero de `status` no parezca que falta algo.
        """
        total = 0
        if not self.root.is_dir():
            return 0
        try:
            tipo_dirs = list(self.root.iterdir())
        except OSError:
            return 0
        for tipo_dir in tipo_dirs:
            if not tipo_dir.is_dir() or self._is_junction(tipo_dir):
                continue
            try:
                skill_dirs = list(tipo_dir.iterdir())
            except OSError:
                continue
            for skill_dir in skill_dirs:
                if not skill_dir.is_dir():
                    continue
                try:
                    for extra in skill_dir.rglob("SKILL.md"):
                        if extra.parent != skill_dir:
                            total += 1
                except OSError:
                    continue
        return total

    def agents(self) -> list[Path]:
        """Las carpetas `skills/` de cada agente, incluido Reasonix.

        El propio hub NUNCA es un agente. `~/.agents/skills` cae en el barrido de
        `~/.*/skills`, y tratarlo como agente hace que `sync` le enlace cada skill
        dentro de si mismo: 842 junctions que no apuntan a nada nuevo y que git
        materializaria como copias del hub dentro del hub.
        """
        roots: list[Path] = []
        own = self.root.resolve()
        try:
            home_entries = sorted(home_dir().iterdir())
        except OSError:
            home_entries = []
        for entry in home_entries:
            if not entry.is_dir() or not entry.name.startswith("."):
                continue
            if entry.name in self.exclude_agents:
                continue
            cand = entry / "skills"
            try:
                if cand.is_dir() and cand.resolve() != own:
                    roots.append(cand)
            except OSError:
                continue
        appdata = os.environ.get("APPDATA")
        if appdata:
            reasonix = Path(appdata) / "Reasonix" / "skills"
            if reasonix.is_dir() and reasonix.resolve() != own:
                roots.append(reasonix)
        # Una home anidada (`~/.config/opencode`) no cae en el barrido `~/.*/skills`,
        # asi que la tabla responde por ella. Se deduplica por destino real: los
        # harnesses de un solo nivel ya entraron arriba, y el hub nunca es agente.
        seen = set()
        for root in roots:
            try:
                seen.add(root.resolve())
            except OSError:
                continue
        for row in detect_harnesses():
            skills_dir = row.get("skills_dir")
            if not skills_dir:
                continue
            cand = Path(skills_dir)
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved == own or resolved in seen:
                continue
            seen.add(resolved)
            roots.append(cand)
        return roots

    @staticmethod
    def _is_junction(p: Path) -> bool:
        try:
            return bool(os.readlink(p)) and os.path.isdir(p)
        except OSError:
            return False


def is_link(path) -> bool:
    """Un enlace nuestro: symlink o junction.

    Ojo: en Windows `os.DirEntry.is_symlink()` devuelve False para las junctions
    (son reparse points, pero no symlinks). Si solo se mira eso, los 21 mil links
    se leen como carpetas reales y el drift deja de detectarse.
    """
    # `os.path.isjunction` is 3.12+: on 3.9/3.11 the symlink check is all there is
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            if isjunction(path):
                return True
        except OSError:
            return False
    return os.path.islink(path)


def link_target(path: Path) -> Path | None:
    """A donde apunta una junction, o None si no es un enlace."""
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    if not raw:
        return None
    # Las junctions de Windows vienen con el prefijo \\?\ y a veces relativas.
    raw = raw.replace("\\\\?\\", "")
    target = Path(raw)
    if not target.is_absolute():
        target = (path.parent / target).resolve()
    return target


SKILL_META = "SKILLMETA.json"

AGENTS_DIR = "agents"

VAULT_DIR = "_vault"


def skill_meta(skill_dir: Path) -> dict:
    path = skill_dir / SKILL_META
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_skill_meta(skill_dir: Path, meta: dict) -> None:
    path = skill_dir / SKILL_META
    try:
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def record_skill_usage(
    skill_dir: Path,
    project_name: str | None = None,
    stack_tags: list[str] | None = None,
    by: str = "agent",
    unuse: bool = False,
) -> dict:
    """Persist organic usage, project survival, and stack affinity in SKILLMETA.json."""
    if not skill_dir.is_dir():
        return {}
    meta = skill_meta(skill_dir)
    usage = dict(meta.get("usage") or {})
    if unuse:
        usage["unuse_count"] = int(usage.get("unuse_count", 0)) + 1
        projects = [p for p in (usage.get("projects") or []) if p != project_name]
        usage["projects"] = projects
    else:
        usage["total_hits"] = int(usage.get("total_hits", 0)) + 1
        usage["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        usage["last_used_by"] = by
        if project_name:
            projects = list(usage.get("projects") or [])
            if project_name not in projects:
                projects.append(project_name)
            usage["projects"] = projects
        if stack_tags:
            shits = dict(usage.get("stack_hits") or {})
            for tag in stack_tags:
                t = str(tag).strip().lower()
                if t:
                    shits[t] = int(shits.get(t, 0)) + 1
            usage["stack_hits"] = shits
    meta["usage"] = usage
    write_skill_meta(skill_dir, meta)
    return usage


def vault_skills(hub: Hub) -> dict[str, str]:
    """Return {skill_name: category} for skills archived in `<hub>/_vault/<category>/<name>`."""
    vroot = hub.root / VAULT_DIR
    found: dict[str, str] = {}
    if not vroot.is_dir():
        return found
    try:
        cats = sorted(vroot.iterdir())
    except OSError:
        return found
    for cat in cats:
        if not cat.is_dir() or cat.name.startswith("."):
            continue
        try:
            entries = sorted(cat.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                found.setdefault(entry.name, cat.name)
    return found


def unvault_skill(hub: Hub, name: str) -> Path | None:
    """Restore a vaulted skill back into `<hub>/<category>/<name>` on demand."""
    vmap = vault_skills(hub)
    if name not in vmap:
        return None
    tipo = vmap[name]
    src = hub.root / VAULT_DIR / tipo / name
    dest = hub.root / tipo / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    try:
        shutil.move(str(src), str(dest))
        return dest
    except OSError:
        return None


PROJECT_DIR = ".agents"


PROJECT_CONFIG = "config.json"


PROJECT_MANIFEST = "skills.json"


def project_dir(project: Path) -> Path:
    return project / PROJECT_DIR


def _project_name(path: Path) -> str:
    return (path.name or path.drive or "unknown").strip()


def skill_hash(skill_dir: Path) -> str:
    """The content hash of a skill: what it says, not where it came from.

    Two exclusions, both learned the hard way:

    - `SKILLMETA.json` carries a copy of this very hash, so counting it would make the
      value false the moment it is written; a provenance-only update (a `port`, an
      `adopt`) would also read as a content change in every manifest that compares hashes.
    - Line endings are normalised. The same skill checked out on Windows (CRLF) and on
      Linux (LF) is the same skill, and a hash that disagrees would report a whole shipped
      library as stale on the other machine.
    """
    h = hashlib.sha256()
    try:
        files = sorted(skill_dir.rglob("*"))
    except OSError:
        files = []
    for f in files:
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        if f.name == SKILL_META and f.parent == skill_dir:
            continue
        h.update(f.relative_to(skill_dir).as_posix().encode("utf-8", "replace"))
        try:
            h.update(f.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            pass
    return h.hexdigest()[:16]


def dir_bytes(path: Path) -> int:
    """Physical bytes under `path`, never following symlinks or junctions."""
    total = 0
    if not path.exists() or is_link(path):
        return 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if is_link(entry.path):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def project_config_name(project: Path) -> str | None:
    """project_name declarado en <proyecto>/.agents/config.json (si existe)."""
    cfg = project_dir(project) / PROJECT_CONFIG
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    name = str(data.get("project_name") or "").strip()
    return name or None


def _read_manifest(project: Path) -> dict:
    path = project_dir(project) / PROJECT_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("project_name", project_config_name(project) or _project_name(project))
    if not isinstance(data.get("skills"), dict):
        data["skills"] = {}
    return data


def _write_manifest(project: Path, data: dict) -> None:
    """The manifest is committed by the project, and it is written with LF on every
    platform: a value this tool owns must not land as a whole-file diff just because
    the machine is Windows (`use` on a LF manifest used to rewrite all 185 lines).
    """
    path = project_dir(project) / PROJECT_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


HARNESS_HOMES = {
    "claude-code": ".claude",
    "codex": ".codex",
    "cursor": ".cursor",
    "gemini-cli": ".gemini",
    "hermes": ".hermes",
    # OpenCode resolves its global config as `OPENCODE_CONFIG_DIR`, else
    # `$XDG_CONFIG_HOME/opencode`, else `~/.config/opencode` -- and its user skills
    # live in `<config>/skills`. `~/.opencode` was never the home.
    "opencode": ".config/opencode",
    "copilot": ".copilot",
    "windsurf": ".windsurf",
    "kiro": ".kiro",
    "qwen-code": ".qwen",
    "aider": ".aider-desk",
    "antigravity": ".antigravity",
    "kilo": ".kilo",
    "commandcode": ".commandcode",
}


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def detect_harnesses() -> list:
    """Homes de harness conocidos: si estan instalados y si tienen carpeta skills/."""
    out = []
    for app, folder in HARNESS_HOMES.items():
        home = home_dir() / folder
        skills = home / "skills"
        out.append({
            "app": app,
            "home": str(home),
            "instalado": home.is_dir(),
            "skills_dir": str(skills) if skills.is_dir() else None,
        })
    appdata = os.environ.get("APPDATA")
    if appdata:
        rhome = Path(appdata) / "Reasonix"
        rdir = rhome / "skills"
        out.append({
            "app": "reasonix",
            "home": str(rhome),
            "instalado": rhome.is_dir(),
            "skills_dir": str(rdir) if rdir.is_dir() else None,
        })
    return sorted(out, key=lambda r: (not r["instalado"], r["app"]))


DEFAULT_CATEGORY = "imported"


def _skill_summary(skill_dir: Path, limit: int = 400) -> str:
    """Primeras lineas utiles de un SKILL.md, sin frontmatter."""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("---")]
    return "\n".join(lines)[:limit]


def re_findall_words(text: str) -> list:
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum() or ch in "-_":
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


HARNESS_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "gemini": "gemini-cli",
    "qwen": "qwen-code",
    "qwen-code": "qwen-code",
    "aider-desk": "aider",
}


def normalize_harness(value: str) -> str:
    name = str(value or "").strip().lower().replace(" ", "-")
    return HARNESS_ALIASES.get(name, name)


def _which(name: str) -> str | None:
    """shutil.which with a hook the tests can patch."""
    try:
        return shutil.which(name)
    except (OSError, AttributeError):
        return None


def shell_cmd(argv: list) -> str:
    return " ".join(f'"{a}"' if (" " in str(a) or "\t" in str(a)) else str(a) for a in argv)


def launch_argv() -> list:
    """How to start this tool from the outside: `[python, <entry>]`, no subcommand.

    From a checkout that is the `skillgram.py` shim next to the package (a path that
    imports cleanly from any cwd); when installed, `-m skillgram`. One place decides,
    so the MCP registration and the tests cannot disagree.
    """
    python = sys.executable or "python"
    shim = Path(__file__).resolve().parent.parent / "skillgram.py"
    if shim.is_file():
        return [python, str(shim)]
    return [python, "-m", "skillgram"]


def _first_lines(text: str, count: int = 6) -> str:
    return "\n".join((text or "").strip().splitlines()[:count])


def run_cli(argv: list, timeout: int = 300, stdin_text: str | None = None) -> tuple:
    """Run a harness CLI. Returns (exit_code, combined output). Never raises."""
    spawn = list(argv)
    exe = _which(argv[0])
    if exe:
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            spawn = ["cmd", "/c", exe, *argv[1:]]
        else:
            spawn = [exe, *argv[1:]]
    try:
        proc = subprocess.run(spawn, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              input=stdin_text,
                              stdin=None if stdin_text is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def log_path() -> Path:
    """Where the tool writes its own log (env override included)."""
    return Path(os.environ.get("SKILLGRAM_LOG") or os.environ.get("SKILLS_SYNC_LOG")
                or (home_dir() / ".agents" / "skillgram.log"))


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


"""Store and links: the hub, what a link is, and the content hash every
later module quotes. No sibling imports: this is the base."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.9.0"


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
        for tipo_dir in sorted(self.root.iterdir()):
            if not tipo_dir.is_dir() or tipo_dir.is_symlink() or self._is_junction(tipo_dir):
                continue
            if tipo_dir.name.startswith(".") or tipo_dir.name.startswith("_") or tipo_dir.name in self.exclude_agents:
                continue
            for skill_dir in sorted(tipo_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                if (skill_dir / "SKILL.md").is_file():
                    found[skill_dir.name] = tipo_dir.name
        return found

    def tipos(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and not self._is_junction(d) and not d.name.startswith(".") and not d.name.startswith("_"):
                # un tipo es una carpeta cuyas hijas son skills
                if any((c / "SKILL.md").is_file() for c in d.iterdir() if c.is_dir()):
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
        for tipo_dir in self.root.iterdir():
            if not tipo_dir.is_dir() or self._is_junction(tipo_dir):
                continue
            for skill_dir in tipo_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                for extra in skill_dir.rglob("SKILL.md"):
                    if extra.parent != skill_dir:
                        total += 1
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
        for entry in sorted(home_dir().iterdir()):
            if not entry.is_dir() or not entry.name.startswith("."):
                continue
            if entry.name in self.exclude_agents:
                continue
            cand = entry / "skills"
            if cand.is_dir() and cand.resolve() != own:
                roots.append(cand)
        appdata = os.environ.get("APPDATA")
        if appdata:
            reasonix = Path(appdata) / "Reasonix" / "skills"
            if reasonix.is_dir() and reasonix.resolve() != own:
                roots.append(reasonix)
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


PROJECT_DIR = ".agents"


PROJECT_CONFIG = "config.json"


PROJECT_MANIFEST = "skills.json"


def project_dir(project: Path) -> Path:
    return project / PROJECT_DIR


def _project_name(path: Path) -> str:
    return (path.name or path.drive or "unknown").strip()


def skill_hash(skill_dir: Path) -> str:
    """Hash del contenido de la skill (para detectar drift hub vs proyecto)."""
    h = hashlib.sha256()
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        h.update(f.relative_to(skill_dir).as_posix().encode("utf-8", "replace"))
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


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
    path = project_dir(project) / PROJECT_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


HARNESS_HOMES = {
    "claude-code": ".claude",
    "codex": ".codex",
    "cursor": ".cursor",
    "gemini-cli": ".gemini",
    "hermes": ".hermes",
    "opencode": ".opencode",
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


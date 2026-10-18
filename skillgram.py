#!/usr/bin/env python3
"""skillgram - one skill hub, linked into every agent; project scope on demand.

What it is
----------
A single directory of skills on disk (the *hub*) and one junction/symlink per
skill inside each agent's own `skills/` folder. Agents keep their native skills:
skillgram only adds links and never copies, adopts, moves or deletes anything.

    ~/.agents/skills/<tipo>/<skill>/SKILL.md     <- the hub (the only real copy)
    ~/.<agent>/skills/<skill>                    -> junction to the hub
    ~/.<agent>/skills/<tipo>                     -> junction to the category

No skills are bundled. Bring your own hub (or start empty) and skillgram keeps
every agent, IDE and harness on the same library.

Project scope (engram-style)
----------------------------
The hub stays the only store; a project gets a *view* of it:

    <project>/.agents/config.json    {"project_name": "..."}          (init)
    <project>/.agents/skills/<name>  junction to the hub              (use)
    <project>/.agents/skills.json    manifest: tipo, source, hash     (use)

Resolution order mirrors engram: --project -> SKILLGRAM_PROJECT -> nearest
.agents/config.json up to the git root -> cwd. If it cannot resolve, it says so;
it never guesses.

Commands
--------
    doctor   installed harnesses, native skills per agent, name collisions with
             the hub, dead/missing links. Read-only.
    status   current drift, touches nothing.
    sync     apply the desired state (idempotent): create missing links, repoint
             wrong ones, remove dead ones. Never copies, never touches natives.
    watch    watch the hub (add/remove/rename) and re-sync by itself.
    daemon   JSON-line server for supervisors.
    init     write <project>/.agents/config.json (and the empty skills dir).
    use      promote hub skills into a project (junction + manifest).
    unuse    drop them from the project scope.
    project-status   health of a project scope.
    install-autostart / uninstall-autostart   scheduled task on login (Windows).

Output contract: exactly one JSON line per operation on stdout.

    {"ok":true,"op":"status","drift":0,"skills":841,"agents":50,"watching":false}

Quick start
-----------
    python skillgram.py doctor
    python skillgram.py sync
    python skillgram.py watch --interval 5
    python skillgram.py daemon
    python skillgram.py install-autostart

Stdlib only (Python 3.9+). Windows-first (junctions + schtasks); also works on
macOS/Linux with symlinks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Rutas por defecto
# ---------------------------------------------------------------------------

HOME = Path(os.path.expanduser("~"))


def default_hub() -> Path:
    """El hub canonico. Se puede cambiar con --hub o SKILLS_SYNC_HUB."""
    env = os.environ.get("SKILLGRAM_HUB") or os.environ.get("SKILLS_SYNC_HUB")
    return Path(env) if env else HOME / ".agents" / "skills"


# Carpetas que parecen agentes pero no lo son, o que no queremos tocar.
DEFAULT_EXCLUDE = {
    "_skill-library-migration",  # el registro de la migracion, no un agente
}

MISSING = object()


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------


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
            if tipo_dir.name.startswith(".") or tipo_dir.name in self.exclude_agents:
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
            if d.is_dir() and not self._is_junction(d) and not d.name.startswith("."):
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
        """Las carpetas `skills/` de cada agente, incluido el propio hub y Reasonix."""
        roots: list[Path] = []
        for entry in sorted(HOME.iterdir()):
            if not entry.is_dir() or not entry.name.startswith("."):
                continue
            if entry.name in self.exclude_agents:
                continue
            cand = entry / "skills"
            if cand.is_dir():
                roots.append(cand)
        appdata = os.environ.get("APPDATA")
        if appdata:
            reasonix = Path(appdata) / "Reasonix" / "skills"
            if reasonix.is_dir():
                roots.append(reasonix)
        return roots

    @staticmethod
    def _is_junction(p: Path) -> bool:
        try:
            return bool(os.readlink(p)) and os.path.isdir(p)
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Plan / drift
# ---------------------------------------------------------------------------


class Plan:
    """Lo que hay que hacer para que el disco diga lo que dice el hub."""

    def __init__(self) -> None:
        self.missing: list[tuple[Path, Path]] = []  # (link, target)
        self.wrong: list[tuple[Path, Path]] = []  # (link, target correcto)
        self.dead: list[Path] = []  # junctions que apuntan al hub y ya no aplican
        self.skipped: list[tuple[Path, str]] = []  # (ruta, motivo)
        self.by_agent: dict[str, dict[str, int]] = {}

    @property
    def drift(self) -> int:
        return len(self.missing) + len(self.wrong) + len(self.dead)

    def summary(self) -> dict:
        return {
            "faltan": len(self.missing),
            "mal_apuntados": len(self.wrong),
            "muertos": len(self.dead),
            "omitidos": len(self.skipped),
            "drift": self.drift,
            # solo los agentes con algo que hacer: el payload lo lee un LED
            "por_agente": {k: v for k, v in self.by_agent.items() if any(v.values())},
        }


def is_link(path) -> bool:
    """Un enlace nuestro: symlink o junction.

    Ojo: en Windows `os.DirEntry.is_symlink()` devuelve False para las junctions
    (son reparse points, pero no symlinks). Si solo se mira eso, los 21 mil enlaces
    se leen como carpetas reales y el drift deja de detectarse.
    """
    try:
        if os.path.isjunction(path):
            return True
    except AttributeError:  # Python < 3.12
        pass
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


def plan(hub: Hub, agents: list[Path] | None = None, deep: bool = False) -> Plan:
    """Calcula el drift sin tocar nada.

    RÃ¡pido por defecto: **un `scandir` por agente** y `readlink` sÃ³lo en los enlaces que
    sobran. AsÃ­ `status` responde en milisegundos y el LED de edgePass puede sondearlo
    cada 5 s sin castigar la mÃ¡quina (con el chequeo completo tardaba 3.3 s porque hacÃ­a
    21 mil `readlink`).

    `deep=True` ademÃ¡s verifica **a dÃ³nde apunta** cada enlace esperado: es lo que usa
    `sync` para arreglar un enlace torcido (el caso raro: alguien re-apuntÃ³ una junction).
    """
    skills = hub.skills()
    tipos = hub.tipos()
    result = Plan()
    hub_root = hub.root.resolve()
    agents = agents if agents is not None else hub.agents()

    for agent in agents:
        is_hub = agent.resolve() == hub_root
        counts = {"tipos": 0, "planas": 0, "arregladas": 0, "muertas": 0, "omitidas": 0}
        expected: dict[str, Path] = {}
        if not is_hub:
            for tipo in tipos:
                expected[tipo] = hub_root / tipo
        for name, tipo in skills.items():
            expected[name] = hub_root / tipo / name

        present: set[str] = set()
        links: set[str] = set()
        try:
            with os.scandir(agent) as it:
                for entry in it:
                    present.add(entry.name)
                    if is_link(entry.path):
                        links.add(entry.name)
        except OSError:
            result.skipped.append((agent, "no pude leer la carpeta"))
            result.by_agent[str(agent)] = counts
            continue

        # 1) lo que deberia existir
        for name, target in expected.items():
            link = agent / name
            if name in present:
                if name not in links:
                    # Una carpeta real con nombre de skill: no la pisamos jamas.
                    counts["omitidas"] += 1
                    result.skipped.append((link, "existe y no es un enlace"))
                    continue
                if deep:
                    existing = link_target(link)
                    if existing is None or os.path.normcase(str(existing)) != os.path.normcase(
                        str(target)
                    ):
                        if target.exists():
                            result.wrong.append((link, target))
                        else:
                            counts["omitidas"] += 1
                            result.skipped.append((link, "el destino no existe en el hub"))
                continue
            if not target.exists():
                counts["omitidas"] += 1
                result.skipped.append((link, "el destino no existe en el hub"))
                continue
            result.missing.append((link, target))
            counts["tipos" if name in tipos and not is_hub else "planas"] += 1

        # 2) enlaces nuestros que ya no aplican (skill borrada, renombrada o manzana retirada)
        for name in sorted(links - set(expected)):
            link = agent / name
            target = link_target(link)
            if target is None:
                continue
            try:
                inside = os.path.normcase(str(target.resolve())).startswith(
                    os.path.normcase(str(hub_root))
                )
            except OSError:
                inside = False
            if inside:
                result.dead.append(link)
                counts["muertas"] += 1

        result.by_agent[str(agent)] = counts
    return result


# ---------------------------------------------------------------------------
# Aplicar
# ---------------------------------------------------------------------------


def _run_batch(lines: list[str]) -> None:
    """Corre una lista de comandos de cmd en un solo proceso (mklink es builtin)."""
    if not lines:
        return
    fd, path = tempfile.mkstemp(suffix=".bat", prefix="skills-sync-")
    try:
        with os.fdopen(fd, "w", encoding="mbcs" if os.name == "nt" else "utf-8") as fh:
            fh.write("@echo off\r\n")
            fh.write("\r\n".join(lines))
            fh.write("\r\nexit /b 0\r\n")
        subprocess.run(["cmd", "/c", path], check=False, capture_output=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _remove_junction(path: Path) -> bool:
    """Borra el enlace, jamas el contenido. rmdir sin /s es seguro con junctions."""
    try:
        os.rmdir(path)
        return True
    except OSError:
        pass
    try:
        subprocess.run(["cmd", "/c", "rmdir", str(path)], check=False, capture_output=True)
        return not os.path.lexists(path)
    except OSError:
        return False


def apply_plan(p: Plan, quiet: bool = False) -> dict:
    """Aplica el plan. Devuelve el conteo de lo hecho."""
    created = fixed = removed = 0

    # Los torcidos primero se retiran (mklink no sobrescribe), luego se recrean.
    for link, _target in p.wrong:
        _remove_junction(link)
    todo = p.missing + list(p.wrong)
    batch = []
    for link, target in todo:
        link.parent.mkdir(parents=True, exist_ok=True)
        batch.append(f'mklink /J "{link}" "{target}"')
    _run_batch(batch)

    for link, _target in todo:
        if os.path.lexists(link):
            created += 1
    fixed = len(p.wrong)

    for link in p.dead:
        if _remove_junction(link):
            removed += 1

    report = {
        "creados": created,
        "arreglados": fixed,
        "retirados": removed,
        "omitidos": len(p.skipped),
        "drift_antes": p.drift,
    }
    if not quiet:
        log(
            f"sync: +{created} creados, ~{fixed} arreglados, -{removed} retirados, "
            f"{len(p.skipped)} omitidos (drift era {p.drift})"
        )
    return report


def sync(hub: Hub, quiet: bool = False, dry_run: bool = False) -> dict:
    # sync va profundo: es el que arregla un enlace torcido.
    p = plan(hub, deep=True)
    if dry_run:
        return {"ok": True, "dryRun": True, **p.summary()}
    done = apply_plan(p, quiet=quiet)
    after = plan(hub, deep=True)
    return {
        "ok": after.drift == 0,
        "op": "sync",
        "hub": str(hub.root),
        "skills": len(hub.skills()),
        "tipos": len(hub.tipos()),
        "anidadas": hub.nested(),
        "agentes": len(hub.agents()),
        **done,
        "drift": after.drift,
        "drift_despues": after.drift,
    }


def status(hub: Hub, watching: bool = False, deep: bool = False) -> dict:
    p = plan(hub, deep=deep)
    return {
        "ok": True,
        "op": "status",
        "version": VERSION,
        "hub": str(hub.root),
        "skills": len(hub.skills()),
        "tipos": len(hub.tipos()),
        "anidadas": hub.nested(),
        "agentes": len(hub.agents()),
        "drift": p.drift,
        "watching": watching,
        **p.summary(),
    }


# ---------------------------------------------------------------------------
# Scope de proyecto (modelo engram: un solo store, una vista por proyecto)
# ---------------------------------------------------------------------------
#
# El hub es el unico store (como ~/.engram/engram.db). "Promover" una skill a un
# proyecto = crear una junction en <proyecto>/.agents/skills/<skill> y anotarla en
# <proyecto>/.agents/skills.json. Nunca se copia contenido: la skill sigue viviendo
# en el hub y todos los agentes la ven igual.
#
# Resolucion del proyecto, mismo orden que engram:
#   --project  ->  SKILLS_PROJECT  ->  .agents/config.json mas cercano (hasta el git
#   root)  ->  cwd/git root. Si no se puede resolver, se dice; no se adivina.

PROJECT_ENV = "SKILLS_PROJECT"
PROJECT_DIR = ".agents"
PROJECT_CONFIG = "config.json"
PROJECT_SKILLS = "skills"
PROJECT_MANIFEST = "skills.json"


def project_dir(project: Path) -> Path:
    return project / PROJECT_DIR


def _project_name(path: Path) -> str:
    return (path.name or path.drive or "unknown").strip()


def git_root(start: Path) -> Path | None:
    p = start.resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def resolve_project(explicit: str | None = None) -> dict:
    """--project -> SKILLS_PROJECT -> .agents/config.json cercano -> git root/cwd."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return {"path": p, "name": _project_name(p), "source": "explicit"}
    env = os.environ.get(PROJECT_ENV)
    if env:
        p = Path(env).expanduser().resolve()
        return {"path": p, "name": _project_name(p), "source": "env"}
    cwd = Path.cwd().resolve()
    stop = git_root(cwd) or cwd
    for cand in [cwd, *cwd.parents]:
        cfg = cand / PROJECT_DIR / PROJECT_CONFIG
        if cfg.is_file():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                data = {}
            name = str(data.get("project_name") or "").strip()
            if name:
                return {"path": cand, "name": name, "source": "config"}
        if cand == stop:
            break
    return {"path": stop, "name": _project_name(stop), "source": "cwd"}


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


def init_project(name: str | None = None, path: str | None = None, force: bool = False) -> dict:
    """Escribe <proyecto>/.agents/config.json (equivalente a `engram init`)."""
    proj = Path(path).expanduser().resolve() if path else Path.cwd().resolve()
    proj.mkdir(parents=True, exist_ok=True)
    cfg = project_dir(proj) / PROJECT_CONFIG
    if cfg.exists() and not force:
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8")).get("project_name")
        except (OSError, ValueError):
            existing = None
        return {
            "ok": False,
            "op": "init",
            "project": str(proj),
            "config": str(cfg),
            "error": f"ya existe {cfg} (project_name={existing}); usa --force para reemplazar",
        }
    cfg.parent.mkdir(parents=True, exist_ok=True)
    payload = {"project_name": (name or _project_name(proj)).strip(), "hub": str(default_hub())}
    cfg.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (project_dir(proj) / PROJECT_SKILLS).mkdir(parents=True, exist_ok=True)
    return {"ok": True, "op": "init", "project": str(proj), "config": str(cfg), "project_name": payload["project_name"]}


def promote(hub: Hub, names: list[str], project: Path) -> dict:
    """Promueve skills del hub al proyecto (junction + manifest). Idempotente."""
    known = hub.skills()
    manifest = _read_manifest(project)
    links_dir = project_dir(project) / PROJECT_SKILLS
    links_dir.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    already: list[str] = []
    unknown: list[str] = []
    occupied: list[str] = []
    batch: list[tuple[Path, Path]] = []

    for raw in names:
        name = raw.strip()
        tipo = known.get(name)
        if not tipo:
            unknown.append(name)
            continue
        src = hub.root / tipo / name
        dest = links_dir / name
        if is_link(dest):
            tgt = link_target(dest)
            if tgt and Path(tgt).resolve() == src.resolve():
                already.append(name)
            else:
                _remove_junction(dest)
                batch.append((dest, src))
                created.append(name)
        elif dest.exists():
            occupied.append(name)
        else:
            batch.append((dest, src))
            created.append(name)

    if batch:
        _run_batch([f'mklink /J "{d}" "{s}"' for d, s in batch])
        created = [n for n in created if os.path.lexists(links_dir / n)]

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for name in created + already:
        tipo = known[name]
        manifest["skills"][name] = {
            "tipo": tipo,
            "source": str(hub.root / tipo / name),
            "hash": skill_hash(hub.root / tipo / name),
            "promoted_at": stamp,
        }
    manifest["project_name"] = project_config_name(project) or manifest.get("project_name") or _project_name(project)
    _write_manifest(project, manifest)

    return {
        "ok": not unknown and not occupied,
        "op": "use",
        "project": str(project),
        "project_name": manifest["project_name"],
        "creadas": created,
        "ya_estaban": already,
        "desconocidas": unknown,
        "ocupadas_sin_enlace": occupied,
        "manifest": str(project_dir(project) / PROJECT_MANIFEST),
    }


def unpromote(project: Path, names: list[str]) -> dict:
    """Saca skills del scope del proyecto (borra el enlace y la entrada)."""
    manifest = _read_manifest(project)
    links_dir = project_dir(project) / PROJECT_SKILLS
    removed: list[str] = []
    absent: list[str] = []
    for raw in names:
        name = raw.strip()
        dest = links_dir / name
        if is_link(dest):
            _remove_junction(dest)
            removed.append(name)
        else:
            absent.append(name)
        manifest["skills"].pop(name, None)
    _write_manifest(project, manifest)
    return {
        "ok": True,
        "op": "unuse",
        "project": str(project),
        "retiradas": removed,
        "no_estaban": absent,
    }


def project_status(hub: Hub, project: Path) -> dict:
    """Salud del scope de proyecto: enlaces vivos, hash vs hub, y enlaces sin registrar."""
    known = hub.skills()
    manifest = _read_manifest(project)
    links_dir = project_dir(project) / PROJECT_SKILLS

    ok: list[str] = []
    broken: list[str] = []
    stale: list[str] = []
    unlinked: list[str] = []

    for name, entry in sorted(manifest["skills"].items()):
        dest = links_dir / name
        tipo = known.get(name)
        if not tipo:
            unlinked.append(f"{name}: ya no existe en el hub")
            continue
        src = hub.root / tipo / name
        if not is_link(dest):
            unlinked.append(f"{name}: falta el enlace en el proyecto")
            continue
        tgt = link_target(dest)
        if not tgt or Path(tgt).resolve() != src.resolve():
            broken.append(f"{name}: apunta a {tgt}")
            continue
        if entry.get("hash") and entry["hash"] != skill_hash(src):
            stale.append(name)
        else:
            ok.append(name)

    registered = set(manifest["skills"])
    extra = []
    duplicates = []
    local_only = []
    if links_dir.is_dir():
        for d in links_dir.iterdir():
            if d.name in registered:
                continue
            if is_link(d):
                extra.append(d.name)
            elif (d / "SKILL.md").exists():
                (duplicates if d.name in known else local_only).append(d.name)
            elif d.is_dir():
                local_only.append(f"{d.name} (sin SKILL.md)")

    return {
        "ok": not broken and not unlinked,
        "op": "project-status",
        "hub": str(hub.root),
        "project": str(project),
        "project_name": manifest.get("project_name"),
        "tiene_config": bool(project_config_name(project)),
        "promocionadas": len(registered),
        "sanas": ok,
        "desactualizadas": stale,
        "rotas": broken,
        "sin_enlace": unlinked,
        "enlaces_sin_registrar": extra,
        "duplicadas_locales": duplicates,
        "locales_propias": local_only,
    }


# ---------------------------------------------------------------------------
# Doctor: que harnesss hay, cuantas skills nativas tiene cada uno
# ---------------------------------------------------------------------------
#
# Regla de oro: las skills NATIVAS de cada agente no se tocan. skillgram solo
# agrega enlaces al hub. Si un agente ya tiene una skill con el mismo nombre,
# `sync` la saltea (nunca sobrescribe) y `doctor` la reporta como colision.

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
        home = HOME / folder
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


def doctor(hub: Hub) -> dict:
    """Inventario read-only: enlaces, muertas, faltantes, nativas y colisiones."""
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
            "enlaces_al_hub": links,
            "muertas": dead,
            "faltantes": len([n for n in known if n not in present]),
            "nativas": natives,
            "colisiones": sorted(collisions),
        })

    recos = []
    if any(r["faltantes"] or r["muertas"] for r in rows):
        recos.append("corre `sync` para crear los enlaces faltantes y retirar los muertos")
    nat = sum(r["nativas"] for r in rows)
    if nat:
        recos.append(f"{nat} skills nativas de agentes: quedan como estan, skillgram no las toca")
    col = sum(len(r["colisiones"]) for r in rows)
    if col:
        recos.append(f"{col} colisiones de nombre: `sync` las saltea (nunca sobrescribe)")
    if not known:
        recos.append(f"el hub {hub.root} esta vacio: agrega <tipo>/<skill>/SKILL.md")

    return {
        "ok": True,
        "op": "doctor",
        "version": VERSION,
        "hub": str(hub.root),
        "skills_hub": len(known),
        "tipos": len(hub.tipos()),
        "agentes": rows,
        "harnesses": detect_harnesses(),
        "recomendaciones": recos,
    }

# ---------------------------------------------------------------------------
# Onboarding: detectar harnesss, unificar (opcionalmente con LLM conectada)
# ---------------------------------------------------------------------------
#
# Flujo: inventario -> decision de alcance (todos o solo el actual) -> plan ->
# aplicar. El plan es JSON y se puede revisar antes de tocar nada.
#
# Con una LLM conectada (cualquier endpoint OpenAI-compatible) el modelo hace el
# trabajo sucio: categorizar, detectar duplicados y decidir el flavor de cada
# skill. La LLM NUNCA escribe archivos: propone un JSON de acciones, skillgram lo
# valida y lo ejecuta.

LLM_DEFAULTS = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-flash",
    "timeout": 90,
}

DEFAULT_CATEGORY = "imported"


def llm_config() -> dict | None:
    """Config de la LLM conectada (OpenAI-compatible). None si no hay clave."""
    base = os.environ.get("SKILLGRAM_LLM_BASE_URL") or LLM_DEFAULTS["base_url"]
    model = os.environ.get("SKILLGRAM_LLM_MODEL") or LLM_DEFAULTS["model"]
    key = os.environ.get("SKILLGRAM_LLM_API_KEY")
    if not key:
        for env_name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            key = os.environ.get(env_name)
            if key:
                break
    if not key:
        return None
    if base.endswith("/v1") or "/v" in base.rsplit("/", 1)[-1]:
        url = base.rstrip("/") + "/chat/completions"
    else:
        url = base.rstrip("/") + "/v1/chat/completions"
    return {"url": url, "model": model, "key": key, "timeout": LLM_DEFAULTS["timeout"]}


def llm_chat(cfg: dict, system: str, user: str) -> str:
    """Un turno contra la LLM conectada. Devuelve el texto o levanta OSError."""
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 4000,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"],
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise OSError(f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSError(f"LLM inalcanzable: {exc}") from exc
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise OSError(f"respuesta LLM inesperada: {str(body)[:300]}") from exc


def _skill_summary(skill_dir: Path, limit: int = 400) -> str:
    """Primeras lineas utiles de un SKILL.md, sin frontmatter."""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("---")]
    return "\n".join(lines)[:limit]


def harness_inventory(hub: Hub, only: str | None = None) -> list:
    """Skills nativas (reales) por harness, con hash y descripcion corta."""
    known = hub.skills()
    hub_paths = {name: hub.root / tipo / name for name, tipo in known.items()}
    rows = []
    for agent_dir in hub.agents():
        app = agent_dir.parent.name.lstrip(".")
        if only and app != only:
            continue
        natives = []
        links = 0
        for entry in sorted(agent_dir.iterdir()):
            if is_link(entry):
                links += 1
                continue
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                natives.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "hash": skill_hash(entry),
                        "in_hub": entry.name in known,
                        "summary": _skill_summary(entry),
                    }
                )
        rows.append(
            {
                "app": app,
                "skills_dir": str(agent_dir),
                "enlaces_al_hub": links,
                "nativas": natives,
            }
        )
    return rows


def _heuristic_category(name: str, categories: list) -> str:
    low = name.lower()
    for word in ("stripe", "payment", "billing"):
        if word in low and "integrations" in categories:
            return "integrations"
    for word, cat in (
        ("deploy", "infra-ops"),
        ("docker", "infra-ops"),
        ("k8s", "infra-ops"),
        ("azure", "cloud-azure"),
        ("aws", "cloud-azure"),
        ("react", "web-platforms"),
        ("next", "web-platforms"),
        ("test", "dev-practices"),
        ("review", "dev-practices"),
        ("doc", "docs-writing"),
        ("design", "design-ui"),
        ("ui", "design-ui"),
    ):
        if word in low and cat in categories:
            return cat
    return categories[0] if categories else DEFAULT_CATEGORY


def build_onboard_plan(hub: Hub, scope: str = "all", harness: str | None = None) -> dict:
    """Plan de unificacion: que se importa, a que categoria y que es duplicado."""
    only = None if scope == "all" else harness
    inv = harness_inventory(hub, only=only)
    categories = hub.tipos()
    seen: dict = {}
    actions = []
    for row in inv:
        for nat in row["nativas"]:
            action = {
                "skill": nat["name"],
                "harness": row["app"],
                "from": nat["path"],
                "hash": nat["hash"],
                "categoria": _heuristic_category(nat["name"], categories),
                "accion": "import",
                "motivo": "",
            }
            if nat["hash"] in seen:
                action["accion"] = "dedupe"
                action["motivo"] = f"identica a {seen[nat['hash']]}"
            elif nat["in_hub"]:
                action["accion"] = "conflict"
                action["motivo"] = "ya existe una skill con ese nombre en el hub"
            seen.setdefault(nat["hash"], f"{row['app']}/{nat['name']}")
            actions.append(action)
    return {
        "op": "onboard-plan",
        "hub": str(hub.root),
        "scope": scope,
        "harness": harness,
        "harnesses": [{"app": r["app"], "nativas": len(r["nativas"]), "skills_dir": r["skills_dir"]} for r in inv],
        "acciones": actions,
        "resumen": {
            "import": sum(1 for a in actions if a["accion"] == "import"),
            "dedupe": sum(1 for a in actions if a["accion"] == "dedupe"),
            "conflict": sum(1 for a in actions if a["accion"] == "conflict"),
        },
    }


LLM_SYSTEM = (
    "You are skillgram's consolidation assistant. You receive a JSON list of skills "
    "found in several agent harnesses. Reply ONLY with a JSON object: "
    '{"skills":[{"name":"...","harness":"...","category":"...","flavor":"generic|claude|codex|hermes|openai|other",'
    '"duplicate_of":"name-or-empty","reason":"short"}]}. '
    "Rules: use one of the given hub categories when possible; flavor=generic unless the "
    "skill text hard-codes one agent's CLI or tooling; duplicate_of only when the two "
    "skills are the same tool with different wording; never invent skills."
)


def llm_refine_plan(hub: Hub, plan: dict, cfg: dict) -> dict:
    """Pide a la LLM categorias/flavor/duplicados y los mezcla en el plan."""
    items = [
        {
            "name": a["skill"],
            "harness": a["harness"],
            "current_category": a["categoria"],
            "summary": _skill_summary(Path(a["from"])),
        }
        for a in plan["acciones"]
    ]
    if not items:
        return plan
    user = json.dumps(
        {"categories": hub.tipos(), "skills": items[:80]},
        ensure_ascii=False,
    )
    raw = llm_chat(cfg, LLM_SYSTEM, user)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        plan["llm_error"] = "la LLM no devolvio JSON"
        return plan
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError as exc:
        plan["llm_error"] = f"JSON invalido: {exc}"
        return plan
    index = {(a["skill"], a["harness"]): a for a in plan["acciones"]}
    applied = 0
    for item in data.get("skills", []):
        key = (str(item.get("name", "")).strip(), str(item.get("harness", "")).strip())
        action = index.get(key)
        if not action:
            continue
        cat = str(item.get("category", "")).strip()
        if cat and cat in hub.tipos():
            action["categoria"] = cat
        flavor = str(item.get("flavor", "")).strip().lower()
        if flavor:
            action["flavor"] = flavor
        dup = str(item.get("duplicate_of", "")).strip()
        if dup and dup != action["skill"] and action["accion"] == "import":
            action["accion"] = "dedupe"
            action["motivo"] = f"la LLM dice que es lo mismo que {dup}"
        if item.get("reason"):
            action["motivo"] = action["motivo"] or str(item["reason"])[:200]
        applied += 1
    plan["llm"] = {"model": cfg["model"], "aplicadas": applied}
    return plan


def apply_onboard(hub: Hub, plan: dict) -> dict:
    """Importa al hub: copia, verifica hash y recien ahi reemplaza por link."""
    imported, skipped, failed = [], [], []
    for action in plan["acciones"]:
        if action["accion"] != "import":
            skipped.append(f"{action['harness']}/{action['skill']} ({action['accion']})")
            continue
        src = Path(action["from"])
        dest = hub.root / action["categoria"] / action["skill"]
        if dest.exists():
            failed.append(f"{action['skill']}: ya existe {dest}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(src, dest)
        except OSError as exc:
            failed.append(f"{action['skill']}: {exc}")
            continue
        if skill_hash(dest) != action["hash"]:
            shutil.rmtree(dest, ignore_errors=True)
            failed.append(f"{action['skill']}: hash distinto tras copiar")
            continue
        imported.append({"skill": action["skill"], "harness": action["harness"], "to": str(dest), "harness_dir": str(src.parent)})
    return {"imported": imported, "skipped": skipped, "failed": failed}


def replace_native_with_link(hub: Hub, imported: list, quarantine: Path) -> dict:
    """Reemplaza la copia nativa por un junction al hub (la copia va a cuarentena).

    Solo se llama con --replace-natives. El contenido ya esta verificado en el hub
    (mismo hash), y la copia original se mueve a <quarantine>/<harness>/<skill>:
    nunca se borra.
    """
    moved, linked, failed = [], [], []
    for item in imported:
        src = Path(item["harness_dir"]) / item["skill"] if item.get("harness_dir") else None
        if not src or not src.is_dir():
            failed.append(f"{item['skill']}: no encuentro la carpeta nativa")
            continue
        dest = Path(item["to"])
        qdir = quarantine / item["harness"]
        qdir.mkdir(parents=True, exist_ok=True)
        target = qdir / item["skill"]
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(src), str(target))
            moved.append(str(target))
        except OSError as exc:
            failed.append(f"{item['skill']}: no pude mover a cuarentena ({exc})")
            continue
        _run_batch([f'mklink /J "{src}" "{dest}"'])
        if os.path.lexists(src):
            linked.append(str(src))
        else:
            failed.append(f"{item['skill']}: cuarentena ok pero fallo el link")
    return {"cuarentena": moved, "enlaces": linked, "fallos": failed}

def onboard(hub: Hub, scope: str = "all", harness: str | None = None, use_llm: bool = True) -> dict:
    plan = build_onboard_plan(hub, scope=scope, harness=harness)
    cfg = llm_config() if use_llm else None
    if cfg:
        try:
            plan = llm_refine_plan(hub, plan, cfg)
        except OSError as exc:
            plan["llm_error"] = str(exc)
    plan["llm_usada"] = bool(cfg) and "llm" in plan
    return plan

# ---------------------------------------------------------------------------
# Log y estado
# ---------------------------------------------------------------------------


LOG_PATH = Path(os.environ.get("SKILLGRAM_LOG") or os.environ.get("SKILLS_SYNC_LOG") or (HOME / ".agents" / "skillgram.log"))


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Vigilante
# ---------------------------------------------------------------------------


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
        # una skill que aparezca *durante* ese sync quedaría dentro de la base sin haberse
        # enlazado, y el vigilante no volvería a mirarla hasta el siguiente cambio no
        # relacionado (pasaba de verdad: una skill se quedaba sin enlaces).
        # Con la base de antes, el primer sondeo ve el cambio y sincroniza otra vez;
        # repetir un sync es idempotente, así que no cuesta nada.
        previous = hub_fingerprint(self.hub)
        self.last_sync = sync(self.hub, quiet=self.quiet)
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            current = hub_fingerprint(self.hub)
            if os.environ.get("SKILLGRAM_DEBUG") or os.environ.get("SKILLS_SYNC_DEBUG"):
                log(f"debug: huella {len(previous)} -> {len(current)} · iguales={current == previous}")
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
    """True si este proceso es el único vigilante.

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


# ---------------------------------------------------------------------------
# Daemon: JSON por linea (lo usa edgePass)
# ---------------------------------------------------------------------------


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
            reply({"ok": False, "error": "linea no es JSON"})
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
                    reply({"ok": False, "error": "ya hay un vigilante corriendo"})
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
                reply({"ok": False, "error": f"op desconocida: {op or '(vacia)'}"})
        except Exception as exc:  # noqa: BLE001 - el daemon no debe morir por una op
            reply({"ok": False, "op": op, "error": f"{type(exc).__name__}: {exc}"})
    watcher.stop()
    return 0


# ---------------------------------------------------------------------------
# Autostart (tarea programada al iniciar sesion)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="skillgram",
        description="Mantiene los enlaces de la biblioteca de skills en todos los agentes.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hub", default=str(default_hub()), help="raiz del hub (default ~/.agents/skills)")
    common.add_argument("--json", action="store_true", help="salida JSON (una linea)")
    common.add_argument("--quiet", action="store_true", help="sin logs en stdout")

    sub = ap.add_subparsers(dest="cmd", required=True)

    p_onb = sub.add_parser("onboard", parents=[common], help="detecta harnesss y unifica sus skills en el hub (LLM opcional)")
    p_onb.add_argument("--scope", choices=["all", "current"], help="unificar todos los harnesss o solo uno (si falta, pregunta)")
    p_onb.add_argument("--harness", help="nombre del harness para --scope current")
    p_onb.add_argument("--apply", action="store_true", help="ejecuta el plan (sin esto solo lo muestra)")
    p_onb.add_argument("--replace-natives", action="store_true", help="reemplaza la copia nativa por un link (la copia va a cuarentena)")
    p_onb.add_argument("--no-llm", action="store_true", help="no consulta la LLM conectada")
    p_onb.add_argument("--plan", help="escribe el plan JSON en esa ruta")
    sub.add_parser("doctor", parents=[common], help="inventario: harnesss, nativas, colisiones, drift")
    p_status = sub.add_parser("status", parents=[common], help="drift actual sin tocar nada")
    p_status.add_argument("--verify", action="store_true", help="ademas verifica a donde apunta cada enlace (lento)")
    p_sync = sub.add_parser("sync", parents=[common], help="aplica el estado deseado (idempotente)")
    p_sync.add_argument("--dry-run", action="store_true")
    p_watch = sub.add_parser("watch", parents=[common], help="vigila el hub y re-sincroniza solo")
    p_watch.add_argument("--interval", type=float, default=5.0)
    p_daemon = sub.add_parser("daemon", parents=[common], help="servidor JSON-linea para edgePass")
    p_daemon.add_argument("--interval", type=float, default=5.0)
    p_daemon.add_argument("--watch", action="store_true", help="arranca vigilando")
    p_auto = sub.add_parser("install-autostart", parents=[common], help="tarea programada al iniciar sesion")
    p_auto.add_argument("--interval", type=float, default=10.0)
    sub.add_parser("uninstall-autostart", parents=[common], help="quita la tarea programada")

    p_init = sub.add_parser("init", parents=[common], help="crea .agents/config.json del proyecto")
    p_init.add_argument("project", nargs="?", help="nombre del proyecto (default: basename del cwd)")
    p_init.add_argument("--path", help="carpeta del proyecto (default: cwd)")
    p_init.add_argument("--force", action="store_true", help="reemplaza un config existente")

    p_use = sub.add_parser("use", parents=[common], help="promueve skills del hub al proyecto")
    p_use.add_argument("skills", nargs="+")
    p_use.add_argument("--project", help="carpeta del proyecto (default: resolucion tipo engram)")

    p_unuse = sub.add_parser("unuse", parents=[common], help="saca skills del scope del proyecto")
    p_unuse.add_argument("skills", nargs="+")
    p_unuse.add_argument("--project")

    p_pstat = sub.add_parser("project-status", parents=[common], help="salud del scope de proyecto")
    p_pstat.add_argument("--project")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    hub = Hub(Path(args.hub))

    if args.cmd == "status":
        result = status(hub, deep=bool(getattr(args, "verify", False)))
    elif args.cmd == "doctor":
        result = doctor(hub)
    elif args.cmd == "onboard":
        scope = args.scope
        if not scope:
            ans = ""
            try:
                if sys.stdin.isatty() and not getattr(args, "json", False):
                    ans = input("Unificar TODOS los harnesss o solo el actual? [all/current] (all): ")
            except (EOFError, OSError):
                ans = ""
            scope = "current" if ans.strip().lower().startswith("c") else "all"
        harness = args.harness or os.environ.get("SKILLGRAM_HARNESS")
        if scope == "current" and not harness:
            print(json.dumps({"ok": False, "error": "scope=current requiere --harness o SKILLGRAM_HARNESS"}), flush=True)
            return 2
        plan = onboard(hub, scope=scope, harness=harness, use_llm=not args.no_llm)
        if args.plan:
            Path(args.plan).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result = plan
        if args.apply:
            applied = apply_onboard(hub, plan)
            replaced = {"cuarentena": [], "enlaces": [], "fallos": []}
            if args.replace_natives and applied["imported"]:
                replaced = replace_native_with_link(
                    hub, applied["imported"], HOME / ".agents" / "skillgram-quarantine"
                )
            linked = sync(hub, quiet=True)
            result = {
                **plan,
                "aplicado": applied,
                "reemplazo": replaced,
                "sync": linked,
                "ok": not applied["failed"] and not replaced["fallos"],
            }
    elif args.cmd == "sync":
        result = sync(hub, quiet=args.quiet, dry_run=args.dry_run)
    elif args.cmd == "watch":
        if not hub.root.is_dir():
            print(json.dumps({"ok": False, "error": f"no existe el hub {hub.root}"}), flush=True)
            return 2
        if not single_instance():
            print(
                json.dumps({"ok": False, "error": "ya hay un vigilante corriendo (no arranco otro)"}),
                flush=True,
            )
            return 3
        watcher = Watcher(hub, interval=args.interval)
        # El sync inicial lo hace el propio vigilante (ver `_loop`), para no dejar sin
        # sincronizar lo que aparezca entre ese sync y la primera huella.
        watcher.start()
        try:
            while watcher.running:
                time.sleep(1)
        except KeyboardInterrupt:
            watcher.stop()
        return 0
    elif args.cmd == "daemon":
        return daemon(hub, interval=args.interval, watch_at_start=args.watch)
    elif args.cmd == "install-autostart":
        result = install_autostart(hub, args.interval)
    elif args.cmd == "uninstall-autostart":
        result = uninstall_autostart()
    elif args.cmd == "init":
        result = init_project(args.project, args.path, force=args.force)
    elif args.cmd == "use":
        proj = resolve_project(args.project)
        result = promote(hub, args.skills, Path(proj["path"]))
        result["resolucion"] = proj["source"]
    elif args.cmd == "unuse":
        proj = resolve_project(args.project)
        result = unpromote(Path(proj["path"]), args.skills)
        result["resolucion"] = proj["source"]
    elif args.cmd == "project-status":
        proj = resolve_project(args.project)
        result = project_status(hub, Path(proj["path"]))
        result["resolucion"] = proj["source"]
    else:  # pragma: no cover - argparse ya valida
        return 2

    if args.quiet and args.cmd == "sync":
        print(json.dumps(result, ensure_ascii=False), flush=True)
    elif args.json or args.cmd in {"daemon"}:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    elif args.cmd == "onboard":
        p = result
        print(
            f"scope: {p['scope']}"
            + (f" ({p['harness']})" if p.get("harness") else " (todos los harnesss)")
            + f"  | hub: {p['hub']}\n"
            + "harnesss con skills nativas:\n"
            + ("\n".join(f"  {h['app']:16} {h['nativas']:>4} nativas" for h in p["harnesses"] if h["nativas"])
               or "  (ninguno)")
            + f"\nplan: {p['resumen']['import']} importar | {p['resumen']['dedupe']} duplicadas | "
            f"{p['resumen']['conflict']} en conflicto"
            + (f"\nllm: {p['llm']['model']} ({p['llm']['aplicadas']} skills clasificadas)" if p.get("llm") else "\nllm: no usada")
            + (f"\nllm_error: {p['llm_error']}" if p.get("llm_error") else "")
            + "\n"
            + "\n".join(
                f"  {a['accion']:8} {a['harness']}/{a['skill']} -> {a['categoria']}"
                + (f" [{a.get('flavor')}]" if a.get("flavor") else "")
                + (f"  ({a['motivo']})" if a.get("motivo") else "")
                for a in p["acciones"][:40]
            )
            + (f"\n  ... y {len(p['acciones']) - 40} mas" if len(p["acciones"]) > 40 else "")
            + ("\n\naplicado: " + str(len(p["aplicado"]["imported"])) + " importadas, "
               + str(len(p["aplicado"]["skipped"])) + " salteadas, "
               + str(len(p["aplicado"]["failed"])) + " fallidas" if p.get("aplicado") else "\n(solo plan: corre con --apply para ejecutarlo)"),
            flush=True,
        )
    elif args.cmd == "doctor":
        d = result
        inst = [h["app"] for h in d["harnesses"] if h["instalado"]]
        with_skills = [h["app"] for h in d["harnesses"] if h["skills_dir"]]
        print(
            f"hub: {d['hub']}\n"
            f"skills: {d['skills_hub']} en {d['tipos']} tipos\n"
            f"harnesss instalados ({len(inst)}): {', '.join(inst) or '-'}\n"
            f"con carpeta skills/ ({len(with_skills)}): {', '.join(with_skills) or '-'}\n"
            "agente                        enlaces  muertas  faltan  nativas  colisiones\n"
            + "\n".join(
                f"{r['app'][:28]:28}  {r['enlaces_al_hub']:>7}  {r['muertas']:>7}  "
                f"{r['faltantes']:>6}  {r['nativas']:>7}  {len(r['colisiones']):>10}"
                for r in d["agentes"]
            )
            + "\n"
            + ("\n".join(f"- {r}" for r in d["recomendaciones"]) if d["recomendaciones"] else ""),
            flush=True,
        )
    elif args.cmd in {"status", "sync", "install-autostart", "uninstall-autostart"}:
        if args.cmd == "status":
            s = result
            print(
                f"hub: {s['hub']}\n"
                f"skills: {s['skills']} en {s['tipos']} tipos ({s['anidadas']} anidadas), agentes: {s['agentes']}\n"
                f"drift: {s['drift']} (faltan {s['faltan']}, mal {s['mal_apuntados']}, "
                f"muertos {s['muertos']}, omitidos {s['omitidos']})",
                flush=True,
            )
        else:
            print(json.dumps(result, ensure_ascii=False), flush=True)
    elif args.cmd == "init":
        if result.get("ok"):
            print(f"proyecto: {result['project_name']}  ({result['config']})", flush=True)
        else:
            print(result.get("error", "no se pudo inicializar"), flush=True)
    elif args.cmd == "use":
        print(
            f"proyecto: {result['project_name']} ({result['project']})\n"
            f"promocionadas: {len(result['creadas'])} nuevas, {len(result['ya_estaban'])} ya estaban\n"
            + (f"desconocidas en el hub: {', '.join(result['desconocidas'])}\n" if result["desconocidas"] else "")
            + (f"ocupadas sin enlace: {', '.join(result['ocupadas_sin_enlace'])}\n" if result["ocupadas_sin_enlace"] else "")
            + f"manifest: {result['manifest']}",
            flush=True,
        )
    elif args.cmd == "unuse":
        print(
            f"proyecto: {result['project']}\n"
            f"retiradas: {len(result['retiradas'])}"
            + (f" | no estaban: {', '.join(result['no_estaban'])}" if result["no_estaban"] else ""),
            flush=True,
        )
    elif args.cmd == "project-status":
        s = result
        print(
            f"proyecto: {s['project_name']} ({s['project']})  resolucion: {s['resolucion']}\n"
            f"promocionadas: {s['promocionadas']} | sanas {len(s['sanas'])} | "
            f"desactualizadas {len(s['desactualizadas'])} | rotas {len(s['rotas'])} | sin enlace {len(s['sin_enlace'])}\n"
            + (f"desactualizadas: {', '.join(s['desactualizadas'])}\n" if s["desactualizadas"] else "")
            + (f"rotas: {'; '.join(s['rotas'])}\n" if s["rotas"] else "")
            + (f"sin enlace: {'; '.join(s['sin_enlace'])}\n" if s["sin_enlace"] else "")
            + (
                f"enlaces sin registrar: {', '.join(s['enlaces_sin_registrar'])}\n"
                if s["enlaces_sin_registrar"]
                else ""
            )
            + (
                f"duplicadas locales (estan en el hub y ademas copiadas): {', '.join(s['duplicadas_locales'])}\n"
                if s["duplicadas_locales"]
                else ""
            )
            + (f"locales propias: {', '.join(s['locales_propias'])}\n" if s["locales_propias"] else "")
            + ("" if s["tiene_config"] else "sin .agents/config.json (corre `init` para fijar el nombre)\n"),
            flush=True,
        )
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())

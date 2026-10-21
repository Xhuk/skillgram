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

No work skills are bundled. Bring your own hub (or start empty) and skillgram
keeps every agent, IDE and harness on the same library. The only skill in this
repo is skills/skillgram-report: it tells an agent how to report a bug or a gap.

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
    install  one idempotent command: resolve the harness -> check the hub ->
             register the MCP server through the harness' own CLI -> propagate.
    propagate   mount + protocol + ledger for every git repo of one harness.
    install-autostart / uninstall-autostart   scheduled task on login (Windows).

Output contract: exactly one JSON line per operation on stdout.

    {"ok":true,"op":"status","drift":0,"skills":841,"agents":50,"watching":false}

Quick start
-----------
    python skillgram.py install
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
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = "1.7.0"

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
        for entry in sorted(HOME.iterdir()):
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
            "missing": len(self.missing),
            "mispointed": len(self.wrong),
            "dead": len(self.dead),
            "skipped": len(self.skipped),
            "drift": self.drift,
            # solo los agentes con algo que hacer: el payload lo lee un LED
            "por_agente": {k: v for k, v in self.by_agent.items() if any(v.values())},
        }


def is_link(path) -> bool:
    """Un enlace nuestro: symlink o junction.

    Ojo: en Windows `os.DirEntry.is_symlink()` devuelve False para las junctions
    (son reparse points, pero no symlinks). Si solo se mira eso, los 21 mil links
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


def plan(hub: Hub, agents: list[Path] | None = None, deep: bool = False,
         policy: list | None = None) -> Plan:
    """Calcula el drift sin tocar nada.

    RÃƒÂ¡pido por defecto: **un `scandir` por agente** y `readlink` sÃƒÂ³lo en los links que
    sobran. AsÃƒÂ­ `status` responde en milisegundos y el LED de edgePass puede sondearlo
    cada 5 s sin castigar la mÃƒÂ¡quina (con el chequeo completo tardaba 3.3 s porque hacÃƒÂ­a
    21 mil `readlink`).

    `deep=True` ademÃƒÂ¡s verifica **a dÃƒÂ³nde apunta** cada enlace esperado: es lo que usa
    `sync` para arreglar un enlace torcido (el caso raro: alguien re-apuntÃƒÂ³ una junction).
    """
    skills = hub.skills()
    tipos = hub.tipos()
    result = Plan()
    hub_root = hub.root.resolve()
    agents = agents if agents is not None else hub.agents()
    levels = policy if policy is not None else []
    blocked = {}
    if levels:
        # a denied skill is never linked into a home either: `sync` is the second door
        for name, tipo in skills.items():
            verdict = policy_verdict(levels, tipo, name)
            if not verdict["allowed"]:
                blocked[name] = verdict["rule"]

    for agent in agents:
        is_hub = agent.resolve() == hub_root
        counts = {"categories": 0, "flat": 0, "repointed": 0, "dead": 0, "skipped": 0}
        expected: dict[str, Path] = {}
        if not is_hub:
            for tipo in tipos:
                expected[tipo] = hub_root / tipo
        for name, tipo in skills.items():
            if name in blocked:
                continue
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
            result.skipped.append((agent, "could not read the folder"))
            result.by_agent[str(agent)] = counts
            continue

        # 1) lo que deberia existir
        for name, target in expected.items():
            link = agent / name
            if name in present:
                if name not in links:
                    # Una carpeta real con nombre de skill: no la pisamos jamas.
                    counts["skipped"] += 1
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
                            counts["skipped"] += 1
                            result.skipped.append((link, "the link target does not exist in the hub"))
                continue
            if not target.exists():
                counts["skipped"] += 1
                result.skipped.append((link, "the link target does not exist in the hub"))
                continue
            result.missing.append((link, target))
            counts["categories" if name in tipos and not is_hub else "flat"] += 1

        # 2) links nuestros que ya no aplican (skill borrada, renombrada o bloqueada por
        # policy: deny no es un consejo)
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
                counts["dead"] += 1

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
    if os.name != "nt":
        # POSIX: un symlink a un directorio se borra con unlink, no con rmdir.
        try:
            os.unlink(path)
            return True
        except OSError:
            return False
    try:
        subprocess.run(["cmd", "/c", "rmdir", str(path)], check=False, capture_output=True)
        return not os.path.lexists(path)
    except OSError:
        return False


def make_links(pairs: list) -> int:
    """Crea un enlace por cada (destino, origen). Junctions en Windows, symlinks fuera.

    Windows crea todo en un solo `cmd` porque `mklink` es un builtin. En macOS/Linux
    no hay `cmd`: sin esta rama el tool entero era Windows-only aunque el README
    prometiera symlinks.
    """
    created = 0
    if not pairs:
        return created
    if os.name == "nt":
        _run_batch([f'mklink /J "{dest}" "{src}"' for dest, src in pairs])
    else:
        for dest, src in pairs:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(src, dest, target_is_directory=True)
            except OSError:
                pass
    for dest, _src in pairs:
        if os.path.lexists(dest):
            created += 1
    return created


def apply_plan(p: Plan, quiet: bool = False) -> dict:
    """Aplica el plan. Devuelve el conteo de lo hecho."""
    created = fixed = removed = 0

    # Los torcidos primero se retiran (un enlace no se sobrescribe), luego se recrean.
    for link, _target in p.wrong:
        _remove_junction(link)
    todo = p.missing + list(p.wrong)
    make_links(todo)

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
        "removed": removed,
        "skipped": len(p.skipped),
        "drift_antes": p.drift,
    }
    if not quiet:
        log(
            f"sync: +{created} creados, ~{fixed} arreglados, -{removed} removed, "
            f"{len(p.skipped)} omitidos (drift era {p.drift})"
        )
    return report


def sync(hub: Hub, quiet: bool = False, dry_run: bool = False) -> dict:
    # sync va profundo: es el que arregla un enlace torcido.
    p = plan(hub, deep=True, policy=policy_for(None))
    if dry_run:
        return {"ok": True, "dryRun": True, **p.summary()}
    done = apply_plan(p, quiet=quiet)
    after = plan(hub, deep=True)
    return {
        "ok": after.drift == 0,
        "op": "sync",
        "hub": str(hub.root),
        "skills": len(hub.skills()),
        "categories": len(hub.tipos()),
        "nested": hub.nested(),
        "agents": len(hub.agents()),
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
        "categories": len(hub.tipos()),
        "nested": hub.nested(),
        "agents": len(hub.agents()),
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
            "error": f"already exists: {cfg} (project_name={existing}); use --force to replace",
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
    denied: list[dict] = []
    batch: list[tuple[Path, Path]] = []
    levels = policy_for(project)

    for raw in names:
        name = raw.strip()
        tipo = known.get(name)
        if not tipo:
            unknown.append(name)
            continue
        verdict = policy_verdict(levels, tipo, name)
        if not verdict["allowed"]:
            denied.append({"skill": name, "category": tipo, "rule": verdict["rule"]})
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
        make_links(batch)
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
        "ok": not unknown and not occupied and not denied,
        "op": "use",
        "project": str(project),
        "project_name": manifest["project_name"],
        "created": created,
        "already_present": already,
        "unknown": unknown,
        "ocupadas_sin_enlace": occupied,
        "denied": denied,
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
        "removed": removed,
        "absent": absent,
    }


def project_status(hub: Hub, project: Path) -> dict:
    """Salud del scope de proyecto: links vivos, hash vs hub, y unregistered links."""
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
            unlinked.append(f"{name}: no longer exists in the hub")
            continue
        src = hub.root / tipo / name
        if not is_link(dest):
            unlinked.append(f"{name}: missing link in the project")
            continue
        tgt = link_target(dest)
        if not tgt or Path(tgt).resolve() != src.resolve():
            broken.append(f"{name}: points to {tgt}")
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
                local_only.append(f"{d.name} (no SKILL.md)")

    return {
        "ok": not broken and not unlinked,
        "op": "project-status",
        "hub": str(hub.root),
        "project": str(project),
        "project_name": manifest.get("project_name"),
        "tiene_config": bool(project_config_name(project)),
        "promoted": len(registered),
        "healthy": ok,
        "stale": stale,
        "broken": broken,
        "missing_links": unlinked,
        "links_sin_registrar": extra,
        "local_duplicates": duplicates,
        "local_only": local_only,
        # the user's own seeds, minus the ones this project already has: a
        # recommendation, not drift (it never turns `ok` false)
        "seeds_missing": [s for s in read_seeds()["always"] if s not in registered],
    }


# ---------------------------------------------------------------------------
# Doctor: que harnesss hay, cuantas skills native tiene cada uno
# ---------------------------------------------------------------------------
#
# Regla de oro: las skills NATIVAS de cada agente no se tocan. skillgram solo
# agrega links al hub. Si un agente ya tiene una skill con el mismo nombre,
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
    """Skills native (reales) por harness, con hash y descripcion corta."""
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
                "hub_links": links,
                "native": natives,
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
        for nat in row["native"]:
            action = {
                "skill": nat["name"],
                "harness": row["app"],
                "from": nat["path"],
                "hash": nat["hash"],
                "category": _heuristic_category(nat["name"], categories),
                "action": "import",
                "reason": "",
            }
            if nat["hash"] in seen:
                action["action"] = "dedupe"
                action["reason"] = f"identical to {seen[nat['hash']]}"
            elif nat["in_hub"]:
                action["action"] = "conflict"
                action["reason"] = "a skill with that name already exists in the hub"
            seen.setdefault(nat["hash"], f"{row['app']}/{nat['name']}")
            actions.append(action)
    return {
        "ok": True,
        "op": "onboard-plan",
        "hub": str(hub.root),
        "scope": scope,
        "harness": harness,
        "harnesses": [{"app": r["app"], "native": len(r["native"]), "skills_dir": r["skills_dir"]} for r in inv],
        "actions": actions,
        "summary": {
            "import": sum(1 for a in actions if a["action"] == "import"),
            "dedupe": sum(1 for a in actions if a["action"] == "dedupe"),
            "conflict": sum(1 for a in actions if a["action"] == "conflict"),
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
            "current_category": a["category"],
            "summary": _skill_summary(Path(a["from"])),
        }
        for a in plan["actions"]
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
        plan["llm_error"] = "the LLM did not return JSON"
        return plan
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError as exc:
        plan["llm_error"] = f"JSON invalido: {exc}"
        return plan
    index = {(a["skill"], a["harness"]): a for a in plan["actions"]}
    applied = 0
    for item in data.get("skills", []):
        key = (str(item.get("name", "")).strip(), str(item.get("harness", "")).strip())
        action = index.get(key)
        if not action:
            continue
        cat = str(item.get("category", "")).strip()
        if cat and cat in hub.tipos():
            action["category"] = cat
        flavor = str(item.get("flavor", "")).strip().lower()
        if flavor:
            action["flavor"] = flavor
        dup = str(item.get("duplicate_of", "")).strip()
        if dup and dup != action["skill"] and action["action"] == "import":
            action["action"] = "dedupe"
            action["reason"] = f"the LLM says it is the same as {dup}"
        if item.get("reason"):
            action["reason"] = action["reason"] or str(item["reason"])[:200]
        applied += 1
    plan["llm"] = {"model": cfg["model"], "aplicadas": applied}
    return plan


def apply_onboard(hub: Hub, plan: dict) -> dict:
    """Importa al hub: copia, verifica hash y recien ahi reemplaza por link."""
    imported, skipped, failed = [], [], []
    for action in plan["actions"]:
        if action["action"] != "import":
            skipped.append(f"{action['harness']}/{action['skill']} ({action['action']})")
            continue
        src = Path(action["from"])
        dest = hub.root / action["category"] / action["skill"]
        if dest.exists():
            failed.append(f"{action['skill']}: already exists: {dest}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(src, dest)
        except OSError as exc:
            failed.append(f"{action['skill']}: {exc}")
            continue
        if skill_hash(dest) != action["hash"]:
            shutil.rmtree(dest, ignore_errors=True)
            failed.append(f"{action['skill']}: hash mismatch after copying")
            continue
        imported.append({"skill": action["skill"], "harness": action["harness"], "to": str(dest), "harness_dir": str(src.parent)})
    return {"imported": imported, "skipped": skipped, "failed": failed}


def replace_native_with_link(hub: Hub, imported: list, quarantine: Path) -> dict:
    """Reemplaza la copia nativa por un junction al hub (la copia va a quarantine).

    Solo se llama con --replace-natives. El contenido ya esta verificado en el hub
    (mismo hash), y la copia original se mueve a <quarantine>/<harness>/<skill>:
    nunca se borra.
    """
    moved, linked, failed = [], [], []
    for item in imported:
        src = Path(item["harness_dir"]) / item["skill"] if item.get("harness_dir") else None
        if not src or not src.is_dir():
            failed.append(f"{item['skill']}: native folder not found")
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
            failed.append(f"{item['skill']}: could not move to quarantine ({exc})")
            continue
        make_links([(src, dest)])
        if os.path.lexists(src):
            linked.append(str(src))
        else:
            failed.append(f"{item['skill']}: quarantine ok but the link failed")
    return {"quarantine": moved, "links": linked, "failures": failed}

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
# Flavor: que agente espera cada skill, y portarla a los demas
# ---------------------------------------------------------------------------
#
# El hub tiene TODO (no se recorta por agente). Lo que se resuelve por flavor es
# la redaccion: si una skill esta cableada a Claude y el harness es Reasonix, se
# genera `agents/<harness>.md` con la version portada. El SKILL.md nunca se
# reescribe, asi que cualquier harness sigue leyendo lo mismo.

SKILL_META = "SKILLMETA.json"
AGENTS_DIR = "agents"

FLAVOR_HINTS = {
    "claude": ("claude code", "claude-code", "`claude", "claude -p", ".claude/"),
    "codex": ("codex cli", "`codex", "codex exec", ".codex/"),
    "hermes": ("`hermes", "hermes_cli", "~/.hermes", "hermes agent"),
    "reasonix": ("`reasonix", "reasonix.toml", "reasonix run", "reasonix -p"),
    "cursor": (".cursor/rules", "cursor rules", "`cursor"),
    "gemini": ("gemini cli", "`gemini"),
    "opencode": ("`opencode", "opencode run"),
    "copilot": ("copilot cli", "`copilot"),
}

GENERIC_FLAVOR = "generic"


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


def detect_flavor(skill_dir: Path) -> dict:
    """Heuristica determinista: que agente cablea el texto de la skill."""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return {"flavor": GENERIC_FLAVOR, "score": {}, "evidence": []}
    score = {}
    evidence = []
    for flavor, needles in FLAVOR_HINTS.items():
        hits = [n for n in needles if n in text]
        if hits:
            score[flavor] = len(hits)
            evidence.extend(f"{flavor}:{h}" for h in hits)
    best = max(score, key=score.get) if score else GENERIC_FLAVOR
    flavor = best if score.get(best, 0) >= 2 else GENERIC_FLAVOR
    return {"flavor": flavor, "score": score, "evidence": evidence[:6]}


def flavor_scan(hub: Hub, apply: bool = False) -> dict:
    """Detecta el flavor de cada skill del hub y (opcional) lo guarda en el sidecar."""
    rows = []
    for name, tipo in sorted(hub.skills().items()):
        d = hub.root / tipo / name
        det = detect_flavor(d)
        meta = skill_meta(d)
        changed = apply and meta.get("flavor") != det["flavor"]
        if changed:
            meta.update({"flavor": det["flavor"], "flavor_score": det["score"]})
            write_skill_meta(d, meta)
        rows.append({"skill": name, "tipo": tipo, **det, "antes": meta.get("flavor"), "guardado": changed})
    counts = {}
    for r in rows:
        counts[r["flavor"]] = 1 + counts.get(r["flavor"], 0)
    return {
        "ok": True,
        "op": "flavor-scan",
        "applied": apply,
        "skills": len(rows),
        "by_flavor": counts,
        "detail": rows,
    }


PORT_SYSTEM = (
    "You port agent skills between coding harnesses. You receive one SKILL.md and a "
    "target harness. Reply ONLY with a markdown fragment (no frontmatter, no fences) "
    "that a generic agent loading the original SKILL.md can follow in the target "
    "harness: translate tool names, CLI invocations and subagent mechanics. Keep every "
    "step, file path and constraint of the original. Do not invent steps. If the "
    "original already works unchanged, reply exactly: NO_PORT_NEEDED"
)


def port_skill(hub: Hub, name: str, to_harness: str, cfg: dict | None) -> dict:
    """Genera agents/<harness>.md para una skill cableada a otro agente."""
    tipos = hub.skills()
    tipo = tipos.get(name)
    if not tipo:
        return {"ok": False, "op": "port", "skill": name, "error": "not found in the hub"}
    d = hub.root / tipo / name
    meta = skill_meta(d)
    flavor = meta.get("flavor") or detect_flavor(d)["flavor"]
    if flavor == GENERIC_FLAVOR:
        return {"ok": True, "op": "port", "skill": name, "skipped": "the skill is already generic"}
    if not cfg:
        return {"ok": False, "op": "port", "skill": name, "error": "no LLM configured"}
    existing = d / AGENTS_DIR / f"{to_harness}.md"
    if existing.is_file():
        return {"ok": True, "op": "port", "skill": name, "skipped": f"already exists: {existing}"}
    body = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    try:
        answer = llm_chat(cfg, PORT_SYSTEM, f"target harness: {to_harness}\n\n---\n{body[:12000]}")
    except OSError as exc:
        return {"ok": False, "op": "port", "skill": name, "error": str(exc)}
    answer = answer.strip()
    if answer.upper().startswith("NO_PORT_NEEDED"):
        return {"ok": True, "op": "port", "skill": name, "skipped": "the model says no port is needed"}
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(answer + "\n", encoding="utf-8")
    ports = meta.setdefault("ports", {})
    ports[to_harness] = {"path": f"{AGENTS_DIR}/{to_harness}.md", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    meta["flavor"] = flavor
    write_skill_meta(d, meta)
    return {"ok": True, "op": "port", "skill": name, "flavor": flavor, "to": to_harness, "wrote": str(existing)}


def port_missing(hub: Hub, harnesses: list, cfg: dict | None, apply: bool = False) -> dict:
    """Porta a cada harness las skills cuyo flavor no es generico ni suyo."""
    plan, done = [], []
    for name, tipo in sorted(hub.skills().items()):
        d = hub.root / tipo / name
        flavor = skill_meta(d).get("flavor") or detect_flavor(d)["flavor"]
        if flavor == GENERIC_FLAVOR:
            continue
        for h in harnesses:
            if h == flavor:
                continue
            if (d / AGENTS_DIR / f"{h}.md").is_file():
                continue
            plan.append({"skill": name, "flavor": flavor, "to": h})
            if apply:
                done.append(port_skill(hub, name, h, cfg))
    return {"ok": True, "op": "port-scan", "applied": apply, "pendientes": plan, "hechas": done}


# ---------------------------------------------------------------------------
# Precedencia: el hub como capa de arriba en la config de cada harness
# ---------------------------------------------------------------------------
#
# No se mueve ningun archivo: se declara el hub PRIMERO en la lista de roots que
# cada harness ya soporta. Si borran skillgram, cada harness se queda con lo suyo.

def harness_configs() -> list:
    """Configs donde se puede declarar precedencia (harness, archivo, tipo)."""
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(("reasonix", Path(appdata) / "Reasonix" / "config.toml", "toml"))
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        out.append(("hermes", Path(hermes_home) / "config.yaml", "yaml"))
    out.append(("hermes", HOME / ".hermes" / "config.yaml", "yaml"))
    seen = set()
    uniq = []
    for h, p, k in out:
        if not p.is_file():
            continue
        key = str(p.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append((h, p, k))
    return uniq


def _same_path(entry: str, hub_path: str) -> bool:
    """Same directory even when the separator or the case differs.

    A hub already declared as `C:/Users/me/.agents/skills` must not gain a second
    entry `C:\\Users\\me\\.agents\\skills`: two identical roots in a harness config
    is one skill loaded twice, not a preference.
    """
    def norm(value: str) -> str:
        value = value.strip().strip('"').strip("'").rstrip("/\\")
        # a backslash is a separator on Windows only, so unify both spellings
        unified = value.replace("\\", os.sep).replace("/", os.sep)
        return os.path.normcase(str(Path(unified)))

    return norm(entry) == norm(hub_path)


def _hub_already_listed(text: str, hub_path: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not (line.startswith("- ") or line.startswith('"-') or '"-' in line):
            continue
        entry = line[2:].strip()
        if _same_path(entry, hub_path):
            return True
    return False


def _prepend_toml_paths(text: str, hub_path: str) -> tuple:
    """Puts the hub first in `paths = [...]` inside [skills]. (text, changed)"""
    lines = text.splitlines()
    in_skills = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_skills = stripped == "[skills]"
            continue
        if in_skills and stripped.startswith("paths"):
            if hub_path in line:
                return text, False
            start = i
            depth = 0
            chunk = []
            j = i
            while j < len(lines):
                chunk.append(lines[j])
                depth += lines[j].count("[") - lines[j].count("]")
                if depth <= 0:
                    break
                j += 1
            block = "\n".join(chunk)
            if not block.rstrip().endswith("]"):
                return text, False
            inner = block.split("=", 1)[1].strip()
            items = inner.strip().lstrip("[").rstrip("]").strip()
            if any(_same_path(part, hub_path) for part in items.split(",") if part.strip()):
                return text, False
            new_inner = f'["{hub_path}"' + (f", {items}" if items else "") + "]"
            indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
            lines[start : j + 1] = [f"{indent}paths = {new_inner}"]
            return "\n".join(lines), True
    return text, False


def _prepend_yaml_external_dirs(text: str, hub_path: str) -> tuple:
    """Puts the hub first in skills.external_dirs. (text, changed)"""
    if f"- {hub_path}" in text or f'- "{hub_path}"' in text or _hub_already_listed(text, hub_path):
        return text, False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "skills:":
            indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                if lines[j].strip().startswith("external_dirs:"):
                    sub = len(lines[j]) - len(lines[j].lstrip())
                    lines.insert(j + 1, " " * (sub + 2) + f"- {hub_path}")
                    return "\n".join(lines), True
                j += 1
            lines.insert(i + 1, " " * (indent + 2) + "external_dirs:")
            lines.insert(i + 2, " " * (indent + 4) + f"- {hub_path}")
            return "\n".join(lines), True
    return text, False


def apply_precedence(hub: Hub, apply: bool = False, only: str | None = None) -> dict:
    """Declares the hub first in the config of every harness that supports it.

    `only` restricts it to one harness: declaring the hub in Reasonix's
    config.toml when the ask was "make Hermes see the files" is a side effect
    nobody asked for.
    """
    hub_path = str(hub.root)
    rows = []
    for app, path, kind in harness_configs():
        if only and app != only:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            rows.append({"harness": app, "config": str(path), "error": str(exc)})
            continue
        if kind == "toml":
            new, changed = _prepend_toml_paths(text, hub_path)
        else:
            new, changed = _prepend_yaml_external_dirs(text, hub_path)
        row = {"harness": app, "config": str(path), "cambiado": changed, "applied": False}
        if changed and apply:
            backup = path.with_suffix(path.suffix + f".skillgram-bak-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                backup.write_text(text, encoding="utf-8")
                path.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8")
                row["applied"] = True
                row["backup"] = str(backup)
            except OSError as exc:
                row["error"] = str(exc)
        rows.append(row)
    return {
        "ok": True,
        "op": "precedence",
        "hub": hub_path,
        "applied": apply,
        "configs": rows,
        "cambios": sum(1 for r in rows if r.get("cambiado")),
    }


# ---------------------------------------------------------------------------
# Auto-adopcion: el "hijack" cooperativo
# ---------------------------------------------------------------------------

def adopt_skill(hub: Hub, path: Path, quarantine_root: Path | None = None) -> dict:
    """Adopta una skill nativa: copia al hub, verifica hash, deja link, original a quarantine."""
    src = Path(path)
    if not (src / "SKILL.md").is_file():
        return {"ok": False, "op": "adopt", "path": str(src), "error": "not a skill (missing SKILL.md)"}
    name = src.name
    tipo = _heuristic_category(name, hub.tipos())
    dest = hub.root / tipo / name
    if dest.exists():
        return {"ok": False, "op": "adopt", "path": str(src), "error": f"already exists: {dest}"}
    original_hash = skill_hash(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dest)
    except OSError as exc:
        return {"ok": False, "op": "adopt", "path": str(src), "error": str(exc)}
    if skill_hash(dest) != original_hash:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "op": "adopt", "path": str(src), "error": "hash mismatch after copying"}
    qroot = quarantine_root or (HOME / ".agents" / "skillgram-quarantine")
    qdir = qroot / src.parent.name
    qdir.mkdir(parents=True, exist_ok=True)
    target = qdir / name
    try:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(src), str(target))
    except OSError as exc:
        return {"ok": False, "op": "adopt", "path": str(src), "imported": str(dest), "error": f"quarantine: {exc}"}
    make_links([(src, dest)])
    return {
        "ok": os.path.lexists(src),
        "op": "adopt",
        "skill": name,
        "tipo": tipo,
        "hub": str(dest),
        "quarantine": str(target),
        "link": str(src),
    }


def adopt_natives(hub: Hub, harnesses: list | None = None) -> dict:
    """Adopta todas las skills native que encuentre (auto-adopcion del watch)."""
    done, failed = [], []
    for row in harness_inventory(hub):
        if harnesses and row["app"] not in harnesses:
            continue
        for nat in row["native"]:
            res = adopt_skill(hub, Path(nat["path"]))
            (done if res.get("ok") else failed).append(res)
    return {"ok": not failed, "op": "adopt-all", "adoptadas": done, "failed": failed}


# ---------------------------------------------------------------------------
# Mount: el scope es el PROYECTO (no el home del harness)
# ---------------------------------------------------------------------------
#
# El hub es la biblioteca (900 skills, existen). El proyecto declara las suyas
# (`use`) y `mount` las materializa SOLO dentro del proyecto, donde cada harness
# mira cuando trabaja ahi. Fuera del proyecto, el harness no ve nada del hub:
# asi dos agentes distintos sobre el mismo repo ven exactamente el mismo set.

PROJECT_SKILL_DIRS = {
    "agents": (".agents/skills",),
    "reasonix": (".agents/skills",),
    "claude": (".claude/skills",),
    "claude-code": (".claude/skills",),
    "codex": (".codex/skills",),
    "cursor": (".cursor/skills",),
    "hermes": (".agents/skills", ".hermes/skills"),
    "gemini": (".gemini/skills",),
    "gemini-cli": (".gemini/skills",),
    "opencode": (".opencode/skills",),
    "kiro": (".kiro/skills",),
    "qwen-code": (".qwen/skills",),
    "aider": (".aider/skills",),
    "copilot": (".github/skills",),
}


def project_scope(hub: Hub, project: Path, known: dict | None = None) -> dict:
    """Las skills del scope del proyecto (manifest de `use`).

    `known` (hub.skills()) se puede pasar ya calculado: `propagate` recorre
    decenas de repos y no puede reescanear el hub en cada uno.
    """
    manifest = _read_manifest(project)
    known = hub.skills() if known is None else known
    rows = []
    for name in sorted(manifest["skills"]):
        tipo = known.get(name)
        if not tipo:
            continue
        rows.append({"name": name, "tipo": tipo, "src": str(hub.root / tipo / name)})
    return {"project": str(project), "project_name": manifest.get("project_name"), "skills": rows}


def mount_project(hub: Hub, project: Path, apply: bool = False, prune_global: bool = False,
                  only: list[str] | None = None, known: dict | None = None) -> dict:
    """Materializa el scope del proyecto en cada harness que mira dirs de proyecto.

    `only` restringe los harnesss destino (lo usa `propagate --harness X`: propaga
    el set de ESE harness, no el de todos los instalados).
    """
    scope = project_scope(hub, project, known)
    if not scope["skills"]:
        return {
            "ok": False,
            "op": "mount",
            "project": str(project),
            "error": "the project has no skills in scope; run `skillgram use <skill>...` first",
        }
    wanted = {r["name"]: Path(r["src"]) for r in scope["skills"]}
    # the same gate as `use`: a skill the policy denies is never exposed here either
    tiers = policy_for(project)
    denied_mount = {}
    for name, src in list(wanted.items()):
        verdict = policy_verdict(tiers, src.parent.name, name)
        if not verdict["allowed"]:
            denied_mount[name] = verdict["rule"]
            del wanted[name]
    harnesses = [h["app"] for h in detect_harnesses() if h["instalado"]]
    if only:
        wanted_harnesses = {normalize_harness(h) for h in only}
        harnesses = [a for a in harnesses if a in wanted_harnesses or normalize_harness(a) in wanted_harnesses]
    rows = []
    for app in harnesses:
        rels = PROJECT_SKILL_DIRS.get(app)
        if not rels:
            continue
        for rel in rels:
            target_dir = project / rel
            created, removed, kept, skipped = [], [], [], []
            batch = []
            for name, src in wanted.items():
                dest = target_dir / name
                if is_link(dest):
                    tgt = link_target(dest)
                    if tgt and Path(tgt).resolve() == src.resolve():
                        kept.append(name)
                        continue
                    _remove_junction(dest)
                elif dest.exists():
                    skipped.append(name)
                    continue
                batch.append((dest, src))
            if target_dir.is_dir():
                for entry in target_dir.iterdir():
                    if entry.name in wanted or not is_link(entry):
                        continue
                    tgt = link_target(entry)
                    if tgt and _under(Path(tgt), hub.root):
                        if apply:
                            _remove_junction(entry)
                        removed.append(entry.name)
            if batch and apply:
                target_dir.mkdir(parents=True, exist_ok=True)
                make_links(batch)
                created = [d.name for d, _s in batch if os.path.lexists(d)]
            else:
                created = [d.name for d, _s in batch]
            rows.append(
                {
                    "harness": app,
                    "dir": str(target_dir),
                    "creados": created,
                    "already_present": kept,
                    "removed": removed,
                    "nativos_sin_tocar": skipped,
                }
            )

    pruned = {"harness": None, "removed": []}
    if prune_global:
        pruned = prune_global_links(hub, apply=apply)
    # a repo whose git sees the links would commit the skills themselves. The project
    # scope always holds links (promote wrote them), harnesses add their own dirs.
    ignore_dirs = [str(project_dir(project) / PROJECT_SKILLS), *[r["dir"] for r in rows]]
    ignores = sync_gitignore(project, ignore_dirs, apply=apply)
    return {
        "ok": True,
        "op": "mount",
        "project": str(project),
        "project_name": scope["project_name"],
        "skills": len(wanted),
        "applied": apply,
        "harnesses": rows,
        "gitignore": ignores,
        "policy_skipped": [{"skill": name, "rule": rule} for name, rule in sorted(denied_mount.items())],
        "global": pruned,
    }


def _dir_links_and_natives(dirpath: Path, hub_root: Path) -> tuple:
    """(links that resolve inside the hub, native skill dirs) for one skills folder."""
    links, natives = set(), set()
    if not dirpath.is_dir():
        return links, natives
    try:
        entries = sorted(dirpath.iterdir())
    except OSError:
        return links, natives
    for entry in entries:
        if is_link(entry):
            tgt = link_target(entry)
            if tgt and _under(Path(tgt), hub_root):
                links.add(entry.name)
            continue
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            natives.add(entry.name)
    return links, natives


def _align_rows(hub: Hub, project: Path, names: set) -> list:
    """One row per installed harness: what it can load, and what of the set it cannot."""
    home = {}
    for agent_dir in hub.agents():
        keys = {agent_dir.parent.name.lstrip("."), normalize_harness(agent_dir.parent.name.lstrip("."))}
        links, natives = _dir_links_and_natives(agent_dir, hub.root)
        for key in keys:
            entry = home.setdefault(key, {"links": set(), "natives": set()})
            entry["links"] |= links
            entry["natives"] |= natives
    rows = []
    for harness in detect_harnesses():
        if not harness["instalado"]:
            continue
        app = harness["app"]
        rels = PROJECT_SKILL_DIRS.get(app) or ()
        via_project, native_project = set(), set()
        for rel in rels:
            links, natives = _dir_links_and_natives(project / rel, hub.root)
            via_project |= links
            native_project |= natives
        visible = home.get(app) or home.get(normalize_harness(app)) or {"links": set(), "natives": set()}
        sees = via_project | visible["links"]
        missing = sorted(names - sees)
        extra = sorted(sees - names)
        if rels and via_project:
            mode = "project"
        elif rels:
            mode = "mixed" if visible["links"] else "project"
        else:
            mode = "global-only"
        rows.append({
            "harness": app,
            "mode": mode,
            "project_dirs": list(rels),
            "sees": len(sees),
            "via_project": len(via_project),
            "via_home": len(visible["links"]),
            "missing": missing,
            "extra": extra[:ALIGN_LIST_LIMIT],
            "extra_count": len(extra),
            "native": sorted(native_project | visible["natives"]),
            # who can fix a missing skill: mount writes project dirs, sync writes homes
            "fix": None if not missing else ("mount" if rels else "sync"),
        })
    return rows


def align(hub: Hub, project: Path, apply: bool = False, known: dict | None = None) -> dict:
    """What each harness actually sees of one project's set -- from the disk.

    Read-only by default. `--apply` only *adds* the links the project needs: an
    `extra` is reported, never removed, and a native directory is never touched (the
    project set is the only source; a harness that sees more is a violation to report,
    not a suggestion to propagate).
    """
    scope = project_scope(hub, project, known)
    if not scope["skills"]:
        return {"ok": False, "op": "align", "project": str(project),
                "error": "the project has no skills in scope; run `skillgram use <skill>...` first"}
    names = {row["name"] for row in scope["skills"]}
    rows = _align_rows(hub, project, names)
    applied = []
    if apply:
        todo = [r["harness"] for r in rows if r["missing"] and r["project_dirs"]]
        if todo:
            mount_project(hub, project, apply=True, only=todo, known=known)
            applied = todo
            rows = _align_rows(hub, project, names)
    unfixable = [r["harness"] for r in rows if r["missing"]]
    return {
        "ok": not unfixable,
        "op": "align",
        "project": str(project),
        "project_name": scope["project_name"],
        "skills": len(names),
        "applied": applied,
        "unfixable": unfixable,
        "rows": rows,
    }


def prune_global_links(hub: Hub, apply: bool = False) -> dict:
    """Retira de los homes los links al hub: strict project-only."""
    if not apply:
        total = 0
        for agent_dir in hub.agents():
            for entry in agent_dir.iterdir():
                if is_link(entry):
                    tgt = link_target(entry)
                    if tgt and _under(Path(tgt), hub.root):
                        total += 1
        return {"harness": "*", "removed": [], "pendientes": total, "applied": False}
    removed = 0
    for agent_dir in hub.agents():
        for entry in list(agent_dir.iterdir()):
            if not is_link(entry):
                continue
            tgt = link_target(entry)
            if tgt and _under(Path(tgt), hub.root) and _remove_junction(entry):
                removed += 1
    return {"harness": "*", "removed": [f"{removed} links"], "applied": True}

# ---------------------------------------------------------------------------
# Bootstrap: repo sin scope -> signals -> candidates -> selection LLM -> registro
# ---------------------------------------------------------------------------
#
# Uso de LLM: explicito y auditado. Solo decide el set inicial, porta flavor y
# redacta skills nuevas. Nunca borra ni escribe sin aprobacion. Cada decision deja
# rastro en el manifest (`by`) y en ~/.agents/skillgram-llm.log.

LLM_LOG = HOME / ".agents" / "skillgram-llm.log"

SIGNAL_FILES = {
    "node": "package.json",
    "ts": "tsconfig.json",
    "next": "next.config.js",
    "go": "go.mod",
    "python": "pyproject.toml",
    "rust": "Cargo.toml",
    "java": "pom.xml",
    "php": "composer.json",
    "ruby": "Gemfile",
    "docker": "Dockerfile",
    "compose": "docker-compose.yml",
    "terraform": "main.tf",
    "prisma": "prisma/schema.prisma",
}

# A repo keeps its stack in manifests, and only these say which stack it is: turning
# every dependency into a tag ranked noise first (vetgroom-demo's root package.json
# lists 89, most of them transitive: ansi-regex, bare-fs, ieee754...). The raw deps
# are still reported, but they only become tags when they name a known framework.
DEP_TAGS = {
    "next": "next", "react": "react", "react-dom": "react", "vue": "vue", "nuxt": "nuxt",
    "svelte": "svelte", "@angular/core": "angular", "express": "express", "fastify": "fastify",
    "@nestjs/core": "nestjs", "vite": "vite", "tailwindcss": "tailwind", "prisma": "prisma",
    "@prisma/client": "prisma", "drizzle-orm": "drizzle", "@supabase/supabase-js": "supabase",
    "supabase": "supabase", "typescript": "ts", "playwright": "playwright",
    "@playwright/test": "playwright", "jest": "jest", "vitest": "vitest", "stripe": "stripe",
    "electron": "electron", "expo": "expo", "react-native": "react-native",
    "dockerode": "docker", "mongoose": "mongodb", "pg": "postgres", "mysql2": "mysql",
    "@tanstack/react-query": "react", "zustand": "react",
}

EXTRA_SIGNALS = {
    "requirements.txt": "python",
    "Pipfile": "python",
    "next.config.mjs": "next",
    "next.config.ts": "next",
    "vite.config.ts": "vite",
    "vite.config.js": "vite",
    "angular.json": "angular",
    "supabase": "supabase",
    "railway.json": "railway",
    "vercel.json": "vercel",
    "fly.toml": "fly",
    "Chart.yaml": "helm",
}

# The stack usually lives one level down in a monorepo (backend/go.mod,
# frontend/package.json). node_modules and friends are never scanned.
MANIFEST_SKIP = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build",
    "target", "vendor", ".next", ".turbo", "coverage", "bin", "obj",
}

BOOTSTRAP_SYSTEM = (
    "You pick which skills from a catalog a repository actually needs. You receive the "
    "repo signals and the candidate skills (name, category, score, short summary). "
    'Reply ONLY with JSON: {"selected":[{"skill":"exact-name","why":"max 90 chars"}],'
    '"gaps":["capability the repo needs and no skill covers"]}. '
    "Select between 3 and 12 skills. Only exact names from the candidates. Prefer fewer, "
    "higher-value skills. gaps = concrete missing capabilities, max 5."
)

SKILL_DRAFT_SYSTEM = (
    "You write an agent skill (SKILL.md). Reply ONLY with the file content: YAML "
    "frontmatter with `name` (kebab-case) and `description` (one sentence, <=140 chars), "
    "then a body with: Activation Contract, Hard Rules, Decision Gates, Execution Steps, "
    "Output Contract. Be concrete and short (max 120 lines). No fences around the whole file."
)


def llm_log(action: str, detail: dict, model: str = "") -> None:
    try:
        LLM_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LLM_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action,
                                 "model": model, "detail": detail}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _scan_dirs(project: Path, depth: int = 1) -> list:
    """The repo root plus its immediate subdirectories (a monorepo keeps the stack
    one level down). Build output and dependency trees are never entered."""
    out = [project]
    if depth <= 0:
        return out
    try:
        children = sorted(project.iterdir())
    except OSError:
        return out
    for entry in children:
        if not entry.is_dir() or entry.name.lower() in MANIFEST_SKIP:
            continue
        if entry.name.startswith(".") and entry.name != ".github":
            continue
        out.append(entry)
    return out


def repo_signals(project: Path) -> dict:
    """Senales deterministas del repo (sin LLM).

    Los manifiestos del root y de sus subdirectorios, y solo los tags que nombran un
    stack conocido: `bootstrap` decide con esto, asi que un package.json con el
    lockfile volcado adentro no puede convertirse en 89 tags de dependencias
    transitivas (paso con vetgroom-demo: ansi-regex, bare-fs, ieee754...).
    """
    files: list = []
    tags: set = set()
    deps: list = []
    folders = _scan_dirs(project)
    for folder in folders:
        for tag, rel in SIGNAL_FILES.items():
            if (folder / rel).exists() and tag not in files:
                files.append(tag)
                tags.add(tag)
        for rel, tag in EXTRA_SIGNALS.items():
            if (folder / rel).exists() and tag not in files:
                files.append(tag)
                tags.add(tag)
        for alt in ("compose.yml", "compose.yaml", "docker-compose.yaml"):
            if (folder / alt).exists() and "compose" not in files:
                files.append("compose")
                tags.add("compose")
        pkg = folder / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            names = list(data.get("dependencies", {}) or {}) + list(data.get("devDependencies", {}) or {})
            deps.extend(n for n in names if n not in deps)
            for name in names:
                # exact name first, then the scope: @stripe/stripe-js and @stripe/x
                # are the same stack signal
                tag = DEP_TAGS.get(name) or (
                    DEP_TAGS.get(name.split("/")[0].lstrip("@")) if "/" in name else None
                )
                if tag:
                    tags.add(tag)
    if (project / ".github" / "workflows").is_dir():
        files.append("ci")
        tags.add("ci")
    if (project / ".env.example").exists() or (project / ".env.sample").exists():
        files.append("env")
        tags.add("env")
    return {
        "files": sorted(set(files)),
        "deps": sorted(set(deps))[:40],
        # no length filter: "go" and "ts" are two of the most useful tags there are
        "tags": sorted(tags),
        "scanned": [f.name or str(f) for f in folders],
    }


RANK_NAME = 3
RANK_CATEGORY = 2
RANK_BODY = 1
RANK_MIN = 2

# A tag is a word, not a fragment: "go" must match `golang-patterns` and must not
# match `godmode`; "node" must not match `nodejs-keccak256`. The body keeps a plain
# substring match, because prose says "deployment" and means deploy.
TOKEN_ALIASES = {
    "go": ("go", "golang"),
    "ts": ("ts", "typescript"),
    "js": ("js", "javascript"),
    "py": ("py", "python"),
    "k8s": ("k8s", "kubernetes"),
    "postgres": ("postgres", "postgresql"),
    "supabase": ("supabase",),
}

SEEDS_ENV = "SKILLGRAM_SEEDS"
SEED_MODES = ("always", "never")
ALIGN_LIST_LIMIT = 25

# ---- policy: the gate ------------------------------------------------------
# One format, three levels. A child level may only restrict, never expand: the
# effective rule for a skill is the union of every restriction that applies, so an
# organization `deny` cannot be undone by a project or a user file.
POLICY_FILE = "policy.json"
POLICY_ENV = "SKILLGRAM_POLICY"
POLICY_MODES = ("open", "closed")


def seeds_path() -> Path:
    env = (os.environ.get(SEEDS_ENV) or "").strip()
    return Path(env) if env else HOME / ".agents" / "skillgram-seeds.json"


def read_seeds() -> dict:
    """The user's standing decision: what is always recommended, and what never is.

    It lives in the hub (`~/.agents/skillgram-seeds.json`), not in the project,
    because a taste is not a repo: `qring-anti-slop` is wanted everywhere or nowhere.
    """
    try:
        data = json.loads(seeds_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = {}
    for mode in SEED_MODES:
        values = data.get(mode) if isinstance(data, dict) else None
        out[mode] = sorted({str(v).strip() for v in (values or []) if str(v).strip()})
    return out


def write_seeds(data: dict) -> Path:
    path = seeds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {mode: sorted(set(data.get(mode) or [])) for mode in SEED_MODES}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def seed_skills(hub: Hub, names: list, mode: str = "always", remove: bool = False) -> dict:
    """Add/remove seeds (or just list them when no name is given)."""
    if mode not in SEED_MODES:
        return {"ok": False, "op": "seed", "error": f"mode must be one of {', '.join(SEED_MODES)}"}
    seeds = read_seeds()
    if not names:
        return {"ok": True, "op": "seed", "always": seeds["always"], "never": seeds["never"],
                "file": str(seeds_path())}
    known = hub.skills()
    other = "never" if mode == "always" else "always"
    done, unknown = [], []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        if name not in known:
            unknown.append(name)
            continue
        if remove:
            if name in seeds[mode]:
                seeds[mode].remove(name)
                done.append(name)
            continue
        if name not in seeds[mode]:
            seeds[mode].append(name)
        if name in seeds[other]:
            seeds[other].remove(name)
        done.append(name)
    path = write_seeds(seeds)
    return {
        "ok": not unknown,
        "op": "seed",
        "mode": mode,
        "removed": remove,
        "changed": done,
        "unknown": unknown,
        "always": seeds["always"],
        "never": seeds["never"],
        "file": str(path),
    }


def _pattern_hits(pattern: str, category: str, name: str) -> bool:
    """A pattern matches `<category>/<skill>` or the bare skill name, `*` included."""
    pat = pattern.lstrip("!")
    return fnmatch.fnmatchcase(f"{category}/{name}", pat) or fnmatch.fnmatchcase(name, pat)


def user_policy_path() -> Path:
    return HOME / ".agents" / POLICY_FILE


def org_policy_ref() -> str:
    return (os.environ.get(POLICY_ENV) or "").strip()


def _policy_shape(data) -> dict:
    """Only the documented keys, with documented types. Unknown keys never count."""
    if not isinstance(data, dict):
        return {}
    mode = str(data.get("mode") or "open").strip().lower()
    out = {"mode": mode if mode in POLICY_MODES else "open"}
    for key in ("allow", "deny", "require"):
        values = data.get(key)
        out[key] = [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []
    harnesses = data.get("harnesses")
    out["harnesses"] = harnesses if isinstance(harnesses, dict) else {}  # reserved: validated, not read
    source = data.get("source")
    out["source"] = source if isinstance(source, dict) else {}
    return out


def _policy_hash(data: dict) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _read_policy_file(path: Path) -> tuple:
    """(shape, error). A missing file is not an error: most levels are absent."""
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, f"{path}: {exc}"
    except ValueError as exc:
        return {}, f"{path}: invalid JSON ({exc})"
    return _policy_shape(data), ""


def _materialize_policy(ref: str) -> tuple:
    """A policy reference is a git repo (`url#ref`) or a file on disk."""
    looks_git = (ref.startswith(("git@", "http://", "https://", "ssh://"))
                 or ref.endswith(".git") or "#" in ref)
    if not looks_git:
        path = Path(ref).expanduser()
        return path, ("" if path.is_file() else f"policy file not found: {path}")
    url, _, branch = ref.partition("#")
    branch = branch or "main"
    key = hashlib.sha1(f"{url}#{branch}".encode("utf-8")).hexdigest()[:12]
    dest = HOME / ".agents" / "policy-cache" / key
    git = _which("git")
    if not git:
        return Path(), "git is not on PATH, so the organization policy cannot be fetched"
    if (dest / ".git").is_dir():
        code, out = run_cli([git, "-C", str(dest), "fetch", "--depth", "1", "origin", branch], timeout=180)
        if code != 0:
            return Path(), f"git fetch failed for {url}#{branch}: {_first_lines(out, 2)}"
        run_cli([git, "-C", str(dest), "reset", "--hard", "FETCH_HEAD"], timeout=120)
    else:
        code, out = run_cli([git, "clone", "--depth", "1", "--branch", branch, url, str(dest)], timeout=180)
        if code != 0:
            return Path(), f"git clone failed for {url}#{branch}: {_first_lines(out, 2)}"
    for candidate in (dest / POLICY_FILE, dest / ".agents" / POLICY_FILE):
        if candidate.is_file():
            return candidate, ""
    return Path(), f"{POLICY_FILE} not found in {url}#{branch}"


def load_policy(project: Path | None = None, org: str | None = None) -> dict:
    """Every level that exists, in blocking order, with the hash an audit can quote."""
    org_ref = (org if org is not None else org_policy_ref()).strip()
    resolvers = []
    if org_ref:
        resolvers.append(("org", lambda: _materialize_policy(org_ref)))
    if project is not None:
        resolvers.append(("project", lambda: (project_dir(project) / POLICY_FILE, "")))
    resolvers.append(("user", lambda: (user_policy_path(), "")))
    levels, errors = [], []
    for level, resolve in resolvers:
        path, error = resolve()
        if error:
            errors.append(f"{level}: {error}")
            continue
        data, read_error = _read_policy_file(path)
        if read_error:
            errors.append(f"{level}: {read_error}")
            continue
        if not data:
            continue
        levels.append({"level": level, "path": str(path), "hash": _policy_hash(data), **data})
    return {
        "levels": levels,
        "errors": errors,
        "org": org_ref,
        # `require` is the union: a parent baseline cannot be relaxed either
        "require": sorted({item for level in levels for item in level["require"]}),
    }


def policy_verdict(levels: list, category: str, name: str) -> dict:
    """First level that blocks decides the reason; inside a level, deny beats allow."""
    for level in levels:
        for pattern in level["deny"]:
            if _pattern_hits(pattern, category, name):
                return {"allowed": False, "rule": f"{level['level']}:deny:{pattern}"}
        if level["mode"] == "closed":
            matched = False
            for pattern in level["allow"]:
                if _pattern_hits(pattern, category, name):
                    # `!pattern` re-includes; the last matching pattern wins
                    matched = not pattern.startswith("!")
            if not matched:
                return {"allowed": False, "rule": f"{level['level']}:closed"}
    return {"allowed": True, "rule": ""}


def policy_filter(levels: list, items: list) -> tuple:
    """(allowed names, denied rows) for [(name, category), ...]."""
    allowed, denied = [], []
    for name, category in items:
        verdict = policy_verdict(levels, category or "", name)
        if verdict["allowed"]:
            allowed.append(name)
        else:
            denied.append({"skill": name, "category": category or "", "rule": verdict["rule"]})
    return allowed, denied


def policy_for(project: Path | None = None) -> list:
    """The levels that apply right now -- what the doors call before they write."""
    return load_policy(project).get("levels") or []


def policy_show(project: Path | None = None, org: str | None = None) -> dict:
    loaded = load_policy(project, org)
    return {
        "ok": not loaded["errors"],
        "op": "policy",
        "action": "show",
        "project": str(project) if project is not None else None,
        "org": loaded["org"],
        "levels": [
            {"level": level["level"], "path": level["path"], "mode": level["mode"],
             "hash": level["hash"], "allow": level["allow"], "deny": level["deny"],
             "require": level["require"],
             "harnesses_reserved": sorted(level["harnesses"])}
            for level in loaded["levels"]
        ],
        "require": loaded["require"],
        "errors": loaded["errors"],
    }


def policy_check(hub: Hub, project: Path, org: str | None = None) -> dict:
    """Violations in this project's scope, plus the `require`d skills it is missing."""
    loaded = load_policy(project, org)
    scope = project_scope(hub, project)
    items = [(row["name"], row["tipo"]) for row in scope["skills"]]
    _allowed, denied = policy_filter(loaded["levels"], items)
    required_missing = [
        pattern for pattern in loaded["require"]
        if not any(_pattern_hits(pattern, row["tipo"], row["name"]) for row in scope["skills"])
    ]
    return {
        "ok": not denied and not required_missing and not loaded["errors"],
        "op": "policy",
        "action": "check",
        "project": str(project),
        "levels": [{"level": level["level"], "path": level["path"], "hash": level["hash"],
                    "mode": level["mode"]} for level in loaded["levels"]],
        "errors": loaded["errors"],
        "scope": len(items),
        "violations": denied,
        "required_missing": required_missing,
    }


def rank_skills(hub: Hub, tokens: list, limit: int | None = None,
                prefer: list | None = None, avoid: list | None = None) -> list:
    """Weighted, deterministic scoring of the catalog.

    A token in the skill NAME is worth 3, in its category 2, in the head of the
    SKILL.md 1: "deploy" in the name means the skill is about deploying, matching it
    in 1200 chars of body may only be a mention. Anything below `RANK_MIN` is noise
    and is dropped instead of padded into the list (that is how `apple-meta-craft`
    used to rank for a Go+Vite repo). `prefer` is the user's seed list and comes
    first whatever its score, because a seed is a decision, not a guess.

    `bootstrap` and `missing` only differ in what they publish from each row.
    """
    prefer_set = set(prefer or [])
    avoid_set = set(avoid or [])
    compiled = []
    for token in (str(t).lower() for t in tokens if str(t).strip()):
        words = TOKEN_ALIASES.get(token, (token,))
        body = "|".join(re.escape(w) for w in words)
        compiled.append((token, re.compile(r"(?<![a-z0-9])(" + body + r")(?![a-z0-9])")))
    rows = []
    for name, tipo in hub.skills().items():
        if name in avoid_set:
            continue
        try:
            head = (hub.root / tipo / name / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            head = ""
        low_head = head.lower()
        low_tipo = tipo.lower()
        low_name = name.lower()
        score = hits = 0
        for token, pattern in compiled:
            in_name = bool(pattern.search(low_name))
            in_category = bool(pattern.search(low_tipo))
            in_body = token in low_head
            where = in_name * RANK_NAME + in_category * RANK_CATEGORY + in_body * RANK_BODY
            if where:
                score += where
                hits += 1
        seeded = name in prefer_set
        if seeded or score >= RANK_MIN:
            rows.append({"skill": name, "tipo": tipo, "score": score, "head": head,
                         "seed": seeded, "hits": hits})
    rows.sort(key=lambda r: (not r["seed"], -r["score"], -r["hits"], len(r["skill"]), r["skill"]))
    return rows if limit is None else rows[:limit]


def candidate_skills(hub: Hub, signals: dict, limit: int = 60) -> list:
    """Filtro determinista del catalogo: 900 -> ~60 candidates (seeds included)."""
    seeds = read_seeds()
    return [
        {"skill": row["skill"], "tipo": row["tipo"], "score": row["score"], "seed": row["seed"],
         "summary": row["head"][:280].replace("\n", " ")}
        for row in rank_skills(hub, signals["tags"], limit,
                               prefer=seeds["always"], avoid=seeds["never"])
    ]


def llm_select(cfg: dict, project: Path, signals: dict, candidates: list) -> dict:
    """La LLM elige el set final y los gaps. Devuelve dict validado."""
    payload = {
        "repo": project.name,
        "signals": signals,
        "candidates": [
            {"skill": c["skill"], "category": c["tipo"], "score": c["score"], "summary": c["summary"][:200]}
            for c in candidates
        ],
    }
    raw = llm_chat(cfg, BOOTSTRAP_SYSTEM, json.dumps(payload, ensure_ascii=False)[:24000])
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise OSError("the LLM did not return JSON")
    data = json.loads(raw[start:end + 1])
    known = {c["skill"] for c in candidates}
    selected = []
    for item in data.get("selected", []) or []:
        name = str(item.get("skill", "")).strip()
        if name in known:
            selected.append({"skill": name, "why": str(item.get("why", ""))[:140]})
    gaps = [str(g)[:160] for g in (data.get("gaps") or [])][:5]
    return {"selected": selected, "gaps": gaps, "skipped": len(candidates) - len(selected)}


def bootstrap(hub: Hub, project: Path, cfg: dict | None, apply: bool = False) -> dict:
    """Plan (y opcional aplicacion) del set inicial de un proyecto sin scope."""
    scope = project_scope(hub, project)
    if scope["skills"]:
        return {"ok": True, "op": "bootstrap", "skipped": "the project already has a scope",
                "project": str(project), "skills": len(scope["skills"])}
    signals = repo_signals(project)
    candidates = candidate_skills(hub, signals)
    seeds = read_seeds()
    result = {
        "ok": True,
        "op": "bootstrap",
        "project": str(project),
        "signals": signals,
        "candidates": len(candidates),
        "seeds": seeds["always"],
        "selection": [],
        "gaps": [],
        "applied": False,
        "llm": cfg["model"] if cfg else None,
    }
    if not cfg:
        result["note"] = "no LLM configured: candidates only (no set is chosen)"
        result["top_candidates"] = [c["skill"] for c in candidates[:12]]
        if apply and seeds["always"]:
            # a seed is a standing decision: with no LLM to pick a set, the seeds
            # are the set. `qring-anti-slop` should not depend on a model call.
            res = promote(hub, seeds["always"], project)
            manifest = _read_manifest(project)
            for name in seeds["always"]:
                entry = manifest["skills"].get(name)
                if entry:
                    entry["by"] = "seed"
                    entry["why"] = "always recommended (skillgram seed)"
            manifest["bootstrap_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _write_manifest(project, manifest)
            result["applied"] = True
            result["registradas"] = res["created"] + res["already_present"]
        return result
    selection = llm_select(cfg, project, signals, candidates)
    # the seeds are not up for a vote: the model may add, never subtract
    picked = {s["skill"] for s in selection["selected"]}
    missing_seeds = [{"skill": name, "why": "always recommended (skillgram seed)"}
                     for name in seeds["always"] if name not in picked]
    if missing_seeds:
        selection["selected"] = missing_seeds + selection["selected"]
    llm_log("bootstrap-select", {"project": str(project), "selected": [s["skill"] for s in selection["selected"]],
                                 "seeds": seeds["always"],
                                 "gaps": selection["gaps"], "candidates": len(candidates)}, cfg["model"])
    result.update({
        "selection": selection["selected"],
        "gaps": selection["gaps"],
        "skipped": selection["skipped"],
    })
    if apply and selection["selected"]:
        names = [s["skill"] for s in selection["selected"]]
        res = promote(hub, names, project)
        manifest = _read_manifest(project)
        for item in selection["selected"]:
            entry = manifest["skills"].get(item["skill"])
            if entry:
                entry["why"] = item["why"]
                entry["by"] = f"llm:{cfg['model']}"
        manifest["bootstrap_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_manifest(project, manifest)
        result["applied"] = True
        result["registradas"] = res["created"] + res["already_present"]
    return result


def missing(hub: Hub, text: str, limit: int = 8) -> dict:
    """Consulta determinista: hay skill para esto? (sin LLM).

    The seeds do not answer a lookup: a query is a specific need, so `prefer` stays
    empty here (only `never` applies, because the user said never).
    """
    toks = [t for t in re_findall_words(text) if len(t) > 2]
    ranked = rank_skills(hub, toks, avoid=read_seeds()["never"])
    found = bool(ranked and ranked[0]["score"] >= 2)
    best = ranked[0]["skill"] if ranked else ""
    record_demand(text, found, best=best, hits=len(ranked))
    return {
        "ok": True,
        "op": "missing",
        "query": text,
        "found": found,
        "candidates": [
            {"skill": row["skill"], "tipo": row["tipo"], "score": row["score"]} for row in ranked[:limit]
        ],
    }


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


def propose_skill(hub: Hub, project: Path, need: str, cfg: dict | None, apply: bool = False) -> dict:
    """Redacta una skill nueva (LLM), la valida y (con apply) la publica + registra."""
    if not cfg:
        return {"ok": False, "op": "propose", "error": "no LLM configured"}
    signals = repo_signals(project)
    prompt = (
        f"need: {need}\nrepo: {project.name}\nsignals: {', '.join(signals['files'])}\n"
        f"categories available: {', '.join(hub.tipos()) or 'any'}"
    )
    try:
        draft = llm_chat(cfg, SKILL_DRAFT_SYSTEM, prompt).strip()
    except OSError as exc:
        return {"ok": False, "op": "propose", "error": str(exc)}
    if draft.startswith("```"):
        draft = draft.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    name = ""
    for line in draft.splitlines()[:12]:
        if line.strip().lower().startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"').strip("'")
            break
    ok_front = draft.startswith("---") and "description:" in draft[:600] and bool(name)
    result = {
        "ok": ok_front,
        "op": "propose",
        "skill": name,
        "valida": ok_front,
        "caracteres": len(draft),
        "llm": cfg["model"],
        "applied": False,
    }
    if not ok_front:
        result["error"] = "the draft has no valid frontmatter (name + description)"
        result["draft_head"] = draft[:400]
        return result
    if name in hub.skills():
        result["ok"] = False
        result["error"] = f"a skill named this already exists: {name} en el hub"
        return result
    if not apply:
        result["draft_head"] = draft[:600]
        return result
    tipo = _heuristic_category(name, hub.tipos())
    dest = hub.root / tipo / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(draft if draft.endswith("\n") else draft + "\n", encoding="utf-8")
    # the authoring gate: the five sections of docs/SKILL-CONTRACT.md are required,
    # not optional advice in a prompt
    gate = lint_skill(dest, require_contract=True)
    desc_long = any("description is" in w for w in gate["warnings"])
    if gate["errors"] or desc_long:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "op": "propose", "skill": name, "error": "draft failed lint: " + "; ".join(gate["errors"]),
                "lint": gate}
    write_skill_meta(dest, {"flavor": GENERIC_FLAVOR, "by": f"llm:{cfg['model']}",
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "hash": skill_hash(dest),
                            "source": {"kind": "authored"}})
    res = promote(hub, [name], project)
    manifest = _read_manifest(project)
    entry = manifest["skills"].get(name)
    if entry:
        entry["why"] = f"creada por skillgram para: {need[:120]}"
        entry["by"] = f"llm:{cfg['model']}"
    _write_manifest(project, manifest)
    llm_log("propose-skill", {"skill": name, "tipo": tipo, "need": need[:200], "project": str(project)}, cfg["model"])
    result.update({"ok": True, "applied": True, "tipo": tipo, "hub": str(dest),
                   "registradas": res["created"] + res["already_present"]})
    return result

# ---------------------------------------------------------------------------
# Indice FTS5: buscar en el hub de verdad (no substring)
# ---------------------------------------------------------------------------

INDEX_PATH = HOME / ".agents" / "skillgram-index.db"


def _index_conn(path: Path | None = None):
    import sqlite3

    p = Path(path or INDEX_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS skills USING fts5("
                 "name, tipo, description, body, tokenize='unicode61')")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def index_build(hub: Hub, path: Path | None = None) -> dict:
    """(Re)construye el indice FTS5 del hub. Idempotente."""
    conn = _index_conn(path)
    try:
        conn.execute("DELETE FROM skills")
        rows = 0
        for name, tipo in sorted(hub.skills().items()):
            d = hub.root / tipo / name
            try:
                text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            desc = ""
            for line in text.splitlines()[:20]:
                if line.strip().lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
                    break
            conn.execute("INSERT INTO skills (name, tipo, description, body) VALUES (?,?,?,?)",
                         (name, tipo, desc, text[:20000]))
            rows += 1
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('built_at', ?)",
                     (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('skills', ?)", (str(rows),))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "op": "index", "skills": rows, "path": str(path or INDEX_PATH)}


def index_search(text: str, limit: int = 10, path: Path | None = None) -> dict:
    """Busqueda FTS5 con fallback a tokens si el indice no existe."""
    conn = _index_conn(path)
    try:
        q = " OR ".join(f'"{t}"' for t in re_findall_words(text) if len(t) > 2) or '""'
        try:
            cur = conn.execute(
                "SELECT name, tipo, description, bm25(skills) AS rank FROM skills "
                "WHERE skills MATCH ? ORDER BY rank LIMIT ?", (q, limit))
            rows = [{"skill": r[0], "tipo": r[1], "description": r[2][:160], "rank": round(r[3], 3)}
                    for r in cur.fetchall()]
        except Exception:
            rows = []
        total = conn.execute("SELECT v FROM meta WHERE k='skills'").fetchone()
        built = conn.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()
    finally:
        conn.close()
    return {"ok": True, "op": "search", "query": text, "results": rows,
            "indexed": int(total[0]) if total else 0, "built_at": built[0] if built else None}


# ---------------------------------------------------------------------------
# Ciclo de vida: uso, revision, pin y retiro con tombstone
# ---------------------------------------------------------------------------

RETIRED_DIR = "_retired"
RETIRED_LOG = "_retired.json"


def touch_usage(project: Path, names: list, by: str = "agent") -> dict:
    """Marca uso en el manifest del proyecto (last_used / use_count)."""
    manifest = _read_manifest(project)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    touched, absent = [], []
    for name in names:
        entry = manifest["skills"].get(name)
        if not entry:
            absent.append(name)
            continue
        entry["last_used"] = stamp
        entry["use_count"] = int(entry.get("use_count", 0)) + 1
        entry["last_used_by"] = by
        # Per harness, because the harness is a mask: switching agents must not lose
        # which skills this project has been living on.
        tallies = entry.setdefault("by_harness", {})
        tallies[by] = int(tallies.get(by, 0)) + 1
        touched.append(name)
    _write_manifest(project, manifest)
    out = {"ok": bool(touched), "op": "touch", "by": by, "touched": touched}
    if absent:
        out["not_in_scope"] = absent
        out["hint"] = "run `skillgram use <skill>` first: usage is declared per project"
    return out


def list_skills(hub: Hub, category: str | None = None, project: Path | None = None,
                used: str | None = None, limit: int | None = None) -> dict:
    """What is in the hub, and what has actually been used in a project.

    `used` is `used` / `unused` when a project is given: the only usage signal
    skillgram has is what the agent declares with `touch` (or `use`), because a
    harness reading a file is invisible from here.
    """
    known = hub.skills()
    usage = _read_manifest(project)["skills"] if project is not None else {}
    seeded = set(read_seeds()["always"])
    rows = []
    for name, tipo in sorted(known.items()):
        if category and tipo != category:
            continue
        meta = skill_meta(hub.root / tipo / name)
        entry = usage.get(name, {})
        count = int(entry.get("use_count", 0) or 0)
        if used == "used" and not count:
            continue
        if used == "unused" and count:
            continue
        source = meta.get("source") or {}
        rows.append({
            "skill": name,
            "category": tipo,
            "by": meta.get("by", ""),
            "source": source.get("kind", ""),
            "seed": name in seeded,
            "use_count": count,
            "last_used": entry.get("last_used", ""),
            # who used it, so another harness can pick up where the last one left off
            "last_used_by": entry.get("last_used_by", ""),
            "by_harness": dict(entry.get("by_harness") or {}),
        })
    total = len(rows)
    if limit:
        rows = rows[:limit]
    return {
        "ok": True,
        "op": "list",
        "category": category,
        "project": str(project) if project is not None else None,
        "used": used,
        "skills": total,
        "rows": rows,
    }


def pin_skill(project: Path, names: list, pinned: bool = True) -> dict:
    manifest = _read_manifest(project)
    done = []
    for name in names:
        entry = manifest["skills"].get(name)
        if entry:
            entry["pinned"] = pinned
            done.append(name)
    _write_manifest(project, manifest)
    return {"ok": True, "op": "pin", "pinned": pinned, "skills": done}


def review_list(hub: Hub, project: Path | None = None, days: int = 90, fail_if_stale: bool = False) -> dict:
    """Skills promoted long ago and not used recently (or never used)."""
    manifest = _read_manifest(project) if project else {"skills": {}}
    now = time.time()
    stale, never, pinned = [], [], []
    for name, entry in sorted(manifest["skills"].items()):
        if entry.get("pinned"):
            pinned.append(name)
            continue
        last = entry.get("last_used") or entry.get("promoted_at")
        if not entry.get("use_count"):
            never.append(name)
            continue
        try:
            ts = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError):
            continue
        if (now - ts) / 86400 >= days:
            stale.append({"skill": name, "dias": int((now - ts) / 86400), "use_count": entry.get("use_count")})
    return {"ok": True, "op": "review", "project": str(project) if project else None,
            "threshold_days": days, "never_used": never, "stale": stale, "pinned": pinned,
            "ok": not (fail_if_stale and (never or stale))}


def retire_skill(hub: Hub, name: str, reason: str = "") -> dict:
    """Retires a skill from the hub into _retired/ with a tombstone (never deletes)."""
    tipo = hub.skills().get(name)
    if not tipo:
        return {"ok": False, "op": "retire", "skill": name, "error": "not found in the hub"}
    src = hub.root / tipo / name
    dest = hub.root / RETIRED_DIR / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(src), str(dest))
    except OSError as exc:
        return {"ok": False, "op": "retire", "skill": name, "error": str(exc)}
    log_path = hub.root / RETIRED_DIR / RETIRED_LOG
    try:
        data = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []
    except (OSError, ValueError):
        data = []
    if not isinstance(data, list):
        data = []
    data.append({"skill": name, "tipo": tipo, "retired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "reason": reason[:200], "hash": skill_hash(dest)})
    log_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "op": "retire", "skill": name, "tipo": tipo, "to": str(dest)}


# ---------------------------------------------------------------------------
# Export / import del hub (portable, con hashes)
# ---------------------------------------------------------------------------

def export_hub(hub: Hub, out: Path) -> dict:
    import tarfile

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "hub": str(hub.root), "skills": []}
    with tarfile.open(out, "w:gz") as tar:
        for name, tipo in sorted(hub.skills().items()):
            d = hub.root / tipo / name
            manifest["skills"].append({"name": name, "tipo": tipo, "hash": skill_hash(d)})
            tar.add(str(d), arcname=f"{tipo}/{name}")
        tmp = out.with_suffix(".json")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tar.add(str(tmp), arcname="hub.json")
        tmp.unlink(missing_ok=True)
    return {"ok": True, "op": "export", "file": str(out), "skills": len(manifest["skills"]),
            "bytes": out.stat().st_size}


def import_hub(hub: Hub, src: Path, apply: bool = False) -> dict:
    import tarfile
    import tempfile

    src = Path(src)
    if not src.is_file():
        return {"ok": False, "op": "import", "error": f"not found: {src}"}
    added, skipped, failed = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(src, "r:gz") as tar:
            tar.extractall(tmp)
        meta_path = Path(tmp) / "hub.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {"skills": []}
        for item in meta.get("skills", []):
            name, tipo = item.get("name"), item.get("tipo")
            src_dir = Path(tmp) / tipo / name
            if not src_dir.is_dir():
                failed.append(f"{name}: missing from the archive")
                continue
            if skill_hash(src_dir) != item.get("hash"):
                failed.append(f"{name}: hash mismatch")
                continue
            dest = hub.root / tipo / name
            if dest.exists():
                skipped.append(name)
                continue
            if apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_dir, dest)
                added.append(name)
            else:
                added.append(name)
    return {"ok": not failed, "op": "import", "file": str(src), "applied": apply,
            "imported": added, "already_present": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# MCP: skillgram como tool del agente (stdio, JSON-RPC 2.0 por linea)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Lint: gate de calidad deterministico (corre antes de publicar)
# ---------------------------------------------------------------------------

MAX_DESCRIPTION = 140
MAX_BODY_LINES = 400
MIN_BODY_LINES = 6
MIN_DESCRIPTION = 20

# The five sections every authored skill declares (docs/SKILL-CONTRACT.md).
CONTRACT_SECTIONS = (
    "Activation Contract",
    "Hard Rules",
    "Decision Gates",
    "Execution Steps",
    "Output Contract",
)

# Narrow on purpose: these match a live credential, not the placeholders and
# banner strings that documentation legitimately contains (a bare `ghp_xxx`, a
# PEM header with no body, a JWT header fragment).
SECRET_PATTERNS = (
    ("stripe live key", r"sk_live_[A-Za-z0-9]{20,}"),
    ("pypi token", r"pypi-AgEI[A-Za-z0-9_-]{30,}"),
    ("github fine-grained token", r"github_pat_[A-Za-z0-9_]{40,}"),
    ("github token", r"ghp_[A-Za-z0-9]{30,}"),
    ("aws access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,40}?[A-Za-z0-9+/]{100,}"),
    ("slack token", r"xox[baprs]-[0-9A-Za-z-]{20,}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
)

PLACEHOLDERS = ("TODO", "TBD", "FIXME", "XXX-PLACEHOLDER")
LOCAL_PATH = re.compile(r"C:\\Users|C:/Users|/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/")


def _frontmatter(text: str) -> dict:
    """Parseo minimo del frontmatter YAML (clave: valor de una linea)."""
    text = text.lstrip("\ufeff")  # a BOM from a Windows editor is not "no frontmatter"
    out = {}
    if not text.startswith("---"):
        return out
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, val = line.partition(":")
            out[key.strip().lower()] = val.strip().strip('"').strip("'")
    return out


def _sections(text: str) -> dict:
    """{titulo de nivel 2: cuerpo}. Un titulo sin cuerpo queda como cadena vacia."""
    out: dict = {}
    current, buf = None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def lint_skill(skill_dir: Path, require_contract: bool = False) -> dict:
    """Errores duros y avisos de una skill.

    `require_contract` is the authoring gate: the five sections of
    docs/SKILL-CONTRACT.md become errors, not warnings. The hub's existing 843
    skills are linted without it so `lint` reports the debt instead of failing
    wholesale; `propose` and `new` publish through the strict form.
    """
    errors, warnings = [], []
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return {"skill": skill_dir.name, "errors": ["missing SKILL.md"], "warnings": []}
    text = path.read_text(encoding="utf-8", errors="replace")
    text = text.lstrip("\ufeff")
    fm = _frontmatter(text)
    if not fm:
        errors.append("missing frontmatter")
    name = fm.get("name", "")
    if not name:
        errors.append("frontmatter has no `name`")
    else:
        if name != skill_dir.name:
            message = f"name `{name}` != folder `{skill_dir.name}`"
            (errors if require_contract else warnings).append(message)
        if name != name.lower() or " " in name or "_" in name:
            errors.append("name must be kebab-case")
    desc = fm.get("description", "")
    if not desc:
        errors.append("frontmatter has no `description`")
    elif len(desc) > MAX_DESCRIPTION:
        warnings.append(f"description is {len(desc)} chars (max {MAX_DESCRIPTION})")
    elif len(desc) < MIN_DESCRIPTION:
        errors.append(f"description is {len(desc)} chars (min {MIN_DESCRIPTION}): say when the skill applies")
    body_lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(body_lines) < MIN_BODY_LINES:
        errors.append(f"body has {len(body_lines)} non-empty lines (min {MIN_BODY_LINES})")
    elif len(body_lines) > MAX_BODY_LINES:
        warnings.append(f"body has {len(body_lines)} non-empty lines (max {MAX_BODY_LINES})")
    for ref in ("references/", "scripts/", "assets/"):
        if ref in text:
            d = skill_dir / ref.strip("/")
            if not d.is_dir():
                warnings.append(f"mentions {ref} but the folder does not exist")

    sections = _sections(text)
    for wanted in CONTRACT_SECTIONS:
        if wanted not in sections:
            message = f"missing section: ## {wanted}"
            (errors if require_contract else warnings).append(message)
        elif not sections[wanted]:
            errors.append(f"empty section: ## {wanted}")

    for label, pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            errors.append(f"looks like a live credential ({label})")
    for word in PLACEHOLDERS:
        if word in text:
            errors.append(f"placeholder left in the text ({word})")
    if LOCAL_PATH.search(text):
        warnings.append("mentions a local absolute path (the skill stops being portable)")
    return {"skill": skill_dir.name, "errors": errors, "warnings": warnings,
            "sections": [s for s in CONTRACT_SECTIONS if s in sections]}


def lint_hub(hub: Hub, only: str | None = None, strict: bool = False, contract: bool = False) -> dict:
    """Lint de una skill o de todo el hub. `contract` exige las cinco secciones."""
    names = [only] if only else sorted(hub.skills())
    known = hub.skills()
    rows = []
    for name in names:
        tipo = known.get(name)
        if not tipo:
            rows.append({"skill": name, "errors": ["not found in the hub"], "warnings": []})
            continue
        rows.append(lint_skill(hub.root / tipo / name, require_contract=contract))
    with_errors = [r for r in rows if r["errors"]]
    with_warnings = [r for r in rows if r["warnings"]]
    failing = bool(with_errors) or (strict and bool(with_warnings))
    return {
        "ok": not failing,
        "op": "lint",
        "strict": strict,
        "contract": contract,
        "skills": len(rows),
        "errors": with_errors,
        "warnings": with_warnings,
        "detail": rows if only else [],
    }


# ---------------------------------------------------------------------------
# Authoring: scaffold del contrato (docs/SKILL-CONTRACT.md)
# ---------------------------------------------------------------------------
#
# `new` no publica nada: escribe el esqueleto y la provenance, y el que decide
# si sirve es `lint --contract` (que es tambien el gate de `propose` y de
# `add`). Una sola puerta, tres entradas: new (authorear), propose (LLM),
# add (importar de un repo).

SCAFFOLD = """---
name: {name}
description: TODO una frase que diga CUANDO se usa esta skill (max 140 chars)
---

# {name}

## Activation Contract

Use when: TODO. (Una condicion concreta: un comando, un error, una decision.)

Not for: TODO. (El caso vecino que alguien confundiria con este.)

## Hard Rules

1. TODO una regla verificable (no un deseo).

## Decision Gates

| Observation | Do this |
|---|---|
| TODO | TODO |

## Execution Steps

1. TODO el comando exacto, con las rutas que de verdad existen.

## Output Contract

Report: TODO que tiene que devolver el agente (campos, no prosa).
"""


def new_skill(hub: Hub, name: str, category: str | None = None, author: str = "human") -> dict:
    """Crea el esqueleto de una skill nueva. No la publica: eso lo decide `lint`."""
    slug = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return {"ok": False, "op": "new", "error": "name must be kebab-case (a-z, 0-9 and - only)"}
    tipos = hub.tipos()
    cat = (category or "imported").strip()
    # a category counts if it already holds skills OR its folder exists: the first
    # skill of a fresh category is exactly the case `new --category` is for
    if cat != DEFAULT_CATEGORY and cat not in tipos and not (hub.root / cat).is_dir():
        return {"ok": False, "op": "new",
                "error": f"unknown category `{cat}`; existing: {', '.join(tipos) or '(none)'}"}
    dest = hub.root / cat / slug
    if (dest / "SKILL.md").exists():
        return {"ok": False, "op": "new", "error": f"already exists: {dest}"}
    if slug in hub.skills():
        return {"ok": False, "op": "new", "error": f"a skill named `{slug}` already exists in another category"}
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(SCAFFOLD.format(name=slug), encoding="utf-8")
    write_skill_meta(dest, {
        "by": author,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hash": skill_hash(dest),
        "source": {"kind": "authored"},
    })
    return {
        "ok": True,
        "op": "new",
        "skill": slug,
        "category": cat,
        "path": str(dest),
        "next": f"fill the TODOs, then `skillgram lint {slug} --contract` (it fails until they are gone)",
    }


# ---------------------------------------------------------------------------
# Protocolo: que el agente sepa que el hub existe (se inyecta al montar)
# ---------------------------------------------------------------------------

PROTOCOL_BEGIN = "<!-- skillgram:protocol:begin -->"
PROTOCOL_END = "<!-- skillgram:protocol:end -->"

# `mount` writes links inside the repo. Git does not know what a junction is: it
# sees a directory and would commit the skill's files -- the one thing skillgram
# promises never to do (no copies). The scope itself stays tracked, because that is
# the memory the next harness reads (`.agents/config.json`, `.agents/skills.json`).
GITIGNORE_BEGIN = "# skillgram:links:begin (written by `skillgram mount`; do not commit links)"
GITIGNORE_END = "# skillgram:links:end"


def link_ignores(dirs: list, project: Path) -> list:
    """The mounted dirs as git paths, relative to the project and slash-terminated."""
    out = []
    for raw in dirs:
        try:
            rel = Path(raw).resolve().relative_to(Path(project).resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if rel and rel != "." and f"{rel}/" not in out:
            out.append(f"{rel}/")
    return sorted(out)


def sync_gitignore(project: Path, dirs: list, apply: bool = False) -> dict:
    """Keep `git status` clean after a mount. Idempotent, delimited, reversible."""
    paths = link_ignores(dirs, project)
    out = {"ok": True, "op": "gitignore", "file": str(project / ".gitignore"), "paths": paths,
           "applied": apply, "changed": False}
    if not paths:
        return out
    path = project / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as exc:
        out.update({"ok": False, "error": str(exc)})
        return out
    block = "\n".join([GITIGNORE_BEGIN, *paths, GITIGNORE_END])
    if GITIGNORE_BEGIN in text and GITIGNORE_END in text:
        start = text.index(GITIGNORE_BEGIN)
        end = text.index(GITIGNORE_END) + len(GITIGNORE_END)
        new = text[:start] + block + text[end:]
    else:
        sep = "" if not text or text.endswith("\n") else "\n"
        new = text + sep + ("\n" if text else "") + block + "\n"
    out["changed"] = new != text
    if apply and out["changed"]:
        try:
            path.write_text(new, encoding="utf-8")
        except OSError as exc:
            out.update({"ok": False, "error": str(exc)})
    return out


def protocol_block(hub: Hub) -> str:
    return "\n".join([
        PROTOCOL_BEGIN,
        "## Skills: ask the hub first (skillgram)",
        "",
        f"The project's skills live in `{hub.root}` and are mounted here by skillgram.",
        "The hub is harness-agnostic: changing agent does not change the library, and it does",
        "not change the record of what this project used. Read that record before choosing.",
        "",
        "1. Before picking skills, read what this project has used (the previous harness was not",
        "   a different project): `skillgram list --project . --used` or the MCP tool",
        "   `skillgram_list`. Each row says how often and which harness used it.",
        "2. Before proposing or writing a skill, ask: `skillgram missing \"<capability>\"`",
        "   (or the MCP tool `skillgram_missing`).",
        "3. `found: true` -> use the skill. Read `<hub>/<category>/<skill>/SKILL.md` or invoke it by name.",
        "4. After using one, say so: `skillgram touch <skill> --by <your harness>` (or the MCP tool",
        "   `skillgram_touch`). That is the only usage signal skillgram has, and it is what the",
        "   next harness will read.",
        "5. `found: false` -> do not invent one silently. Tell the user the hub has nothing for it,",
        "   then offer `skillgram propose \"<need>\"` (draft) and wait for approval before `--apply`.",
        "6. Never hand-write files under `.agents/skills/`: use `skillgram use <skill>...`.",
        "   Adding a skill to this project is `skillgram use`, not a copy. A fresh clone has",
        "   no links yet: `skillgram mount --apply` creates them (links only, never copies),",
        "   and `skillgram align` says which harnesses are short.",
        "7. Authoring a skill: `skillgram new <name> --category <c>` then `skillgram lint <name>",
        "   --contract`. The contract is `docs/SKILL-CONTRACT.md`; `lint` is the gate.",
        "8. A skill that comes from a URL or another repo is imported with `skillgram import`",
        "   (an export made by skillgram) or `skillgram adopt` (a local folder): never a copy into",
        "   a `skills/` folder.",
        "",
        PROTOCOL_END,
    ])


def inject_protocol(hub: Hub, project: Path, files: list | None = None, apply: bool = False) -> dict:
    """Escribe/actualiza el bloque de protocolo en los ficheros de instrucciones del proyecto."""
    targets = files or [p for p in (project / "AGENTS.md", project / "CLAUDE.md") if p.is_file()]
    if not targets:
        targets = [project / "AGENTS.md"]
    block = protocol_block(hub)
    results = []
    for path in targets:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError as exc:
            results.append({"file": str(path), "error": str(exc)})
            continue
        if PROTOCOL_BEGIN in text and PROTOCOL_END in text:
            start = text.index(PROTOCOL_BEGIN)
            end = text.index(PROTOCOL_END) + len(PROTOCOL_END)
            new = text[:start] + block + text[end:]
            state = "updated"
        else:
            sep = "" if not text else ("\n\n" if not text.endswith("\n") else "\n")
            new = text + sep + block + "\n"
            state = "added"
        changed = new != text
        if apply and changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8")
        results.append({"file": str(path), "state": state, "changed": changed, "applied": apply and changed})
    return {"ok": True, "op": "protocol", "aplicado": apply, "files": results}

# ---------------------------------------------------------------------------
# install / propagate: one idempotent command per machine
# ---------------------------------------------------------------------------
#
# The identity of the agent asking is never guessed. Order (decided, do not
# reorder): SKILLGRAM_HARNESS (the value this command writes into the MCP
# registration) -> --harness -> process tree -> "unknown", and it says so.
#
# Registration is delegated: every harness owns its MCP config, so skillgram
# shells out to `reasonix mcp add` / `codex mcp add` / `claude mcp add` /
# `hermes mcp add` / `gemini mcp add`. It never edits a harness config by hand.
#
# Two traps found by running the real CLIs (keep them in the argv builders):
#   * reasonix `mcp add` (legacy positional form) has no `--` separator and
#     reads every flag after the command as part of the command, so the hub
#     travels in the environment (SKILLGRAM_HUB), not in argv.
#   * reasonix only honours `--env=K=V`; the spaced `--env K=V` is swallowed
#     into the command ("PATHSEP=marker" not found on PATH).

MCP_SERVER = "skillgram"
HARNESS_ENV = "SKILLGRAM_HARNESS"
LEDGER_ENV = "SKILLGRAM_LEDGER"
MCP_CMD_ENV = "SKILLGRAM_MCP_CMD"

# Harness CLIs that ask a Y/n question before writing anything. stdin is closed on
# purpose (a harness must never block on a prompt), so answer the one question we
# know: hermes asks "Enable all N tools?" for the server it just discovered.
INTERACTIVE_ADD = {"hermes"}

HARNESS_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "gemini": "gemini-cli",
    "qwen": "qwen-code",
    "qwen-code": "qwen-code",
    "aider-desk": "aider",
}

# harness -> the CLI that owns its MCP config (delegation only).
HARNESS_MCP_CLI = {
    "reasonix": "reasonix",
    "claude-code": "claude",
    "codex": "codex",
    "hermes": "hermes",
    "gemini-cli": "gemini",
}

# What the user still has to do for the registration to take effect.
HARNESS_EFFECT = {
    "reasonix": "Reasonix: /reload (or restart the app) to connect the skillgram MCP server",
    "codex": "Codex: restart the session (`codex` again); MCP servers are read at startup",
    "claude-code": "Claude Code: restart the CLI (`claude` again) to load the new MCP server",
    "hermes": "Hermes: restart the Hermes session to connect the MCP server",
    "gemini-cli": "Gemini CLI: restart the CLI to load the new MCP server",
    "cursor": "Cursor: Reload Window, then enable the skillgram server in Settings > MCP",
}
GENERIC_EFFECT = "restart the harness session so it connects the skillgram MCP server"

# Ancestor executable stems we trust as a harness identity. `node`/`python` are
# absent on purpose: a generic runtime is never evidence of who is asking.
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


def normalize_harness(value: str) -> str:
    name = str(value or "").strip().lower().replace(" ", "-")
    return HARNESS_ALIASES.get(name, name)


def _which(name: str) -> str | None:
    """shutil.which with a hook the tests can patch."""
    try:
        return shutil.which(name)
    except (OSError, AttributeError):
        return None


def _process_chain_win(limit: int = 6) -> list:
    script = (
        "$cur = {pid}; $names = @();"
        "for ($i = 0; $i -lt {limit}; $i++) {{"
        "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$cur\" -ErrorAction SilentlyContinue;"
        "  if (-not $p) {{ break }};"
        "  $names += $p.Name; $cur = [int]$p.ParentProcessId; if ($cur -le 0) {{ break }}"
        "}};"
        "if ($names.Count -eq 0) {{ '[]' }} else {{ $names | ConvertTo-Json -Compress }}"
    ).format(pid=os.getpid(), limit=limit)
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
    return Path(env) if env else HOME / ".agents" / "skillgram-ledger.json"


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


def shell_cmd(argv: list) -> str:
    return " ".join(f'"{a}"' if (" " in str(a) or "\t" in str(a)) else str(a) for a in argv)


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
    """How a harness should start the MCP server: [python, skillgram.py, mcp]."""
    override = (os.environ.get(MCP_CMD_ENV) or "").strip()
    if override:
        return override.split()
    exe = _which("skillgram")
    if _is_real_exe(exe):
        return [exe, "mcp"]
    return [sys.executable or "python", str(Path(__file__).resolve()), "mcp"]


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
            runner=None, bootstrap_repos: bool = False, depth: int = 2) -> dict:
    """One idempotent command: harness -> hub -> MCP (delegated) -> propagate."""
    resolved = resolve_harness(harness)
    name = resolved["name"]
    done, already, blocked, effect = [], [], [], []
    failed = False

    # 1. hub
    hub_created = not hub.root.is_dir()
    if hub_created and apply:
        hub.root.mkdir(parents=True, exist_ok=True)
    known = hub.skills()
    if hub_created:
        done.append(f"hub {'created' if apply else 'would be created'}: {hub.root}")
    else:
        already.append(f"hub in place: {hub.root} ({len(known)} skills)")
    if not known:
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

# ---------------------------------------------------------------------------
# Demanda: que busca la gente y el hub no tiene (roadmap real)
# ---------------------------------------------------------------------------


def _demand_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS demand ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, query TEXT, "
                 "found INTEGER, best TEXT, hits INTEGER)")


def record_demand(query: str, found: bool, best: str = "", hits: int = 0, path: Path | None = None) -> None:
    """Registra una consulta a `missing`. Best-effort: nunca rompe la consulta."""
    try:
        conn = _index_conn(path)
        try:
            _demand_table(conn)
            conn.execute("INSERT INTO demand (at, query, found, best, hits) VALUES (?,?,?,?,?)",
                         (time.strftime("%Y-%m-%dT%H:%M:%S"), query[:200], 1 if found else 0, best, hits))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - el registro no puede tumbar la consulta
        pass


def demand_report(limit: int = 20, path: Path | None = None) -> dict:
    """Top de consultas sin cobertura: eso es lo que falta escribir."""
    conn = _index_conn(path)
    try:
        _demand_table(conn)
        rows = conn.execute(
            "SELECT query, COUNT(*) AS n, MAX(at) AS last_at FROM demand WHERE found = 0 "
            "GROUP BY lower(query) ORDER BY n DESC, last_at DESC LIMIT ?", (limit,)).fetchall()
        covered = conn.execute("SELECT COUNT(*) FROM demand WHERE found = 1").fetchone()[0]
    finally:
        conn.close()
    return {
        "ok": True,
        "op": "demand",
        "uncovered": [{"query": r[0], "times": r[1], "last_at": r[2]} for r in rows],
        "covered_queries": covered,
        "total_uncovered": sum(r[1] for r in rows),
    }


# ---------------------------------------------------------------------------
# Hub git-backed: el hub es un repo, las otras maquinas lo ven por git
# ---------------------------------------------------------------------------


def _git(hub: Hub, *args) -> tuple:
    import subprocess as _sp

    proc = _sp.run(["git", "-C", str(hub.root), *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def hub_git(hub: Hub, action: str = "status", apply: bool = False) -> dict:
    """status | init | pull | push del repo del hub. Conflictos: los resuelve git."""
    if not hub.root.is_dir():
        return {"ok": False, "op": "git", "error": f"hub not found: {hub.root}"}
    is_repo = (hub.root / ".git").exists()
    if action == "status":
        if not is_repo:
            return {"ok": True, "op": "git", "hub": str(hub.root), "repo": False,
                    "note": "hub is not a git repo; run `skillgram git init`"}
        rc, out, err = _git(hub, "status", "--porcelain", "--branch")
        return {"ok": rc == 0, "op": "git", "hub": str(hub.root), "repo": True,
                "dirty": bool([l for l in out.splitlines() if not l.startswith("##")]),
                "status": out, "error": err or None}
    if action == "init":
        if is_repo:
            return {"ok": True, "op": "git", "repo": True, "note": "already a repo"}
        if not apply:
            return {"ok": True, "op": "git", "repo": False, "would": "git init + .gitignore", "applied": False}
        rc, out, err = _git(hub, "init", "-b", "main")
        ignore = hub.root / ".gitignore"
        if rc == 0 and not ignore.exists():
            ignore.write_text("skillgram-index.db\n*skillgram-bak-*\n", encoding="utf-8")
        return {"ok": rc == 0, "op": "git", "repo": rc == 0, "out": out, "error": err or None}
    if action in ("pull", "push"):
        if not is_repo:
            return {"ok": False, "op": "git", "error": "hub is not a git repo; run `skillgram git init`"}
        if not apply:
            return {"ok": True, "op": "git", "would": f"git {action}", "applied": False}
        args = ("pull", "--ff-only") if action == "pull" else ("push",)
        rc, out, err = _git(hub, *args)
        return {"ok": rc == 0, "op": "git", "action": action, "out": out[-800:], "error": err[-800:] or None}
    return {"ok": False, "op": "git", "error": f"unknown action: {action}"}

# ---------------------------------------------------------------------------
# Diff: dos copias de la misma skill divergieron? (con el hub en git, esto pasa)
# ---------------------------------------------------------------------------


def _file_hashes(skill_dir: Path) -> dict:
    out = {}
    if not skill_dir.is_dir():
        return out
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(skill_dir).as_posix()
        h = hashlib.sha256()
        try:
            h.update(f.read_bytes())
        except OSError:
            continue
        out[rel] = h.hexdigest()[:12]
    return out


def diff_skill(hub: Hub, name: str, against: Path) -> dict:
    """Compara la copia del hub contra otra copia. No escribe nada."""
    tipo = hub.skills().get(name)
    if not tipo:
        return {"ok": False, "op": "diff", "skill": name, "error": "not found in the hub"}
    here = hub.root / tipo / name
    there = Path(against)
    if not (there / "SKILL.md").is_file() and (there / name / "SKILL.md").is_file():
        there = there / name
    if not there.is_dir():
        return {"ok": False, "op": "diff", "skill": name, "error": f"not a directory: {there}"}
    a, b = _file_hashes(here), _file_hashes(there)
    changed = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
    return {
        "ok": True,
        "op": "diff",
        "skill": name,
        "hub": str(here),
        "against": str(there),
        "equal": not changed and a.keys() == b.keys(),
        "changed": changed,
        "only_in_hub": sorted(a.keys() - b.keys()),
        "only_in_against": sorted(b.keys() - a.keys()),
    }

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


def add_common_flags(parser: argparse.ArgumentParser, with_defaults: bool = True) -> None:
    """--hub/--json/--quiet para un parser.

    `with_defaults=False` deja cada opcion en SUPPRESS: el subparser solo escribe
    el valor cuando el flag aparece de verdad. argparse vuelca los defaults del
    subparser sobre el namespace que ya lleno el parser de arriba, asi que un
    default normal aca borraria `skillgram --hub X status`.
    """
    parser.add_argument(
        "--hub",
        default=str(default_hub()) if with_defaults else argparse.SUPPRESS,
        help="hub root (default ~/.agents/skills)",
    )
    parser.add_argument(
        "--json", action="store_true",
        default=False if with_defaults else argparse.SUPPRESS,
        help="one JSON line on stdout",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        default=False if with_defaults else argparse.SUPPRESS,
        help="no logs on stdout",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="skillgram",
        description="Keeps the skill library linked into every agent.",
    )
    # Los flags globales valen en las dos posiciones: `skillgram --hub X status`
    # y `skillgram status --hub X`. El parser de arriba lleva los defaults; las
    # copias de los subparsers van en SUPPRESS para no pisarlos.
    add_common_flags(ap)
    ap.add_argument("--version", action="version", version=f"skillgram {VERSION}",
                    help="print the version and exit")
    common = argparse.ArgumentParser(add_help=False)
    add_common_flags(common, with_defaults=False)

    sub = ap.add_subparsers(dest="cmd", required=True)

    p_onb = sub.add_parser("onboard", parents=[common], help="detect the harnesses and unify their skills into the hub (optional LLM)")
    p_onb.add_argument("--scope", choices=["all", "current"], help="unify all harnesses or just one (asks when missing)")
    p_onb.add_argument("--harness", help="harness name for --scope current")
    p_onb.add_argument("--apply", action="store_true", help="execute the plan (without it: show it only)")
    p_onb.add_argument("--replace-natives", action="store_true", help="replace the native copy with a link (the copy goes to quarantine)")
    p_onb.add_argument("--no-llm", action="store_true", help="do not ask the connected LLM")
    p_onb.add_argument("--plan", help="write the JSON plan to this path")
    sub.add_parser("flavor", parents=[common], help="detect which agent each hub skill is wired to").add_argument(
        "--apply", action="store_true", help="save the flavor into SKILLMETA.json")
    p_port = sub.add_parser("port", parents=[common], help="port skills hard-wired to another harness (agents/<harness>.md)")
    p_port.add_argument("skill", nargs="?", help="one skill (default: scan the whole hub)")
    p_port.add_argument("--to", help="target harness (default: every installed one)")
    p_port.add_argument("--apply", action="store_true", help="write the files (default: plan only)")
    p_port.add_argument("--no-llm", action="store_true")
    p_prec = sub.add_parser("precedence", parents=[common], help="declare the hub first in each harness config")
    p_prec.add_argument("--apply", action="store_true", help="write (with backup); default: show only")
    p_prec.add_argument("--harness", help="only this harness (e.g. hermes, with its own HERMES_HOME)")
    p_adopt = sub.add_parser("adopt", parents=[common], help="adopt native skills: copy into the hub, verify the hash, leave a link")
    p_adopt.add_argument("path", nargs="?", help="the skill folder")
    p_adopt.add_argument("--all", action="store_true", help="every native skill of every harness")
    p_mount = sub.add_parser("mount", parents=[common], help="materialize the project scope in every harness")
    p_mount.add_argument("--project", help="project folder (default: engram-style resolution)")
    p_mount.add_argument("--apply", action="store_true", help="create/remove the links (default: plan only)")
    p_mount.add_argument("--prune-global", action="store_true", help="also remove the home links to the hub (strict)")
    p_mount.add_argument("--no-protocol", action="store_true", help="do not inject the skillgram protocol into AGENTS.md/CLAUDE.md")
    p_mount.add_argument("--bootstrap", dest="bootstrap", action="store_true", default=None,
                         help="if the repo has no scope, bootstrap it with the connected LLM first (default: ask)")
    p_mount.add_argument("--no-bootstrap", dest="bootstrap", action="store_false", help="never bootstrap from mount")
    p_mount.add_argument("--yes", "-y", action="store_true", help="assume yes for the bootstrap confirmation")
    p_install = sub.add_parser("install", parents=[common],
                               help="one idempotent command: harness -> hub -> MCP -> propagate")
    p_install.add_argument("--harness", help="harness identity (default: SKILLGRAM_HARNESS, then the process tree)")
    p_install.add_argument("--root", help="directory whose git repos get the scope (default: the current project only)")
    p_install.add_argument("--dry-run", action="store_true", help="print the exact commands, write nothing")
    p_install.add_argument("--bootstrap", dest="bootstrap_repos", action="store_true",
                           help="repos without a scope: pick a set with the connected LLM (default: skip and report)")
    p_install.add_argument("--depth", type=int, default=2, help="how deep to look for repos under --root (default 2)")
    p_prop = sub.add_parser("propagate", parents=[common],
                            help="mount + protocol + ledger for every repo of a harness under a root")
    p_prop.add_argument("--harness", help="harness identity (default: SKILLGRAM_HARNESS, then the process tree)")
    p_prop.add_argument("--root", help="directory to scan for git repos (default: the current project)")
    p_prop.add_argument("--apply", action="store_true", help="write (default: plan only)")
    p_prop.add_argument("--force", action="store_true", help="ignore the ledger and redo every repo")
    p_prop.add_argument("--bootstrap", dest="bootstrap_repos", action="store_true",
                        help="repos without a scope: pick a set with the connected LLM")
    p_prop.add_argument("--depth", type=int, default=3, help="how deep to look for repos under --root (default 3)")
    p_boot = sub.add_parser("bootstrap", parents=[common], help="repo without scope: signals + LLM picks the initial set (and reports gaps)")
    p_boot.add_argument("--project")
    p_boot.add_argument("--apply", action="store_true", help="record the chosen set in the project")
    p_boot.add_argument("--no-llm", action="store_true")
    p_miss = sub.add_parser("missing", parents=[common], help="deterministic query: is there a skill for this?")
    p_miss.add_argument("text", nargs="+")
    p_miss.add_argument("--project")
    p_seed = sub.add_parser("seed", parents=[common], help="skills that are always recommended (or never)")
    p_seed.add_argument("skills", nargs="*", help="skill names (none: show the list)")
    p_seed.add_argument("--never", action="store_true", help="never recommend them instead")
    p_seed.add_argument("--remove", action="store_true", help="drop them from the list")
    p_prop = sub.add_parser("propose", parents=[common], help="draft a new skill with the LLM and (with --apply) publish it")
    p_prop.add_argument("need", nargs="+")
    p_prop.add_argument("--project")
    p_prop.add_argument("--apply", action="store_true", help="publish into the hub, record it and mount it")
    p_prop.add_argument("--no-llm", action="store_true")
    sub.add_parser("index", parents=[common], help="(re)build the hub FTS5 index")
    p_srch = sub.add_parser("search", parents=[common], help="full-text search in the hub")
    p_srch.add_argument("text", nargs="+")
    p_srch.add_argument("--limit", type=int, default=10)
    p_rev = sub.add_parser("review", parents=[common], help="skills never used or not used recently")
    p_rev.add_argument("--project")
    p_rev.add_argument("--days", type=int, default=90)
    p_rev.add_argument("--fail-if-stale", action="store_true", help="exit 1 when something is stale (CI gate)")
    p_pin = sub.add_parser("pin", parents=[common], help="pin project skills (they never go stale)")
    p_pin.add_argument("skills", nargs="+")
    p_pin.add_argument("--project")
    p_pin.add_argument("--unpin", action="store_true")
    p_ret = sub.add_parser("retire", parents=[common], help="retire a hub skill into _retired (never deletes)")
    p_ret.add_argument("skill")
    p_ret.add_argument("--reason", default="")
    p_exp = sub.add_parser("export", parents=[common], help="export the hub into a tar.gz with hashes")
    p_exp.add_argument("file")
    p_imp = sub.add_parser("import", parents=[common], help="import a tar.gz exported by skillgram")
    p_imp.add_argument("file")
    p_imp.add_argument("--apply", action="store_true")
    p_lint = sub.add_parser("lint", parents=[common], help="quality gate: frontmatter, name, description, body size")
    p_lint.add_argument("skill", nargs="?", help="one skill (default: the whole hub)")
    p_lint.add_argument("--strict", action="store_true", help="warnings also fail")
    p_lint.add_argument("--contract", action="store_true",
                        help="require the five sections of docs/SKILL-CONTRACT.md")
    p_new = sub.add_parser("new", parents=[common], help="scaffold a skill from the contract (does not publish)")
    p_new.add_argument("name", help="kebab-case name; it becomes the folder")
    p_new.add_argument("--category", help="existing hub category (default: imported)")
    p_new.add_argument("--by", default="human", help="who authors it (default: human)")
    p_list = sub.add_parser("list", parents=[common], help="what is in the hub (and what was used in this project)")
    p_list.add_argument("--category", help="only this category")
    p_list.add_argument("--project", help="read usage from this project scope")
    p_list.add_argument("--used", choices=["used", "unused"], help="only used / never used in that project")
    p_list.add_argument("--limit", type=int, help="show at most N")
    p_touch = sub.add_parser("touch", parents=[common], help="declare that you used these skills (the only usage signal)")
    p_touch.add_argument("skills", nargs="+")
    p_touch.add_argument("--project", help="project scope (default: engram-style resolution)")
    p_touch.add_argument("--by", default="agent", help="who used it (harness or agent, default: agent)")
    p_dem = sub.add_parser("demand", parents=[common], help="queries the hub could not cover (real roadmap)")
    p_dem.add_argument("--limit", type=int, default=20)
    p_git = sub.add_parser("git", parents=[common], help="hub as a git repo: status | init | pull | push")
    p_git.add_argument("action", choices=["status", "init", "pull", "push"], nargs="?", default="status")
    p_git.add_argument("--apply", action="store_true")
    p_diff = sub.add_parser("diff", parents=[common], help="compare a hub skill against another copy (divergence)")
    p_diff.add_argument("skill")
    p_diff.add_argument("--against", required=True, help="path to the other copy")
    p_mcp = sub.add_parser("mcp", parents=[common], help="MCP server (stdio) so the agent can query the hub")
    p_mcp.add_argument("--read-only", action="store_true", help="the agent can query but not write")
    sub.add_parser("doctor", parents=[common], help="inventory: harnesses, native, collisions, drift")
    p_status = sub.add_parser("status", parents=[common], help="current drift, touching nothing")
    p_status.add_argument("--verify", action="store_true", help="also verify where each link points (slow)")
    p_sync = sub.add_parser("sync", parents=[common], help="apply the desired state (idempotent)")
    p_sync.add_argument("--dry-run", action="store_true")
    p_watch = sub.add_parser("watch", parents=[common], help="watch the hub and re-sync on change")
    p_watch.add_argument("--interval", type=float, default=5.0)
    p_daemon = sub.add_parser("daemon", parents=[common], help="JSON-line server for edgePass")
    p_daemon.add_argument("--interval", type=float, default=5.0)
    p_daemon.add_argument("--watch", action="store_true", help="start watching immediately")
    p_auto = sub.add_parser("install-autostart", parents=[common], help="scheduled task at logon")
    p_auto.add_argument("--interval", type=float, default=10.0)
    sub.add_parser("uninstall-autostart", parents=[common], help="remove the scheduled task")

    p_init = sub.add_parser("init", parents=[common], help="create the project's .agents/config.json")
    p_init.add_argument("project", nargs="?", help="project name (default: the cwd basename)")
    p_init.add_argument("--path", help="project folder (default: cwd)")
    p_init.add_argument("--force", action="store_true", help="replace an existing config")

    p_use = sub.add_parser("use", parents=[common], help="promote hub skills into the project")
    p_use.add_argument("skills", nargs="+")
    p_use.add_argument("--project", help="project folder (default: engram-style resolution)")
    p_use.add_argument("--by", help="who is using them (default: the harness that invoked skillgram)")

    p_unuse = sub.add_parser("unuse", parents=[common], help="drop skills from the project scope")
    p_unuse.add_argument("skills", nargs="+")
    p_unuse.add_argument("--project")

    p_pstat = sub.add_parser("project-status", parents=[common], help="health of the project scope")
    p_pstat.add_argument("--project")

    p_align = sub.add_parser("align", parents=[common],
                             help="what each harness actually sees of this project, and what it lacks")
    p_align.add_argument("--project")
    p_align.add_argument("--apply", action="store_true", help="link the missing ones (never removes, never touches natives)")

    p_policy = sub.add_parser("policy", parents=[common],
                              help="the gate: show the effective policy, or check a project against it")
    p_policy.add_argument("action", nargs="?", choices=["show", "check"], default="show")
    p_policy.add_argument("--project")
    p_policy.add_argument("--policy", help="organization policy: a path, or url#ref of a git repo")
    return ap


def _mount_bootstrap(hub: Hub, root: Path, args, result: dict) -> dict | None:
    """Repo sin scope: ofrece bootstrapear y, si el set se aplico, re-monta.

    Devuelve el resultado nuevo, o None para quedarse con el original (solo se
    anoto la pista o no se aplico nada). La confirmacion es interactiva salvo
    --bootstrap/--yes, y sin LLM solo se explica el paso siguiente.
    """
    cfg = llm_config()
    if not cfg:
        result["bootstrap_hint"] = "no LLM configured: run `skillgram use <skill>...` in this repo"
        return None
    if not args.apply:
        return None
    want = args.bootstrap is True or bool(args.yes)
    if not want and sys.stdin.isatty() and not args.json:
        ans = ""
        try:
            ans = input(f"no skills in scope for {root.name}; bootstrap it now? [y/N]: ")
        except (EOFError, OSError):
            ans = ""
        want = ans.strip().lower().startswith("y")
    if not want:
        return None
    result["bootstrap"] = bootstrap(hub, root, cfg, apply=True)
    if not result["bootstrap"].get("applied"):
        return None
    again = mount_project(hub, root, apply=True, prune_global=args.prune_global)
    again["bootstrap"] = "done"
    return again


def _onboard(hub: Hub, args) -> dict | int:
    """onboard: sin --scope pregunta, sin --apply solo devuelve el plan.

    Un int es una salida temprana con su codigo (invocacion mala), igual que en
    `dispatch`.
    """
    scope = args.scope
    if not scope:
        ans = ""
        try:
            if sys.stdin.isatty() and not args.json:
                ans = input("Unify ALL harnesses or only the current one? [all/current] (all): ")
        except (EOFError, OSError):
            ans = ""
        scope = "current" if ans.strip().lower().startswith("c") else "all"
    harness = args.harness or os.environ.get("SKILLGRAM_HARNESS")
    if scope == "current" and not harness:
        print(json.dumps({"ok": False, "error": "scope=current requires --harness or SKILLGRAM_HARNESS"}), flush=True)
        return 2
    plan = onboard(hub, scope=scope, harness=harness, use_llm=not args.no_llm)
    if args.plan:
        Path(args.plan).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not args.apply:
        return plan
    applied = apply_onboard(hub, plan)
    replaced = {"quarantine": [], "links": [], "failures": []}
    if args.replace_natives and applied["imported"]:
        replaced = replace_native_with_link(
            hub, applied["imported"], HOME / ".agents" / "skillgram-quarantine"
        )
    linked = sync(hub, quiet=True)
    return {
        **plan,
        "applied": applied,
        "reemplazo": replaced,
        "sync": linked,
        "ok": not applied["failed"] and not replaced["failures"],
    }


def dispatch(args, hub: Hub) -> dict | int:
    """Corre un comando y devuelve su resultado.

    Un int significa que el comando ya hizo su propia I/O (mcp, watch, daemon) y
    `main` no imprime nada mas. Hay UNA sola cadena de decision: el texto humano
    vive en `human_output` y solo lee el dict que sale de aca, asi que un comando
    no puede ejecutarse dos veces.
    """
    cmd = args.cmd

    if cmd == "status":
        return status(hub, deep=args.verify)

    if cmd == "flavor":
        return flavor_scan(hub, apply=args.apply)

    if cmd == "port":
        cfg = None if args.no_llm else llm_config()
        if not args.skill:
            targets = [args.to] if args.to else [h["app"] for h in detect_harnesses() if h["skills_dir"]]
            return port_missing(hub, targets, cfg, apply=args.apply)
        if not args.to:
            return {"ok": False, "op": "port", "error": "--to <harness> is required for a single skill"}
        return port_skill(hub, args.skill, args.to, cfg)

    if cmd == "precedence":
        return apply_precedence(hub, apply=args.apply,
                                only=normalize_harness(args.harness) if args.harness else None)

    if cmd == "list":
        proj = resolve_project(args.project) if args.project else None
        return list_skills(hub, category=args.category,
                           project=Path(proj["path"]) if proj else None,
                           used=args.used, limit=args.limit)

    if cmd == "touch":
        proj = resolve_project(args.project)
        result = touch_usage(Path(proj["path"]), args.skills, by=args.by)
        result["project"] = str(proj["path"])
        return result

    if cmd == "new":
        return new_skill(hub, args.name, category=args.category, author=args.by)

    if cmd == "lint":
        return lint_hub(hub, only=args.skill, strict=args.strict, contract=args.contract)

    if cmd == "diff":
        return diff_skill(hub, args.skill, Path(args.against))

    if cmd == "demand":
        return demand_report(limit=args.limit)

    if cmd == "git":
        return hub_git(hub, action=args.action, apply=args.apply)

    if cmd == "index":
        return index_build(hub)

    if cmd == "search":
        return index_search(" ".join(args.text), limit=args.limit)

    if cmd == "review":
        proj = resolve_project(args.project)
        return review_list(hub, Path(proj["path"]), days=args.days, fail_if_stale=args.fail_if_stale)

    if cmd == "pin":
        proj = resolve_project(args.project)
        return pin_skill(Path(proj["path"]), args.skills, pinned=not args.unpin)

    if cmd == "retire":
        result = retire_skill(hub, args.skill, reason=args.reason)
        if result.get("ok"):
            result["sync"] = sync(hub, quiet=True)
        return result

    if cmd == "export":
        return export_hub(hub, Path(args.file))

    if cmd == "import":
        return import_hub(hub, Path(args.file), apply=args.apply)

    if cmd == "mcp":
        return mcp_serve(hub, read_only=args.read_only)

    if cmd == "bootstrap":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = bootstrap(hub, Path(proj["path"]), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "missing":
        proj = resolve_project(args.project)
        result = missing(hub, " ".join(args.text))
        result["project"] = str(proj["path"])
        return result

    if cmd == "seed":
        return seed_skills(hub, args.skills, mode="never" if args.never else "always",
                           remove=args.remove)

    if cmd == "propose":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = propose_skill(hub, Path(proj["path"]), " ".join(args.need), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "mount":
        proj = resolve_project(args.project)
        root = Path(proj["path"])
        result = mount_project(hub, root, apply=args.apply, prune_global=args.prune_global)
        if not result.get("ok") and args.bootstrap is not False:
            booted = _mount_bootstrap(hub, root, args, result)
            if booted is not None:
                result = booted
        if result.get("ok") and args.apply and not args.no_protocol:
            result["protocol"] = inject_protocol(hub, root, apply=True)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "adopt":
        if args.all:
            return adopt_natives(hub)
        if args.path:
            return adopt_skill(hub, Path(args.path))
        return {"ok": False, "op": "adopt", "error": "pass a folder or --all"}

    if cmd == "install":
        return install(hub, harness=args.harness, root=args.root, apply=not args.dry_run,
                       bootstrap_repos=args.bootstrap_repos, depth=args.depth)

    if cmd == "propagate":
        return propagate(hub, harness=args.harness, root=args.root, apply=args.apply,
                         depth=args.depth, force=args.force, bootstrap_repos=args.bootstrap_repos)

    if cmd == "doctor":
        return doctor(hub)

    if cmd == "onboard":
        return _onboard(hub, args)

    if cmd == "sync":
        return sync(hub, quiet=args.quiet, dry_run=args.dry_run)

    if cmd == "watch":
        if not hub.root.is_dir():
            print(json.dumps({"ok": False, "error": f"hub not found {hub.root}"}), flush=True)
            return 2
        if not single_instance():
            print(
                json.dumps({"ok": False, "error": "a watcher is already running (not starting another)"}),
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

    if cmd == "daemon":
        return daemon(hub, interval=args.interval, watch_at_start=args.watch)

    if cmd == "install-autostart":
        return install_autostart(hub, args.interval)

    if cmd == "uninstall-autostart":
        return uninstall_autostart()

    if cmd == "init":
        return init_project(args.project, args.path, force=args.force)

    if cmd == "use":
        proj = resolve_project(args.project)
        result = promote(hub, args.skills, Path(proj["path"]))
        # promoting a skill *is* using it: record who did it, so the next harness
        # reads a name instead of "agent"
        by = args.by or caller_identity()
        result["uso"] = touch_usage(Path(proj["path"]), args.skills, by=by).get("touched", [])
        result["by"] = by
        result["resolucion"] = proj["source"]
        return result

    if cmd == "unuse":
        proj = resolve_project(args.project)
        result = unpromote(Path(proj["path"]), args.skills)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "project-status":
        proj = resolve_project(args.project)
        result = project_status(hub, Path(proj["path"]))
        result["resolucion"] = proj["source"]
        return result

    if cmd == "align":
        proj = resolve_project(args.project)
        result = align(hub, Path(proj["path"]), apply=args.apply)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "policy":
        proj = resolve_project(args.project) if (args.project or args.action == "check") else None
        root = Path(proj["path"]) if proj else None
        if args.action == "check":
            result = policy_check(hub, root or Path.cwd(), org=args.policy)
        else:
            result = policy_show(root, org=args.policy)
        if proj:
            result["resolucion"] = proj["source"]
        return result

    return {"ok": False, "op": cmd, "error": f"unknown command: {cmd}"}  # argparse ya valida


def human_output(args, result: dict) -> str | None:
    """Reporte humano de los comandos que tienen uno.

    None = comando de maquina: una sola linea JSON (el contrato de AGENTS.md),
    nunca texto a medias. Todo lo que se lee aca tiene que existir en el dict que
    devuelve `dispatch`: es el punto donde un rename de clave rompe el CLI en
    silencio.
    """
    cmd = args.cmd

    if cmd == "doctor":
        installed = [h["app"] for h in result["harnesses"] if h["instalado"]]
        with_skills = [h["app"] for h in result["harnesses"] if h["skills_dir"]]
        return (
            f"hub: {result['hub']}\n"
            f"skills: {result['skills_hub']} in {result['categories']} categories\n"
            f"harnesses installed ({len(installed)}): {', '.join(installed) or '-'}\n"
            f"with a skills/ folder ({len(with_skills)}): {', '.join(with_skills) or '-'}\n"
            "harness                       links  dead  missing  native  collisions\n"
            + "\n".join(
                f"{r['app'][:28]:28}  {r['hub_links']:>7}  {r['dead']:>7}  "
                f"{r['missing']:>6}  {r['native']:>7}  {len(r['collisions']):>10}"
                for r in result["agents"]
            )
            + "\n"
            + ("\n".join(f"- {r}" for r in result["recommendations"]) if result["recommendations"] else "")
        )

    if cmd == "onboard":
        return (
            f"scope: {result['scope']}"
            + (f" ({result['harness']})" if result.get("harness") else " (all harnesses)")
            + f"  | hub: {result['hub']}\n"
            + "harnesses with native skills:\n"
            + ("\n".join(f"  {h['app']:16} {h['native']:>4} native" for h in result["harnesses"] if h["native"])
               or "  (none)")
            + f"\nplan: {result['summary']['import']} import | {result['summary']['dedupe']} duplicate | "
            f"{result['summary']['conflict']} conflict"
            + (f"\nllm: {result['llm']['model']} ({result['llm']['aplicadas']} skills classified)"
               if result.get("llm") else "\nllm: not used")
            + (f"\nllm_error: {result['llm_error']}" if result.get("llm_error") else "")
            + "\n"
            + "\n".join(
                f"  {a['action']:8} {a['harness']}/{a['skill']} -> {a['category']}"
                + (f" [{a.get('flavor')}]" if a.get("flavor") else "")
                + (f"  ({a['reason']})" if a.get("reason") else "")
                for a in result["actions"][:40]
            )
            + (f"\n  ... and {len(result['actions']) - 40} more" if len(result["actions"]) > 40 else "")
            + ("\n\napplied: " + str(len(result["applied"]["imported"])) + " imported, "
               + str(len(result["applied"]["skipped"])) + " skipped, "
               + str(len(result["applied"]["failed"])) + " failed"
               if result.get("applied") else "\n(plan only: run with --apply to execute it)")
        )

    if cmd == "status":
        return (
            f"hub: {result['hub']}\n"
            f"skills: {result['skills']} in {result['categories']} categories "
            f"({result['nested']} nested), agents: {result['agents']}\n"
            f"drift: {result['drift']} (missing {result['missing']}, mispointed {result['mispointed']}, "
            f"dead {result['dead']}, skipped {result['skipped']})"
        )

    if cmd == "install":
        return install_report(result)

    if cmd == "propagate":
        return propagate_report(result)

    if cmd == "init":
        if result.get("ok"):
            return f"project: {result['project_name']}  ({result['config']})"
        return result.get("error", "could not initialize")

    if cmd == "use":
        return (
            f"project: {result['project_name']} ({result['project']})\n"
            f"promoted: {len(result['created'])} new, {len(result['already_present'])} already present\n"
            + (f"unknown in hub: {', '.join(result['unknown'])}\n" if result["unknown"] else "")
            + (f"occupied without link: {', '.join(result['ocupadas_sin_enlace'])}\n"
               if result["ocupadas_sin_enlace"] else "")
            + f"manifest: {result['manifest']}"
        )

    if cmd == "unuse":
        lines = [
            f"project: {result['project']}",
            f"removed: {len(result['removed'])}",
        ]
        if result["absent"]:
            lines.append(f"not in scope: {', '.join(result['absent'])}")
        return "\n".join(lines)

    if cmd == "seed":
        lines = []
        for label, key in (("always", "always"), ("never", "never")):
            if result.get(key):
                lines.append(f"{label}: {', '.join(result[key])}")
        if not lines:
            lines.append("no seeds yet: `skillgram seed <skill>...` marks one as always recommended")
        if result.get("changed"):
            verb = "removed from" if result.get("removed") else "seeded in"
            lines.append(f"{verb} {result['mode']}: {', '.join(result['changed'])}")
        if result.get("unknown"):
            lines.append(f"not in the hub: {', '.join(result['unknown'])}")
        lines.append(f"file: {result['file']}")
        return "\n".join(lines)

    if cmd == "policy" and result.get("action") == "show":
        lines = [f"organization: {result['org'] or '(none: set SKILLGRAM_POLICY or --policy)'}"]
        if not result["levels"]:
            lines.append("no policy files found: everything is allowed (mode open by default)")
        for level in result["levels"]:
            lines.append(f"{level['level']:<8} {level['mode']:<7} {level['hash']}  {level['path']}")
            for key in ("allow", "deny", "require"):
                if level[key]:
                    lines.append(f"         {key}: {', '.join(level[key])}")
            if level["harnesses_reserved"]:
                lines.append(f"         harnesses (reserved, not enforced in 1.7): "
                             f"{', '.join(level['harnesses_reserved'])}")
        if result["require"]:
            lines.append(f"required anywhere: {', '.join(result['require'])}")
        for error in result["errors"]:
            lines.append(f"error: {error}")
        return "\n".join(lines)

    if cmd == "policy" and result.get("action") == "check":
        lines = [
            f"project: {result['project']}  scope: {result['scope']}",
            "levels: " + (", ".join(f"{lv['level']}@{lv['hash']}" for lv in result["levels"]) or "(none)"),
        ]
        if result["violations"]:
            lines.append(f"violations: {len(result['violations'])}")
            for item in result["violations"]:
                lines.append(f"  {item['skill']}  ({item['category']})  <- {item['rule']}")
        else:
            lines.append("violations: none")
        if result["required_missing"]:
            lines.append(f"required but not in scope: {', '.join(result['required_missing'])}")
        for error in result["errors"]:
            lines.append(f"error: {error}")
        return "\n".join(lines)

    if cmd == "align":
        lines = [
            f"project: {result['project_name']} ({result['project']})  "
            f"skills in scope: {result['skills']}",
            f"{'harness':<16}{'mode':<13}{'project':>8}{'home':>7}{'missing':>9}{'extra':>7}{'native':>8}",
        ]
        for row in result["rows"]:
            lines.append(
                f"{row['harness'][:15]:<16}{row['mode']:<13}{row['via_project']:>8}{row['via_home']:>7}"
                f"{len(row['missing']):>9}{row['extra_count']:>7}{len(row['native']):>8}"
            )
        for row in result["rows"]:
            if row["missing"]:
                lines.append(f"{row['harness']}: missing {', '.join(row['missing'][:ALIGN_LIST_LIMIT])}"
                             + (f" (+{len(row['missing']) - ALIGN_LIST_LIMIT} more)"
                                if len(row["missing"]) > ALIGN_LIST_LIMIT else "")
                             + f" -- fix with `{row['fix']}`")
            if row["native"]:
                lines.append(f"{row['harness']}: native (not touched) {', '.join(row['native'][:ALIGN_LIST_LIMIT])}")
            if row["extra_count"]:
                if row["extra_count"] <= ALIGN_LIST_LIMIT:
                    lines.append(f"{row['harness']}: {row['extra_count']} extra (reported, never removed): "
                                 f"{', '.join(row['extra'])}")
                else:
                    lines.append(f"{row['harness']}: {row['extra_count']} extra -- the whole hub is linked here; "
                                 "`mount --prune-global` makes the project set the only source")
        if result["applied"]:
            lines.append(f"linked in: {', '.join(result['applied'])}")
        if result["unfixable"]:
            lines.append(f"still missing (no project dir to link into): {', '.join(result['unfixable'])}"
                         " -- `skillgram sync` exposes the whole hub instead")
        return "\n".join(lines)

    if cmd == "project-status":
        lines = [
            f"project: {result['project_name']} ({result['project']})  resolution: {result['resolucion']}",
            f"promoted: {result['promoted']} | healthy {len(result['healthy'])} | "
            f"stale {len(result['stale'])} | broken {len(result['broken'])} | "
            f"missing link {len(result['missing_links'])}",
        ]
        for label, items in (
            ("stale", result["stale"]),
            ("broken", result["broken"]),
            ("missing links", result["missing_links"]),
            ("unregistered links", result["links_sin_registrar"]),
            ("local duplicates (in the hub and also copied)", result["local_duplicates"]),
            ("local only", result["local_only"]),
        ):
            if items:
                lines.append(f"{label}: {', '.join(items)}")
        if not result["tiene_config"]:
            lines.append("no .agents/config.json (run `init` to pin the name)")
        if result.get("seeds_missing"):
            lines.append(f"seeds not in this project (recommended): {', '.join(result['seeds_missing'])}"
                         " -- `skillgram use <skill>` to add them")
        return "\n".join(lines)

    return None


def render(args, result: dict) -> None:
    """Un comando de maquina imprime una linea JSON; los humanos, su reporte.

    `--json` gana siempre: es la unica forma de que un humano (o un test) vea el
    dict crudo sin cambiar el flujo.
    """
    text = None if args.json else human_output(args, result)
    print(text if text is not None else json.dumps(result, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = dispatch(args, Hub(Path(args.hub)))
    if isinstance(result, int):
        return result
    render(args, result)
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())

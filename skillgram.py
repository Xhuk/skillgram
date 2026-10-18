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

VERSION = "1.2.1"

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


def plan(hub: Hub, agents: list[Path] | None = None, deep: bool = False) -> Plan:
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

    for agent in agents:
        is_hub = agent.resolve() == hub_root
        counts = {"categories": 0, "flat": 0, "repointed": 0, "dead": 0, "skipped": 0}
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

        # 2) links nuestros que ya no aplican (skill borrada, renombrada o manzana removed)
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
        "ok": not unknown and not occupied,
        "op": "use",
        "project": str(project),
        "project_name": manifest["project_name"],
        "created": created,
        "already_present": already,
        "unknown": unknown,
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
        "promoted": len(registered),
        "healthy": ok,
        "stale": stale,
        "broken": broken,
        "missing_links": unlinked,
        "links_sin_registrar": extra,
        "local_duplicates": duplicates,
        "local_only": local_only,
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
        recos.append(f"{nat} skills native de agentes: quedan como estan, skillgram no las toca")
    col = sum(len(r["collisions"]) for r in rows)
    if col:
        recos.append(f"{col} name collisions: `sync` skips them (never overwrites)")
    if not known:
        recos.append(f"el hub {hub.root} esta vacio: agrega <tipo>/<skill>/SKILL.md")

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
                action["motivo"] = f"identica a {seen[nat['hash']]}"
            elif nat["in_hub"]:
                action["action"] = "conflict"
                action["motivo"] = "a skill with that name already exists in the hub"
            seen.setdefault(nat["hash"], f"{row['app']}/{nat['name']}")
            actions.append(action)
    return {
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
            "current_category": a["categoria"],
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
        plan["llm_error"] = "la LLM no devolvio JSON"
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
            action["categoria"] = cat
        flavor = str(item.get("flavor", "")).strip().lower()
        if flavor:
            action["flavor"] = flavor
        dup = str(item.get("duplicate_of", "")).strip()
        if dup and dup != action["skill"] and action["action"] == "import":
            action["action"] = "dedupe"
            action["motivo"] = f"la LLM dice que es lo mismo que {dup}"
        if item.get("reason"):
            action["motivo"] = action["motivo"] or str(item["reason"])[:200]
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
        dest = hub.root / action["categoria"] / action["skill"]
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
            failed.append(f"{action['skill']}: hash distinto tras copiar")
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
        return {"ok": True, "op": "port", "skill": name, "skipped": "la skill ya es generica"}
    if not cfg:
        return {"ok": False, "op": "port", "skill": name, "error": "no hay LLM conectada configurada"}
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
        return {"ok": True, "op": "port", "skill": name, "skipped": "el modelo dice que no hace falta"}
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
        return {"ok": False, "op": "adopt", "path": str(src), "error": "hash distinto tras copiar"}
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
    return {
        "ok": True,
        "op": "mount",
        "project": str(project),
        "project_name": scope["project_name"],
        "skills": len(wanted),
        "applied": apply,
        "harnesses": rows,
        "global": pruned,
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


def repo_signals(project: Path) -> dict:
    """Senales deterministas del repo (sin LLM)."""
    files = []
    for tag, rel in SIGNAL_FILES.items():
        if (project / rel).exists():
            files.append(tag)
    for alt in ("compose.yml", "compose.yaml", "docker-compose.yaml"):
        if (project / alt).exists() and "compose" not in files:
            files.append("compose")
    if (project / ".github" / "workflows").is_dir():
        files.append("ci")
    if (project / ".env.example").exists() or (project / ".env.sample").exists():
        files.append("env")
    deps = []
    pkg = project / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = sorted(set(list(data.get("dependencies", {}) or {}) +
                               list(data.get("devDependencies", {}) or {})))[:40]
        except (OSError, ValueError):
            pass
    tags = set(files)
    for dep in deps:
        tags.add(dep.split("/")[0].lstrip("@").lower())
    return {"files": sorted(set(files)), "deps": deps, "tags": sorted(t for t in tags if len(t) > 2)}


def candidate_skills(hub: Hub, signals: dict, limit: int = 60) -> list:
    """Filtro determinista del catalogo: 900 -> ~60 candidates."""
    toks = signals["tags"]
    rows = []
    for name, tipo in hub.skills().items():
        try:
            head = (hub.root / tipo / name / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            head = ""
        hay = f"{name} {tipo} {head}".lower()
        score = sum(1 for t in toks if t in hay)
        if score:
            rows.append({"skill": name, "tipo": tipo, "score": score, "summary": head[:280].replace("\n", " ")})
    rows.sort(key=lambda r: (-r["score"], r["skill"]))
    return rows[:limit]


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
        raise OSError("la LLM no devolvio JSON")
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
        return {"ok": True, "op": "bootstrap", "skipped": "el proyecto ya tiene scope",
                "project": str(project), "skills": len(scope["skills"])}
    signals = repo_signals(project)
    candidates = candidate_skills(hub, signals)
    result = {
        "ok": True,
        "op": "bootstrap",
        "project": str(project),
        "signals": signals,
        "candidates": len(candidates),
        "selection": [],
        "gaps": [],
        "applied": False,
        "llm": cfg["model"] if cfg else None,
    }
    if not cfg:
        result["note"] = "no LLM configured: candidates only (no set is chosen)"
        result["top_candidates"] = [c["skill"] for c in candidates[:12]]
        return result
    selection = llm_select(cfg, project, signals, candidates)
    llm_log("bootstrap-select", {"project": str(project), "selected": [s["skill"] for s in selection["selected"]],
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
    """Consulta determinista: hay skill para esto? (sin LLM)."""
    toks = [t for t in re_findall_words(text) if len(t) > 2]
    rows = []
    for name, tipo in hub.skills().items():
        try:
            head = (hub.root / tipo / name / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            head = ""
        hay = f"{name} {tipo} {head}".lower()
        score = sum(1 for t in toks if t in hay)
        if score:
            rows.append({"skill": name, "tipo": tipo, "score": score})
    rows.sort(key=lambda r: (-r["score"], r["skill"]))
    found = bool(rows and rows[0]["score"] >= 2)
    best = rows[0]["skill"] if rows else ""
    record_demand(text, found, best=best, hits=len(rows))
    return {
        "ok": True,
        "op": "missing",
        "query": text,
        "found": found,
        "candidates": rows[:limit],
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
        return {"ok": False, "op": "propose", "error": "no hay LLM conectada configurada"}
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
    gate = lint_skill(dest)
    desc_long = any("description is" in w for w in gate["warnings"])
    if gate["errors"] or desc_long:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "op": "propose", "skill": name, "error": "draft failed lint: " + "; ".join(gate["errors"]),
                "lint": gate}
    write_skill_meta(dest, {"flavor": GENERIC_FLAVOR, "by": f"llm:{cfg['model']}",
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
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
    touched = []
    for name in names:
        entry = manifest["skills"].get(name)
        if not entry:
            continue
        entry["last_used"] = stamp
        entry["use_count"] = int(entry.get("use_count", 0)) + 1
        entry["last_used_by"] = by
        touched.append(name)
    _write_manifest(project, manifest)
    return {"ok": True, "op": "use", "touched": touched}


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
                failed.append(f"{name}: hash distinto")
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
     "description": "Promote hub skills into the current project scope (idempotent).",
     "inputSchema": {"type": "object", "properties": {"skills": {"type": "array", "items": {"type": "string"}},
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
]


def mcp_call(hub: Hub, name: str, args: dict) -> dict:
    """Despacho de una tool MCP. Devuelve el resultado como dict."""
    if name == "skillgram_search":
        return index_search(str(args.get("query", "")), int(args.get("limit", 10) or 10))
    if name == "skillgram_missing":
        return missing(hub, str(args.get("query", "")))
    if name == "skillgram_use":
        proj = resolve_project(args.get("project"))
        return promote(hub, [str(s) for s in args.get("skills", [])], Path(proj["path"]))
    if name == "skillgram_status":
        proj = resolve_project(args.get("project"))
        return {"hub": status(hub), "project": project_status(hub, Path(proj["path"]))}
    if name == "skillgram_propose":
        proj = resolve_project(args.get("project"))
        return propose_skill(hub, Path(proj["path"]), str(args.get("need", "")),
                             llm_config(), apply=bool(args.get("apply")))
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


def _frontmatter(text: str) -> dict:
    """Parseo minimo del frontmatter YAML (clave: valor de una linea)."""
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


def lint_skill(skill_dir: Path) -> dict:
    """Errores duros y avisos de una skill."""
    errors, warnings = [], []
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return {"skill": skill_dir.name, "errors": ["missing SKILL.md"], "warnings": []}
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = _frontmatter(text)
    if not fm:
        errors.append("missing frontmatter")
    name = fm.get("name", "")
    if not name:
        errors.append("frontmatter has no `name`")
    else:
        if name != skill_dir.name:
            warnings.append(f"name `{name}` != folder `{skill_dir.name}`")
        if name != name.lower() or " " in name or "_" in name:
            errors.append("name must be kebab-case")
    desc = fm.get("description", "")
    if not desc:
        errors.append("frontmatter has no `description`")
    elif len(desc) > MAX_DESCRIPTION:
        warnings.append(f"description is {len(desc)} chars (max {MAX_DESCRIPTION})")
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
    return {"skill": skill_dir.name, "errors": errors, "warnings": warnings}


def lint_hub(hub: Hub, only: str | None = None, strict: bool = False) -> dict:
    """Lint de una skill o de todo el hub."""
    names = [only] if only else sorted(hub.skills())
    known = hub.skills()
    rows = []
    for name in names:
        tipo = known.get(name)
        if not tipo:
            rows.append({"skill": name, "errors": ["not found in the hub"], "warnings": []})
            continue
        rows.append(lint_skill(hub.root / tipo / name))
    with_errors = [r for r in rows if r["errors"]]
    with_warnings = [r for r in rows if r["warnings"]]
    failing = bool(with_errors) or (strict and bool(with_warnings))
    return {
        "ok": not failing,
        "op": "lint",
        "strict": strict,
        "skills": len(rows),
        "errors": with_errors,
        "warnings": with_warnings,
        "detail": rows if only else [],
    }


# ---------------------------------------------------------------------------
# Protocolo: que el agente sepa que el hub existe (se inyecta al montar)
# ---------------------------------------------------------------------------

PROTOCOL_BEGIN = "<!-- skillgram:protocol:begin -->"
PROTOCOL_END = "<!-- skillgram:protocol:end -->"


def protocol_block(hub: Hub) -> str:
    return "\n".join([
        PROTOCOL_BEGIN,
        "## Skills: ask the hub first (skillgram)",
        "",
        f"The project's skills live in `{hub.root}` and are mounted here by skillgram.",
        "",
        "1. Before proposing or writing a skill, ask: `skillgram missing \"<capability>\"`",
        "   (or the MCP tool `skillgram_missing`).",
        "2. `found: true` -> use the skill. Read `<hub>/<category>/<skill>/SKILL.md` or invoke it by name.",
        "3. `found: false` -> do not invent one silently. Tell the user the hub has nothing for it,",
        "   then offer `skillgram propose \"<need>\"` (draft) and wait for approval before `--apply`.",
        "4. Never hand-write files under `.agents/skills/`: use `skillgram use <skill>...`.",
        "5. Adding a skill to this project is `skillgram use`, not a copy.",
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
                reply({"ok": False, "error": f"unknown op: {op or '(vacia)'}"})
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
        description="Mantiene los links de la biblioteca de skills en todos los agentes.",
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
    p_onb.add_argument("--replace-natives", action="store_true", help="reemplaza la copia nativa por un link (la copia va a quarantine)")
    p_onb.add_argument("--no-llm", action="store_true", help="no consulta la LLM conectada")
    p_onb.add_argument("--plan", help="escribe el plan JSON en esa ruta")
    sub.add_parser("flavor", parents=[common], help="detecta el flavor (agente) de cada skill del hub").add_argument(
        "--apply", action="store_true", help="guarda el flavor en SKILLMETA.json")
    p_port = sub.add_parser("port", parents=[common], help="porta skills cableadas a otro harness (agents/<harness>.md)")
    p_port.add_argument("skill", nargs="?", help="skill puntual (si falta: escanea todo el hub)")
    p_port.add_argument("--to", help="harness destino (si falta: todos los instalados)")
    p_port.add_argument("--apply", action="store_true", help="genera los archivos (si falta: solo el plan)")
    p_port.add_argument("--no-llm", action="store_true")
    p_prec = sub.add_parser("precedence", parents=[common], help="declara el hub primero en la config de cada harness")
    p_prec.add_argument("--apply", action="store_true", help="escribe (con backup); si falta, solo muestra")
    p_prec.add_argument("--harness", help="solo este harness (p. ej. hermes, con su propio HERMES_HOME)")
    p_adopt = sub.add_parser("adopt", parents=[common], help="adopta skills native: copia al hub, verifica hash, deja link")
    p_adopt.add_argument("path", nargs="?", help="carpeta de la skill")
    p_adopt.add_argument("--all", action="store_true", help="todas las native de todos los harnesss")
    p_mount = sub.add_parser("mount", parents=[common], help="materializa el scope del proyecto en cada harness")
    p_mount.add_argument("--project", help="carpeta del proyecto (default: resolucion tipo engram)")
    p_mount.add_argument("--apply", action="store_true", help="crea/retira los links (si falta: solo el plan)")
    p_mount.add_argument("--prune-global", action="store_true", help="ademas retira los links al hub de los homes (strict)")
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
    p_boot.add_argument("--apply", action="store_true", help="registra el set elegido en el proyecto")
    p_boot.add_argument("--no-llm", action="store_true")
    p_miss = sub.add_parser("missing", parents=[common], help="consulta determinista: hay skill para esto?")
    p_miss.add_argument("text", nargs="+")
    p_miss.add_argument("--project")
    p_prop = sub.add_parser("propose", parents=[common], help="redacta una skill nueva con la LLM y (con --apply) la publica")
    p_prop.add_argument("need", nargs="+")
    p_prop.add_argument("--project")
    p_prop.add_argument("--apply", action="store_true", help="publica en el hub, registra y monta")
    p_prop.add_argument("--no-llm", action="store_true")
    sub.add_parser("index", parents=[common], help="(re)construye el indice FTS5 del hub")
    p_srch = sub.add_parser("search", parents=[common], help="busqueda full-text en el hub")
    p_srch.add_argument("text", nargs="+")
    p_srch.add_argument("--limit", type=int, default=10)
    p_rev = sub.add_parser("review", parents=[common], help="skills never used or not used recently")
    p_rev.add_argument("--project")
    p_rev.add_argument("--days", type=int, default=90)
    p_rev.add_argument("--fail-if-stale", action="store_true", help="exit 1 when something is stale (CI gate)")
    p_pin = sub.add_parser("pin", parents=[common], help="marca skills del proyecto como fijas (no caducan)")
    p_pin.add_argument("skills", nargs="+")
    p_pin.add_argument("--project")
    p_pin.add_argument("--unpin", action="store_true")
    p_ret = sub.add_parser("retire", parents=[common], help="retira una skill del hub a _retired (nunca borra)")
    p_ret.add_argument("skill")
    p_ret.add_argument("--reason", default="")
    p_exp = sub.add_parser("export", parents=[common], help="exporta el hub a un tar.gz con hashes")
    p_exp.add_argument("file")
    p_imp = sub.add_parser("import", parents=[common], help="importa un tar.gz exportado por skillgram")
    p_imp.add_argument("file")
    p_imp.add_argument("--apply", action="store_true")
    p_lint = sub.add_parser("lint", parents=[common], help="quality gate: frontmatter, name, description, body size")
    p_lint.add_argument("skill", nargs="?", help="one skill (default: the whole hub)")
    p_lint.add_argument("--strict", action="store_true", help="warnings also fail")
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
    sub.add_parser("doctor", parents=[common], help="inventario: harnesss, native, collisions, drift")
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
    elif args.cmd == "flavor":
        result = flavor_scan(hub, apply=args.apply)
    elif args.cmd == "port":
        cfg = None if args.no_llm else llm_config()
        if args.skill:
            result = port_skill(hub, args.skill, args.to or "", cfg) if args.to else {
                "ok": False, "op": "port", "error": "--to <harness> es obligatorio para una skill puntual"}
        else:
            targets = [args.to] if args.to else [
                h["app"] for h in detect_harnesses() if h["skills_dir"]]
            result = port_missing(hub, targets, cfg, apply=args.apply)
    elif args.cmd == "precedence":
        result = apply_precedence(hub, apply=args.apply,
                                  only=normalize_harness(args.harness) if args.harness else None)
    elif args.cmd == "lint":
        result = lint_hub(hub, only=args.skill, strict=args.strict)
    elif args.cmd == "diff":
        result = diff_skill(hub, args.skill, Path(args.against))
    elif args.cmd == "demand":
        result = demand_report(limit=args.limit)
    elif args.cmd == "git":
        result = hub_git(hub, action=args.action, apply=args.apply)
    elif args.cmd == "index":
        result = index_build(hub)
    elif args.cmd == "search":
        result = index_search(" ".join(args.text), limit=args.limit)
    elif args.cmd == "review":
        proj = resolve_project(args.project)
        result = review_list(hub, Path(proj["path"]), days=args.days, fail_if_stale=args.fail_if_stale)
    elif args.cmd == "pin":
        proj = resolve_project(args.project)
        result = pin_skill(Path(proj["path"]), args.skills, pinned=not args.unpin)
    elif args.cmd == "retire":
        result = retire_skill(hub, args.skill, reason=args.reason)
        if result.get("ok"):
            result["sync"] = sync(hub, quiet=True)
    elif args.cmd == "export":
        result = export_hub(hub, Path(args.file))
    elif args.cmd == "import":
        result = import_hub(hub, Path(args.file), apply=args.apply)
    elif args.cmd == "mcp":
        return mcp_serve(hub, read_only=args.read_only)
    elif args.cmd == "bootstrap":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = bootstrap(hub, Path(proj["path"]), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
    elif args.cmd == "missing":
        proj = resolve_project(args.project)
        result = missing(hub, " ".join(args.text))
        result["project"] = str(proj["path"])
    elif args.cmd == "propose":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = propose_skill(hub, Path(proj["path"]), " ".join(args.need), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
    elif args.cmd == "mount":
        proj = resolve_project(args.project)
        root = Path(proj["path"])
        result = mount_project(hub, root, apply=args.apply, prune_global=args.prune_global)
        if not result.get("ok") and args.bootstrap is not False:
            cfg = llm_config()
            want = args.bootstrap is True or bool(args.yes)
            if cfg and args.apply and not want:
                ans = ""
                try:
                    if sys.stdin.isatty() and not getattr(args, "json", False):
                        ans = input(f"no skills in scope for {root.name}; bootstrap it now? [y/N]: ")
                except (EOFError, OSError):
                    ans = ""
                want = ans.strip().lower().startswith("y")
            if cfg and args.apply and want:
                result["bootstrap"] = bootstrap(hub, root, cfg, apply=True)
                if result["bootstrap"].get("applied"):
                    result = mount_project(hub, root, apply=True, prune_global=args.prune_global)
                    result["bootstrap"] = "done"
            elif not cfg:
                result["bootstrap_hint"] = "no LLM configured: run `skillgram use <skill>...` in this repo"
        if result.get("ok") and args.apply and not getattr(args, "no_protocol", False):
            result["protocol"] = inject_protocol(hub, root, apply=True)
        result["resolucion"] = proj["source"]
        result["resolucion"] = proj["source"]
    elif args.cmd == "adopt":
        if args.all:
            result = adopt_natives(hub)
        elif args.path:
            result = adopt_skill(hub, Path(args.path))
        else:
            result = {"ok": False, "op": "adopt", "error": "pasame una carpeta o --all"}
    elif args.cmd == "install":
        result = install(hub, harness=args.harness, root=args.root, apply=not args.dry_run,
                         bootstrap_repos=args.bootstrap_repos, depth=args.depth)
    elif args.cmd == "propagate":
        result = propagate(hub, harness=args.harness, root=args.root, apply=args.apply,
                           depth=args.depth, force=args.force, bootstrap_repos=args.bootstrap_repos)
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
            replaced = {"quarantine": [], "links": [], "failures": []}
            if args.replace_natives and applied["imported"]:
                replaced = replace_native_with_link(
                    hub, applied["imported"], HOME / ".agents" / "skillgram-quarantine"
                )
            linked = sync(hub, quiet=True)
            result = {
                **plan,
                "applied": applied,
                "reemplazo": replaced,
                "sync": linked,
                "ok": not applied["failed"] and not replaced["failures"],
            }
    elif args.cmd == "sync":
        result = sync(hub, quiet=args.quiet, dry_run=args.dry_run)
    elif args.cmd == "watch":
        if not hub.root.is_dir():
            print(json.dumps({"ok": False, "error": f"hub not found {hub.root}"}), flush=True)
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
        result["uso"] = touch_usage(Path(proj["path"]), args.skills).get("touched", [])
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
    elif args.cmd == "install":
        print(install_report(result), flush=True)
    elif args.cmd == "propagate":
        print(propagate_report(result), flush=True)
    elif args.cmd == "onboard":
        p = result
        print(
            f"scope: {p['scope']}"
            + (f" ({p['harness']})" if p.get("harness") else " (todos los harnesss)")
            + f"  | hub: {p['hub']}\n"
            + "harnesss con skills native:\n"
            + ("\n".join(f"  {h['app']:16} {h['native']:>4} native" for h in p["harnesses"] if h["native"])
               or "  (ninguno)")
            + f"\nplan: {p['summary']['import']} importar | {p['summary']['dedupe']} duplicadas | "
            f"{p['summary']['conflict']} en conflicto"
            + (f"\nllm: {p['llm']['model']} ({p['llm']['aplicadas']} skills clasificadas)" if p.get("llm") else "\nllm: no usada")
            + (f"\nllm_error: {p['llm_error']}" if p.get("llm_error") else "")
            + "\n"
            + "\n".join(
                f"  {a['action']:8} {a['harness']}/{a['skill']} -> {a['category']}"
                + (f" [{a.get('flavor')}]" if a.get("flavor") else "")
                + (f"  ({a['reason']})" if a.get("reason") else "")
                for a in p["actions"][:40]
            )
            + (f"\n  ... y {len(p['actions']) - 40} mas" if len(p["actions"]) > 40 else "")
            + ("\n\napplied: " + str(len(p["applied"]["imported"])) + " imported, "
               + str(len(p["applied"]["skipped"])) + " salteadas, "
               + str(len(p["applied"]["failed"])) + " failed" if p.get("applied") else "\n(solo plan: corre con --apply para ejecutarlo)"),
            flush=True,
        )
    elif args.cmd == "flavor":
        result = flavor_scan(hub, apply=args.apply)
    elif args.cmd == "port":
        cfg = None if args.no_llm else llm_config()
        if args.skill:
            result = port_skill(hub, args.skill, args.to or "", cfg) if args.to else {
                "ok": False, "op": "port", "error": "--to <harness> es obligatorio para una skill puntual"}
        else:
            targets = [args.to] if args.to else [
                h["app"] for h in detect_harnesses() if h["skills_dir"]]
            result = port_missing(hub, targets, cfg, apply=args.apply)
    elif args.cmd == "precedence":
        result = apply_precedence(hub, apply=args.apply,
                                  only=normalize_harness(args.harness) if args.harness else None)
    elif args.cmd == "lint":
        result = lint_hub(hub, only=args.skill, strict=args.strict)
    elif args.cmd == "diff":
        result = diff_skill(hub, args.skill, Path(args.against))
    elif args.cmd == "demand":
        result = demand_report(limit=args.limit)
    elif args.cmd == "git":
        result = hub_git(hub, action=args.action, apply=args.apply)
    elif args.cmd == "index":
        result = index_build(hub)
    elif args.cmd == "search":
        result = index_search(" ".join(args.text), limit=args.limit)
    elif args.cmd == "review":
        proj = resolve_project(args.project)
        result = review_list(hub, Path(proj["path"]), days=args.days, fail_if_stale=args.fail_if_stale)
    elif args.cmd == "pin":
        proj = resolve_project(args.project)
        result = pin_skill(Path(proj["path"]), args.skills, pinned=not args.unpin)
    elif args.cmd == "retire":
        result = retire_skill(hub, args.skill, reason=args.reason)
        if result.get("ok"):
            result["sync"] = sync(hub, quiet=True)
    elif args.cmd == "export":
        result = export_hub(hub, Path(args.file))
    elif args.cmd == "import":
        result = import_hub(hub, Path(args.file), apply=args.apply)
    elif args.cmd == "mcp":
        return mcp_serve(hub, read_only=args.read_only)
    elif args.cmd == "bootstrap":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = bootstrap(hub, Path(proj["path"]), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
    elif args.cmd == "missing":
        proj = resolve_project(args.project)
        result = missing(hub, " ".join(args.text))
        result["project"] = str(proj["path"])
    elif args.cmd == "propose":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = propose_skill(hub, Path(proj["path"]), " ".join(args.need), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
    elif args.cmd == "mount":
        proj = resolve_project(args.project)
        root = Path(proj["path"])
        result = mount_project(hub, root, apply=args.apply, prune_global=args.prune_global)
        if not result.get("ok") and args.bootstrap is not False:
            cfg = llm_config()
            want = args.bootstrap is True or bool(args.yes)
            if cfg and args.apply and not want:
                ans = ""
                try:
                    if sys.stdin.isatty() and not getattr(args, "json", False):
                        ans = input(f"no skills in scope for {root.name}; bootstrap it now? [y/N]: ")
                except (EOFError, OSError):
                    ans = ""
                want = ans.strip().lower().startswith("y")
            if cfg and args.apply and want:
                result["bootstrap"] = bootstrap(hub, root, cfg, apply=True)
                if result["bootstrap"].get("applied"):
                    result = mount_project(hub, root, apply=True, prune_global=args.prune_global)
                    result["bootstrap"] = "done"
            elif not cfg:
                result["bootstrap_hint"] = "no LLM configured: run `skillgram use <skill>...` in this repo"
        if result.get("ok") and args.apply and not getattr(args, "no_protocol", False):
            result["protocol"] = inject_protocol(hub, root, apply=True)
        result["resolucion"] = proj["source"]
        result["resolucion"] = proj["source"]
    elif args.cmd == "adopt":
        if args.all:
            result = adopt_natives(hub)
        elif args.path:
            result = adopt_skill(hub, Path(args.path))
        else:
            result = {"ok": False, "op": "adopt", "error": "pasame una carpeta o --all"}
    elif args.cmd == "doctor":
        d = result
        inst = [h["app"] for h in d["harnesses"] if h["instalado"]]
        with_skills = [h["app"] for h in d["harnesses"] if h["skills_dir"]]
        print(
            f"hub: {d['hub']}\n"
            f"skills: {d['skills_hub']} in {d['categories']} categories\n"
            f"harnesses installed ({len(inst)}): {', '.join(inst) or '-'}\n"
            f"with a skills/ folder ({len(with_skills)}): {', '.join(with_skills) or '-'}\n"
            "harness                       links  dead  missing  native  collisions\n"
            + "\n".join(
                f"{r['app'][:28]:28}  {r['hub_links']:>7}  {r['dead']:>7}  "
                f"{r['missing']:>6}  {r['native']:>7}  {len(r['collisions']):>10}"
                for r in d["agents"]
            )
            + "\n"
            + ("\n".join(f"- {r}" for r in d["recommendations"]) if d["recommendations"] else ""),
            flush=True,
        )
    elif args.cmd in {"status", "sync", "install-autostart", "uninstall-autostart"}:
        if args.cmd == "status":
            s = result
            print(
                f"hub: {s['hub']}\n"
                f"skills: {s['skills']} in {s['categories']} categories ({s['nested']} nested), agents: {s['agents']}\n"
                f"drift: {s['drift']} (missing {s['missing']}, mispointed {s['mispointed']}, "
                f"dead {s['dead']}, skipped {s['skipped']})",
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
            f"promoted: {len(result['created'])} nuevas, {len(result['already_present'])} already present\n"
            + (f"unknown in hub: {', '.join(result['unknown'])}\n" if result["unknown"] else "")
            + (f"occupied without link: {', '.join(result['ocupadas_sin_enlace'])}\n" if result["ocupadas_sin_enlace"] else "")
            + f"manifest: {result['manifest']}",
            flush=True,
        )
    elif args.cmd == "unuse":
        print(
            f"proyecto: {result['project']}\n"
            f"removed: {len(result['removed'])}"
            + (f" | no estaban: {', '.join(result['absent'])}" if result["no_estaban"] else ""),
            flush=True,
        )
    elif args.cmd == "project-status":
        s = result
        print(
            f"proyecto: {s['project_name']} ({s['project']})  resolucion: {s['resolucion']}\n"
            f"promoted: {s['promoted']} | healthy {len(s['healthy'])} | "
            f"stale {len(s['stale'])} | broken {len(s['broken'])} | missing link {len(s['missing_links'])}\n"
            + (f"stale: {', '.join(s['stale'])}\n" if s["stale"] else "")
            + (f"broken: {'; '.join(s['broken'])}\n" if s["broken"] else "")
            + (f"missing link: {'; '.join(s['missing_links'])}\n" if s["sin_enlace"] else "")
            + (
                f"unregistered links: {', '.join(s['links_sin_registrar'])}\n"
                if s["links_sin_registrar"]
                else ""
            )
            + (
                f"local duplicates (estan en el hub y ademas copiadas): {', '.join(s['local_duplicates'])}\n"
                if s["duplicadas_locales"]
                else ""
            )
            + (f"local only: {', '.join(s['local_only'])}\n" if s["locales_propias"] else "")
            + ("" if s["tiene_config"] else "sin .agents/config.json (corre `init` para fijar el nombre)\n"),
            flush=True,
        )
    else:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())

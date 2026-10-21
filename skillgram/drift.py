"""Drift, apply, and the hub arriving from git: `plan`, `sync`, `status`,
`clone`. Writes links; decides nothing about policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .govern import policy_for, policy_verdict
from .hub import (
    VERSION,
    Hub,
    _first_lines,
    _which,
    default_hub,
    is_link,
    link_target,
    log,
    run_cli,
)


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


def clone_hub(url: str, target: Path | None = None, ref: str | None = None,
              force: bool = False, sync_after: bool = True) -> dict:
    """Bring a hub from a git repo (private or public) into this machine.

    git does the transport, and therefore the auth for a private repo; skillgram
    verifies what arrived and -- unless `--no-sync` -- links it into the harnesses so
    the skills are usable without a second command. Nothing is ever overwritten: a
    non-empty target is merged only with `--force`, and the skills it already has win.
    """
    dest = Path(target).expanduser() if target else default_hub()
    out = {"ok": False, "op": "clone", "url": url, "hub": str(dest), "ref": ref or ""}
    git = _which("git")
    if not git:
        out["error"] = "git is not on PATH, so a hub cannot be cloned here"
        return out
    branch = ["--branch", ref] if ref else []
    is_repo = (dest / ".git").is_dir()
    non_empty = dest.exists() and any(dest.iterdir()) if dest.exists() else False

    if is_repo:
        code, text = run_cli([git, "-C", str(dest), "remote", "get-url", "origin"], timeout=60)
        origin = text.strip()
        if origin and origin != url:
            if not force:
                out["error"] = (f"{dest} already tracks {origin}; pass --force to re-point it, "
                                "or clone into another --hub")
                return out
            run_cli([git, "-C", str(dest), "remote", "set-url", "origin", url], timeout=60)
        code, text = run_cli([git, "-C", str(dest), "pull", "--ff-only"] + (["origin", ref] if ref else []),
                             timeout=600)
        out["transport"] = "pull"
        if code != 0:
            out["error"] = f"git pull failed: {_first_lines(text, 3)}"
            return out
    elif non_empty:
        if not force:
            out["error"] = (f"{dest} is not empty and is not a git repo; pass --force to merge into it "
                            "(skills it already has are never overwritten)")
            return out
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "hub"
            code, text = run_cli([git, "clone", "--depth", "1", *branch, url, str(staging)], timeout=600)
            if code != 0:
                out["error"] = f"git clone failed: {_first_lines(text, 3)}"
                return out
            added, conflicts = [], []
            for name, tipo in Hub(staging).skills().items():
                src = staging / tipo / name
                to = dest / tipo / name
                if to.exists():
                    conflicts.append(f"{tipo}/{name}")
                    continue
                to.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, to)
                added.append(f"{tipo}/{name}")
            out.update({"transport": "merge", "added": added, "conflicts": conflicts})
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # shallow: a hub is content, not history; `git log` is still there for the audit
        code, text = run_cli([git, "clone", "--depth", "1", *branch, url, str(dest)], timeout=600)
        out["transport"] = "clone"
        if code != 0:
            out["error"] = f"git clone failed: {_first_lines(text, 3)}"
            return out

    hub = Hub(dest)
    skills = hub.skills()
    head_code, head = run_cli([git, "-C", str(dest), "rev-parse", "--short", "HEAD"], timeout=60)
    origin_code, origin = run_cli([git, "-C", str(dest), "remote", "get-url", "origin"], timeout=60)
    out.update({
        "skills": len(skills),
        "categories": len(hub.tipos()),
        "commit": head.strip() if head_code == 0 else "",
        "origin": origin.strip() if origin_code == 0 else "",
        "synced": None,
    })
    if not skills:
        out["error"] = "the repository has no <category>/<skill>/SKILL.md: is this a skill hub?"
        return out
    out["ok"] = True
    if sync_after:
        out["synced"] = sync(hub, quiet=True)
    return out

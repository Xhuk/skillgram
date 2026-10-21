"""A project's set: the manifest, `use`/`mount`/`align`, and what the
policy says about it."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .drift import _remove_junction, make_links
from .govern import (
    _pattern_hits,
    load_policy,
    policy_filter,
    policy_for,
    policy_verdict,
    read_seeds,
)
from .hub import (
    PROJECT_CONFIG,
    PROJECT_DIR,
    PROJECT_MANIFEST,
    Hub,
    _project_name,
    _read_manifest,
    _under,
    _write_manifest,
    default_hub,
    detect_harnesses,
    is_link,
    link_target,
    normalize_harness,
    project_config_name,
    project_dir,
    skill_hash,
)

PROJECT_ENV = "SKILLS_PROJECT"


PROJECT_SKILLS = "skills"


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


def _state_of(dest: Path, hub_root: Path) -> dict:
    """What a harness reads under `dest`: the hub's link, a forked copy, or nothing."""
    if is_link(dest):
        tgt = link_target(dest)
        if tgt and _under(Path(tgt), hub_root):
            return {"state": "link"}
        return {"state": "link-elsewhere"}
    if dest.is_dir() and (dest / "SKILL.md").is_file():
        return {"state": "native", "hash": skill_hash(dest)}
    return {"state": "absent"}


def align_hashes(hub: Hub, project: Path, known: dict | None = None) -> list:
    """Which content each harness reads of every skill in the set.

    A link cannot diverge: it points at the hub, so it *is* the hub's hash. What can
    diverge is a forked copy (a real directory) or a stale manifest entry (the hub
    moved after `use`). This is the report that says which one you are looking at.
    """
    scope = project_scope(hub, project, known)
    home_dirs = {}
    for agent_dir in hub.agents():
        app = agent_dir.parent.name.lstrip(".")
        home_dirs.setdefault(app, agent_dir)
        home_dirs.setdefault(normalize_harness(app), agent_dir)
    rows = []
    for item in scope["skills"]:
        name, tipo = item["name"], item["tipo"]
        src = Path(item["src"])
        hub_hash = skill_hash(src)
        entry = _read_manifest(project)["skills"].get(name) or {}
        recorded = entry.get("hash") or ""
        harnesses = {}
        for harness in detect_harnesses():
            if not harness["instalado"]:
                continue
            app = harness["app"]
            found = None
            for rel in PROJECT_SKILL_DIRS.get(app) or ():
                state = _state_of(project / rel / name, hub.root)
                if state["state"] != "absent":
                    found = {**state, "where": rel}
                    break
            if found is None:
                agent_dir = home_dirs.get(app) or home_dirs.get(normalize_harness(app))
                if agent_dir is not None:
                    state = _state_of(agent_dir / name, hub.root)
                    if state["state"] != "absent":
                        found = {**state, "where": "home"}
            harnesses[app] = found or {"state": "absent"}
        rows.append({
            "skill": name,
            "category": tipo,
            "hub": hub_hash,
            "manifest": recorded,
            # the hub moved after this project took the skill: `use` re-seals it
            "stale": bool(recorded) and recorded != hub_hash,
            "promoted_at": entry.get("promoted_at", ""),
            "harnesses": harnesses,
        })
    return rows


def align(hub: Hub, project: Path, apply: bool = False, known: dict | None = None,
          hashes: bool = False) -> dict:
    """What each harness actually sees of one project's set -- from the disk.

    Read-only by default. `--apply` only *adds* the links the project needs: an
    `extra` is reported, never removed, and a native directory is never touched (the
    project set is the only source; a harness that sees more is a violation to report,
    not a suggestion to propagate). `hashes=True` adds the content each harness reads.
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
    result = {
        "ok": not unfixable,
        "op": "align",
        "project": str(project),
        "project_name": scope["project_name"],
        "skills": len(names),
        "applied": applied,
        "unfixable": unfixable,
        "rows": rows,
    }
    if hashes:
        result["hashes"] = align_hashes(hub, project, known)
    return result


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


ALIGN_LIST_LIMIT = 25


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


PROTOCOL_BEGIN = "<!-- skillgram:protocol:begin -->"


PROTOCOL_END = "<!-- skillgram:protocol:end -->"


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
    results: list[dict] = []
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

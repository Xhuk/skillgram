"""A skill's life on disk: list, touch, pin, review, retire, export,
import, diff, git, index, search, demand, and the authoring gate (`lint`, `new`)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from .govern import read_seeds
from .hub import (
    DEFAULT_CATEGORY,
    Hub,
    _read_manifest,
    _write_manifest,
    home_dir,
    re_findall_words,
    skill_hash,
    skill_meta,
    write_skill_meta,
)


def index_path() -> Path:
    """The FTS5 database; rebuilt by `index`, read by `search` and `demand`."""
    return home_dir() / ".agents" / "skillgram-index.db"


def _index_conn(path: Path | None = None):
    import sqlite3

    p = Path(path or index_path())
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
    return {"ok": True, "op": "index", "skills": rows, "path": str(path or index_path())}


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
    return {"op": "review", "project": str(project) if project else None,
            "threshold_days": days, "never_used": never, "stale": stale, "pinned": pinned,
            # `--fail-if-stale` turns the report into a CI gate; the key was written
            # twice here and the second one silently won
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


def export_hub(hub: Hub, out: Path) -> dict:
    import tarfile

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "hub": str(hub.root), "skills": []}
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


MAX_DESCRIPTION = 140


MAX_BODY_LINES = 400


MIN_BODY_LINES = 6


MIN_DESCRIPTION = 20


CONTRACT_SECTIONS = (
    "Activation Contract",
    "Hard Rules",
    "Decision Gates",
    "Execution Steps",
    "Output Contract",
)


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

# A placeholder is a *marker*, not the word. A skill that says "mark unknowns as TBD with an
# owner" is being honest; a skill that says "TODO: fill this in" is not finished. Only the
# marker shapes fail: the word starting a line, a table cell, or a `Label: WORD` value.
_PLACEHOLDER_LINE = re.compile(
    r"^[\s>#*+\-`\[]*(?P<word>" + "|".join(PLACEHOLDERS) + r")\b(?![/(])", re.MULTILINE)
_PLACEHOLDER_CELL = re.compile(
    r"\|\s*(?P<word>" + "|".join(PLACEHOLDERS) + r")\s*\|")
_PLACEHOLDER_LABEL = re.compile(
    r"\b(?:" + "|".join(PLACEHOLDERS) + r")\s*:\s*(?:$|[^/\w])", re.MULTILINE)


def _placeholder_hits(text: str, front: dict | None = None) -> list:
    """The placeholders a reader would call unfinished, with the shape that matched."""
    hits = []
    for match in _PLACEHOLDER_LINE.finditer(text):
        hits.append(f"{match.group('word')} starts a line")
    for match in _PLACEHOLDER_CELL.finditer(text):
        hits.append(f"{match.group('word')} is a table cell")
    for key in ("description", "name"):
        value = ((front or {}).get(key) or "").strip().strip('"').strip("'")
        if value:
            first = value.split(" ", 1)[0].rstrip(":.,(")
            if first in PLACEHOLDERS:
                hits.append(f"{first} is the whole {key}")
    for match in _PLACEHOLDER_LABEL.finditer(text):
        hits.append(f"{match.group(0).split(':')[0].strip()} used as a label")
    return list(dict.fromkeys(hits))


LOCAL_PATH = re.compile(r"C:\\Users|C:/Users|/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/")

# `../<slug>/SKILL.md` only works when every skill sits in one flat directory. The hub keeps
# categories, so a path like that resolves to a sibling category and nothing else: name the
# skill instead. This is the check that would have caught a whole pack of dead links.
CROSS_LINK = re.compile(r"\.\./([A-Za-z0-9._-]+)/(SKILL\.md)?")


def _frontmatter(text: str) -> dict:
    """The YAML this tool actually writes: `key: value`, quoted values, and the block
    scalars `>`/`>-`/`|`/`|-` (a description that spans lines).

    It is not a YAML parser, on purpose: the contract needs `name` and `description`, and a
    real parser would drag a dependency into a stdlib-only tool. What it must not do is
    mistake `description: >-` for a two-character description â€” that turns the gate into a
    liar for every file written the readable way.
    """
    text = text.lstrip("\ufeff")  # a BOM from a Windows editor is not "no frontmatter"
    out: dict = {}
    if not text.startswith("---"):
        return out
    key, block, style = None, [], ""

    def flush() -> None:
        nonlocal key, block, style
        if key is not None:
            joiner = "\n" if style.startswith("|") else " "
            out[key] = joiner.join(part.strip() for part in block if part.strip()).strip()
        key, block, style = None, [], ""

    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            flush()
            break
        if line.startswith((" ", "\t")):
            if key is not None:
                block.append(line)
            continue
        if ":" not in line:
            continue
        flush()
        name, _, val = line.partition(":")
        val = val.strip()
        if val in {">", ">-", ">+", "|", "|-", "|+"}:
            key, style = name.strip().lower(), val
        else:
            out[name.strip().lower()] = val.strip('"').strip("'")
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


def _reference_warnings(text: str, skill_dir: Path, known: dict | None) -> list:
    """Pointers in the skill text that do not resolve: a missing folder, a broken skill link.

    A skill that names a file it does not ship, or a sibling that lives in another category,
    reads as complete and is not. This is the check the AD3 pack needed for a year.
    """
    warnings = []
    for ref in ("references/", "scripts/", "assets/"):
        # a *skill-relative* folder: `assets/` counts, `typst/assets/` is a path in the host
        if re.search(r"(?<![\w/.-])" + re.escape(ref), text) and not (skill_dir / ref.strip("/")).is_dir():
            warnings.append(f"mentions {ref} but the folder does not exist")
    if known is None:
        return warnings
    for match in CROSS_LINK.finditer(text):
        target, explicit = match.group(1), bool(match.group(2))
        if (skill_dir.parent / target).is_dir():
            continue  # a real sibling: the relative path resolves, nothing to say
        if target in known:
            warnings.append(f"`../{target}/` assumes a flat layout: that skill lives in "
                            f"`{known[target]}`; reference it by name")
        elif explicit:
            # only an explicit `../x/SKILL.md` claims a skill exists; `../references/` in the
            # middle of a sentence is a path in the host, and not our business
            warnings.append(f"`../{target}/SKILL.md` does not resolve: no skill by that name here")
    return warnings


def lint_skill(skill_dir: Path, require_contract: bool = False, known: dict | None = None) -> dict:
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
    warnings.extend(_reference_warnings(text, skill_dir, known))

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
    for hit in _placeholder_hits(text, fm):
        errors.append(f"placeholder left in the text ({hit})")
    if LOCAL_PATH.search(text):
        warnings.append("mentions a local absolute path (the skill stops being portable)")
    return {"skill": skill_dir.name, "errors": errors, "warnings": warnings,
            "sections": [s for s in CONTRACT_SECTIONS if s in sections]}


def lint_hub(hub: Hub, only: str | None = None, strict: bool = False, contract: bool = False,
             baseline: Path | None = None, write_baseline: Path | None = None) -> dict:
    """Lint de una skill o de todo el hub. `contract` exige las cinco secciones.

    `baseline` es la deuda aceptada: un corpus real tiene fallos viejos, y un gate que
    siempre esta rojo no es un gate. Lo que figura en el archivo se reporta como
    `accepted`; solo lo nuevo hace fallar. `write_baseline` lo escribe.
    """
    names = [only] if only else sorted(hub.skills())
    known = hub.skills()
    rows = []
    for name in names:
        tipo = known.get(name)
        if not tipo:
            rows.append({"skill": name, "errors": ["not found in the hub"], "warnings": []})
            continue
        rows.append(lint_skill(hub.root / tipo / name, require_contract=contract, known=known))
    with_errors = [r for r in rows if r["errors"]]
    with_warnings = [r for r in rows if r["warnings"]]
    accepted: list[str] = []
    new = with_errors
    target = write_baseline or baseline
    if target is not None:
        current = sorted(f"{r['skill']}: {error}" for r in with_errors for error in r["errors"])
        if write_baseline is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"accepted": current}, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        known_bad: set = set()
        try:
            known_bad = set(json.loads(target.read_text(encoding="utf-8")).get("accepted") or [])
        except (OSError, ValueError):
            known_bad = set()
        accepted = [item for item in current if item in known_bad]
        new = [r for r in with_errors
               if any(f"{r['skill']}: {error}" not in known_bad for error in r["errors"])]
    failing = bool(new) or (strict and bool(with_warnings))
    return {
        "ok": not failing,
        "op": "lint",
        "strict": strict,
        "contract": contract,
        "skills": len(rows),
        "errors": with_errors,
        "new": new,
        "accepted": accepted,
        "warnings": with_warnings,
        "detail": rows if only else [],
    }


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
                "dirty": bool([line for line in out.splitlines() if not line.startswith("##")]),
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


"""Bringing in what the harnesses already have: inventory, the plan and
its apply, adoption, flavor, and precedence."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .drift import make_links
from .hub import (
    AGENTS_DIR,
    DEFAULT_CATEGORY,
    Hub,
    _skill_summary,
    home_dir,
    is_link,
    skill_hash,
    skill_meta,
    write_skill_meta,
)
from .llm import LLM_SYSTEM, llm_chat, llm_config


def harness_inventory(hub: Hub, only: str | None = None) -> list:
    """Skills native (reales) por harness, con hash y descripcion corta."""
    known = hub.skills()
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


def harness_configs() -> list:
    """Configs donde se puede declarar precedencia (harness, archivo, tipo)."""
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(("reasonix", Path(appdata) / "Reasonix" / "config.toml", "toml"))
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        out.append(("hermes", Path(hermes_home) / "config.yaml", "yaml"))
    out.append(("hermes", home_dir() / ".hermes" / "config.yaml", "yaml"))
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
    qroot = quarantine_root or (home_dir() / ".agents" / "skillgram-quarantine")
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

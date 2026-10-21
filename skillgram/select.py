"""Which skills a repo is offered: signals, ranking, seeds, bootstrap,
`missing`, `propose`."""

import json
import re
import shutil
import time
from pathlib import Path

from .adopt import GENERIC_FLAVOR, _heuristic_category, write_skill_meta
from .govern import read_seeds
from .hub import Hub, _read_manifest, _write_manifest, re_findall_words, skill_hash
from .lifecycle import lint_skill, record_demand
from .llm import llm_chat, llm_log
from .scope import project_scope, promote

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


TOKEN_ALIASES = {
    "go": ("go", "golang"),
    "ts": ("ts", "typescript"),
    "js": ("js", "javascript"),
    "py": ("py", "python"),
    "k8s": ("k8s", "kubernetes"),
    "postgres": ("postgres", "postgresql"),
    "supabase": ("supabase",),
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

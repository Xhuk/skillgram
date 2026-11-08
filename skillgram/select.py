"""Which skills a repo is offered: signals, ranking, seeds, bootstrap,
`missing`, `propose`."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from .adopt import GENERIC_FLAVOR, _heuristic_category, adopt_skill
from .features import feature_hits
from .govern import read_seeds
from .hub import (
    VAULT_DIR,
    Hub,
    _project_name,
    _read_manifest,
    _write_manifest,
    home_dir,
    is_link,
    re_findall_words,
    record_skill_usage,
    run_cli,
    skill_hash,
    skill_meta,
    vault_skills,
    write_skill_meta,
)
from .lifecycle import _frontmatter, lint_skill, record_demand
from .llm import llm_chat, llm_log
from .scope import PROJECT_SKILL_DIRS, mount_project, project_scope, promote

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


FEATURE_SCORE = 99  # a registered name beats any scored row: it is a decision, not a guess


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
    active = hub.skills()
    catalog = [(n, t, hub.root / t / n) for n, t in active.items()]
    for n, t in vault_skills(hub).items():
        if n not in active:
            catalog.append((n, t, hub.root / VAULT_DIR / t / n))
    for name, tipo, skill_dir in catalog:
        if name in avoid_set:
            continue
        try:
            head = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            head = ""
        low_head = head.lower()
        low_tipo = tipo.lower()
        low_name = name.lower()
        usage = skill_meta(skill_dir).get("usage") or {}
        stack_hits = usage.get("stack_hits") or {}
        total_hits = int(usage.get("total_hits", 0) or 0)
        unuse_count = int(usage.get("unuse_count", 0) or 0)
        score = hits = 0
        for token, pattern in compiled:
            in_name = bool(pattern.search(low_name))
            in_category = bool(pattern.search(low_tipo))
            in_body = token in low_head
            in_stack = int(stack_hits.get(token, 0) or 0) > 0
            where = (
                in_name * RANK_NAME
                + in_category * RANK_CATEGORY
                + in_body * RANK_BODY
                + in_stack * RANK_CATEGORY
            )
            if where:
                score += where
                hits += 1
        if score > 0 and total_hits > unuse_count:
            score += min(3, total_hits - unuse_count)
        seeded = name in prefer_set
        if seeded or score >= RANK_MIN:
            rows.append({"skill": name, "tipo": tipo, "score": score, "head": head,
                         "seed": seeded, "hits": hits, "usage_hits": total_hits})
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

    The features registry answers first and outranks every scored row: a name
    somebody registered is a decision, while a score is a guess. The seeds do not
    answer a lookup -- a query is a specific need, so `prefer` stays empty here
    (only `never` applies, because the user said never).
    """
    registered = [hit for hit in feature_hits(hub, text) if hit["known"]]
    toks = [t for t in re_findall_words(text) if len(t) > 2]
    ranked = rank_skills(hub, toks, avoid=read_seeds()["never"])
    feature_rows = [
        {"skill": hit["skill"], "tipo": hit["category"], "score": FEATURE_SCORE,
         "why": f"feature: {hit['request']}"}
        for hit in registered
    ]
    known_first = {row["skill"] for row in feature_rows}
    candidates = feature_rows + [
        {"skill": row["skill"], "tipo": row["tipo"], "score": row["score"]}
        for row in ranked if row["skill"] not in known_first
    ]
    found = bool(feature_rows) or bool(ranked and ranked[0]["score"] >= 2)
    best = candidates[0]["skill"] if candidates else ""
    record_demand(text, found, best=best, hits=len(candidates))
    return {
        "ok": True,
        "op": "missing",
        "query": text,
        "found": found,
        "candidates": candidates[:limit],
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


INJECTION_PATTERNS = [
    ("prompt-injection", re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE)),
    ("prompt-injection", re.compile(r"disregard\s+(all\s+)?system\s+(prompt|instructions)", re.IGNORECASE)),
    ("remote-pipe-exec", re.compile(r"(curl|wget)\s+[^\n|]+\|\s*(ba)?sh\b", re.IGNORECASE)),
]


def absorb_project_skills(hub: Hub, project: Path, keep_quarantine: bool = False) -> dict:
    """Detect physical skills installed locally in a project, adopt them into the hub,
    promote them into the project's manifest, and tag them with the project's stack signals.
    """
    signals = repo_signals(project)
    proj_name = _project_name(project)
    seen_dirs: set[Path] = set()
    absorbed: list[dict] = []
    failed: list[str] = []

    for rels in PROJECT_SKILL_DIRS.values():
        for rel in rels:
            sdir = project / rel
            if not sdir.is_dir() or sdir in seen_dirs:
                continue
            seen_dirs.add(sdir)
            try:
                entries = sorted(sdir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if is_link(entry) or not entry.is_dir() or not (entry / "SKILL.md").is_file():
                    continue
                res = adopt_skill(hub, entry, keep_quarantine=keep_quarantine)
                if not res.get("ok"):
                    failed.append(f"{entry.name}: {res.get('error', 'adopt failed')}")
                    continue
                promote(hub, [entry.name], project)
                record_skill_usage(
                    Path(res["hub"]),
                    project_name=proj_name,
                    stack_tags=signals["tags"],
                    by="local-install",
                )
                absorbed.append({
                    "skill": entry.name,
                    "hub": res["hub"],
                    "deduped": res.get("deduped", False),
                    "from": str(entry),
                    "stack_tags": signals["tags"],
                })
    return {
        "ok": not failed,
        "op": "absorb",
        "project": str(project),
        "project_name": proj_name,
        "stack_tags": signals["tags"],
        "absorbed": absorbed,
        "failed": failed,
    }


def gate_skill(hub: Hub, target: str | Path) -> dict:
    """Admission gate for a candidate or existing skill: token budget, security/injection,
    contract compliance, and routing collision check against the hub.
    """
    cand_path = Path(str(target))
    known = hub.skills()
    vmap = vault_skills(hub)
    if cand_path.is_dir() and (cand_path / "SKILL.md").is_file():
        skill_dir = cand_path
        skill_name = cand_path.name
    elif str(target) in known:
        skill_name = str(target)
        skill_dir = hub.root / known[skill_name] / skill_name
    elif str(target) in vmap:
        skill_name = str(target)
        skill_dir = hub.root / VAULT_DIR / vmap[skill_name] / skill_name
    else:
        return {"ok": False, "op": "gate", "skill": str(target), "error": f"skill not found: {target}"}

    md = skill_dir / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "op": "gate", "skill": skill_name, "error": str(exc)}

    fm = _frontmatter(text)
    desc = str(fm.get("description", ""))
    prompt_tokens = max(1, (len(skill_name) + len(desc) + 20) // 4)
    body_tokens = max(1, len(text) // 4)

    basic_lint = lint_skill(skill_dir, require_contract=False)
    contract_lint = lint_skill(skill_dir, require_contract=True)

    security_issues = []
    for label, pat in INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            security_issues.append(f"{label}: '{m.group(0)[:60]}'")

    budget_warnings = list(basic_lint["warnings"])
    if body_tokens > 2500:
        budget_warnings.append(
            f"SKILL.md is ~{body_tokens} tokens (>2500); split heavy reference text into references/"
        )

    toks = [t for t in re_findall_words(f"{skill_name} {desc}") if len(t) > 2]
    collisions = [
        {"skill": r["skill"], "category": r["tipo"], "score": r["score"]}
        for r in rank_skills(hub, toks, limit=6)
        if r["skill"] != skill_name and r["score"] >= 4
    ]

    aux_files = []
    try:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file() and f.name not in ("SKILL.md", "SKILLMETA.json") and "__pycache__" not in f.parts:
                aux_files.append(f.relative_to(skill_dir).as_posix())
    except OSError:
        pass

    meta = skill_meta(skill_dir)
    ok = not basic_lint["errors"] and not security_issues
    return {
        "ok": ok,
        "op": "gate",
        "skill": skill_name,
        "path": str(skill_dir),
        "hash": skill_hash(skill_dir),
        "prompt_tokens": prompt_tokens,
        "body_tokens": body_tokens,
        "aux_files": len(aux_files),
        "contract_ok": not contract_lint["errors"],
        "lint_errors": basic_lint["errors"],
        "lint_warnings": budget_warnings,
        "security_issues": security_issues,
        "collisions": collisions,
        "usage": meta.get("usage") or {},
        "upstream": meta.get("upstream") or {},
    }


def _find_skill_in_clone(root: Path, subpath: str | None = None) -> Path | None:
    if subpath:
        cand = root / subpath
        if (cand / "SKILL.md").is_file():
            return cand
    if (root / "SKILL.md").is_file():
        return root
    try:
        for md in sorted(root.rglob("SKILL.md")):
            if ".git" not in md.parts:
                return md.parent
    except OSError:
        pass
    return None


def try_skill(
    hub: Hub,
    source: str,
    project: Path | None = None,
    accept: bool = False,
    reject: bool = False,
    category: str | None = None,
    subpath: str | None = None,
) -> dict:
    """Curator trial workflow: stage a skill (from path or git URL), run `gate_skill`,
    optionally create a temporary `git worktree` of `project` with the skill mounted,
    and `--accept` (promote to hub with upstream metadata) or `--reject` (clean up).
    """
    trials_root = home_dir() / ".agents" / "skillgram-trials"
    trials_root.mkdir(parents=True, exist_ok=True)
    raw_name = Path(source.rstrip("/\\").split("#")[0]).stem.replace(".git", "")

    if accept or reject:
        meta_file = trials_root / f"{raw_name}.trial.json"
        trial = {}
        if meta_file.is_file():
            try:
                trial = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                trial = {}
        staged_dir = Path(trial.get("staged") or (trials_root / raw_name))
        wt_path = Path(trial["worktree"]) if trial.get("worktree") else None
        proj_path = Path(trial["project"]) if trial.get("project") else project

        def _cleanup_trial() -> None:
            if wt_path and wt_path.exists():
                if proj_path and (proj_path / ".git").exists():
                    run_cli(["git", "-C", str(proj_path), "worktree", "remove", "--force", str(wt_path)], timeout=15)
                shutil.rmtree(wt_path, ignore_errors=True)
            if staged_dir.exists():
                shutil.rmtree(staged_dir, ignore_errors=True)
            if meta_file.exists():
                try:
                    meta_file.unlink()
                except OSError:
                    pass

        if reject:
            _cleanup_trial()
            return {"ok": True, "op": "try", "action": "rejected", "skill": raw_name}

        if not (staged_dir / "SKILL.md").is_file():
            return {"ok": False, "op": "try", "skill": raw_name, "error": f"no staged trial found for {raw_name}"}

        gate = gate_skill(hub, staged_dir)
        skill_name = staged_dir.name
        tipo = category or hub.skills().get(skill_name) or _heuristic_category(skill_name, hub.tipos())
        dest = hub.root / tipo / skill_name
        existing_meta = skill_meta(dest) if dest.is_dir() else {}
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(staged_dir, dest)
        verified_hash = skill_hash(dest)
        upstream = dict(trial.get("upstream") or {})
        upstream["verified_hash"] = verified_hash
        upstream["accepted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        new_meta = {
            **existing_meta,
            "flavor": existing_meta.get("flavor", GENERIC_FLAVOR),
            "hash": verified_hash,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"kind": "curated", "upstream": upstream.get("repo") or upstream.get("local_source", "")},
            "upstream": upstream,
        }
        write_skill_meta(dest, new_meta)
        _cleanup_trial()
        promoted = None
        if proj_path and proj_path.is_dir():
            promoted = promote(hub, [skill_name], proj_path)
            mount_project(hub, proj_path, apply=True)
        return {
            "ok": True,
            "op": "try",
            "action": "accepted",
            "skill": skill_name,
            "category": tipo,
            "hub": str(dest),
            "hash": verified_hash,
            "upstream": upstream,
            "gate": gate,
            "promoted": promoted["created"] if promoted else [],
        }

    # Stage from local folder or git repo
    upstream = {}
    src_path = Path(source)
    if src_path.is_dir() and (src_path / "SKILL.md").is_file():
        skill_name = src_path.name
        staged_dir = trials_root / skill_name
        if staged_dir.exists():
            shutil.rmtree(staged_dir, ignore_errors=True)
        shutil.copytree(src_path, staged_dir)
        upstream = {"local_source": str(src_path.resolve())}
    elif source.startswith(("http://", "https://", "git@")) or source.endswith(".git"):
        tmp_clone = Path(tempfile.mkdtemp(prefix="skillgram-clone-"))
        try:
            code, _out, err = run_cli(["git", "clone", "--depth", "1", source, str(tmp_clone)], timeout=60)
            if code != 0:
                return {"ok": False, "op": "try", "error": f"git clone failed: {err[:240]}"}
            _c, sha, _e = run_cli(["git", "-C", str(tmp_clone), "rev-parse", "HEAD"], timeout=10)
            found = _find_skill_in_clone(tmp_clone, subpath=subpath)
            if found is None:
                return {"ok": False, "op": "try", "error": "no SKILL.md found in cloned repository"}
            skill_name = found.name if found != tmp_clone else raw_name
            staged_dir = trials_root / skill_name
            if staged_dir.exists():
                shutil.rmtree(staged_dir, ignore_errors=True)
            shutil.copytree(found, staged_dir, ignore=shutil.ignore_patterns(".git"))
            upstream = {
                "repo": source,
                "subpath": subpath or (found.relative_to(tmp_clone).as_posix() if found != tmp_clone else ""),
                "commit": sha.strip()[:40],
            }
        finally:
            shutil.rmtree(tmp_clone, ignore_errors=True)
    else:
        return {"ok": False, "op": "try", "error": f"source is not a skill folder or git URL: {source}"}

    gate = gate_skill(hub, staged_dir)
    wt_dir: Path | None = None
    if project and (project / ".git").exists() and shutil.which("git"):
        wt_candidate = trials_root / f"wt-{project.name}-{skill_name}"
        if wt_candidate.exists():
            run_cli(["git", "-C", str(project), "worktree", "remove", "--force", str(wt_candidate)], timeout=15)
            shutil.rmtree(wt_candidate, ignore_errors=True)
        code, _out, _err = run_cli(
            ["git", "-C", str(project), "worktree", "add", "--detach", str(wt_candidate), "HEAD"],
            timeout=30,
        )
        if code == 0 and wt_candidate.is_dir():
            wt_dir = wt_candidate
            for rel in (".agents/skills", ".claude/skills"):
                tdir = wt_dir / rel / skill_name
                tdir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staged_dir, tdir, dirs_exist_ok=True)

    meta_file = trials_root / f"{skill_name}.trial.json"
    meta_file.write_text(
        json.dumps({
            "skill": skill_name,
            "staged": str(staged_dir),
            "worktree": str(wt_dir) if wt_dir else None,
            "project": str(project) if project else None,
            "upstream": upstream,
            "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": gate["ok"],
        "op": "try",
        "action": "staged",
        "skill": skill_name,
        "staged": str(staged_dir),
        "worktree": str(wt_dir) if wt_dir else None,
        "upstream": upstream,
        "gate": gate,
    }

"""What is allowed and what is recommended in the hub: the seed list
(taste) and the policy levels. Pure reads plus the one write (`seed`)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path

from .hub import Hub, _first_lines, _which, home_dir, project_dir, run_cli

SEEDS_ENV = "SKILLGRAM_SEEDS"


SEED_MODES = ("always", "never")


POLICY_FILE = "policy.json"


POLICY_ENV = "SKILLGRAM_POLICY"


POLICY_MODES = ("open", "closed")


def seeds_path() -> Path:
    env = (os.environ.get(SEEDS_ENV) or "").strip()
    return Path(env) if env else home_dir() / ".agents" / "skillgram-seeds.json"


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
    return home_dir() / ".agents" / POLICY_FILE


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
    key = hashlib.sha1(f"{url}#{branch}".encode()).hexdigest()[:12]
    dest = home_dir() / ".agents" / "policy-cache" / key
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

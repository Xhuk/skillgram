"""The names that are not skill names, and the skill each one means.

`impecable` is not a skill: it is how this founder spells `impeccable`, and a door
that answers "unknown in hub" for it is the tool being pedantic with its own user.
The registry (`~/.agents/skillgram-features.json`) is consulted BEFORE the names the
hub registers, so `use`, `missing` and `search` resolve a decided name first and only
then fall back to the catalog.

The matching is a dict lookup, never a guess: the key is normalized (lowercase, no
accents, dashes for everything else) on both sides, so `Impecáble!` finds the row
somebody wrote as `impecable`, and a typo nobody registered stays unknown instead of
walking off to the nearest name by edit distance.
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from pathlib import Path

from .hub import Hub, home_dir

FEATURES_ENV = "SKILLGRAM_FEATURES"

FEATURES_FILE = "skillgram-features.json"

SOURCE_FEATURE = "feature"

SOURCE_HUB = "hub"


def features_path() -> Path:
    """Where the registry lives: next to the seeds, because it is taste too."""
    env = (os.environ.get(FEATURES_ENV) or "").strip()
    return Path(env) if env else home_dir() / ".agents" / FEATURES_FILE


def normalize_name(text: str) -> str:
    """`Impecáble!` -> `impecable`: the key both sides are compared on."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    flat = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    dashed = "".join(ch if ch.isalnum() else "-" for ch in flat.lower())
    return "-".join(part for part in dashed.split("-") if part)


def read_features() -> dict:
    """{key: {"skill", "note", "added"}} -- a row is a decision, so it is kept."""
    try:
        data = json.loads(features_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("features") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    for key, value in (raw or {}).items():
        row = value if isinstance(value, dict) else {"skill": value}
        skill = str(row.get("skill") or "").strip()
        normalized = normalize_name(key)
        if not normalized or not skill:
            continue
        out[normalized] = {
            "skill": skill,
            "note": str(row.get("note") or "").strip(),
            "added": str(row.get("added") or "").strip(),
        }
    return out


def write_features(features: dict) -> Path:
    path = features_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"features": {key: features[key] for key in sorted(features)}}
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def _resolve(known: dict, features: dict, name: str) -> dict:
    requested = str(name or "").strip()
    key = normalize_name(requested)
    row = features.get(key)
    if row is not None:
        skill = row["skill"]
        return {
            "requested": requested,
            "key": key,
            "skill": skill,
            "source": SOURCE_FEATURE,
            "known": skill in known,
            "category": known.get(skill, ""),
            "note": row["note"],
        }
    if requested in known:
        return {"requested": requested, "key": key, "skill": requested,
                "source": SOURCE_HUB, "known": True, "category": known[requested], "note": ""}
    # `Impeccable` and `impeccable` are the same skill; the catalog is not case-sensitive
    for candidate in sorted(known):
        if normalize_name(candidate) == key:
            return {"requested": requested, "key": key, "skill": candidate,
                    "source": SOURCE_HUB, "known": True, "category": known[candidate], "note": ""}
    return {"requested": requested, "key": key, "skill": "", "source": "",
            "known": False, "category": "", "note": ""}


def resolve(hub: Hub, name: str, features: dict | None = None) -> dict:
    """One name, resolved: features first, the hub's own names second.

    `known` is False when a feature points at a skill the hub does not have (the
    row survived a retire): the caller reports it instead of linking a ghost.
    """
    return _resolve(hub.skills(), read_features() if features is None else features, name)


def resolve_all(hub: Hub, names: list) -> list:
    """The same, reading the catalog and the registry once for the whole batch."""
    known = hub.skills()
    features = read_features()
    return [_resolve(known, features, raw) for raw in names]


def feature_hits(hub: Hub, text: str) -> list:
    """Every registered name that shows up in a query, longest match first.

    The whole string is tried first (a feature can be a phrase), then its words: a
    query is prose ("necesito impecable para el landing"), not a bare name.
    """
    known = hub.skills()
    features = read_features()
    probe = str(text or "").strip()
    if not probe:
        return []
    keys = [normalize_name(probe)] + normalize_name(probe).split("-")
    rows, seen = [], set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        row = features.get(key)
        if row is None or row["skill"] in {r["skill"] for r in rows}:
            continue
        rows.append({"request": key, "skill": row["skill"], "category": known.get(row["skill"], ""),
                     "source": SOURCE_FEATURE, "known": row["skill"] in known, "note": row["note"]})
    return rows


def _rows(features: dict, known: dict) -> list:
    return [
        {"name": key, "skill": features[key]["skill"], "note": features[key]["note"],
         "added": features[key]["added"], "known": features[key]["skill"] in known}
        for key in sorted(features)
    ]


LIST_WORDS = ("list", "ls")


def feature_skills(hub: Hub, pairs: list, remove: bool = False, note: str = "") -> dict:
    """The door: `list` (or nothing) lists, `<alias> <skill>` registers, `--remove` drops.

    The word `list` is an action only when it stands alone and nothing is being
    removed: with two arguments it is an alias like any other, so a skill that is
    called `list` can still be registered.
    Registering refuses a skill the hub does not have: the registry is a pointer,
    not a wish. It also reports a row that shadows a real skill name, because
    features are read first and a shadow is exactly what the founder asked for.
    """
    features = read_features()
    known = hub.skills()
    args = [str(raw).strip() for raw in pairs if str(raw).strip()]
    if not args or (not remove and len(args) == 1 and normalize_name(args[0]) in LIST_WORDS):
        return {
            "ok": True,
            "op": "feature",
            "action": "list",
            "features": _rows(features, known),
            "file": str(features_path()),
        }
    if remove:
        dropped, absent = [], []
        for raw in args:
            key = normalize_name(raw)
            if key in features:
                features.pop(key)
                dropped.append(key)
            else:
                absent.append(key)
        path = write_features(features)
        return {
            "ok": not absent,
            "op": "feature",
            "action": "remove",
            "removed": dropped,
            "absent": absent,
            "features": _rows(features, known),
            "file": str(path),
        }
    if len(args) % 2:
        return {"ok": False, "op": "feature", "action": "add",
                "error": "pass <alias> <skill> pairs: skillgram feature impecable impeccable",
                "file": str(features_path())}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    changed, unknown, shadows = [], [], []
    for alias_raw, skill_raw in zip(args[0::2], args[1::2]):
        key = normalize_name(alias_raw)
        skill = skill_raw.strip()
        if not key:
            unknown.append(alias_raw)
            continue
        if skill not in known:
            unknown.append(skill)
            continue
        if key in known and key != skill:
            shadows.append({"name": key, "skill": skill})
        previous = features.get(key) or {}
        features[key] = {
            "skill": skill,
            "note": (note or previous.get("note") or "").strip(),
            "added": previous.get("added") or stamp,
        }
        changed.append({"name": key, "skill": skill})
    path = write_features(features)
    return {
        "ok": not unknown,
        "op": "feature",
        "action": "add",
        "changed": changed,
        "unknown": sorted(set(unknown)),
        "shadows": shadows,
        "features": _rows(features, known),
        "file": str(path),
    }

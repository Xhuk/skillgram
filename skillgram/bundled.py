"""The skills that ship with the tool: the base every fresh machine gets.

A tool that governs skill content while shipping none of its own is asking a stranger to
trust an empty library: before this, `install` *blocked* on an empty hub. Two skills ship
inside the package now — one that explains how to work with this tool, one that closes the
maintainability gap this repo just walked through — and `install` seeds them into the hub.

Seeding never overwrites: a hub that already has the skill keeps its copy (the user may
have edited it), and the row says so. The sidecar records `source.kind = bundled` plus the
version it came from, so a bundled copy is distinguishable from an adopted one.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .hub import SKILL_META, VERSION, Hub, skill_hash

# name -> hub category. The category is where the skill lands in a new hub; it is not
# configurable, because a base that lands in a random folder is not a base.
BUNDLED = {
    "anti-slop-guardrails": "dev-practices",
    "skillgram-report": "meta-skills",
}


def bundled_root() -> Path:
    """The directory inside the installed package that holds the shipped skills."""
    return Path(__file__).resolve().parent / "skills"


def bundled_skills() -> dict:
    """name -> category, for the bundled skills actually present on disk.

    Read from disk rather than assumed: a source checkout with a half-copied directory
    should report one skill, not a name that is not there.
    """
    root = bundled_root()
    return {name: category for name, category in sorted(BUNDLED.items())
            if (root / name / "SKILL.md").is_file()}


def seed_hub(hub: Hub, only: list | None = None, apply: bool = True) -> dict:
    """Copy the bundled skills into the hub, once. Idempotent; never overwrites."""
    rows = []
    for name, category in bundled_skills().items():
        if only and name not in only:
            continue
        dest = hub.root / category / name
        if dest.exists():
            rows.append({"skill": name, "category": category, "path": str(dest),
                         "state": "present", "note": "already in the hub; left untouched"})
            continue
        if not apply:
            rows.append({"skill": name, "category": category, "path": str(dest),
                         "state": "would", "note": "dry run"})
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundled_root() / name, dest)
        meta = {
            "by": "skillgram",
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hash": skill_hash(dest),
            "source": {"kind": "bundled", "version": VERSION},
        }
        (dest / SKILL_META).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
        rows.append({"skill": name, "category": category, "path": str(dest),
                     "state": "created", "note": f"bundled base from skillgram {VERSION}"})
    created = [row["skill"] for row in rows if row["state"] == "created"]
    return {"ok": True, "op": "bundled", "rows": rows, "created": created,
            "hub": str(hub.root), "root": str(bundled_root())}

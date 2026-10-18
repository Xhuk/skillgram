# AGENTS.md

Notes for any coding agent working on this repo.

## What this is

`skillgram.py` is a single-file, stdlib-only CLI. No dependencies, no framework.
Keep it that way unless there is a hard reason not to.

## Contract

- One JSON line per operation on stdout for machine commands; human output only
  for `doctor`, `onboard`, `status`, `init`, `use`, `unuse`, `project-status`.
- Exit `0` healthy, `1` drift/error, `2` bad invocation.
- Never copy skill content into an agent folder. Links only (junctions on Windows,
  symlinks elsewhere).
- Never delete a real directory. Dead-link cleanup must stay scoped to links that
  resolve inside the hub (`_under`).
- Native skills of an agent are never modified nor adopted. A name collision is
  skipped and reported.
- The LLM (when used) only proposes a JSON plan. Validation and file operations
  happen in code, never inside the model's answer.

## Layout

    skillgram.py          the whole tool
    tests/test_*.py       stdlib unittest

## Before committing

    python -m unittest discover -s tests -v
    python skillgram.py doctor --json
    python skillgram.py status --json
    python skillgram.py onboard --no-llm --json

## 0.5 additions

- `index` / `search` (FTS5 at `~/.agents/skillgram-index.db`), `mcp` (stdio JSON-RPC).
- Lifecycle: `review`, `pin`, `retire` (`<hub>/_retired/` + `_retired.json` tombstones).
- `export` / `import` (tar.gz with a `hub.json` of hashes).
- `_`-prefixed directories in the hub are never treated as categories.
- Known gaps are listed in README ("Known gaps"); keep that list honest.
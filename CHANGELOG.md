# Changelog

## 1.1.2 — credits, and where gaps/errors go

- README: **Prior art and credits** — Engram (project scope, CLI+MCP shape), Agent Skills
  `SKILL.md`, the harness `skills/` conventions, ECC as the scale test. No code was copied;
  the sections that learned from another tool say so. Ideas deserve the footnote.
- Documents the two report channels that already exist: a **gap** (the hub has nothing for
  a capability) is a *request* — `skillgram_missing` → `propose`, recorded in the demand log
  (`~/.agents/skillgram-index.db`, `skillgram demand`); a **bug** is never a proposal — it
  exits non-zero with one JSON line (`ok:false`, `error`), and the issue tracker is the
  honest channel for it.

## 1.1.1 — install brief, real verification, multi-launcher harnesses

- **`INSTALL.md`**: a copy-paste brief to hand to a fresh agent session (what to run, what
  to verify, and the rules it may not break), plus the env switches, the per-harness table
  and the traps. README points at it from `## Install`.
- **Registration is verified, not assumed**: after `mcp add`, the config is probed again.
  Hermes exits **0** when its own "Enable all N tools? [Y/n/select]" prompt is cancelled
  (stdin is closed, so it always was), which made a cancelled registration look successful.
  skillgram now answers that prompt and reports `failed` whenever nothing was actually
  written.
- **`SKILLGRAM_MCP_CLI_<HARNESS>`**: pick the launcher that owns the config when a harness
  ships more than one (a shared install plus an isolated `HERMES_HOME`, a venv shim next to
  a self-updating one). It is used for the probe, the `mcp add` and the `mcp remove` hint.
- `hermes mcp list` truncates the command, so the "registered but points elsewhere" check is
  skipped for hermes instead of firing on a cut-off path.
- 72 stdlib tests.

## 1.1.0 — `install`, `propagate` and a ledger

One idempotent command per machine: `skillgram install`.

- Harness identity is never guessed: `SKILLGRAM_HARNESS` → `--harness` → process tree → `unknown` (and it says so).
- The MCP server is registered by delegating to each harness CLI (`reasonix` / `codex` / `claude` / `hermes` / `gemini mcp add`), with `SKILLGRAM_HARNESS` and `SKILLGRAM_HUB` in the server environment. No config file is ever edited by hand.
- `propagate --harness X --root <dir>` discovers git repos (depth-bounded), runs `mount --apply` + protocol + a ledger entry, and skips whatever is already in `~/.agents/skillgram-ledger.json` (`--force` to redo it).
- `install --dry-run` prints the exact commands and writes nothing.
- Output in three blocks — `done now` / `already in place` / `to take effect` — plus `needs you` when something is blocked.
- `mount --only` restricts a mount to one harness, and `mount` now maps `reasonix` to `.agents/skills`.
- Traps found by running the real CLIs: reasonix' legacy `mcp add` form takes `--env=K=V` (the spaced form is swallowed into the command) and rejects `--`; `shutil.which("skillgram")` can resolve to `./skillgram.py` or a directory, so the launch command is validated as a real executable.
- 70 stdlib tests.

## 1.0.0 — first public release

One skill hub for every coding agent. Links only: skillgram never copies, adopts,
moves or deletes the skills an agent already has.

**Hub**
- `sync` (idempotent: create missing links, repoint wrong ones, drop dead ones), `status`, `watch`, `daemon`, `install-autostart`.
- `doctor`: installed harnesses, native skills per agent, name collisions, dead/missing links.
- `onboard`: detect every installed harness, ask "all or just this one?", consolidate with the connected LLM.
- `adopt`: cooperative adoption (copy → verify hash → quarantine the original → leave a link).

**Project scope (engram-style)**
- `init`, `use`, `unuse`, `project-status`, `mount` (project-only view across 13 harness layouts).
- Resolution order: `--project` → `SKILLGRAM_PROJECT` → nearest `.agents/config.json` up to the git root → cwd.
- `mount --apply` also injects the skillgram protocol into `AGENTS.md`/`CLAUDE.md`.

**LLM, explicitly**
- `bootstrap`: repo signals → candidates → the LLM picks the set and reports gaps.
- `propose`: the LLM drafts a skill, `lint` gates it, then it is published and mounted.
- Every automatic decision is stamped `by: llm:<model>` and logged to `~/.agents/skillgram-llm.log`.

**Operations**
- `index` / `search` (FTS5), `missing` (deterministic "do we have something for this?"), `demand` (uncovered queries = roadmap).
- `review`, `pin`, `retire` (never deletes: `<hub>/_retired/` + tombstones).
- `export` / `import` (tar.gz with a hash manifest), `diff` (divergence between two copies), `git` (hub as a repo: status/init/pull/push).
- `mcp` (stdio JSON-RPC, 5 tools) and `mcp --read-only`.

**Surface**: English CLI, English JSON keys, stdlib only (Python 3.9+), Windows-first with macOS/Linux symlinks.

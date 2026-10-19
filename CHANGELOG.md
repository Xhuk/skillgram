# Changelog

## 1.3.0 — the skill contract, and a gate that actually checks it

`propose` asked the model for five sections; `lint` verified **none** of them. A skill could be
written by hand with no trigger, no rules and no output, and pass the gate. Now there is one
normative text and one gate for all three ways a skill can arrive.

- **`docs/SKILL-CONTRACT.md`** — frontmatter rules, the five sections in order (Activation
  Contract, Hard Rules, Decision Gates, Execution Steps, Output Contract), the prohibitions
  (credentials, `TODO`/`TBD`/`FIXME`, name conflicts) and the provenance sidecar.
- **`lint --contract`** enforces it: a missing section is an error, an empty section is an error,
  a scaffold that still says TODO is an error. Without the flag those sections are warnings, so
  the hub reports its debt instead of failing wholesale.
- **`skillgram new <name> --category <c>`** scaffolds the contract into the hub and writes
  `SKILLMETA.json` (`by`, `added_at`, `hash`, `source.kind`). It does not publish: `lint` decides.
- **`propose --apply` now runs the strict form of the gate** and rolls the skill back if the
  model's draft ignores the contract. Its `SKILLMETA.json` carries the same hash and `by:
  llm:<model>`.
- **`lint` catches more, without guessing**: `description` under 20 chars is an error (say *when*
  it applies), `TODO/TBD/FIXME` is an error, and live-looking credentials are errors — with
  narrow patterns, so a `ghp_xxx` placeholder or a PEM banner alone still pass. A local absolute
  path is a warning (portability). A UTF-8 BOM from a Windows editor no longer reads as "no
  frontmatter".
- The protocol block that `mount` injects now names both doors: author with `new`, import with
  `add` (coming), and `lint` is the gate.

**The hub's own debt, measured with the new gate** (843 skills): 817 have at least one error —
4,063 missing sections, 36 descriptions of one or two characters, 24 leftovers of
`TODO`/`FIXME`/`TBD`, 4 skills with no usable frontmatter — and 649 have warnings (52 mention a
`scripts/` folder that does not exist, 24 hard-code a local absolute path). That is the backlog
the contract makes visible; it is not fixed by this release.

88 stdlib tests.

## 1.2.2 — the hub is not an agent, and precedence stops duplicating itself

Three defects that only appear on a machine with a real hub:

- **`sync` was linking the hub into itself.** `Hub.agents()` returned `~/.agents/skills`
  because it matches the `~/.*/skills` sweep, so the hub received one junction per skill —
  842 on this machine, each pointing at the category folder one level down. A junction is
  not a symlink, so git materialised them as copies of the hub inside the hub: that is why
  the hub repo could never take a first commit. The hub is now excluded by resolved path.
  Measured after: 842 links removed, `sync` creates 0, 843 skills and drift 0 unchanged,
  agents 50 → 49 (the hub was never an agent).
- **`precedence` touched every harness.** Asking for Hermes also edited Reasonix's
  `config.toml`. New `--harness` flag and `apply_precedence(..., only=...)`.
- **`precedence` duplicated the same path.** The comparison was a raw string match, so a hub
  already listed as `C:/Users/me/.agents/skills` got a second entry
  `C:\Users\me\.agents\skills` — the same root twice in one config, which is one skill loaded
  twice. Paths are normalised (separator and case) for YAML `external_dirs` and TOML `paths`.

78 stdlib tests, CI green on Python 3.9 / 3.11 / 3.13.

## 1.2.1 — the PyPI page stops contradicting itself

The 1.2.0 upload was built from the README *before* the `pip install` fix, so the project page on
PyPI announced "this is not on PyPI yet, so `pip install skillgram` would fail" right under a
`pip install skillgram` box. A published description cannot be edited, only republished.

- New `README.pypi.md` and `readme = "README.pypi.md"`: the long description is now Markdown-only
  (no raw HTML, which PyPI flattens into a line of alt texts) with absolute links, badges, what it
  is, what it is not, and where the docs live. The GitHub README keeps the centred logo header.
- `release.yml` moved to Trusted Publishing: `environment: pypi`, `id-token: write`, and
  `pypa/gh-action-pypi-publish` — no PyPI token and no secret in the repository.
- Same code as 1.2.0: this release exists to fix what the registry shows.

## 1.2.0 — the repo ships the skill that teaches an agent to report

- `skills/skillgram-report/SKILL.md`: a real, lint-clean skill — activation contract, hard
  rules, decision gates, execution steps, output contract — that tells an agent how to file a
  bug or a gap with evidence: capture the command, the exit code and one JSON line, read the
  harness config back **after** the command that claimed success, redact, then open the issue.
  A bug is never filed as a skill; a gap is never filed as a bug. It is the only skill this
  repo ships, and installing it is a copy plus `skillgram index` / `sync`.
- `docs/diagrams/skillgram-install.html`: the install flow as a standalone interactive diagram
  (source in `docs/diagrams/skillgram-install.json`): resolve the harness → verify the hub →
  probe → delegate the add → **re-read the config** → propagate → report, with the hard stops.
  Authored with archify; validation, delivery, strict check and the browser gate all pass.
  Its receipts and captures are gitignored: they are regenerable evidence, not source.
- README: both linked, with how to put the skill into your own hub.
- **Published as `skillgram` 1.2.0 on PyPI** (`pip install skillgram`), and as a site:
  <https://xhuk.github.io/skillgram/>. `.github/workflows/release.yml` builds `sdist` + wheel
  and uploads them on a published release (or by `workflow_dispatch`), with `--skip-existing`
  so a re-run cannot fail. Two metadata traps it cost to learn: PyPI rejects the legacy
  `license = { text = "MIT" }` table (PEP 639: `license = "MIT"` + `license-files`, no
  `License ::` classifier), and an invented classifier answers a bare `400` that only
  `twine --verbose` explains.

## 1.1.3 — issue templates (no behaviour change)

- `.github/ISSUE_TEMPLATE/bug_report.yml`: command, output + exit code, environment,
  optional `doctor --json`. A bug is never a skill.
- `.github/ISSUE_TEMPLATE/skill_request.yml`: the query you tried, what `missing` answered,
  when you would use it, and whether a portable `SKILL.md` is enough or the tool has to change.
- `config.yml`: no blank issues, and install failures are routed to `INSTALL.md` first.

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

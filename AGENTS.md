# AGENTS.md

Notes for any coding agent working on this repo.

## What this is

`skillgram.py` is a single-file, stdlib-only CLI. No dependencies, no framework.
Keep it that way unless there is a hard reason not to.

## Contract

- One JSON line per operation on stdout for machine commands; human output only
  for `doctor`, `onboard`, `status`, `init`, `use`, `unuse`, `project-status`, `seed`,
  `align`, `policy` and `clone`.
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

    skillgram/            the tool, one module per concern:
      hub.py              the store: Hub, links, content hash, HOME (via home_dir())
      drift.py            plan / apply / sync / status / clone
      scope.py            a project's set: manifest, use, mount, align, protocol, gitignore
      govern.py           seeds (taste) and the policy levels
      llm.py              the connected model, one call, one audit log
      lifecycle.py        list, touch, pin, review, retire, export, import, diff, git,
                          index, search, demand, lint, new
      select.py           signals, ranking, bootstrap, missing, propose
      harnesses.py        identity, detection, doctor, ledger, MCP registration, install
      adopt.py            inventory, onboard plan/apply, adoption, flavor, precedence
      serve.py            MCP stdio server, watcher, daemon, autostart
      cli.py              parser, dispatch, renderers, main
    skillgram/skills/     the two skills that ship WITH the tool (see below); package data,
                          so `install` can seed a fresh hub with no network
    skillgram.py          six-line shim so `python skillgram.py <cmd>` works from a
                          checkout; not shipped in the wheel
    tests/test_*.py       stdlib unittest
    ruff.toml, mypy.ini   the static gates CI runs

The dependency direction is a DAG with `hub` at the bottom and `cli` at the top: a module
imports only from the layers below it. `sync` and `onboard` are exported *functions*, so
their modules are called `drift` and `adopt` — otherwise `__init__` would shadow them.

### The bundled base

`skillgram/skills/<name>/SKILL.md` are the skills that travel inside the wheel:
`skillgram-report` (how to report a bug) and `anti-slop-guardrails` (how to de-slop a codebase so
it stays de-slopped). `install` seeds them into `<hub>/<category>/<name>/` and records them as
seeds (`always`) — a fresh machine used to land on an empty hub, which is a bad first minute.
Seeding never overwrites: a hub that already has the skill keeps the edited copy, and the row says
`present`. `install --no-bundled` opts out. The sidecar records `source.kind = bundled` plus the
version, so a shipped copy is distinguishable from an adopted one.

Adding one: put it in `skillgram/skills/<name>/`, add it to `BUNDLED` in `bundled.py` with its hub
category, and it must pass `lint --contract --strict` — a test enforces that
(`TestBundledSkills.test_the_shipped_skills_pass_the_gate_they_describe`).

`HOME` is read through `hub.home_dir()` and never captured at import time: there is one
patch point (`skillgram.hub.HOME`) and every module follows it. A module that keeps its
own copy of HOME is a bug, not a style choice.

## Before committing

    python -m unittest discover -s tests -v
    ruff check . && mypy
    python skillgram.py doctor --json
    python skillgram.py status --json
    python skillgram.py onboard --no-llm --json
    python skillgram.py install --dry-run

## 1.1 additions

- `install` (harness -> hub -> MCP -> propagate, idempotent) and `propagate`
  (git repo discovery + `~/.agents/skillgram-ledger.json`).
- The MCP server is registered **only** through the harness CLI
  (`reasonix|codex|claude|hermes|gemini mcp add`). Never edit a harness config
  by hand, and keep the argv shapes in `mcp_add_argv` as they are: they were
  checked against the real CLIs.
- Harness identity order is fixed: `SKILLGRAM_HARNESS` -> `--harness` -> process
  tree -> `unknown`. Never extend `PROCESS_HARNESS` with a generic runtime
  (`node`, `python`).
- **Never trust the CLI's exit code for the registration**: probe the config after
  `mcp add` (hermes exits 0 after cancelling its own Y/n prompt). `INTERACTIVE_ADD`
  is the small set of CLIs that ask before writing; answer the prompt there and
  re-probe, don't widen it on a hunch.
- `SKILLGRAM_MCP_CLI_<HARNESS>` is the only supported way to point at a launcher
  that is not the one on `PATH` (several homes of the same harness exist in the
  wild). Never edit `config.yaml`/`config.toml`/`.claude.json` to compensate.
- `INSTALL.md` is the human/agent entry point: keep its commands true to the CLI.

## 0.5 additions

- `index` / `search` (FTS5 at `~/.agents/skillgram-index.db`), `mcp` (stdio JSON-RPC).
- Lifecycle: `review`, `pin`, `retire` (`<hub>/_retired/` + `_retired.json` tombstones).
- `export` / `import` (tar.gz with a `hub.json` of hashes).
- `_`-prefixed directories in the hub are never treated as categories.
- Known gaps are listed in README ("Known gaps"); keep that list honest.
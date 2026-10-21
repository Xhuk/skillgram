# AGENTS.md

Notes for any coding agent working on this repo.

## What this is

`skillgram.py` is a single-file, stdlib-only CLI. No dependencies, no framework.
Keep it that way unless there is a hard reason not to.

## Contract

- One JSON line per operation on stdout for machine commands; human output only
  for `doctor`, `onboard`, `status`, `init`, `use`, `unuse`, `project-status`, `seed`,
  `align` and `policy`.
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
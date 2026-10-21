# Install skillgram (brief for a fresh agent session)

skillgram keeps **one** skill library on disk (the *hub*, `~/.agents/skills`) and links it
into each harness on demand. Nothing is copied into an agent's folder, an agent's native
skills are never touched, and every command is idempotent.

- Repo: `C:\proyectos\skillgram` (source of truth) — <https://github.com/Xhuk/skillgram>
- Entry point: `python C:\proyectos\skillgram\skillgram.py <command>`
- Requirements: Python 3.9+, no dependencies. Windows-first (junctions), also works on
  macOS/Linux (symlinks).

## Copy-paste this into a new session

```
Install skillgram on this machine (one skill hub, linked into every agent).

Repo: C:\proyectos\skillgram   (https://github.com/Xhuk/skillgram)
Entry point: python C:\proyectos\skillgram\skillgram.py

Do this, in order, and show me the output of each step:

1. python C:\proyectos\skillgram\skillgram.py install --dry-run
   Nothing is written. It prints the exact commands it would run and how it
   identified the harness. Read them back to me before continuing.

2. python C:\proyectos\skillgram\skillgram.py install
   Idempotent. It resolves the harness (SKILLGRAM_HARNESS -> --harness -> process
   tree -> unknown), verifies or creates the hub (~/.agents/skills), registers the
   MCP server through the harness' own CLI, and propagates to the current repo.

3. Do what the "to take effect" block says (Reasonix: /reload, Codex: restart the
   session, Claude Code: restart the CLI, Cursor: Reload Window), then verify:
     python C:\proyectos\skillgram\skillgram.py status
   and confirm the MCP tools skillgram_search / skillgram_missing / skillgram_status
   are available in the harness.

4. Optional, only if I ask for it: sweep the repos of this harness. Repos without a
   skill scope are reported one by one with the exact next command.
     python C:\proyectos\skillgram\skillgram.py propagate --harness <name> --root C:\proyectos --apply

Hard rules while you do this:
- Never edit a harness config file by hand. Registration goes through the harness CLI
  (reasonix|codex|claude|hermes|gemini `mcp add`); if a CLI is missing or broken, say so
  and stop, don't hand-write the config.
- Never copy skill files and never delete a real directory. Links only (soft mount).
- If install says `harness unknown`, do not guess: re-run with `--harness <name>`.
- If a repo has no skill scope, do not invent one. `skillgram use <skill>...` is the
  decision; `--bootstrap` lets the connected LLM propose a set (it never writes without
  approval).
```

## The same thing, by hand

```bash
cd C:\proyectos\skillgram

python skillgram.py install --dry-run     # preview: exact commands, nothing written
python skillgram.py install               # harness -> hub -> MCP -> propagate

python skillgram.py status                # drift, hub size, agents
python skillgram.py doctor                # harnesses, native skills, collisions
```

`install` prints four blocks: `done now`, `already in place`, `needs you` (only when
something is blocked) and `to take effect`.

### What it does, in order

1. **Harness identity**, never guessed: `SKILLGRAM_HARNESS` (the value install writes into
   the MCP registration) → `--harness` → the process tree → `unknown` (and it says so).
2. **Hub**: creates `~/.agents/skills` if it is missing, reports how many skills it holds.
3. **MCP server**: `skillgram mcp` is registered as the `skillgram` server by delegating to
   the harness' own CLI, with `SKILLGRAM_HARNESS` and `SKILLGRAM_HUB` in the server
   environment. Already registered → `mcp get` says so and nothing is touched. After
   adding, the config is **probed again**: a CLI that exits 0 without writing anything is
   reported as a failure, never as success.
4. **Propagation**: `mount --apply` (project scope only) + the protocol block in
   `AGENTS.md`/`CLAUDE.md` + an entry in `~/.agents/skillgram-ledger.json`, for the repos
   of that harness that are not in the ledger yet.

### Environment switches

| Variable | What it does |
|---|---|
| `SKILLGRAM_HARNESS` | harness identity when nobody set it (written by `install`) |
| `SKILLGRAM_HUB` | hub root (default `~/.agents/skills`); also passed to the MCP server |
| `SKILLGRAM_LEDGER` | ledger path (default `~/.agents/skillgram-ledger.json`) |
| `SKILLGRAM_MCP_CMD` | exact command a harness should spawn (default: `python skillgram.py mcp`) |
| `SKILLGRAM_MCP_CLI_<HARNESS>` | the launcher that owns that harness' config, when the one on `PATH` is not the right one (e.g. `SKILLGRAM_MCP_CLI_HERMES=C:\iasdk\hermes\bin\hermes.cmd`) |
| `SKILLGRAM_PROJECT` | project root, engram-style (`--project` equivalent) |
| `SKILLGRAM_LLM_BASE_URL` / `SKILLGRAM_LLM_MODEL` / `SKILLGRAM_LLM_API_KEY` | the optional LLM for bootstrap/port/propose (`DEEPSEEK_API_KEY`/`OPENAI_API_KEY` also work) |

### When a harness has more than one launcher

A shared install plus an isolated home (or a venv shim next to a self-updating one) means
the `hermes`/`claude` on `PATH` may not be the launcher whose config you want written.
Say which one owns the config with `SKILLGRAM_MCP_CLI_<HARNESS>`: `install` uses it for the
probe, the `mcp add` and the `mcp remove` hint, so everything stays consistent.

```powershell
$env:SKILLGRAM_MCP_CLI_HERMES = 'C:\iasdk\hermes\bin\hermes.cmd'   # an isolated HERMES_HOME
```

### Which CLI owns the MCP config

| Harness | Delegated command | Project skill dirs | Takes effect |
|---|---|---|---|
| Reasonix | `reasonix mcp add` | `.agents/skills` | `/reload` or restart |
| Claude Code | `claude mcp add -s user -e K=V --` | `.claude/skills` | restart the CLI |
| Codex | `codex mcp add --env K=V --` | `.codex/skills` | restart the session |
| Hermes | `hermes mcp add --command ... --args ...` | `.agents/skills`, `.hermes/skills` | restart the session |
| Gemini CLI | `gemini mcp add -e K=V` | `.gemini/skills` | restart the CLI |
| Cursor | no CLI: register from Settings > MCP | `.cursor/skills` | Reload Window |
| Copilot / Kiro / Qwen / Aider / OpenCode | no MCP CLI | `.github`, `.kiro`, `.qwen`, `.aider`, `.opencode` `/skills` | — |

Two traps this repo already paid for, in case a harness misbehaves:

- Reasonix' legacy `mcp add` form only honours `--env=K=V` (the spaced `--env K=V` is
  swallowed into the command) and rejects a `--` separator.
- `shutil.which("skillgram")` can resolve to `./skillgram.py` or to a directory, so the
  launch command is validated as a real executable before it is registered.
- Hermes' `mcp add` is interactive ("Enable all N tools? [Y/n/select]") and exits **0**
  even when its prompt is cancelled, saving nothing. skillgram answers that prompt and
  then re-reads the config, so a cancelled registration can never look successful.

## Verify

```bash
python skillgram.py status --json          # one JSON line: drift, skills, agents
reasonix mcp get skillgram                 # the registration reasonix wrote
claude mcp get skillgram                   # same for Claude Code
codex mcp get skillgram                    # same for Codex
hermes mcp list                            # same for Hermes (with its own HERMES_HOME)
```

Inside the harness, ask the hub before writing a skill:

```
skillgram_missing "capability I need"   -> found:false means: do not invent one silently
skillgram_search  "docker deploy"       -> full-text over the hub
skillgram_status                        -> hub health + project scope
skillgram_touch   <skill>               -> declare what you used (the only usage signal)
skillgram_list    --used                -> what this project has used, and from which harness
skillgram_use     <skill>               -> promote it; records the harness that asked
```

`mount` injects that loop into the repo's `AGENTS.md`/`CLAUDE.md` between
`<!-- skillgram:protocol:begin -->` markers, so a new session in a new harness reads it without
anyone explaining it again. Say what you always want recommended once, in the hub:

```bash
skillgram seed qring-anti-slop          # always recommended, every repo
skillgram seed <skill> --never          # never recommended, any repo
```

## Uninstall

`skillgram` never owns your content, so uninstalling is just removing the wiring:

```bash
reasonix mcp remove skillgram       # or codex/claude/hermes mcp remove skillgram
skillgram unuse <skill>...          # per project, drop it from the scope
skillgram seed <skill> --remove     # undo a taste decision
```

`mount` also leaves a delimited block in the repo's `.gitignore` (the directories it links, so
git never sees the skills as copies) and the protocol block in `AGENTS.md`; both are marked and
safe to delete by hand.

The hub at `~/.agents/skills` stays: it is your library, not skillgram's.

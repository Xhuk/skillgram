# skillgram

**One skill library, linked into every coding agent.** No copies, no drift, no
"which folder does this harness read?".

skillgram keeps a single directory of skills (the *hub*) and puts a junction (or
symlink) per skill inside each agent's own `skills/` folder. Every harness —
Claude Code, Codex, Cursor, Hermes, Reasonix, Gemini CLI, OpenCode, Kiro, Qwen
Code, Aider, DeepSeek harnesses, and anything else that reads `SKILL.md` — sees
the same library.

**skillgram ships zero skills.** It is the plumbing, not the content.

```
~/.agents/skills/<tipo>/<skill>/SKILL.md     the hub (the only real copy)
~/.claude/skills/<skill>                     -> junction to the hub
~/.codex/skills/<skill>                      -> junction to the hub
~/.cursor/skills/agent-workflows             -> junction to the category
%APPDATA%/Reasonix/skills/<skill>            -> junction to the hub
```

## Why

Linking by hand once is easy. Keeping 50 agents in sync is not: agents re-create
their own copies, rename folders, and a new hub skill never shows up by itself.
skillgram turns that into one idempotent command (and, with `watch`, into no
command at all).

## Install

```bash
pip install skillgram
```

From source:

```bash
git clone <this repo>
cd skillgram
python skillgram.py --help
```

Stdlib only (Python 3.9+). Windows-first (junctions + `schtasks`), works on
macOS/Linux with symlinks.

## Onboarding: it finds the skills you already have

```bash
skillgram onboard              # asks: unify ALL harnesses or just the current one?
skillgram onboard --scope all --json --plan plan.json     # review first
skillgram onboard --scope all --apply                      # import into the hub + link everything
```

`onboard` inspects **every installed harness** (any `~/.<name>/skills/` plus
`%APPDATA%/Reasonix`), lists each native skill and asks what to do with them:

- `--scope all` → consolidate the skills of every harness you have.
- `--scope current --harness <name>` → only that one.
- No flag and a real terminal → it asks.

### The connected LLM does the dirty work

If there is an OpenAI-compatible model available, `onboard` uses it to classify
what it found: pick the hub category per skill, group byte-different duplicates,
and label the **flavor** (`generic`, `claude`, `codex`, `hermes`, ...) by reading
whether the skill text hard-codes one agent's CLI.

```
SKILLGRAM_LLM_BASE_URL   default https://api.deepseek.com/v1
SKILLGRAM_LLM_MODEL      default deepseek-flash
SKILLGRAM_LLM_API_KEY    or DEEPSEEK_API_KEY / OPENAI_API_KEY
```

The LLM **never writes files**. It proposes a JSON plan; skillgram validates it
(categories must exist, skills must be real) and only then applies it. Turn it off
with `--no-llm`.

### What "apply" actually does

1. Copies each native skill into `<hub>/<categoria>/<skill>`.
2. Verifies the copy has the same content hash; if not, removes the copy and
   reports a failure. Nothing is silently lost.
3. Runs `sync`, so every harness gets links.

By default the **original native copy stays where it was** (so `sync` reports it as
a *collision* and skips it). To really unify, add:

```bash
skillgram onboard --scope all --apply --replace-natives
```

That moves the original into `~/.agents/skillgram-quarantine/<harness>/<skill>`
(never deletes it) and leaves a junction in its place.

## What skillgram does NOT do (on purpose)

Agents that already ship their own skills — Hermes bundles dozens — keep them
exactly as they are:

- `sync` only **adds** links. It never copies, moves, adopts or deletes native skills.
- A native skill with the same name as a hub skill is **skipped** (reported as
  *collision*), never overwritten.
- `doctor` tells you how many native skills each agent has, before you touch anything.

## Project scope (engram-style)

The hub stays the only store; a project gets a *view* of it.

```bash
skillgram init my-api                 # <project>/.agents/config.json
skillgram use archify ad3 --project . # junction + manifest, idempotent
skillgram project-status              # sane / stale / broken / missing links
skillgram unuse archify
```

```
<project>/.agents/config.json    {"project_name": "my-api"}
<project>/.agents/skills/<name>  junction to the hub
<project>/.agents/skills.json    manifest: tipo, source, hash, promoted_at
```

Resolution order (same as engram):

```
--project  ->  SKILLGRAM_PROJECT  ->  nearest .agents/config.json up to the git root  ->  cwd
```

If it cannot resolve, it says so. It never guesses.

`project-status` also flags **duplicated local skills**: a skill that lives both
in the hub and as a real copy inside the project (promote it, delete the copy).

## Commands

| Command | What it does |
|---|---|
| `doctor` | installed harnesses, native skills per agent, collisions, dead/missing links. Read-only. |
| `onboard` | detect harnesses and consolidate their skills into the hub (LLM optional). |
| `status` | current drift. `--verify` also checks where each link points. |
| `sync` | apply the desired state. Idempotent. `--dry-run` to preview. |
| `watch` | poll the hub and re-sync on add/remove/rename. |
| `daemon` | JSON-line server over stdin/stdout, for supervisors. |
| `init [name]` | write `<project>/.agents/config.json`. `--force` to replace. |
| `use <skill>...` | promote hub skills into the project (junction + manifest). |
| `unuse <skill>...` | remove them from the project scope. |
| `project-status` | health of a project scope. |
| `install-autostart` / `uninstall-autostart` | login task (Windows) that keeps `watch` alive. |

Every command prints **exactly one JSON line** on stdout (`--json` for the human
ones too). Exit code `0` healthy, `1` drift/error, `2` bad invocation.

```json
{"ok":true,"op":"status","version":"0.1.0","hub":"C:\\Users\\me\\.agents\\skills","skills":841,"tipos":18,"agentes":50,"drift":0}
```

## How agent discovery works

`doctor`, `sync` and `onboard` look for `skills/` folders under every
`~/.<something>/` (plus `%APPDATA%/Reasonix`). Known harnesses are labelled
(`claude-code`, `codex`, `cursor`, `hermes`, `gemini-cli`, `opencode`, `copilot`,
`windsurf`, `kiro`, `qwen-code`, `aider`, `antigravity`, `kilo`, `commandcode`,
`reasonix`); anything else with a `skills/` folder is treated the same way.

Each agent gets two kinds of links:

- **one per category** (`skills/<tipo>` -> `hub/<tipo>`): browsable.
- **one flat per skill** (`skills/<skill>` -> `hub/<tipo>/<skill>`): for harnesses
  that only scan one level.

## Safety

- Links only. `sync` removes dead junctions **that point into the hub** — never
  content, never a real directory.
- `onboard` copies and verifies the hash before reporting success; the original
  only moves to quarantine with `--replace-natives`.
- Never touches your repos unless you run `init` / `use`.
- Preview anything: `sync --dry-run`, `onboard --json --plan plan.json`.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT.

## Mounting model (v0.2)

**soft-mount + precedence + auto-adoption.** Nothing is moved or replaced by default:

- `skillgram precedence` declares the hub **first** in each harness's own config
  (`skills.paths` for Reasonix, `skills.external_dirs` for Hermes). Backups are
  written next to the file (`*.skillgram-bak-<timestamp>`). If you delete
  skillgram, every harness keeps exactly what it had.
- `skillgram adopt <path>` (or `--all`) is the cooperative "hijack": copies the
  native skill into the hub, verifies the hash, moves the original to
  `~/.agents/skillgram-quarantine/<harness>/<skill>` and leaves a junction in its
  place. Never deletes.
- The hard-mount model (one junction for the whole `skills/` dir) is **not** the
  default on purpose: a `rimraf skills` or a harness uninstall would wipe the
  whole library of every agent.

## Flavor and porting

The hub keeps **everything**; scope is not trimmed per harness. What changes is
the wording:

```bash
skillgram flavor                 # detect which agent each skill is wired to
skillgram flavor --apply         # persist it in SKILLMETA.json (SKILL.md untouched)
skillgram port --apply           # generate agents/<harness>.md where a port is missing
skillgram port my-skill --to reasonix --apply
```

`skillgram port` asks the connected LLM to translate the skill for the target
harness (CLI names, subagent mechanics) and writes **only** `agents/<harness>.md`
inside the skill. The original stays generic, so any harness can still read it;
the ported fragment is what the target harness should follow.

## Scope: the project is the unit

The hub is the library; a *project* is what an agent actually needs while working
in it.

```bash
skillgram init                 # project identity
skillgram use <skill>...       # this project needs these
skillgram project-status       # drift of that scope
skillgram mount                # materialize the project scope into each harness
```

`mount` writes the project's set where harnesses look **per project**
(`<project>/.agents/skills`, `<project>/.claude/skills`, `<project>/.cursor/rules`),
so two different agents working on the same repo see the same skills without
depending on their global homes.
### Strict project-only (default target)

The hub holds the library (900+ skills exist), but a harness working inside a
project sees **only that project's set** — 20 skills, not 900:

```bash
skillgram mount --apply              # materialize the project scope per harness
skillgram mount --apply --prune-global   # strict: also drop the hub links from ~/.<harness>/skills
```

`mount` writes links only inside the project, in every place a harness looks when
it works there:

| Harness | Project dir |
|---|---|
| Hermes | `.agents/skills`, `.hermes/skills` |
| Claude Code | `.claude/skills` |
| Codex | `.codex/skills` |
| Cursor | `.cursor/skills` |
| Gemini CLI | `.gemini/skills` |
| Copilot | `.github/skills` |
| Kiro / Qwen / Aider / OpenCode | `.kiro/skills`, `.qwen/skills`, `.aider/skills`, `.opencode/skills` |

So two different agents working on the same repo see the same skills, and
skillgram's only job is: be the hub, and fill the gaps (adopt what a harness
installs, port the flavor when a skill is wired to another agent).

`--prune-global` is the strict switch: it removes the hub links from the harness
homes (only links that resolve into the hub; native skills stay). After that, a
harness outside a project sees nothing from the hub — by design.
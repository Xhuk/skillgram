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
## Two modes: bootstrap (LLM decides) and régime (skillgram only serves)

skillgram is explicit about this: **a connected LLM is used to administer the
library**, and only in two places — choosing a project's initial set, and writing
a skill that does not exist yet. It never deletes, and never writes without
approval. Every automatic decision is stamped `by: llm:<model>` in the project
manifest and logged to `~/.agents/skillgram-llm.log`. `doctor` reports which model
is connected; `--no-llm` and `SKILLGRAM_LLM=off` turn it off.

### Bootstrap — a repo with no scope yet

```bash
skillgram bootstrap            # asks for confirmation; add --apply to register
skillgram bootstrap --apply    # register the chosen set in this project
```

1. **Deterministic signals** from the repo (`package.json`/deps, `go.mod`,
   `Dockerfile`, `compose`, CI, `prisma`, `.env.example`, ...).
2. **Catalog filter**: 900 skills → ~50 candidates by category, name and text match.
3. **The LLM picks the set and justifies each one**, plus the gaps it sees:
   `{"selected":[{"skill":"docker-patterns","why":"repo has Docker"}], "gaps":["CI config"]}`.
4. `--apply` registers them with `why` + `by` in `.agents/skills.json`.

### Filling a gap

```bash
skillgram propose "github actions CI for a dockerized node app"           # draft only
skillgram propose "github actions CI for a dockerized node app" --apply   # publish + register
```

The LLM writes the `SKILL.md` (frontmatter + Activation Contract / Hard Rules /
Decision Gates / Execution Steps / Output Contract). skillgram **validates** it
(frontmatter, unique name, non-empty body), publishes it into the hub, registers it
in the project and mounts it. Nothing is published without `--apply`.

### Régime — a project that already has skills

No LLM. skillgram just serves the set and answers "do we have something for this?":

```bash
skillgram missing "deploy to a vps"
# {"found": true, "candidates": [{"skill": "docker-local-vps-deploy", "score": 3}]}
skillgram missing "quote a mortgage"
# {"found": false, "candidates": []}
```

When `found` is `false`, skillgram stops there: **the agent** (Hermes, Claude,
Codex, Reasonix, ...) is the one that proposes something to the user. skillgram
only administers.
## Search, lifecycle, portability and MCP (0.5)

```bash
skillgram index                          # FTS5 index over the hub (~/.agents/skillgram-index.db)
skillgram search "docker deploy vps" --limit 5
skillgram mcp                            # MCP server (stdio) for the agent
```

MCP tools exposed: `skillgram_search`, `skillgram_missing`, `skillgram_use`,
`skillgram_status`, `skillgram_propose`. With this, an agent asks the hub **as a
tool call** instead of shelling out: `missing` returning `{"found": false}` is the
contract that says "do not invent, tell the user".

Lifecycle (answers "is this skill still alive?"):

```bash
skillgram review --days 90        # never used / not used recently
skillgram pin <skill>...          # pinned skills never show up as stale
skillgram retire <skill>          # moves it to <hub>/_retired/ + tombstone, never deletes
```

`use` now stamps `last_used`, `use_count` and `last_used_by` in the project manifest.
`_retired/` keeps the content plus `_retired.json` (who/when/why/hash), and `_retired`
is never treated as a category.

Portability:

```bash
skillgram export hub.tar.gz       # tar.gz with every skill + hub.json of hashes
skillgram import hub.tar.gz       # verifies hashes; --apply to materialize
```

## Known gaps (documented, not implemented)

Compared with Engram (Gentleman-Programming), which is a service with full lifecycle,
these remain open by decision:

1. **No cross-machine sync.** `export`/`import` is manual: there is no transport
   (git/cloud) and no conflict resolution between two hubs.
2. **No merge of divergent skills.** Same name + different content fails instead of
   merging; a merge has to be proposed by hand (or by the LLM) and applied.
3. **No real usage telemetry.** `use_count` counts explicit promotions and MCP `use`
   calls; skillgram does not observe which skills an agent actually reads.
4. **Lexical search only.** FTS5 has no embeddings, so synonyms across languages still
   miss (measured: `"cotizar credito hipotecario"` → 1 weak hit).
5. **No permissions/profiles.** No agent/admin split: anyone with a shell can publish
   or retire. Engram separates tool profiles.
6. **No skill quality gate.** `propose` validates frontmatter and uniqueness, not
   whether the skill is any good (no lint, no eval).
7. **No scheduled review.** `review` is manual; there is no cadence, cron or
   notification when a project's set goes stale.
8. **MCP is tools-only.** No `resources`, no `prompts`, no sampling.
## Language

Everything user-facing is **English**: the CLI output, error messages and every
JSON key (`promoted`, `already_present`, `missing_links`, `native`, `collisions`,
`signals`, `selection`, ...). That is the API other tools read.

Internal code comments and docstrings are still partly Spanish; translating them is
tracked as cleanup, not as behaviour.
## Demand log and a git-backed hub (0.8)

```bash
skillgram missing "quote a mortgage"   # every call is recorded
skillgram demand                       # what the hub could NOT cover, ranked by times asked
```

`demand` is the roadmap: the queries nobody could answer. `covered_queries` counts
the ones the hub did answer, so you can see the ratio. (In this workspace, `quote a
mortgage` showed up 3 times across sessions — that is a skill waiting to be written.)

```bash
skillgram git status        # is the hub a repo? dirty?
skillgram git init --apply  # make the hub a git repo (adds .gitignore for the index)
skillgram git pull --apply  # ff-only: git resolves conflicts, skillgram does not
skillgram git push --apply
```

The hub is the asset. With git under it, "sync between machines" is git's job —
skillgram only reports and pushes. Conflicts are resolved by git, never by
skillgram guessing which skill version wins.

## Remaining gaps

1. No semantic search (FTS5 only): cross-language synonyms still miss.
2. No merge of divergent skills: same name + different content is a conflict to resolve by hand.
3. No permissions/profile split (agent vs admin). `mcp` could grow a `--read-only`.
4. No scheduled review; run `skillgram review --days N` from your own cron/CI.
5. MCP is tools-only (no resources/prompts/sampling).
## Read-only MCP, CI gate and diff (0.9)

```bash
skillgram mcp --read-only     # the agent can query the hub but cannot use/propose
skillgram review --days 30 --fail-if-stale   # exit 1 when the project set is stale (CI gate)
skillgram diff <skill> --against <path>      # divergence between two copies of the same skill
```

`diff` is what you reach for when the hub lives in git and two machines changed the
same skill: it tells you which files differ, which exist only here and which only
there. Merging stays a human (or LLM) decision — skillgram never guesses.
## `mount` from any repo

`mount` resolves the project like engram (cwd → nearest `.agents/config.json` up to
the git root), so you can run it from **any repo, any subdirectory**. If that repo
has no skills in scope yet, `mount` offers to bootstrap it with the connected LLM:

```bash
cd ~/code/my-repo/subdir
skillgram mount --apply --yes     # no scope -> LLM picks the set -> mount -> protocol
skillgram mount --apply --no-bootstrap   # never bootstrap, only mount what exists
```

Then it propagates that set to every harness that reads project-level dirs, and
writes the skillgram protocol into `AGENTS.md`/`CLAUDE.md`.
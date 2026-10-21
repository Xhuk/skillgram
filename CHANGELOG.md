# Changelog

## 1.9.0 (unreleased in PyPI) — the file became a package, the gates exist, and it ships skills

Two skills travel inside the package now, and `install` seeds them into the hub:
[`skillgram-report`](skillgram/skills/skillgram-report/SKILL.md) and
[`anti-slop-guardrails`](skillgram/skills/anti-slop-guardrails/SKILL.md) — a review checklist, a
behaviour-preserving refactor protocol, and the gates-plus-baseline habit that keeps a cleanup
from washing away. The second one is this repo's own refactor, generalised.

- **A fresh machine is no longer an empty hub.** `install` used to block on one; it now copies
  the shipped base into `<hub>/<category>/` and records it as seeds (`always`). Idempotent and
  never overwriting: an edited copy stays edited and reports `present`. `install --no-bundled`
  keeps the old behaviour, blocked message included.
- **`skill_hash` means content, not bookkeeping.** It now ignores `SKILLMETA.json` (which carries
  a copy of the hash, so counting it made the value false the moment it was written) and
  normalises line endings (the same skill checked out on Windows and on Linux used to hash
  differently, which would call a whole library stale on the other machine). Manifests written
  before this report `stale` once; re-run `use`/`mount`.
- `SKILLMETA.json` gains `source.kind = bundled` with the version it came from.
- The provenance helpers (`SKILL_META`, `skill_meta`, `write_skill_meta`) moved to `hub.py`:
  only the base layer can write them without an import cycle.
- **The lint gate stopped lying in three places**, all found while repairing the AD3 pack:
  - it reads YAML block scalars (`description: >-`), so a description written the readable way
    is no longer measured as the two characters `>-`;
  - a placeholder is a *marker* (start of line, table cell, the whole description), not the
    word: "mark unknowns as TBD with an owner" is an honest rule and now passes;
  - a `../<skill>/` link is checked against the hub: 6 of 847 skills pointed at a sibling
    category and nothing else. Naming the skill is the fix, and `lint` says which category.
- 193 tests (`TestBundledSkills`, `TestLintParsesWhatWeWrite`), `ruff` and `mypy` clean.



The score that was lowest was "maintainability: 5294 lines in one file, zero static
analysis". Both are gone.

- **One module per concern.** `skillgram/` with `hub`, `drift`, `scope`, `govern`, `llm`,
  `lifecycle`, `select`, `harnesses`, `adopt`, `serve`, `cli`; dependencies form a DAG
  with `hub` at the bottom, and `__init__` re-exports the whole old surface so
  `import skillgram` keeps working. `skillgram.py` stays as a six-line shim for
  `python skillgram.py <cmd>` from a checkout, and is not shipped in the wheel.
- **`HOME` is read, not captured.** `hub.home_dir()` is the single patch point; before
  the split, every module would have kept its own copy and a test that moved HOME would
  silently leave half the tool on the real one (that bug cost an afternoon: the suite
  "hung" scanning 50 real harness homes).
- **Static gates in CI**: a `quality` job runs `ruff` (pycodestyle, pyflakes, bugbear,
  import order, complexity) and `mypy`, and builds + installs the wheel to prove the
  packaging. They found real things on the first run: a dead local, an `l` variable, a
  `dict` literal with `"ok"` written twice (the second silently won), `os.path.isjunction`
  used on 3.9 without the `getattr` guard, and two string ops on `object`.
- **`lint --baseline FILE` / `--write-baseline FILE`**: a corpus with legacy failures
  needs a gate that can be green. The baseline records the accepted debt; only new
  failures exit 1.
- **The two untested surfaces now have tests**: `watch` (a real polling thread that links
  a skill that appears after it starts) and `install` against a **real** harness CLI when
  one is on `PATH` — read-only: it asks the CLI whether it understands the verb we build,
  and never writes a config.
- 172 tests (4 new), `ruff` and `mypy` clean, wheel builds and imports.

## 1.8.0 (unreleased in PyPI) — `clone` between machines, and `align --hashes`

`export`/`import` moved a hub as a tarball; the README called git under the hub "the
workaround". It is the actual answer, so it now has a command with the safety rails.

- **`skillgram clone <url> [--hub PATH] [--ref BRANCH|TAG] [--force] [--no-sync]`** — git
  does the transport (and the auth for a private repo); skillgram verifies what arrived
  and, unless `--no-sync`, links the harnesses so the skills are usable immediately. It
  reports `skills`, `categories`, `commit` and `origin`.
- **Nothing is overwritten.** Same origin → `pull --ff-only`; another origin → refused
  unless `--force`; a non-empty folder that is not a repo → refused unless `--force`, and
  then it merges without clobbering (`conflicts` names what it kept).
- **`align --hashes`** answers "which version does each harness read": per skill, the hub
  hash, the hash the project recorded at `use` time (`stale` when the hub moved), and per
  harness `link` / `native <hash>` / `absent`. A link cannot diverge — it *is* the hub's
  content — so the only real divergence is a forked copy or a stale manifest, and both
  are now visible in one table.
- 10 tests (7 for clone against a real git repo: clone, pull, other origin, forced merge
  that keeps yours, a repo that is not a hub, the CLI; 3 for the hash table), 168 total.


`docs/POLICY.md` described two commands. `align` shipped in 1.6.0; this is the other half,
with the enforcement the document promised and none of the things it refused.

- **Three levels, monotonic merge.** Organization (`SKILLGRAM_POLICY` or `--policy`: a
  path, or `url#ref` of a git repo, cloned shallow into `~/.agents/policy-cache` and
  hashed), project (`<repo>/.agents/policy.json`), user (`~/.agents/policy.json`). The
  effective verdict is the union of the restrictions, so a project file cannot undo an
  organization `deny` — the one property that makes the word policy mean something.
- **Shape as signed:** `mode: open|closed`, `allow` (with `!pattern` re-includes, last
  match wins), `deny` (beats allow, same level and above), `require` (the baseline),
  `harnesses` accepted, validated and **not interpreted** (reserved), `source` for origin.
  An unreadable file is an error, never a silent pass.
- **`policy` / `policy check`.** `show` prints every level with its hash (what an audit
  quotes) and the merged `require`; `check` names each violation with the rule that
  produced it plus the `require`d skills the project is missing, and exits 1.
- **The doors refuse.** `use` rejects the skill and keeps the allowed ones
  (`denied: [{skill, rule}]`); `mount` drops it from the set (`policy_skipped`); `sync`
  does not create the link and removes one that already exists — that is what `deny`
  means, and it is reversible by allowing it again. `touch` is never blocked: recording
  that something was used is evidence, not access. `require` is enforced by `check` and
  reported, not by `mount`: a baseline is a statement about a scope, and the scope is the
  developer's until CI says otherwise.
- **11 tests** (levels, hash, `!` re-include, closed mode, monotonicity, require,
  malformed file, reserved key, the CLI, the three doors, `touch` untouched), 158 total.

### Building notes that changed the design

- `--fail` was dropped: a check that has to be told to fail is a check that lies by
  default. `policy check` exits 1 on its own.
- `sync` removing a denied link was not in the draft. It follows from `deny`: if the
  policy says the skill must not be exposed, a link that already exists is drift.


`main` carries it and CI is green; the index still serves 1.5.0 on purpose. It goes out
together with `policy` (see [docs/POLICY.md](docs/POLICY.md)), so the release notes and
the docs do not have to be rewritten twice.

The tool could tell a project what it had, and could paint that set into every harness
layout — but it could not answer the question a multi-harness developer actually asks:
*what does each of my agents see of this repo, and where is it short?*

- **`skillgram align [--project P] [--apply]`** — one row per installed harness with
  `mode` (`project` / `mixed` / `global-only`), `via_project` and `via_home` counts, the
  skills it is `missing`, the ones it sees that are not in the set (`extra`, capped and
  counted), and the `native` directories it will not touch. `fix` says who can repair a
  gap: `mount` (it has a project dir) or `sync` (its only view is the home).
- **`--apply` only adds.** It links the missing skills into the harness layouts that
  support one; an `extra` is reported and never removed, a native directory is never
  modified. Exit `1` while anything is still missing, so it works as a check.
- The rule it encodes, and the reason it is not a merge: **the project set is the only
  source**. A harness that sees more is a violation to report, not a suggestion to
  propagate — the union of every harness is the 850-skill problem the tool exists to
  solve. Bringing a native into everyone's view is `adopt` → `use` → `align`, a decision
  with a name on it.
- Real numbers on a mounted repo: the 14 harnesses with a project dir show
  `via_project 11 / missing 0`, while the global-only ones show the whole hub as `extra`
  (851) — which is exactly the asymmetry the command makes visible.

6 tests, 142 total; 4 junction tests skip on POSIX.

## 1.5.0 — taste is a decision, not a score

`bootstrap` ranked the hub with a flat count of tags found anywhere in the name, the
category or 1200 chars of `SKILL.md`, so a weak mention tied with a real match and the
tie broke alphabetically. On a Go+Vite repo that put `apple-meta-craft` and
`ad3-recon` in the top twelve. And there was no way to say "this one, always": if you
like a skill, the score could still outvote you.

- **`skillgram seed <skill>...`** — the hub-level list of skills that are *always*
  recommended (`~/.agents/skillgram-seeds.json`). `--never` is the opposite list and
  removes the skill from every recommendation, `--remove` takes it out, and
  `skillgram seed` alone prints both lists. An unknown name is reported, not seeded.
- **Seeds win.** They come first in `bootstrap`'s candidates whatever their score,
  they are promoted by `bootstrap --apply` **even with no LLM configured** (a standing
  decision should not need a model call), and the model may only add to them, never
  drop them. `list` marks the rows (`seed: true`).
- **`project-status` recommends the missing ones.** `seeds_missing` lists the seeds the
  project does not have, with the command to add them. It never turns `ok` false: a
  recommendation is not drift.
- **The ranking is weighted and precise.** A token in the name is worth 3, in the
  category 2, in the body 1; anything under 2 is dropped instead of padding the list;
  and a tag matches a *word*, not a fragment, with aliases where the tag is not the
  word (`go` → `go`/`golang`, `ts` → `ts`/`typescript`). On a Go+Vite repo the top
  candidates went from `apple-meta-craft, ad3-recon, azure-kubernetes` to
  `react-testing, react-patterns, react-performance, react-build-resolver, go-testing`.

136 stdlib tests.

## 1.4.3 — two defects a real repo found in the first ten minutes

Both showed up mounting a real monorepo (vetgroom-demo: Go backend, Vite/React
frontend, Supabase, Dokploy, Katra auth).

- **`mount` polluted `git status`, and git would have committed the links.** The
  links it writes inside the project are directories to git, so a `git add -A` turns
  every mounted skill into a real copy in the repo — the one thing skillgram promises
  never to do. `mount` now keeps a delimited block in `.gitignore` with the dirs it
  writes (one per harness plus the project scope), idempotent and reversible, and the
  scope itself (`.agents/config.json`, `.agents/skills.json`) stays tracked because it
  is the memory the next harness reads.
- **`bootstrap` ranked noise.** It read only the root `package.json` and turned every
  dependency into a tag, so on vetgroom-demo (whose root `package.json` carries a
  flattened lockfile) the top signals were `ansi-regex`, `bare-fs`, `ieee754` and the
  real stack was invisible. Signals now come from the root *and its immediate
  subdirectories* (`backend/go.mod`, `frontend/package.json`) and only dependencies
  that name a known framework become tags; scoped packages count by scope
  (`@stripe/x` → `stripe`). Two-character tags (`go`, `ts`) are no longer dropped.
- 5 tests: a monorepo reports its real stack, a lockfile in dependencies does not
  become a stack, short tags survive, and the ignore block is written once, keeps the
  existing rules and never hides the scope.

125 stdlib tests.

## 1.4.2 — the usage record names the harness, not a constant

`by_harness` shipped in 1.4.1 as "which agent used this", and two of the three paths that
write it could not know: `skillgram use` recorded the literal `agent`, and the MCP
`skillgram_touch` recorded the literal `mcp`. So "which skills was I using in the other
harness" was answered by a bucket called `agent`.

- `caller_identity()` resolves the harness the way `install` does (`SKILLGRAM_HARNESS` →
  `--harness` → the process tree) and falls back to the caller's own default when nothing
  knows. It never invents a name.
- `use` records that identity (`--by` overrides it) and the MCP `skillgram_use` records it
  too — the MCP server runs *inside* the harness, so the process tree names it there as
  well. `skillgram_touch` now defaults to it instead of `mcp`.
- Names are canonical (`normalize_harness`), so `claude` and `claude-code` land in one bucket.
- 4 tests: the resolved identity, the `agent` fallback, `--by` winning, and the MCP path.

120 stdlib tests.

## 1.4.1 — the loop is feedable, the CLI works, and the harness is not the identity

Four defects found while reviewing the CLI surface (the code `main` runs exactly once, and whose
reporters read keys that must exist), plus the two surfaces that had never run, plus the hole that
made 1.4.0's "declared usage" unfillable.

- **`onboard --apply` was dead**: `apply_onboard` and `llm_refine_plan` read a `categoria` key that
  `build_onboard_plan` never writes (`category`), so the flagship command raised `KeyError` on the
  first import — with and without an LLM. The plan now carries one spelling of each key
  (`category`, `reason`), and the human report shows the reason it was hiding before.
- **`onboard` exited 1 on a healthy plan**: the plan had no `ok`, so `main` read `False`. A plan is
  a result, not an error — `precedence` and `bootstrap` already behaved that way. The `--apply` run
  still exits 1 when an import fails.
- **`--hub`, `--json` and `--quiet` only worked after the subcommand**: `skillgram --hub X status`
  died inside argparse (`invalid choice`). They are attached to the top-level parser now, with the
  subparser copies at `SUPPRESS` so they cannot overwrite the value already parsed.
- **The CLI is English again** (0.6.0 promised it): the Spanish strings that had crept back into
  `--help`, the doctor recommendations, the onboard report and every error/skip message are gone.
  Comments and docstrings were never part of that promise and stay as they are. The legacy Spanish
  **JSON keys** (`resolucion`, `tiene_config`, `llm_usada`, `aplicadas`, `reemplazo`, `creados`,
  `arreglados`, `drift_antes`, `drift_despues`, `nativos_sin_tocar`, `ocupadas_sin_enlace`,
  `links_sin_registrar`, `tipo`) are untouched on purpose: renaming them breaks consumers and
  belongs to a version, not to a cleanup.
- **The injected protocol now closes the loop.** It says to read the project's record *before*
  choosing (`skillgram list --project . --used`, MCP `skillgram_list`) and to declare usage *after*
  using a skill (`skillgram touch <skill> --by <harness>`, MCP `skillgram_touch`). Before, the block
  never mentioned `touch`, so `review`, `pin` and `list --used` could only ever be empty.
- **It also taught `skillgram add <url>`, which never existed** — every mounted `AGENTS.md`/
  `CLAUDE.md` told the agent to run a command that fails. Rule 8 now points at `import`/`adopt`.
  A test parses the block and fails if it names a command the parser does not have.
- **The harness is a mask; the project is the identity.** `touch` now tallies per harness
  (`by_harness`) and `list` exposes `last_used_by` + `by_harness`, so switching agent does not lose
  which skills the project was living on: the next harness reads the same manifest.
- **`skillgram --version`** exists (it exited 2 with "arguments are required: cmd", the first thing
  a new user types).
- **`install` → MCP registration now has a test against a real spawn** (the documented
  `SKILLGRAM_MCP_CLI_<HARNESS>` switch behind a stub): probe -> add -> probe, and mutating it to
  trust the exit code fails the test. **`daemon` has one too** (JSON line in, JSON line out, `quit`
  exits 0) — it is how edgePass talks to skillgram and it had none.

116 stdlib tests; 4 junction tests skip on POSIX.

## 1.4.0 — governance is the product, and the loop is measurable

Alignment pass: the tool was described as plumbing (links, sync, hubs) while the part that is
actually hard to replicate is the governance. Two new commands, two new MCP tools, and the project
now says what it is.

- **`skillgram list [--category C] [--project P] [--used|--unused] [--limit N]`** — what is in the
  hub, with provenance (`by`, `source.kind`) and, when a project is given, how many times each
  skill was declared as used. This is the view a user needs to answer "do I even want this one".
- **`skillgram touch <skill>... [--by <harness>]`** — the agent declares "I used these for this
  task". Recorded in the project manifest (`last_used`, `use_count`, `last_used_by`) and reported
  honestly when a skill is not in scope.
- **MCP grows to 7 tools**: `skillgram_touch` and `skillgram_list` join search / missing / use /
  status / propose. The agent can now close the loop it opens by asking the hub.
- **The honest limit, stated where it belongs**: skillgram cannot see what an agent reads. Usage is
  what the agent declares, and every surface that shows usage says so.
- **Positioning**: README, landing page, GitHub description and topics moved from "skill management
  / hub of links" to **skill governance** — contract, provenance, declared usage, policy — with
  links as the mechanism. "The four levers" table added to the README.

92 stdlib tests.

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

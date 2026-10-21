# Keeping things in order: `align`, and `policy` as the gate

Design note, not implemented. Two commands are proposed here; nothing in this
document is code yet. It exists so the decisions can be argued and signed before
anything is built.

The audience is the same person in two postures:

- **Order (personal).** "I have three or four harnesses and I want the same project to
  see the same skills in all of them, and I want to know when one drifts."
- **Governance (a company).** "Our developers may use these skills and not those, and
  I want that to be checkable, not a PDF."

The mechanism is the same; what changes is **who has the authority**.

## 1. Vocabulary (because "install" hides three different verbs)

| Verb | Command today | Means | Who decides |
|---|---|---|---|
| **grant** | `use` | add a skill to the project's set (`skills.json`) | the developer |
| **expose** | `mount`, `sync` | create the links so a harness can *see* it | the tool |
| **install** | `install` | wire the machine once (hub + MCP + propagate) | whoever sets it up |
| **allow** | `policy` (proposed) | say what may be granted/exposed at all | the authority |

A rule like "don't let them install others" is meaningless until we say which of the
first three it blocks. This document says: `policy` gates **grant** and **expose**, and
can only *report* what a human did by hand.

## 2. The gate has two real doors (the honest limit)

skillgram is not a sandbox. Someone with a shell can copy a folder into
`.claude/skills/` or edit a config. What it can do:

1. **Refuse to materialize**: `mount` / `sync` do not create links for disallowed
   skills.
2. **Refuse to serve**: the MCP tools (`skillgram_use`, `skillgram_propose --apply`,
   `skillgram_touch` on a skill out of scope) reject it.
3. **Detect and report drift**: `status`, `project-status`, `doctor`, `align`.
4. **Fail CI**: `policy check --fail` exits 1, and that is the only prohibition that
   actually blocks work — it blocks the merge, not the shell.

Anything beyond that is theatre. Prevention belongs to the OS (ACLs, MDM, managed
configs); skillgram produces the desired state and the alarm.

## 3. The primitive that is missing: *what does each harness actually see?*

Verified in the code today: 14 harness names have a project directory
(`PROJECT_SKILL_DIRS`: `.agents/skills`, `.claude/skills`, `.codex/skills`,
`.cursor/skills`, `.gemini/skills`, `.kiro/skills`, `.qwen/skills`, `.aider/skills`,
`.github/skills`, `.opencode/skills`, `.hermes/skills`), and roughly 35 harnesses only
have a global home. That asymmetry explains the symptom:

- For the 14, `mount` already writes **the same set** into every one: they cannot drift.
- For the rest, the only thing they see is the whole hub via `sync`: there is no "the 5
  of this project", there are 850.
- The drift that does exist is a **native skill**: someone added it in one harness.
  `doctor` sees it, `adopt` lifts it into the hub, `onboard` proposes it.

So what is missing is not a sync model. It is one read-only computation:

```
effective(harness, project) =
    project links in PROJECT_SKILL_DIRS[harness]      (what mount wrote)
  ∪ home links into the hub in that harness            (what sync wrote)
  ∪ native directories                                 (what a human did)
  ∪ what a precedence config makes the harness read    (hub-first declaration)
```

## 4. `align` — order, no policy needed

```
skillgram align [--project P] [--json]
skillgram align --apply
```

Per harness, for one project:

| Column | Meaning |
|---|---|
| `sees` | skills the harness can load right now |
| `missing` | in the project set, not visible to this harness |
| `extra` | visible, not in the project set (reported, never removed) |
| `native` | directories a human created; reported only |
| `mode` | `project` (has a project dir), `global-only` (hub via `sync`), `mixed` |

`--apply` links the project set into every harness layout that supports one, and
**never** touches natives or global-only homes. Exit code: 0 when nothing is missing,
1 when a harness cannot be brought in line.

The rule this encodes, and the reason it is not a merge:

> **The project set is the only source. A harness that sees something extra is a
> violation to report, not a suggestion to propagate.**

The tempting alternative — "A has 5, B added 1, suggest it to A" — converges on the
union of everything, which is the problem the tool exists to solve. If a native skill
matters, the path is `adopt` (hub) → `use` (project) → `align` (everyone), and that is
a decision with a name on it.

## 5. `policy` — the gate

One format, three locations, one merged result.

### 5.1 Locations and authority

| Level | File | Who may change it | Use |
|---|---|---|---|
| Organization | `--policy <path\|url\|git>` (hash recorded) | not the developer | corporate gate |
| Project | `<repo>/.agents/policy.json` | the repo (reviewed in a PR) | team of one / a few |
| User | `~/.agents/policy.json` | the developer | personal taste, `deny` for skills you never want |

Merge is **monotonic**: a child level may only *restrict*, never expand. If the
organization denies, no project policy can allow it back. This is what makes a policy
worth the name in a company, and it is a single rule to remember.

### 5.2 Shape

```json
{
  "mode": "open",
  "allow": ["infra-ops/*", "agent-workflows/qring-*", "!agent-workflows/qring-legacy"],
  "deny": ["clients/lotly-*"],
  "require": ["contract-first", "qring-anti-slop"],
  "harnesses": {
    "cursor": {"deny": ["design-ui/*"]}
  },
  "source": {"from": "git@…/skills-policy.git", "hash": "…", "at": "2026-10-21T…"}
}
```

- `mode`: `open` (default) = everything not denied is allowed; `closed` = only `allow`.
  Personal starts `open`; a company that wants an allow-list starts `closed`.
- `allow` / `deny`: globs over `<category>/<skill>`, with `!` to re-include inside an
  allow pattern. `deny` always wins over `allow`, at the same level and above it.
- `require`: must be **in the project's set**; `mount` and `policy check` fail while one
  is missing. This is the baseline ("everyone uses the contract gate"), which is what
  teams actually ask for before they ask for prohibitions.
- `harnesses`: per-harness restriction, same keys. v1 may accept and ignore it, but the
  shape is reserved so the file does not change later.
- `source`: only when the policy came from elsewhere; the hash is what makes an
  organization policy auditable rather than aspirational.

### 5.3 What each door does on a violation

| Door | `deny` / not in `allow` | `require` missing |
|---|---|---|
| `use` (grant) | refuses, exit 1, names the rule | n/a |
| `mount` / `sync` (expose) | skips the link, reports it | fails at the end, exit 1 |
| MCP `skillgram_use` / `propose --apply` | refuses with the rule | n/a |
| `align --apply` | skips and reports | reports |
| `doctor` / `status` / `project-status` | report `policy_violations` | report `seeds_missing`-style |
| `policy check` | exit 1 | exit 1 |

`touch` is never blocked: recording that something was used is evidence, not access.

### 5.4 `policy check` — the CI line

```
$ skillgram policy check --fail
{"ok": false, "op": "policy-check", "violations": [{"skill": "clients/lotly-billing",
  "rule": "org:deny:clients/lotly-*", "where": "project scope"}], "required_missing": []}
$ echo $?
1
```

That is the whole corporate story in one step: a repo, a policy, one line in the
pipeline, and a violation that cannot be merged.

## 6. What this deliberately is not

- **No accounts, no roles, no server, no daemon as authority.** The authority travels as
  a file with a hash, the same way provenance already travels. It keeps the tool
  installable in one command, reversible, and honest: delete skillgram and each harness
  keeps exactly what it had.
- **No prevention.** See §2. Copying files by hand is outside the threat model, and
  saying so is part of the product.
- **No per-user policy inside an organization.** One machine, one developer; inventing
  "who is this human" would need the accounts this design refuses.
- **No cross-harness "merge" in `align`.** §4.

## 7. Order of work (each step useful alone)

1. **`align`** — personal value, no trust involved, uses only what exists.
2. **`policy` read-only** — parse, merge, report, `check --fail`; no door refuses yet.
   Ship it in a repo for a week and see whether the report is enough.
3. **`policy` enforced** — `mount`/`sync`/MCP refuse; the same code path with a switch.
4. **Org policy by reference** (`--policy git@…`, hash in the report) — the corporate
   story, still no service.
5. **Per-harness rules** — reserved in the schema from day one, interpreted last.

## 8. How a company adopts it (the GitHub-link path)

```bash
pip install skillgram                      # or python skillgram.py
git clone <their repo> && cd <their repo>
skillgram policy check --fail              # red until the repo has a policy
skillgram mount --apply                    # the set, where each harness looks
```

plus two lines in the pipeline (`skillgram policy check --fail`) and the block that
`mount` already injects into `AGENTS.md`, which is what makes a new session, in any
harness, read the same rules without being told.

## 9. Open decisions (the ones worth arguing)

1. **Where does the organizational policy live** — a path, a git remote, or an HTTPS
   URL? The hash makes all three auditable; HTTPS needs no credentials but no private
   policy either.
2. **Default `mode` for a company** — `open` + `deny` (adoptable, stops the known bad)
   or `closed` + `allow` (strict, high friction). My recommendation: start `open`.
3. **Is `require` part of v1?** It is the half of governance teams ask for first
   ("everyone with the QA gate"), and it is cheaper than `allow`.
4. **Per-harness in v1?** I would reserve the key and ignore it until asked.

## 10. What would tell us this is worth building

- `align` on the repos already mounted shows real `missing`/`extra` (if everything is
  already aligned, the personal story is thinner than it looks).
- A week of `policy check --fail` in one repo: if the violations are all noise, the
  rule set was wrong, not the tool.
- Someone outside this machine runs `policy check` with their own policy file.

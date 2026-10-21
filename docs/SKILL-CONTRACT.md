# The skill contract

One normative text. `skillgram lint --contract` **enforces** it, `skillgram new` scaffolds it, and
`skillgram propose` refuses a draft that ignores it. If this file and a skill disagree, the skill is
wrong — unless this file is wrong, in which case fix it here first.

Why it exists: a `SKILL.md` is machine-read instructions, not an essay. An agent that loads one gets
a trigger, some non-negotiables and a way to report. A skill that cannot state those four things is
a paragraph with a nice filename.

## 1. Frontmatter

```yaml
---
name: kebab-case-name        # must equal the folder name
description: One sentence, <=140 chars, saying WHEN it applies, not what it is about.
---
```

| Field | Rule | `lint` |
|---|---|---|
| `name` | kebab-case, exactly the folder name | error |
| `description` | ≤140 chars, ≥20 chars, states the trigger | error / warning |
| anything else | allowed (`license`, `metadata`) | not checked |

The description may be a single line, a quoted string, or a block scalar (`description: >-`
followed by an indented paragraph) — `lint` reads all three, because writing a long trigger on
one line is how descriptions become 300 characters of prose.

A description that works: *"Use when a Docker build fails on a native dependency in CI."*
A description that does not: *"Docker things."*

## 2. Body, in this order

| Section | Must contain | Why |
|---|---|---|
| `## Activation Contract` | **Use when:** a concrete trigger. **Not for:** the neighbouring case someone would confuse it with. | an agent decides in one line whether to read on — and whether to stop |
| `## Hard Rules` | numbered, testable rules (a rule you cannot check is a wish) | the non-negotiables survive being skimmed |
| `## Decision Gates` | a table: observation → what to do | decisions stop being improvised |
| `## Execution Steps` | exact commands, real paths, in order | copy-paste, not interpretation |
| `## Output Contract` | what to report: fields, not prose | the caller can parse the answer |

`lint --contract` treats a **missing** section as an error and an **empty** one as an error: a
heading followed by nothing is a lie about completeness.

## 3. Prohibitions (errors)

- **Credentials.** No live-looking secrets. `lint` matches narrow, real shapes (a `sk_live_` key,
  a PyPI token, a 40+ char `ghp_`, an AWS key id, a PEM header *with* a base64 body, a full JWT).
  Placeholders like `ghp_xxx` and a bare PEM banner are documentation and pass.
- **Placeholders.** A placeholder *marker*: the word starting a line, a whole table cell, a
  `Label: WORD` value, or the description itself. A scaffold that still says `TODO:` is not a
  skill yet — that is why `new` writes them and `lint` refuses them. Naming the concept is
  documentation and passes: "mark unknowns as **TBD with an owner**" is an honest rule, and a
  gate that refuses it teaches people to write worse skills.
- **Uniqueness.** A name that already exists in the hub with different content is a conflict, not an
  update; `skillgram diff <name> --against <path>` shows the difference before anyone overwrites.

## 4. Warnings (the debt list)

`lint` reports these without failing, so the hub can be honest about its backlog:

- a local absolute path (`C:\Users\...`, `/home/...`) — the skill stops being portable;
- more than 400 non-empty lines — the reader scrolls past the point;
- a `references/`, `scripts/` or `assets/` mention without that folder;
- a name that differs from its folder (an error once you are authoring with `--contract`);
- a `../<skill>/` link to a skill that lives in another category. The hub keeps categories, so a
  relative path resolves only inside one folder: name the skill instead (`good-taste-ui`
  (design-ui)). Six of the hub's 847 skills had this; they were invisible until something
  checked.

`lint --strict` turns warnings into a failure, which is what CI should run.

## 5. Provenance: `SKILLMETA.json`

Every skill that `new` or `propose` creates carries a sidecar next to `SKILL.md`:

```json
{
  "by": "human",
  "added_at": "2026-10-19T01:40:00",
  "hash": "9f2c…",
  "source": { "kind": "authored" }
}
```

`source.kind` is `authored` for something a human wrote, `bundled` for a copy seeded from the
skills that ship inside the package (with the `version` it came from), and `git`/`url` once
`add` lands (with `url`, `ref` and `path`).
The hash is what makes `update` possible later: compare, show the diff, then decide. A skill with
no sidecar is not rejected — the hub has 843 of those — but it cannot be updated, only replaced.

## 6. The three doors, one gate

| Door | Who | Command |
|---|---|---|
| author | a human | `skillgram new <name> --category <c>` then fill it |
| draft | the connected LLM | `skillgram propose "<need>" --apply` (the model's prompt asks for these five sections; `lint --contract` checks it) |
| import | a repo or a link | `skillgram add <url>` (coming: it fetches, lints, and writes `source`) |

All three end in the same place: `lint` → `SKILLMETA.json` with its hash → `sync` → `index`.
Nothing reaches an agent without passing that gate, and nothing is ever deleted: `retire` moves the
skill to `<hub>/_retired/` with a tombstone.

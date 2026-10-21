---
name: anti-slop-guardrails
description: Use when a codebase needs de-slopping that sticks: evidence-based review, behaviour-preserving refactor, and CI gates that keep it clean.
metadata:
  origin: authored
---

# Anti-slop guardrails

Anti-slop is a **separate pass**, not a writing style. The pass finds the habits a model
(or a tired human) repeats in every repo — a dead local, an error handler around code that
cannot fail, a comment that restates the line below, an abstraction with one caller — and
removes them without changing behaviour. Then it leaves **guardrails**: a gate in CI, a test
for every invariant that broke once, so the same slop cannot come back next week.

The distinction that matters: a `deslop`-class skill removes slop once. This one makes the
removal durable, which is the part that costs a codebase its maintainability score.

This text is canonical English and language-neutral. Commands, identifiers, rule codes and
paths are never translated; the checklist itself is meant to be adapted per repository and
per language (see *Adapting the checklist* at the end).

## Activation Contract

**Use when:**
- a review scores maintainability low ("N thousand lines in one file", "zero static analysis",
  "no tests for the surface that runs unattended") and someone has to close it;
- a de-slop pass already happened and nobody guarded the result;
- you are about to split a god module, adopt a linter, or add a gate to a legacy repo;
- a bug shipped that a linter would have caught (a renamed key, an undefined name).

**Not for:**
- formatting or naming conventions — that is a formatter and a style guide;
- rewriting an algorithm or changing behaviour — that is a feature, and it must not share a
  commit with a cleanup;
- reviewing a diff for correctness — that is a code review; this skill reviews *habits*;
- prose. For writing use an `unslop`/`humanizer`-class skill; the checklist is different.

## Hard Rules

1. **Behaviour is frozen for the whole pass.** The suite is green before you start and green
   after, and it is the *same* suite: no assertion relaxed, no test deleted. The only
   permitted test edits are seams (a mock target that moved because the code moved).
2. **Every finding is `path:line` + consequence + fix.** "This is messy" is not a finding.
   "`lifecycle.py:206` writes `"ok"` twice in one dict literal; the second silently wins, so
   `review --fail-if-stale` can never fail" is. No findings means you did not look; never
   answer `LGTM`.
3. **A gate must be able to be green on the day it lands.** Legacy code has debt; a gate that
   is red from birth is deleted within a week. Ship it with a **baseline file** of the
   accepted failures, and let only *new* failures fail the build.
4. **Every invariant that broke once becomes a test**, not a paragraph in a CONTRIBUTING file.
   A rule a machine cannot check is a wish — write the check.
5. **A split is by concern, in an acyclic dependency order.** Not by line count. The base
   module imports nothing local; the entry point imports everything. If two modules need each
   other, the boundary is wrong or a name is doing two jobs.
6. **Nothing mutable is captured at import/load time.** Config, home/homeDir, locale, the
   clock: read them through a function, so there is exactly one place a test can point
   elsewhere. A module that keeps its own copy is a bug — and it presents as a *hang*, not as
   an error, because it quietly scans the real machine.
7. **Remove before adding.** Dead code, unused imports, duplicated literals, an abstraction
   with one caller, a comment that repeats the line below: delete them in this pass. The
   cleanup may not introduce an abstraction unless the second caller already exists.
8. **Two beats three.** Where slop is structural (a helper for one call, a flag for one
   branch, a doc section that says the same thing twice), the fix is fewer things, not
   better things.

## Decision Gates

| Observation | Do |
|---|---|
| The gate finds nothing on a large codebase | Suspicious first: is the config applied (files matched, rules not all disabled)? Then, and only then, is the code clean |
| The gate finds hundreds of problems | Do not fix hundreds. Auto-fix the mechanical ones, disable the noise *with a reason*, baseline the rest |
| The suite hangs or slows down after a refactor | Something captures config at import time, or a test seam (mock target) moved. Fix the seam; do not add a timeout |
| A module name equals an exported function name | Rename the module (`sync` → `drift`, `onboard` → `adopt`). Otherwise the package shadows itself and every patch of that function hits the wrong object |
| A mechanical split breaks compilation on the oldest runtime | The split dropped something that is not a *name*: a directive, pragma or build tag. See step 4 |
| The split needs an ignore/`any` to compile | The annotation is wrong or the value is genuinely dynamic. Fix the annotation: an ignore that is correct on one OS is unused — and therefore an error — on another |
| A file starts with a BOM (`\ufeff`) | It is a syntax error on the oldest interpreter and invisible in review. The editor wrote it: strip it and add a check |
| A refactor wants to change behaviour "to make it cleaner" | Stop. Land it separately, with its own before/after evidence |
| A finding is plausible but unproven | Mark it `needs_governance` and name the evidence that would settle it. Do not guess in a verdict |
| The repo has no test suite at all | Do not refactor. Characterise first (a smoke test of the entry points), then return: with no oracle, "behaviour unchanged" is a claim, not a fact |

## Execution Steps

1. **Measure, do not opine.** Record the numbers you will be judged on:
   ```
   wc -l <the-big-file>; ls tests | wc -l
   <linter> --version          # "no linter" is itself the measurement
   git log --oneline -1
   ```

2. **Adopt the gates before touching structure**, so the refactor happens under them. Pick
   the row for the language; keep fixing the config until the *mechanical* findings are zero:

   | Language | Bugs/lint | Types | Notes |
   |---|---|---|---|
   | Python | `ruff check .` (`E,F,W,B,I,C90`) | `mypy` | `--fix` first; `py39` target keeps you honest |
   | TypeScript | `eslint .` (`no-unused-vars`, `no-explicit-any`) | `tsc --noEmit` | narrow `tsconfig` after the noise is gone |
   | Go | `go vet ./...` + `staticcheck ./...` | `go build ./...` | `staticcheck` is the one that pays |
   | Rust | `cargo clippy -- -D warnings` | `cargo check` | clippy knows the idioms, trust it |
   | Java/Kotlin | `spotbugs` / `detekt` | `javac -Xlint` | same order: bugs, then types |
   | Any | one CI job that runs both **and builds the shippable artifact** | | the artifact build is the step everyone forgets |

3. **Split mechanically, not by hand.** Drive the split with a dependency analysis (parse the
   file, map each top-level symbol to one module, resolve free names to their owning module),
   then let the suite be the oracle. A hand split of a 5000-line file introduces behaviour
   changes you will not find by reading.

4. **Repair the seams the split broke.** Ask what the outside world pointed at: entry points,
   launchers, test patches, CI commands, docs. And check what the splitter *could not see* —
   anything that is a directive rather than a name. Real examples, one per ecosystem:

   | Ecosystem | Dropped by a mechanical split | Symptom |
   |---|---|---|
   | Python | `from __future__ import annotations` | `TypeError: unsupported operand ... 'X | None'` on the oldest supported version |
   | TypeScript | `"use strict"`, triple-slash `/// <reference>` | runtime `this` drift, missing global types |
   | Go | `//go:build` tags, `//go:generate` | a file compiled into the wrong target |
   | Rust | `#![...]` crate attributes, `mod` declarations | items silently unreachable |
   | C/C++ | include guards, `#pragma once` | double definitions |
   | Ruby | `# frozen_string_literal: true` | mutation of literals at runtime |

5. **Add guard tests** for everything that broke once during the pass. Cheap and permanent:
   ```
   test_every_module_defers_its_annotations      # the directive survived the split
   test_no_module_captures_config_at_load_time   # one place to point elsewhere
   test_the_artifact_runs_without_the_dev_shim   # the shipped thing works
   ```

6. **Prove it on the floor and on the ceiling.** Run the suite on the oldest *and* newest
   supported runtime (a container is enough) and let CI own the matrix. A refactor verified on
   the interpreter you happen to have is not verified.

7. **Run the review pass itself** — a checklist, not a mood:
   - dead code: unused locals, imports, parameters, unreachable branches (`F841`, `F401`, `no-unused-vars`);
   - a literal key/field written twice (`F601`, duplicate key): the second wins silently;
   - error handling on a path that cannot fail, or that swallows the error;
   - a cast or ignore that silences a type error instead of fixing the type;
   - nesting an early return would flatten;
   - a comment that restates the line below, or that a rename made false;
   - a name that means two things (module vs function, variable vs import);
   - the same shape three times where a loop or a table belongs;
   - an abstraction with one caller, added "for later".

8. **Record what the gate learned.** The first run of a new gate is a survey of the codebase;
   write that list down, because it is the honest changelog of the pass.

## Output Contract

One block per finding, then one verdict. Fields, not prose:

```
verdict: clean | slop_found | needs_governance
scope: <path or diff range>            measured: <lines, files, tests, gates>
findings:
  - where: <path>:<line>
    slop: dead_code | swallowed_error | silenced_type | deep_nesting | restating_comment
          | duplicate_key | shadowed_name | speculative_abstraction | captured_config
          | single_file_growth | no_static_analysis
    consequence: <what breaks, or what someone will believe wrongly>
    fix: <the edit>
    evidence: <command and its output, or the failing test>
gates_added:
  - tool: <linter|typechecker>  config: <path>  baseline: <path|none>
guards_added:
  - <test name> — <the invariant it pins>
behaviour_change: none | <what, and where it is proven>
```

`verdict: clean` requires that you name the gates you ran and the checklist you walked. A
finding without `evidence` is demoted to `needs_governance`, with the question that would
settle it.

## Adapting the checklist

The patterns are universal; the detector is not. Copy this table into the new repo and fill
the "detector" column from whatever the ecosystem actually provides — that column is what
turns the skill from advice into a gate.

| Slop pattern | Python | TypeScript | Go | Rust |
|---|---|---|---|---|
| `dead_code` | `F841`, `F401` | `no-unused-vars` | `unused` (vet) | `dead_code` lint |
| `swallowed_error` | `B902`/review | `no-empty` | `errcheck` | `let _ =` review |
| `silenced_type` | `type: ignore` review | `no-explicit-any` | `//nolint` review | `#[allow(dead_code)]` review |
| `deep_nesting` | `C901` | `max-depth` | `nestif` | `clippy::too_many_*` |
| `restating_comment` | review only | review only | review only | review only |
| `duplicate_key` | `F601` | `no-dupe-keys` | `structcheck` | unreachable pattern |
| `shadowed_name` | `F811` | `no-redeclare` | `shadow` | `clippy::shadow_*` |
| `captured_config` | guard test | guard test | guard test | guard test |
| `no_static_analysis` | CI job | CI job | CI job | CI job |

Two rules when adapting:

- **Keep the vocabulary.** `slop:` values stay in English and keep their spelling: they are
  the machine-readable part of the verdict, and a translated enum breaks the parser that
  reads it. Translate the *prose* (rules, gates, steps) into the team's language; leave
  commands, paths, rule codes and enum values untouched.
- **Drop what the ecosystem makes impossible.** If the language has no inheritance and no
  dynamic dispatch, some patterns cannot occur. A checklist that keeps a rule nobody can hit
  teaches the reader to skim.

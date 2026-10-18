---
name: skillgram-report
description: Report a skillgram bug or a missing-skill gap with real evidence: pick the channel, capture the JSON, open the issue.
---

# skillgram-report

## Activation Contract

Use this skill when one of these is true:

- skillgram printed `ok:false`, exited `1`/`2`, crashed, or said something the disk contradicts
  (`registered` with no entry in the harness config, a link that points nowhere, a mount that
  reported zero skills while the manifest has some).
- an agent needed a capability and `skillgram missing "<query>"` answered `found:false`.
- an install or registration did not take effect in the harness after the documented restart.

Not for: "how do I run X" (read `INSTALL.md`), or a feature idea with no observed gap.

## Hard Rules

1. A bug is never filed as a skill. Never run `propose` to paper over broken behaviour.
2. A gap is never filed as a bug. Nothing in the hub for a capability is a *request*.
3. No invented evidence: every exit code, path and JSON line comes from a command you ran now.
4. Redact before pasting: tokens, passwords, private hosts, customer names. Keep the shape,
   change the values.
5. Never "fix" skillgram by hand-editing a harness config (`config.toml`, `config.yaml`,
   `.claude.json`, `.cursor/mcp.json`) to compensate. That hides the bug.
6. Read the config back before claiming success: `<harness> mcp get skillgram` (or `mcp list`)
   and `skillgram status`. An exit code is not proof.

## Decision Gates

| Observation | Channel |
|---|---|
| `ok:false`, non-zero exit, wrong link, lie in the output, crash | **bug** → `bug_report.yml` |
| `found:false` for a capability you actually needed | **skill request** → `skill_request.yml` |
| The missing thing is a skillgram command, flag or behaviour, not a skill | **bug** (say "feature" in the title) |
| Works on another machine or harness | note it as evidence; it is not a fix |
| Unsure | collect both evidences and let the user pick the channel |

## Execution Steps

1. Reproduce once, keeping the exact command line. Use the CLI (`skillgram <command> --json`,
   or `python <repo>/skillgram.py <command> --json` when it is not installed) and record the
   exit code (`$LASTEXITCODE` on PowerShell, `$?` on POSIX).
2. Capture the environment:
   - `skillgram status --json`
   - `skillgram doctor --json` (read-only; strip paths you do not want public)
   - OS, Python version, `skillgram` version, the harness, and the launcher you used
     (`SKILLGRAM_MCP_CLI_<HARNESS>` when the one on `PATH` is not the right one).
3. For any registration claim, capture `<harness> mcp get skillgram` (or `mcp list`) **after**
   the command that claimed success.
4. For a gap, capture `skillgram missing "<query>" --json` and `skillgram demand --limit 20`
   (it ranks the queries nobody could answer, so the issue and the demand log line up).
5. Compose the issue from the template and show it to the user before posting. Post with
   `gh issue create --repo Xhuk/skillgram --template bug_report.yml` (or
   `--body-file <file>` when filling the form yourself), or hand the text over.

## Output Contract

Reply with exactly:

- channel: `bug` or `skill-request`
- evidence: the command, the exit code, one JSON line
- environment: OS / Python / skillgram / harness / launcher
- expected instead: one sentence, no speculation
- disposition: posted (with the issue URL) or left as text for the user

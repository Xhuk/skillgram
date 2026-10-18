# skillgram

**Skill management for AI agents:** one skill library, linked into every coding agent.
No copies, no drift, no "which folder does this harness read?".

[![CI](https://github.com/Xhuk/skillgram/actions/workflows/ci.yml/badge.svg)](https://github.com/Xhuk/skillgram/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/skillgram)](https://pypi.org/project/skillgram/)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab)](https://github.com/Xhuk/skillgram)
[![Deps](https://img.shields.io/badge/deps-none-success)](https://github.com/Xhuk/skillgram)

skillgram is a **skill hub** — a registry of `SKILL.md` files that lives in one directory on disk
— plus the admin layer that keeps every agent pointing at it. Claude Code, Codex, Cursor, Hermes,
Reasonix, Gemini CLI, OpenCode, Kiro, Qwen Code, Aider and Copilot all read the same library.

```
~/.agents/skills/<category>/<skill>/SKILL.md   the hub (the only real copy)
~/.claude/skills/<skill>                       -> junction to the hub (symlink off Windows)
~/.codex/skills/<skill>                        -> junction to the hub
```

- **Links, never copies.** A native skill with the same name as a hub skill is skipped and
  reported as a collision, never overwritten.
- **Project scope.** `use` + `mount` give a repo exactly its set — twenty skills, not eight
  hundred — in every harness layout that reads project-level skills.
- **An MCP server.** `skillgram mcp` exposes `search` / `missing` / `use` / `status` / `propose`,
  so the agent asks the hub instead of shelling out. `missing` answering `{"found": false}` is
  the contract that says *do not invent a skill, tell the user*.
- **One idempotent command.** `skillgram install` resolves the harness, verifies the hub,
  registers the MCP server through the harness' own CLI, and propagates the project scope.
- **No LLM unless you ask.** It is used only for `bootstrap`, `propose` and `port`, always as a
  JSON plan that skillgram validates and you approve.

## Install

```bash
pip install skillgram
```

Then, once per machine:

```bash
skillgram install --dry-run   # the exact commands, nothing written
skillgram install             # harness -> hub -> MCP -> project scope
```

Python 3.9+, standard library only, no dependencies. Junctions on Windows, symlinks on
macOS/Linux.

## What it is not

Not a prompt manager, not a memory store, not an embeddings index. It links and administers the
skills your agents already read, and it never guesses which harness is asking.

## Documentation

- Repository and full README: <https://github.com/Xhuk/skillgram>
- Site: <https://xhuk.github.io/skillgram/>
- Install brief for an agent session: <https://github.com/Xhuk/skillgram/blob/main/INSTALL.md>
- The install flow, as an interactive diagram: <https://xhuk.github.io/skillgram/diagrams/skillgram-install.html>
- Changelog: <https://github.com/Xhuk/skillgram/blob/main/CHANGELOG.md>
- Known gaps: <https://github.com/Xhuk/skillgram#known-gaps-documented-not-implemented>

## License

MIT.

# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:1105d646 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

Python 3.13+, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # install deps (streamlit; dev: pytest)
uv run pytest           # run the test suite
```

The app's data lives in `finances.db` (SQLite, gitignored) in the project root;
`housefin.db.connect()` creates and migrates it on first use. Tests use
in-memory or temp-dir databases only.

## Architecture

README.md explains how the money works (signs, tags, months, splits, rent by
rooms, balances) and maps the code. Domain logic lives in `housefin/` with no
Streamlit dependency, so it can be tested directly; `pages/` are the
Streamlit screens, and `*_ui.py` modules hold helpers for one page. Only
`ui.py`, `matching_ui.py` and `fmt.browser_locale` import Streamlit, inside
the functions that need it.

## Conventions

- Money is integer cents. Transaction amounts carry the bank's sign; shares
  carry the person's (see `housefin/splits.py`).
- Calculations are transparent and decisions are made by people: never
  rescale a split, round an entered value or fill in a vacancy silently.
  Surface it and let the user confirm.
- Months and dates shown to people go through `housefin.fmt`, which follows
  the browser's locale. Month arithmetic and month SQL live in
  `housefin.dates`.
- Anything that writes is for editors only: gate it on `ui.can_edit()`
  (viewers' connections are read-only, so a missed control errors rather
  than writes). Roles come from `housefin.auth`.
- Group writes with `db.transaction`. Change the schema by appending to
  `db.MIGRATIONS`; never edit an existing migration.
- Tests use in-memory or temp-dir databases; shared helpers are in
  `tests/helpers.py`.

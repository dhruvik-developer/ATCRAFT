# AGENTS.md

`CLAUDE.md` in this folder is the binding engineering baseline for every coding
agent, whichever tool runs it. Read it before changing anything.

## One working copy

This repository has exactly one working copy on this machine,
`C:\Project\Other\ATCRAFT`, and the owner wants no other.

- Never create a git worktree, clone or copied folder of it: not beside it
  (`ATCRAFT-lanes`, `ATCRAFT-priority2`, `ATCRAFT-review` and
  `ATCRAFT-worktrees` were all made that way and had to be removed), not under a
  tool's own data directory, and not through a tool's "worktree" run mode.
- Keep exactly two branches, locally and on every remote: `main` and `develop`.
  Work directly on `develop`; do not create feature, temporary or backup branches.
  After verification, commit and push the same revision to both branches.
- Keep one Docker stack (`atcraft-erp`), one development database
  (`atcraft_erp`) and its test database (`atcraft_erp_test`). Do not leave
  clone databases, per-task test databases or extra Compose projects behind; if
  a task needs a throwaway one, drop it before you finish.
- Leave `TEST_POSTGRES_DB` unset (it defaults to `atcraft_erp_test`). The test
  fixture creates whatever database that variable names, and nothing drops it.

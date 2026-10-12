# Proposal implementation and continuation

The saved Claude session was recovered on 11 October 2026 and resumed by Codex.
This directory preserves its work-package specifications, proposal traceability
audit and research reports in the repository so continuation does not depend on
temporary session files.

The authorized scope is to close the Cursor audit gaps and cover every software
requirement in the supplied manufacturing ERP proposal. See
[the master plan](specs/PLAN.md), [the proposal checklist](specs/PDF-CHECKLIST.md)
and [the traceability audit](trace/trace.json).

## Current checkpoint

Updated 12 October 2026, 16:55 IST, when Claude resumed after Codex's usage limit.

- `main` is `fd90655` plus this directory: round 1 is integrated. That covers the brand and manufacturer masters, cost centres, and requisition conversion split by procurement category.
- Round 2 is in progress in five isolated worktrees:

| Lane | Branch and worktree | Round 2 packages | State at 16:55 |
|---|---|---|---|
| A | `lane/commercial`, `../ATCRAFT-lanes/commercial` | E1a dispatch linkage and top-up; A2 sales invoices | Codex work uncommitted; Claude takes it over, finishes, commits, then reviews each package |
| B | `lane/operations`, `../ATCRAFT-lanes/operations` | B2 shifts and roster | Codex work uncommitted; Claude takes it over, then reviews it |
| C | `lane/portals`, `../ATCRAFT-lanes/portals` | C1a scoped vendor portal; C1b Web Push | Committed by Codex (`383550c`, see `lane-c-round2-checkpoint.md` in that lane); Claude reviews each package |
| D | `lane/supply`, `../ATCRAFT-lanes/supply` | E8a inventory operations | Codex work uncommitted; Claude takes it over, then reviews it |
| E | `lane/quality`, `../ATCRAFT-lanes/quality` | E4 assembly and one QC-verdict path | New lane, created from `977dffd`; not started |

**Only one agent may work in a lane at a time.** Before starting in a lane:
- check that no other session is editing it: run `git status`, look at recent file times, and look for running servers on the lane's ports;
- then record here who holds it.

The specs in `specs/` are the single source of truth. `specs/PLAN.md` holds the schedule and a status log.
`workflows/` keeps the Claude Workflow scripts that run the lanes (`lane-build.js`) and the round integration (`integrate.js`); they read the specs from this directory.

## Completion sequence

Finish and independently review the round-2 packages, verify each lane, and
commit each lane with the repository hooks. Then follow
[the integration procedure](specs/INTEGRATOR.md): merge all completed lanes,
reconcile the migration chain, regenerate contracts, verify the combined
application and migrate the development database. Continue rounds 3–8 from the
master plan. A package or round is not complete merely because code exists.

Keep the original proposal PDF untracked. Do not change shared virtual
environments or dependency junctions. Do not bypass Git hooks. No Git remote is
configured, so any eventual push needs a configured remote.

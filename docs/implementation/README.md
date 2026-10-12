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

Updated 12 October 2026, about 18:05 IST. **Paused at the user's request; no agent holds any lane.**

- `main` is round 1 integrated (`fd90655`), plus this directory.
- Round 2 is paused. At about 16:57 Claude resumed it in five worktrees; the user stopped every Claude process at about 18:04.
  - Nothing was committed in that hour.
  - Each lane keeps its uncommitted work exactly as it was left: Codex's changes plus Claude's partial takeover.
  - No servers or test runs are left running.
  - The Docker database stack is still up.

| Lane | Branch and worktree | Round 2 packages | State when paused |
|---|---|---|---|
| A | `lane/commercial`, `../ATCRAFT-lanes/commercial` | E1a dispatch linkage and top-up; A2 sales invoices | Uncommitted. Codex's work, partly taken over by Claude. Still to do: finish, verify and commit, then review each package. |
| B | `lane/operations`, `../ATCRAFT-lanes/operations` | B2 shifts and roster | Uncommitted. Codex's work, partly taken over by Claude. Still to do: finish, verify and commit, then review. |
| C | `lane/portals`, `../ATCRAFT-lanes/portals` | C1a scoped vendor portal; C1b Web Push | Codex's commit `383550c`, plus Claude's uncommitted review fixes for C1a. Still to do: finish the C1a review and commit, then review C1b. |
| D | `lane/supply`, `../ATCRAFT-lanes/supply` | E8a inventory operations | Uncommitted. Codex's work, partly taken over by Claude. Still to do: finish, verify and commit, then review. |
| E | `lane/quality`, `../ATCRAFT-lanes/quality` | E4 assembly and one QC-verdict path | Claude's uncommitted, partial backend. Still to do: finish the backend, then the frontend, then review. |

To resume a lane, follow `specs/BRIEF.md` section 1a:
- take the uncommitted work over;
- never discard it wholesale.

`specs/PLAN.md` lists the steps still to run. A resumed run starts each lane with its "take over" step, even in lanes C and E, because both now hold uncommitted changes.

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

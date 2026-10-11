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

- Main is at `fd90655`. Round 1 is integrated: brand and manufacturer masters,
  cost centres and requisition conversion split by procurement category.
- The corrected browser sweeps were verified in that commit. These are
  historical results, not a fresh verification of the current implementation.
- Round 2 was interrupted before any of its original Claude tasks changed a
  worktree. Codex resumed it in the four existing isolated worktrees.
- A usage-limit interruption left implementation changes saved but uncommitted
  in those worktrees. Work resumed on 12 October 2026.

| Lane | Branch and worktree | Round 2 packages | State |
|---|---|---|---|
| A | `lane/commercial`, `../ATCRAFT-lanes/commercial` | E1a dispatch linkage and top-up; A2 sales invoices | Implementation and validation in progress |
| B | `lane/operations`, `../ATCRAFT-lanes/operations` | B2 shifts and roster; E4 assembly and QC completion | Implementation and validation in progress |
| C | `lane/portals`, `../ATCRAFT-lanes/portals` | C1a scoped vendor portal; C1b Web Push | Implementation and validation in progress |
| D | `lane/supply`, `../ATCRAFT-lanes/supply` | E8a inventory operations | Implementation and validation in progress |

The historical specifications describe Claude-specific workflow tools and an
authorship trailer. Codex uses its available collaboration tools and does not
apply that authorship trailer to new work. The engineering rules, user scope,
lane isolation and acceptance criteria still apply.

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

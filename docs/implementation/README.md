# Proposal implementation and continuation

The saved Claude session was recovered on 11 October 2026 and resumed by Codex.
This directory preserves its work-package specifications, proposal traceability
audit and research reports in the repository so continuation does not depend on
temporary session files.

The authorized scope is to close the Cursor audit gaps and cover every software
requirement in the supplied manufacturing ERP proposal. See
[the master plan](specs/PLAN.md), [the proposal checklist](specs/PDF-CHECKLIST.md)
and [the traceability audit](trace/trace.json).

The historical 13 October 2026 [build order](archive/BUILD-ORDER.md) and
[proposal gap checklist](archive/PROPOSAL-GAP-CHECKLIST.md) are archived here to
preserve their original planning and audit evidence.

## Current checkpoint

Updated 26 October 2026. The migration chain has eleven
revisions: the verified `20261021_0100` baseline and ten forward revisions through
`20261031_0100`. The [current full-scope checkpoint](production-scope-20261025.md)
distinguishes this delivery's fixes and verification from the software packages
that remain in the user's authorized full scope. Combined release checks and
full-scope acceptance remain separate; a historical audit or a passing focused
suite does not establish completion of the proposal.
The [26 October local release verification](release-verification-20261026.md)
records the populated portal/responsive/accessibility checks, patched web/proxy
runtime, seed and restore proofs. The signing integration decision remains
pending at the user's request; other full-scope work continues.
The original 58-revision squash is recorded in
[migration-squash-20261021.md](migration-squash-20261021.md). Application-wide
standards checks and their results are recorded in the
[22–23 October verification](development-standards-20261022.md); local setup and
the repeatable demonstration seed are documented in the
[local seed workflow](../local-seed-data.md). The D4/PAdES compliance and
procurement plan is in
[d4-dsc-pades-compliance-20261019.md](d4-dsc-pades-compliance-20261019.md). Work
continues on `develop`; the lane checkpoints and procedures below are historical.
The [24 October repository synchronization](repository-sync-20261024.md)
records the subsequent documentation, configuration, generated-contract and
handbook refresh, including the deployment and shell-check corrections.

### Round-2 integration checkpoint (14 October 2026)

| Lane | Branch | Round-2 packages | State |
|---|---|---|---|
| A | `lane/commercial` @ `16cbe2a` | E1a dispatch linkage + top-up; A2 sales invoices | Merged. Review: approve-with-fix (`16cbe2a`). |
| B | `lane/operations` @ `f3aefcb` | B3a job-work invoices | Merged. Review: approve-with-fix (`f3aefcb`, net-payable money bug). DB-verified. |
| C | `lane/portals` @ `cd38749` | C3a dealer portal backend | Merged. Review: approve-with-fix (`cd38749`). |
| D | `lane/supply` @ `cf38ff4` | E8a inventory operations | Merged. Review: approve. |
| E | `lane/quality` @ `c80a255` | E4 QC unification + assembly UI | Merged. Review: approve. |

Integration commits on main: `faa949a` (D) + `3adcb1b` (E) + `15720f7` (B) + `a0a0dfa` (A) + `80fabb1` (C) + `c42eb04` (reconcile: re-chain, ReportKey union 26 keys, picker unification under `pick_free_chart_code`, contract 624 req/29 folders) + `2033436` + `768a3e7` (cross-lane fixes). Verification: 3523 unit passed, mypy/ruff clean, migration round-trip green, demo idempotent, sweep 44 routes clean, live order→dispatch→invoice SI/2026-27/00002, Playwright 12/12. No lane holds uncommitted work; no agent active in any lane.

Scope update 14 Oct 2026: user approved FULL build — C2a/C2b vendor app, D2 WhatsApp/SMS, D3 courier tracking, D4 signatures all back in scope. C2a/C2b build after the R2 integration (lane C finishes C3a first).

### Previous checkpoint

Updated 12 October 2026, about 19:25 IST. **Resumed by Codex at the user's request.**

- `main` is round 1 integrated (`fd90655`), plus this directory.
- At about 16:57 Claude resumed round 2 in five worktrees; the user stopped every Claude process at about 18:04. Codex resumed from that newer checkpoint at about 19:18.
  - Nothing was committed in that hour.
  - The newer implementations are being reconciled with the saved Codex work, rather than overwritten with older copies.
  - Checks run before the takeover do not verify the current files. Each affected lane must be verified again.
  - The Docker database stack is still up.

| Lane | Branch and worktree | Round 2 packages | State when paused |
|---|---|---|---|
| A | `lane/commercial`, `../ATCRAFT-lanes/commercial` | E1a dispatch linkage and top-up; A2 sales invoices | Uncommitted. Codex's work, partly taken over by Claude. Still to do: finish, verify and commit, then review each package. |
| B | `lane/operations`, `../ATCRAFT-lanes/operations` | B2 shifts and roster | Uncommitted. Codex's work, partly taken over by Claude. Still to do: finish, verify and commit, then review. |
| C | `lane/portals`, `../ATCRAFT-lanes/portals` | C1a scoped vendor portal; C1b Web Push | Codex's commit `383550c`, plus Claude's uncommitted review fixes for C1a. Still to do: finish the C1a review and commit, then review C1b. |
| D | `lane/supply`, `../ATCRAFT-lanes/supply` | E8a inventory operations | Uncommitted. Codex's work, partly taken over by Claude. Still to do: finish, verify and commit, then review. |
| E | `lane/quality`, `../ATCRAFT-lanes/quality` | E4 assembly and one QC-verdict path | Claude's uncommitted, partial backend. Still to do: finish the backend, then the frontend, then review. |

Current lane holders: Codex `/root/commercial_round2` holds A; `/root/project_state`
holds B and will take E after committing B; `/root/portals_round2` holds C and
performs a read-only review of D; `/root` holds D. E has no active writer until
the B handoff. The five-lane schedule remains authoritative.

To resume a lane, follow `specs/BRIEF.md` section 1a:
- take the uncommitted work over;
- never discard it wholesale.

`specs/PLAN.md` lists the steps still to run. A resumed run starts each lane with its "take over" step, even in lanes C and E, because both now hold uncommitted changes.

**Only one agent may work in a lane at a time.** Before starting in a lane:
- check that no other session is editing it: run `git status`, look at recent file times, and look for running servers on the lane's ports;
- then record here who holds it.

The specs in `specs/` are the single source of truth. `specs/PLAN.md` holds the schedule and a status log.
`workflows/` keeps the Claude Workflow scripts that run the lanes (`lane-build.js`) and the round integration (`integrate.js`); they read the specs from this directory.

## Historical completion sequence

Finish and independently review the round-2 packages, verify each lane, and
commit each lane with the repository hooks. Then follow
[the integration procedure](specs/INTEGRATOR.md): merge all completed lanes,
reconcile the migration chain, regenerate contracts, verify the combined
application and migrate the development database. Continue rounds 3–8 from the
master plan. A package or round is not complete merely because code exists.

Keep the original proposal PDF untracked. Do not change shared virtual
environments or dependency junctions. Do not bypass Git hooks.

The original checkpoint had no remote configured. The repository now has
`origin`, and current work is committed and pushed to `develop`. Use the current
[contributing workflow](../../README.md#contributing) and
[development standards](../development-standards.md) for new changes.

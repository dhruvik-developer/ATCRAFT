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

Updated 13 October 2026, about 09:50 IST. **Resumed by a Cursor agent (Claude Opus) as orchestrator, at the user's request.**

Between the checkpoint below and this resume, lane B committed B2 (`c768493`) and lane C committed its C1a/C1b review fixes (`e1c0e7b`). Both lanes are clean and verified. Lanes A, D and E still held uncommitted work; nothing had been written in any lane since 12 October about 19:51.

Each lane now has exactly one subagent holding it:

| Lane | Holder (Cursor subagent) | Step |
|---|---|---|
| A | lane-A takeover agent | Take over E1a and A2, then finish, verify and commit. A review of each package follows. |
| B | lane-B build agent | R3 B3a job-work invoice, full package. Starts now because B2 is done. |
| C | lane-C build agent | R3 C2a vendor app frontend, full package. Starts now because C1a/C1b are done. |
| D | lane-D takeover agent | Take over E8a, then finish, verify and commit. A review follows. |
| E | lane-E takeover agent | Take over the partial E4 backend, then finish the backend and build the frontend. A review follows. |

The next integration merges all five lanes once each holds only committed, reviewed work. Round 2 and the started round-3 packages go in together.

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

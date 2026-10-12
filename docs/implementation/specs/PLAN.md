# Master plan: make every proposal item present (ATCRAFT)

The goal (from the user): fix the Cursor audit points **and** make sure *each and everything* in "Atcraft Manufacturing ERP Development.pdf" is present. The PDF is transcribed in `PDF-CHECKLIST.md`.

The traceability audit (2026-10-11, 477 items, against main `cdac4e0`) found:

| Status | Count |
|---|---|
| PRESENT | 177 |
| PLANNED_FULL | 159 |
| PARTIAL / PLANNED_PARTIAL / ABSENT | 139 |
| Not software | 2 |

Results live in `../trace/trace.json` (full evidence, gap and recommendation per item), `../trace/gaps.md` (non-covered items) and `../trace/notes.md` (cross-cutting defects). The E-series work packages below close the 139.

## Lanes (git worktrees, each with its own dev DB and test DB)

| Lane | Path | Branch | Dev DB | Test DB |
|---|---|---|---|---|
| A | `C:/Project/Other/ATCRAFT-lanes/commercial` | `lane/commercial` | `atcraft_erp_lane_a` | `atcraft_erp_test_a` |
| B | `C:/Project/Other/ATCRAFT-lanes/operations` | `lane/operations` | `atcraft_erp_lane_b` | `atcraft_erp_test_b` |
| C | `C:/Project/Other/ATCRAFT-lanes/portals` | `lane/portals` | `atcraft_erp_lane_c` | `atcraft_erp_test_c` |
| D | `C:/Project/Other/ATCRAFT-lanes/supply` | `lane/supply` | `atcraft_erp_lane_d` | `atcraft_erp_test_d` |

Build workflows run per round, two lanes per workflow (each workflow is capped at 2 concurrent agents on this 4-core box); lane E, added in round 2, runs in a third workflow. Later rounds move a package into lane E whenever one lane would otherwise run much longer than the rest. The script lives at `../workflows/lane-build.js`; the integrator procedure is in `INTEGRATOR.md`.

## Schedule (`→` = sequential steps inside a lane)

| Round | Lane A | Lane B | Lane C | Lane D | Lane E |
|---|---|---|---|---|---|
| R1 | A1 | B1 | C0 | — | — |
| R2 | E1a → A2 | B2 | C1a → C1b | E8a | E4 |
| R3 | A3 → D3 | B3a → D5 → E10a | C2a → C2b | B3b → E2 | (rebalance) |
| R4 | A4 → E7b | E5 → E7a | C3a → C3b | E3a → E1b | (rebalance) |
| R5 | A5 → D2 | B4 (incl. E11) | C4a → C4b | E3b → D1 | (rebalance) |
| R6 | E9 | E6a → E6b | E8b | D4 → B5 | (rebalance) |
| R7 | E10b → E12 | buffer / fixes | buffer / fixes | K01–K10 scenario e2e + fixes | buffer / fixes |
| R8 | final integration: full suites, migrate/seed/demo, stack up, e2e, handbook rebuild, hooks, commit | | | | |

Every implementation step is followed by an adversarial review-and-fix step in the same lane. After each round the integrator merges A, B, C and D into main, re-chains Alembic, reconciles CHECK lists, renumbers assumptions, regenerates the contract, verifies, migrates the dev DB, fast-forwards the lanes and re-clones the lane DBs.

## Status log

- R1 started 16:40 (lanes A/B/C).
- Traceability audit done ~18:40.
- R1 lanes finished ~19:10 (A1, B1, C0 each implemented and reviewed; lane D reset to main, it had no work). Integrator for R1 started 19:15 (wf_44556ce4-8de).
- Round-1 carry-overs folded into E6a §8, E10a §6–7, E8a §2, C1a (portal invoice has no cost centre), A2 §16 (demo bank details), BRIEF (Select sentinel, typed fixtures, global schema names, handbook report count). Unowned gaps in BACKLOG.md for the R7 buffer lanes.
- R1 integrated at f1fa6a2 (19:15-20:00); sweep fixes + traps fd90655. R2 launched ~20:25 on base fd90655.
- R2 launched 20:25: wf_43bfafd9-7b0 (A: E1a full+review, A2 be/fe/review; B: B2 be/fe/review, E4 be/fe/review), wf_dff68a6c-22b (C: C1a be+review, C1b be+review; D: E8a be/fe/review).
- R2 agents all died within 20 s on the Claude weekly usage limit (resets 00:30 IST); no worktree was changed.
- 12 Oct 03:24: Codex preserved the plan in the repository (977dffd, `docs/implementation/`) and resumed R2 in lanes A–D. It committed C1a+C1b in lane C (383550c, 16:43, with `docs/implementation/lane-c-round2-checkpoint.md`) and left E1a+A2 (lane A), B2 (lane B) and E8a (lane D) uncommitted when its own limit stopped it at ~16:48. E4 was not started.
- 12 Oct 16:50: Claude resumed. Orphaned lane servers stopped. The specs in `docs/implementation/specs` are now the single source; the session scratchpad copy is retired. Lane E (`lane/quality`, `atcraft_erp_lane_e`, ports 8750/4750) created from 977dffd for E4.
- R2 continuation: A: resume E1a+A2 → review E1a → review A2; B: resume B2 → review B2; C: review C1a → review C1b; D: resume E8a → review E8a; E: E4 backend → frontend → review.
- 12 Oct ~18:04: paused at the user's request. All five lanes hold uncommitted work (see README checkpoint); nothing committed since 1c2438f. On resume, start every lane with a take-over step (BRIEF 1a), lanes C and E included.

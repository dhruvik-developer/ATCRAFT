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

Two build workflows run per round: lanes A+B in one, lanes C+D in the other. Each workflow is capped at 2 concurrent agents. The script lives at `../workflows/lane-build.js`; the integrator procedure is in `INTEGRATOR.md`.

## Schedule (`→` = sequential steps inside a lane)

| Round | Lane A | Lane B | Lane C | Lane D |
|---|---|---|---|---|
| R1 | A1 | B1 | C0 | — |
| R2 | E1a → A2 | B2 → E4 | C1a → C1b | E8a |
| R3 | A3 → D3 | B3a → D5 → E10a | C2a → C2b | B3b → E2 |
| R4 | A4 → E7b | E5 → E7a | C3a → C3b | E3a → E1b |
| R5 | A5 → D2 | B4 (incl. E11) | C4a → C4b | E3b → D1 |
| R6 | E9 | E6a → E6b | E8b | D4 → B5 |
| R7 | E10b → E12 | buffer / fixes | buffer / fixes | K01–K10 scenario e2e + fixes |
| R8 | final integration: full suites, migrate/seed/demo, stack up, e2e, handbook rebuild, hooks, commit | | | |

Every implementation step is followed by an adversarial review-and-fix step in the same lane. After each round the integrator merges A, B, C and D into main, re-chains Alembic, reconciles CHECK lists, renumbers assumptions, regenerates the contract, verifies, migrates the dev DB, fast-forwards the lanes and re-clones the lane DBs.

## Status log

- R1 started 16:40 (lanes A/B/C).
- Traceability audit done ~18:40.
- R1 lanes finished ~19:10 (A1, B1, C0 each implemented and reviewed; lane D reset to main, it had no work). Integrator for R1 started 19:15 (wf_44556ce4-8de).
- Round-1 carry-overs folded into E6a §8, E10a §6–7, E8a §2, C1a (portal invoice has no cost centre), A2 §16 (demo bank details), BRIEF (Select sentinel, typed fixtures, global schema names, handbook report count). Unowned gaps in BACKLOG.md for the R7 buffer lanes.
- R1 integrated at f1fa6a2 (19:15-20:00); sweep fixes + traps fd90655. R2 launched ~20:25 on base fd90655.
- R2 launched 20:25: wf_43bfafd9-7b0 (A: E1a full+review, A2 be/fe/review; B: B2 be/fe/review, E4 be/fe/review), wf_dff68a6c-22b (C: C1a be+review, C1b be+review; D: E8a be/fe/review).

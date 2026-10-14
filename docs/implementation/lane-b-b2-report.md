# B2 shift master and roster verification

Completed on 12 October 2026 in `lane/operations`. The interrupted implementation
was adopted and reconciled against the B2 specification. This report covers B2;
E4 is assigned to the separate quality lane.

The routing module owns the governed shift master, immutable UTC roster windows,
batched reference and overlap checks, two PostgreSQL exclusion constraints, and
audited lifecycle actions. Production selects shift IDs and keeps code snapshots;
legacy text is retained. Actual bookings remain accepted when they differ from
the roster. Inactive or vendor operators and retired machines are refused.

The roster screen has day, week and shift-master views and assignment, bulk,
copy and cancellation forms. Historical roster projections remain visible when
directories or active-shift pickers cannot resolve them. Production dialogs use
the operator directory and governed shift options, can name an operation, prefill
from the roster, and accept downtime-only bookings. Conflict details render all
the server's named clashes. Reports add daily production by shift and separate
rostered machine utilisation. Demo data creates four shifts, four operators and
a stable week of assignments; the original demo tag remains recognised.

## Migrations

| Revision | Parent | Purpose |
|---|---|---|
| `0ac18597894f` | `159ffb5b9cb4` | Shift and roster tables, exclusion constraints, production shift references |
| `0785162d1e47` | `0ac18597894f` | Grant four shift/roster permissions to system ADMIN and SUPER_ADMIN roles |
| `48e4df04913e` | `0785162d1e47` | Admit SHIFT_PRODUCTION in the report-snapshot CHECK |

`alembic heads` prints only `48e4df04913e`. The migration regression creates its
own disposable database, upgrades from the parent, checks actual columns,
exclusions and validated foreign keys, downgrades to the parent, then upgrades
again. It passed. The lane dev database was read-only checked at the same head;
it was not reset or re-stamped during this final reconciliation.

## Verification

All backend pytest commands used `-p no:cacheprovider -p no:locust`.

- `pytest tests/unit -q -o faulthandler_timeout=900`: **3,127 passed, 1 skipped**,
  59.30 seconds. An earlier run's sole failure was stale Postman output; the
  exporters were regenerated and the final full run passed.
- Targeted shift schema/window, exception-handler, report-KPI, roster service,
  concurrency, roster HTTP, production service, routing service and report-summary
  files: **237 passed**, 223.11 seconds.
- `pytest tests/integration/test_shift_master_migration.py -q`: **1 passed**,
  51.77 seconds, using a fresh disposable database.
- `mypy src`: clean, **205 source files**.
- Ruff check: clean. Ruff format check: **456 files already formatted**.
- Vitest: **41 passed** across work-order, shift-roster, production-register,
  production-detail and production-booking-dialog files. Runs used
  `--no-file-parallelism --maxWorkers=1`.
- Frontend lint, TypeScript, formatting and generated contract-type checks passed.
- `npm run build`: passed all bundle budgets and secret checks; first load
  **194.2 KB gzipped**, largest route **14.6 KB**, shared components **33.4 KB**.
- OpenAPI and Postman export/check passed: **389 endpoints, 800 schemas;
  531 Postman requests in 27 folders**.
- Demo masters/engineering ran twice. The second run created **0** records
  (207 existing masters, 52 existing engineering records).
- Real browser sweep on ports 8720/4720: **43 routes, broken=0,
  http-failures=0, thin=0**.
- Real UI: night-shift create/list, future roster create/list/cancel, and shift
  retirement passed, with no page errors. Temporary records were cancelled or
  retired with audit reasons.
- At 375 px: roster day/week, assignment and shift dialogs had no document
  overflow. Both test servers were stopped and both scratch scripts removed.

The existing Playwright journeys do not cover production bookings or rosters;
the new flow was exercised through the real-UI checks above.

## Integration notes

Permissions added: `shift.view`, `shift.manage`, `shift_roster.view`,
`shift_roster.manage`. Report key added: `SHIFT_PRODUCTION`. No settings,
document types or notification types were added. Assumptions: **§B2-1 through
§B2-8**, plus staffing/calendar open questions.

No B2 implementation gaps remain. Deployed non-system roles need an administrator
to grant the new rights, as documented. The integrator must chain migrations and
reconcile shared permission, report, routing, contract and documentation files
with the other lanes.

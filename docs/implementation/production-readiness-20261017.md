# Production readiness review — 17 October 2026

**Decision: the complete proposed ERP is not ready for production sign-off.**
The current implementation has useful, tested workflows, but confirmed financial
defects, failed release gates, missing proposal capabilities and incomplete UI
flows remain. Passing unit tests and building the web application do not close
those gaps.

Reviewed application code at `f8be35793782a48b4e9a8e24071510c7d08b3777`.
During the review, `develop` advanced to `9118b94901003ad56cc69c9d6c23cafbc4283e0e`;
that commit changes only the proposal status document, so the application findings
and CI evidence still apply. The earlier [proposal comparison](proposal-status-20261016.md)
provides the requirement IDs and broader work-package mapping. This report
refreshes the release decision using current source, fresh checks, disposable
PostgreSQL probes and the actual GitHub Actions results.

> **Update — 19 October 2026.** The 58-revision Alembic chain was squashed to
> one verified baseline, `20261021_0100`. The baseline was generated from the
> migrated head schema, not from ORM metadata, so the deployed constraints,
> defaults, indexes, CHECK vocabularies and append-only triggers are preserved.
> Old-chain and baseline databases matched on 134 tables, 2,771 columns, 3,392
> constraints, 1,054 indexes, 4 triggers and 284 functions; the development
> database and a clone were verified and stamped with
> `alembic stamp --purge 20261021_0100` without changing data. See
> [migration-squash-20261021.md](migration-squash-20261021.md). The 74
> migration/model differences noted below are intentionally unchanged by the
> squash and still require reviewed forward migrations.
>
> Current local verification after the returns/reallocation/debit-note and
> migration-squash work: backend full suite **5,506 passed, 0 skipped**;
> frontend **1,091 passed in 85 files**; Ruff, mypy, ESLint, Prettier,
> typecheck and contract checks clean. Browser/responsive UAT and the full
> production-stack e2e job remain open. For D4, the client selected
> certificate-backed DSC/PAdES over an app-only signature; the
> [compliance and procurement memo](d4-dsc-pades-compliance-20261019.md) is
> ready, and implementation is gated on legal/compliance confirmation and
> certificate procurement.
>
> **E3a delivered — 19 October 2026 (MRP engine depth).** MRP now discovers
> each item's low-level code in batched per-level reads and nets every item
> once, at its deepest level: gross requirement against free stock and dated
> in-time supply (including live production output), then only the net
> shortfall exploded through single-level BOM lines with per-level lead-time
> offsets. Manufacture suggestions use the routing lead time; expected
> availability is stamped on requirements, suggestions and demand lines and
> shown on run detail, the shortage report and sales-order detail, where
> "Use as promised date" writes the promise through the audited PATCH. The
> deliberate behaviour change - production output counts as supply before the
> explosion - is recorded in [assumptions §22](../assumptions.md). Fresh local
> verification after the change: backend **5,537 passed, 0 skipped**; frontend
> **1,093 passed in 85 files**; Ruff, mypy, ESLint, typecheck and contract
> checks clean.
>
> **E8b delivered - 22 October 2026 (batch and serial tracking).** Tracked
> stock now has one register and one capture contract: `/inventory/tracking`
> covers lots, units, lifecycle and trace; serials are captured on goods
> receipts, production and assembly output, transfers and adjustments; dispatch
> names picked units; job work carries one batch per same-item line. FEFO,
> expiry blocking and the daily `STOCK_EXPIRY` digest are enforced in the
> inventory engine. This closes P03.F3, P03.F4, P03.F6, P08.M11 and P09.M6.
> Deliberate boundaries remain: serial job work, serial bin moves/internal
> issues, tracked SKU transformations and barcode-label/PDF printing or
> pattern-generated label runs. Verification after this closeout: backend
> **5,591 passed, 0 skipped**; frontend **1,124 passed in 89 files**; Ruff,
> mypy, ESLint, typecheck, the production build and contract checks clean.
> Live UAT remains outstanding. See
> [the E8b specification](specs/E8b-batch-serial-tracking.md) and
> [assumptions](../assumptions.md) sections E8b-1 to E8b-3.

## Release gates currently failing

The [CI run for the reviewed application commit](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/38987034788)
is **failed**, with these observed results:

| Gate                            | Result and implication                                                                                                                                                                                                                                                                                             |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Backend dependency audit        | `pip-audit` reports **17 advisories in two locked packages**, PyJWT 2.13.0 and urllib3 2.7.0. The quality job stops before its lint, typing, contract and unit-test steps. Update and regenerate the locks, then rerun the security gate.                                                                          |
| Production backend image        | Trivy reports **11 high/critical OS findings and 12 high/critical Python-package findings**. These overlap with dependency-audit findings; they are not 40 distinct vulnerabilities. The backend scan fails and the frontend image scan is skipped. Review applicability, patch/rebuild and obtain a passing scan. |
| Production-stack end-to-end job | `atcraft-e2e-celery-worker-1` becomes **unhealthy** during stack startup. Seeding and the browser journeys are skipped. This run does not prove the deployed worker/PDF/notification flows or browser journeys work.                                                                                               |
| Migration/model agreement       | The verified baseline `20261021_0100` upgrades an empty database and matches the old head catalogue, but comparison with current ORM metadata still returns **74 diff entries** (intentionally unchanged by the squash). See the qualification below.                                                                                                                        |

The CI failures above are direct observations from the reviewed run, not a claim
that every reported dependency advisory has an exploitable path in this ERP.
They still prevent the repository's configured release gates from passing.

## Confirmed stock and money defects

The first five findings were reproduced through real services on an exclusively
owned disposable PostgreSQL database. The temporary probes asserted the faulty
behavior; their passing result confirms the reproduction, not correctness.

| Finding                                                        | Observed behavior                                                                                                                                                                                              | Code to fix                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cumulative credits can exceed the remaining receivable         | Invoice **2,344**, paid **2,144**: two credits of **118** are both accepted. Payments plus credits exceed the invoice by **36**. The API still reports balance **200**.                                        | [Credit validation](../../apps/api/src/modules/finance/credit_note_service.py:237) checks paid amount plus the new credit without subtracting previous issued credits; [invoice summary](../../apps/api/src/modules/finance/sales_invoice_service.py:511) ignores credits. |
| Receipt allocation ignores issued credits                      | Invoice **2,344** with an existing **118** credit still accepts payment allocation **2,344**, producing an economic over-settlement of **118** while the API reports zero balance/PAID.                        | [Allocation validation](../../apps/api/src/modules/finance/sales_invoice_service.py:765) and [status recomputation](../../apps/api/src/modules/finance/sales_invoice_service.py:792) use gross invoice amount and payments.                                                |
| A fully paid invoice's return cannot be received               | Receiving a non-QC return against a fully paid invoice triggers automatic crediting, which raises “Credit notes are supported only for issued or partially paid invoices.” The receipt transaction rolls back. | [Credit-note status gate](../../apps/api/src/modules/finance/credit_note_service.py:184), called by [return receipt](../../apps/api/src/modules/sales_returns/service.py:406). A paid-return/refund or customer-credit policy and implementation are required.             |
| Manual close bypasses return QC settlement                     | A received return can be closed while its inspection is pending. Accepting that inspection afterwards leaves the return CLOSED with **zero credit notes**.                                                     | [Return close path](../../apps/api/src/modules/sales_returns/router.py:86), [transition](../../apps/api/src/modules/sales_returns/service.py:409), and [QC callback status guard](../../apps/api/src/modules/sales_returns/service.py:466).                                |
| Cancelling a credit can leave the invoice incorrectly CREDITED | Invoice **2,344**, payment **2,226**, credit **118** becomes CREDITED. Cancelling the credit leaves it CREDITED although **118** is again due.                                                                 | [Cancellation/recomputation](../../apps/api/src/modules/finance/credit_note_service.py:397) queries issued credits before flushing the cancellation; sessions disable autoflush.                                                                                           |

Additional confirmed source findings:

- **Mixed QC/non-QC return lines can lose later automatic credit.**
  [Return crediting](../../apps/api/src/modules/sales_returns/service.py:556)
  stops when any credit already exists for the return. An immediate credit for
  non-QC lines can therefore suppress a later credit for QC-accepted lines.
- **Serial returns created by the UI cannot be received.** The
  [creation dialog](../../apps/web/src/features/sales/sales-returns-page.tsx:96)
  always sends `requires_qc: true`, including when serial IDs are entered.
  The [backend](../../apps/api/src/modules/sales_returns/service.py:333)
  explicitly refuses QC-required serial returns until per-serial inspection
  disposition is implemented. The safe refusal is present; the UI offers an
  unsupported journey.
- **Urgent reallocation does not update the recipient sales line.** Approval
  creates a [reservation with `demand_line_id=None`](../../apps/api/src/modules/allocation/service.py:1490).
  Sales and [dispatch](../../apps/api/src/modules/dispatch/service.py:1798)
  use the sales line's own `reservation_id`, so an approved urgent allocation
  can remain invisible to fulfilment. The request UI also lacks an approval inbox.
- **MRP has known quantity/date limitations.** The
  [engine documents overplanning](../../apps/api/src/modules/mrp/engine.py:123)
  around live-job components and nested components also sold directly.
  [Date phasing](../../apps/api/src/modules/mrp/engine.py:935) uses the root
  lead time rather than a complete per-level routing schedule.
- **Restricted cost/value data has alternate report paths.**
  [Product profitability](../../apps/api/src/modules/reports/router.py:362)
  and [inventory valuation](../../apps/api/src/modules/reports/router.py:421)
  require only `report.view`. Their
  [catalogue definitions](../../apps/api/src/modules/reports/service.py:674)
  also inherit that default, despite exposing cost/margin/value fields and
  separate inventory routes enforcing `inventory.valuation_view`. Complete the
  permission policy across direct reports, catalogue, jobs and generated files.
- **Other accounting integrations need review.** The
  [GST query](../../apps/api/src/modules/finance/repository.py:1938) aggregates
  invoice lines without issued-credit adjustments; customer outstanding
  projections are not updated by credit-note issuance/cancellation. Purchase
  invoice matching still reads
  [gross accepted receipt quantity](../../apps/api/src/modules/finance/repository.py:1597)
  without netting purchase returns, and purchase returns lack the associated
  vendor debit/payables/tax journal workflow.

## Required features and UI journeys still incomplete

These are current code findings, not claims that the whole associated module is
absent. Existing staff production, assembly, core QC, job work, purchasing,
dispatch, invoicing and payments should be retained.

| Area                                  | Development still required                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Sales planning and MRP outputs        | Automatic, idempotent shortfall planning and replanning; complete order-to-fulfilment links; executable job-work, transfer/reallocation and assembly conversions. [Sales detail](../../apps/web/src/features/sales/sales-order-detail-page.tsx:290) still directs users to run MRP manually; [job-work conversion](../../apps/api/src/modules/job_work/service.py:3297) deliberately refuses.                                                    |
| Tracked stock                         | Batch/serial tracking E8b is implemented: serial GRN, tracked production/assembly output, transfer and adjustment unit capture, serial dispatch, batch job work and the `/inventory/tracking` register/lifecycle. Remaining boundaries: serial job work, serial bin moves/internal issues, tracked SKU transformations and printed or pattern-generated barcode/PDF labels. Live UAT remains outstanding. |
| Vendor app and buyer inbound tracking | Backend support exists, but `/vendor/*` and `/procurement/inbound-tracking` screens are absent from the [route catalogue](../../apps/web/src/app/route-loaders.ts:26). Installable app/service worker and push opt-in/device controls are absent.                                                                                                                                                                                                |
| Dealer storefront and promotions      | Dealer catalogue/cart/order APIs exist; `/dealer/*` storefront and promotion/banner management APIs/screens remain missing. [DealerBanner](../../apps/api/src/modules/dealer_portal/models.py:182) is groundwork, not a complete feature. Dealer lifecycle event names exist without the corresponding emitters.                                                                                                                                 |
| Collections and product details       | Payment follow-up worklist, promises/notes/reminders and dealer-performance reporting remain. Item detail still explicitly returns empty [specifications/accessories](../../apps/api/src/modules/dealer_portal/service.py:501).                                                                                                                                                                                                                  |
| Procurement completion                | Purchase-return financial settlement and QC replacement workflow; remaining requisition/GRN/PO edit-close/dispute screens; executable duplicate merge/increase/cancel; status synchronization and purchase cost-centre propagation. See the broader proposal comparison for work-package mapping.                                                                                                                                                |
| Packing and dispatch                  | Item packing specifications, shortage-to-linked purchase requisition, generic/SO-origin packing UI, printable carton barcode labels and scan/history, forward dispatch planning and gate-time document panels. The [label service](../../apps/api/src/modules/packing/service.py:1881) returns data only; the [delivery UI](../../apps/web/src/features/dispatch/dispatch-page.tsx:602) omits supported actual delivery date input.              |
| Alerts, approvals and navigation      | Timed smart alerts/settings/recovery and a usable approval queue performing business actions remain. Notification links still target nonexistent `/sales/orders/{id}`, `/dispatch/delayed` and `/allocation/reservations/{id}` routes: [dealer](../../apps/api/src/modules/dealer_portal/service.py:1158), [dispatch](../../apps/api/src/modules/dispatch/service.py:2260), [allocation](../../apps/api/src/modules/allocation/service.py:1899). |
| Reporting and final acceptance        | Actual invoice/credit/FIFO profitability, missing purchase/inbound/dealer reports and final restricted-role acceptance of all ten proposal scenarios. Existing profitability is based on order revenue and standard cost.                                                                                                                                                                                                                        |

WhatsApp/SMS, courier-provider automation and PO digital signatures are also not
implemented. Keep them in the proposal's **future-enhancement tier** and the
agreed expanded scope, separate from the stock/money defects and must-have
journeys above.

The new returns page is usable for creating drafts, but also needs status-aware
actions, mutation errors/pending states, cancellation, return-detail/QC links and
supported serial disposition. It currently offers Receive/Close for every
selected status. Credit-note list/detail/PDF UI is now implemented; the earlier
“PDF generation missing” finding is stale.

## Verification and its limits

| Check                         | Evidence                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backend lint and formatting   | Fresh local Ruff checks passed; 594 files formatted correctly.                                                                                                                                                                                                                                                                                                      |
| Backend typing                | Fresh local mypy passed for 250 source files.                                                                                                                                                                                                                                                                                                                       |
| Backend unit tests            | Fresh local **3,620 passed, 1 skipped**. The previous nine unit failures have been fixed.                                                                                                                                                                                                                                                                           |
| API contracts                 | Fresh OpenAPI and Postman checks passed; generated frontend contract check passed.                                                                                                                                                                                                                                                                                  |
| Backend integration/API suite | The reviewed GitHub CI run reports **1,870 passed**. These are CI results, not a claimed fresh rerun of the whole suite locally.                                                                                                                                                                                                                                    |
| Frontend validation           | Fresh local lint, TypeScript, contract check and production build passed, including bundle budgets and credential scan. The same application's GitHub CI run reports **1,080 frontend tests in 83 files passed**, with npm audit, formatting and build passing.                                                                                                     |
| Additional local frontend run | A duplicate full local Vitest run encountered timeouts under concurrent load/low available memory and was stopped without final totals. Those timeouts are not established code regressions; the completed CI result above is the full-suite evidence.                                                                                                              |
| Fresh database                | At review time, an empty PostgreSQL database upgraded to the then-head `20261017_0120`, and the reference-data seed and demonstration-data load passed; migration/model comparison detected the differences below. After the 19 October squash, the baseline `20261021_0100` is tested from empty by `test_migration_baseline.py`; the 74 differences are intentionally unchanged. The exclusively owned database was dropped after review.                                                                                                  |
| Financial edge cases          | Disposable PostgreSQL service probes reproduced all five defects in the first findings table. These cases are not covered by the passing existing lifecycle-only return/credit tests.                                                                                                                                                                               |
| Browser/responsive coverage   | The Browser runtime reported no available browser, so this review could not perform a fresh interactive/responsive sign-off. Earlier [UI readiness evidence](../ui-readiness.md) covered 52 registered routes before the current returns/credits work; the current registry has 56. That earlier evidence does not certify every new screen or business transition. |

The 74 migration/model entries comprise 10 default changes, 9 missing indexes,
5 foreign-key removal/addition pairs, 22 CHECK removal/addition pairs and one
extra index. Paired operations are **not** separate missing features or 74
independent failures. Several differences are naming drift; others are real
defaults/index/deletion-policy differences, including tenant FKs using CASCADE
in migrations versus RESTRICT in models. Review and reconcile them using new
forward migrations, without rewriting deployed history. Integration fixtures
that rebuild from metadata cannot establish migration/model equivalence.

This review did not deploy, reset a business database, alter application code,
merge, commit or push. Disposable test records/databases were used for probes.
Production host configuration, TLS, off-box backup/restore, real integrations,
load/concurrency and operator handover still require the existing
[deployment go-live checks](../deployment.md).

## Recommended release order

1. Fix net invoice balance/credit/payment invariants, credit cancellation and
   paid-return handling; prevent premature return closure and complete supported
   serial UI/QC behavior. Add database-backed regression cases for these paths.
2. Resolve dependency/image scan gates and the Celery production health failure;
   rerun the complete release pipeline, including actual browser journeys.
3. Fix reallocation ownership, MRP correctness and cost/value authorization;
   reconcile migration/model differences through reviewed forward revisions.
4. Complete the required vendor/dealer/operations screens and proposal workflows,
   or obtain an explicit smaller release scope with unsupported flows disabled
   and acceptance criteria recorded.
5. Run restricted-role, responsive and end-to-end UAT against that exact release
   candidate, including return/credit cancellation, paid returns, serial/QC,
   repeated callbacks, partial dispatch, urgent allocation and backup restore.

An explicitly agreed limited pilot may have a smaller scope than the proposal.
It still requires the applicable financial/security defects and deployment gates
to be closed; the current code is not a completed full-scope production release.

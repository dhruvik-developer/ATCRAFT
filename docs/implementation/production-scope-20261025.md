# Full-scope implementation checkpoint — 25 October 2026

This is a source review of the current working tree, including the changes now
being verified. It is not a deployment approval or a claim that the authorized
full proposal is complete. The user confirmed the full scope; unfinished
packages below remain work to implement. Earlier audits retain their original
meaning and dates. Their findings must be checked against the current code.

On 26 October the user kept the DSC/PAdES signing setup decision pending and
asked that other work continue. D4 remains deferred until the signer integration
is selected; the other software packages below remain authorized work.

On 30 October the E3b transfer, reallocation and assembly conversions, then the
nightly scheduled run, were merged. The E3b row below now records only the
reorder and packing-material work that remains.

## Changes in the current delivery

| Area | Current implementation and evidence |
|---|---|
| Planning demand links | [Canonical provenance](../../apps/api/src/core/demand_provenance.py) retains sales/manual roots through BOM expansion independently of capped display facts. Purchase conversion saves complete references and refuses incomplete history atomically. The fulfilment trail labels shared component quantities as aggregate and uses actual PO-to-PR quantity allocations. |
| D1 duplicate actions and revisions | [Order changes](../../apps/api/src/modules/procurement/purchase_order_changes.py) implement merge, increase, separate draft with approval and selected-demand cancellation. Versioned amendments preserve line IDs, allocations and tax snapshots, require reapproval/resend, and serialize with all receipt creation. Duplicate approval captures confirmation; staff history and vendor status-only notices are implemented. Migrations 34/35 preserve existing values. The [verification record](release-verification-20261026.md) records source, migration, seed and responsive browser checks. |
| D5 specifications/accessories | Migration `20261101_0100`, [governed services](../../apps/api/src/modules/products/specification_service.py), staff panels and dealer descriptions/filters are implemented. Combined CI passed 6,338 backend tests, 1,493 frontend tests, 30 browser tests and both accessibility sweeps. All 97 handbook views were freshly captured, including populated category templates, zero/false values and both accessory relations. The [verification record](release-verification-20261026.md) records the focused, migration, seed and combined checks. |
| Return credits | [Credit-note validation and service](../../apps/api/src/modules/finance/credit_note_service.py) validate return state, tenant/customer/invoice linkage, accepted quantity ceilings and prior issued credits. Cancel/reissue counters use issued credits. [Financial integration cases](../../apps/api/tests/integration/test_sales_invoice_service.py) cover the corrected paths. |
| Customer-return FIFO costs and actual profitability | [Return costing](../../apps/api/src/modules/sales_returns/costing.py) restores recorded dispatch layers, including known zero costs, instead of current standard cost. Matching ordinary anchors and serial identities keep their own values; unanchored legacy lines share only the unclaimed source FIFO queue. The [product-profitability report](../../apps/api/src/modules/reports/repository.py) uses issued-invoice revenue less issued return credits, matched FIFO cost net of allocated return cost, and separately flags uninvoiced dispatch estimates, unpriced quantities and unmatched costs. PostgreSQL coverage is in [sales-invoice integration tests](../../apps/api/tests/integration/test_sales_invoice_service.py). |
| Credit settlement and PDF safety | [Cumulative credit rounding](../../apps/api/src/modules/finance/credit_note_rounding.py) allocates the remaining taxable value, GST components and original invoice round-off across partial credits, excluding cancelled credits. The stored residual appears in staff/dealer details and PDFs. [PDF text regressions](../../apps/api/tests/unit/test_pdf_text_security.py) prevent untrusted XML from causing file or HTTP image loads. Credit cancellation now requires an audit reason and handles pending requests and API refusals through the shared UI flow. |
| Serial job work | [Job-work service](../../apps/api/src/modules/job_work/service.py) issues named units, accounts for output, rejection, unprocessed return and scrap, and leaves vendor-held rework outstanding. Receipts name at most 500 units in total across all lines and dispositions; larger orders use partial receipts. |
| SKU conversion | [Serial processing](../../apps/api/src/modules/job_work/serial_tracking.py) keeps the physical label in a new SKU-scoped serial row and links immutable identities. Either identity's [trace](../../apps/api/src/modules/inventory/service.py) follows the family. Same-item processing keeps its original row. One-to-one serial-only conversion is supported; batch job-work must return the same item/lot, while non-one-to-one and mixed-tracking conversions are refused. Batch SKU transformations remain out of scope until an explicit batch-lineage policy is defined. |
| Per-unit QC | [QC serial processing](../../apps/api/src/modules/quality/serial_tracking.py) locks positions and units in batches and validates a disposition for every inspected unit. Final accepted/rejected/scrapped identities cannot be changed in a later cycle. GRN, production, assembly and job work pass their unit sets. [Serial integration tests](../../apps/api/tests/integration/test_serial_job_work.py) and tracked-output tests exercise these paths. |
| FINAL packing QC | [Packing serial rules](../../apps/api/src/modules/packing/serial_tracking.py) persist selected identities, require accepted units before sealing and check carton membership. [Withdrawal](../../apps/api/src/modules/quality/withdrawal.py) preserves an untouched cancelled packing check as terminal WITHDRAWN with result PENDING, retaining history without inventing a verdict. Decided or measured checks retain their QC flow. Migration [31](../../apps/api/migrations/versions/20261031_0100_withdraw_pending_packing_qc.py) guards downgrade against existing withdrawal history. |
| Staff inbound UI | [Inbound tracking](../../apps/web/src/features/procurement/inbound-tracking-page.tsx) uses server filters and paging. [PO controls](../../apps/web/src/features/procurement/purchase-order-inbound-panel.tsx) expose consignments, correction, confirmed gate arrival, typed documents, PDF jobs, vendor-decline guidance and explicit adoption of the vendor date. Shared forms preserve the vendor flows. Nine focused inbound tests and the 25 vendor screen tests passed. |
| Portal screens and content | [Vendor screens](../../apps/web/src/features/vendor-portal/vendor-pages.tsx), [dealer screens](../../apps/web/src/features/dealer-portal/) and [catalogue publishing](../../apps/web/src/features/portals/) are now present. Their existence supersedes the historical claim that only portal APIs exist. They still participate in final combined verification. |
| Vendor job-work / rejected-stock reissue | Migration `20261115_0100` adds the vendor progress-event ledger, overdue marker and vendor-role grant. [Vendor portal](../../apps/api/src/modules/vendor_portal/service.py) exposes vendor/category-scoped orders and audited progress events; challan and overdue notices reach vendor users. [Rework issue](../../apps/api/src/modules/job_work/service.py) locks the source receipt line, nets prior issues, and permits only physically returned `RETURN_TO_STOCK` output from `DAMAGED`; serial rework is constrained to the source rejects. The vendor app includes list/detail/progress screens. Focused integration, API, role, frontend and migration verification is recorded by the current change. |
| Browser sessions and push | [Authentication](../../apps/web/src/features/auth/auth-context.tsx) aborts old login, MFA and logout requests before replacement authentication can set cookies. Failed replacement credentials clear the previous account. Push setup/cleanup uses bounded foreground waits while keeping native device mutations serialized; the worker caches no application responses or tokens. [Focused session regressions](../../apps/web/src/features/auth/auth-logout-races.test.tsx) cover abort ordering and delayed cleanup. |
| Database and storage recovery | The [isolated restore drill](restore-drill-20261025.md) matched all 137 public table counts, five trigger definitions, document hashes and storage permissions, with rolled-back mutation-refusal proofs. A metadata-order regression retains every named foreign key without the previous cyclic-sort warning; the unfiltered schema comparison reports no changes. Production off-box recovery remains separate. |
| Demonstration data | [Serial workflows](../../apps/api/src/core/demo_data/serial_workflows.py) add same-item partial/rework/QC stock, four same-label conversions and FINAL-inspected sealed packing through normal services. The [PostgreSQL regression](../../apps/api/tests/integration/test_demo_serial_workflows.py) passed with unchanged ledger, audit, identity and document counts on rerun. See [local seed instructions](../local-seed-data.md) for searchable tags. |

Focused checks do not replace the combined backend/frontend suites, migration
round trips, complete fresh-seed run, restricted-role browser journeys, worker
execution, restore drill or deployment evidence. Those results belong in the
release verification record once they actually finish. No production server or
domain has been supplied, so live-host verification remains separate.

## Full-scope package status

| Package | Status and current evidence |
|---|---|
| [D2 WhatsApp/SMS](specs/D2-whatsapp-sms-channels.md) | Implemented with disabled-by-default channels, consent and preference gates, provider adapters, retry/recovery workers, delivery records and administration/profile UI. Fake-transport verification is complete; live provider activation still requires the customer's Meta and DLT accounts. |
| [D3 courier automation](specs/D3-courier-api-auto-tracking.md) | Implemented with encrypted provider accounts, signed tenant-resolved webhooks, idempotent events, bounded polling/refresh and tracking administration/UI. Fake-provider verification is complete; live polling/webhooks still require a courier account. |
| [D4 DSC/PAdES](specs/D4-po-digital-signature.md) | Not implemented. Follow the superseding [DSC/PAdES decision memo](d4-dsc-pades-compliance-20261019.md), rather than the old Ed25519 draft. Signer model/authority, retention and remote-signing choices plus certificate procurement remain unresolved; keep those dependencies distinct from completed PDF generation. |
| [E3b planning outputs/scheduling](specs/E3b-mrp-outputs-scheduling.md) | Implemented: transfer, reallocation, assembly and reorder suggestions; configurable per-company nightly [`mrp.scheduled_run`](../../apps/api/src/workers/tasks/mrp.py); and packing-material demand from [item packing specifications](../../apps/api/src/modules/packing/models.py). The [MRP engine](../../apps/api/src/modules/mrp/engine.py) retains level-by-level netting. |
| [E10b finance and reports](specs/E10b-finance-reports-dashboard.md) | Implemented, including invoice revenue net of credits and FIFO dispatch costs net of customer returns for actual profitability. Finance/report integrations, exports, permissions and management reporting were verified in the E10b completion checkpoint. |
| Vendor job-work / rejected-stock reissue | Implemented in this delivery: vendor/category-scoped orders and audited progress events, overdue reminders, and source-linked rework from physically returned `RETURN_TO_STOCK` output. The current focused and full-suite verification is recorded by the delivery checks. |
## Remaining serial-operation boundaries

[BinMoveLineCreate](../../apps/api/src/modules/inventory/schemas.py) now
persists a bounded set of named serial IDs. Serial lines require exactly one
unit per quantity; the service checks tenant, item, batch and exact source
position, then the movement engine locks and revalidates the units while
posting one paired transfer per unit and carrying its FIFO layers. The
inventory UI selects only serials at the chosen source bin. Internal issues
persist named serial IDs and post one `MATERIAL_ISSUE` per unit through the
locked movement engine; the existing FIFO valuation and approval threshold
remain in force. The issue UI selects only serials at the chosen source
position. Scrap disposal remains quantity-only because the serial is retired
when stock enters scrap.

Serial physical-count lines now persist every unit found and reconcile named
shortages and recoveries through reversible count movements while preserving
each unit's stock pool. Unknown, foreign, wrong-position and inconsistent
roster entries refuse atomically; recount clears quantities and identities
together. The
[PostgreSQL boundary regressions](../../apps/api/tests/integration/test_serial_inventory_operation_boundaries.py)
retain coverage for refusal atomicity and ordinary quantity-only behavior.
Bin moves have positive coverage for named-unit posting, destination location,
paired ledger rows and FIFO cost; serial internal issues post per-unit ledger
rows and preserve FIFO value. Unnamed bin moves and scrap disposals retain their
refusal regressions.

Label capabilities must be described precisely. The current
[serial field](../../apps/web/src/features/inventory/serial-numbers-field.tsx)
and register support bounded pattern/range generation as well as scanning or
pasting. The [packing screen](../../apps/web/src/features/packing/packing-order-dialog.tsx)
already prints carton barcode labels. The proposal's P12.M3 carton-label claim
must not be marked missing. Bulk serial registration now offers a tenant-scoped
PDF download of the registered labels: 24 QR labels per A4 page (3 columns by
8 rows), with readable item, serial and lot text, and a 500-unit request cap.
The endpoint requires the serial-management permission, refuses duplicate or
foreign serial IDs, and marks the response `no-store`; generating labels does
not move stock. A manager can also re-download a single unit's label from its
serial trace, without registering the unit again.

The next packages must close these software gaps while retaining the corrected
stock, accounting, security and audit invariants. Passing the current release
checks alone cannot close the remaining full-scope requirements.

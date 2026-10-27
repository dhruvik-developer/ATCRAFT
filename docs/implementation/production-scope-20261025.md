# Full-scope implementation checkpoint — 25 October 2026

This is a source review of the current working tree, including the changes now
being verified. It is not a deployment approval or a claim that the authorized
full proposal is complete. The user confirmed the full scope; unfinished
packages below remain work to implement. Earlier audits retain their original
meaning and dates. Their findings must be checked against the current code.

On 26 October the user kept the DSC/PAdES signing setup decision pending and
asked that other work continue. D4 remains deferred until the signer integration
is selected; the other software packages below remain authorized work.

## Changes in the current delivery

| Area | Current implementation and evidence |
|---|---|
| D5 specifications/accessories | Migration `20261101_0100`, [governed services](../../apps/api/src/modules/products/specification_service.py), staff panels and dealer descriptions/filters are implemented. Combined CI passed 6,338 backend tests, 1,493 frontend tests, 30 browser tests and both accessibility sweeps. All 97 handbook views were freshly captured, including populated category templates, zero/false values and both accessory relations. The [verification record](release-verification-20261026.md) records the focused, migration, seed and combined checks. |
| Return credits | [Credit-note validation and service](../../apps/api/src/modules/finance/credit_note_service.py) validate return state, tenant/customer/invoice linkage, accepted quantity ceilings and prior issued credits. Cancel/reissue counters use issued credits. [Financial integration cases](../../apps/api/tests/integration/test_sales_invoice_service.py) cover the corrected paths. |
| Credit settlement and PDF safety | [Cumulative credit rounding](../../apps/api/src/modules/finance/credit_note_rounding.py) allocates the remaining taxable value, GST components and original invoice round-off across partial credits, excluding cancelled credits. The stored residual appears in staff/dealer details and PDFs. [PDF text regressions](../../apps/api/tests/unit/test_pdf_text_security.py) prevent untrusted XML from causing file or HTTP image loads. Credit cancellation now requires an audit reason and handles pending requests and API refusals through the shared UI flow. |
| Serial job work | [Job-work service](../../apps/api/src/modules/job_work/service.py) issues named units, accounts for output, rejection, unprocessed return and scrap, and leaves vendor-held rework outstanding. Receipts name at most 500 units in total across all lines and dispositions; larger orders use partial receipts. |
| SKU conversion | [Serial processing](../../apps/api/src/modules/job_work/serial_tracking.py) keeps the physical label in a new SKU-scoped serial row and links immutable identities. Either identity's [trace](../../apps/api/src/modules/inventory/service.py) follows the family. Same-item processing keeps its original row. One-to-one serial-only conversion is supported; batch SKU conversion, mixed tracking and other ratios still require a lineage policy. |
| Per-unit QC | [QC serial processing](../../apps/api/src/modules/quality/serial_tracking.py) locks positions and units in batches and validates a disposition for every inspected unit. Final accepted/rejected/scrapped identities cannot be changed in a later cycle. GRN, production, assembly and job work pass their unit sets. [Serial integration tests](../../apps/api/tests/integration/test_serial_job_work.py) and tracked-output tests exercise these paths. |
| FINAL packing QC | [Packing serial rules](../../apps/api/src/modules/packing/serial_tracking.py) persist selected identities, require accepted units before sealing and check carton membership. [Withdrawal](../../apps/api/src/modules/quality/withdrawal.py) preserves an untouched cancelled packing check as terminal WITHDRAWN with result PENDING, retaining history without inventing a verdict. Decided or measured checks retain their QC flow. Migration [31](../../apps/api/migrations/versions/20261031_0100_withdraw_pending_packing_qc.py) guards downgrade against existing withdrawal history. |
| Staff inbound UI | [Inbound tracking](../../apps/web/src/features/procurement/inbound-tracking-page.tsx) uses server filters and paging. [PO controls](../../apps/web/src/features/procurement/purchase-order-inbound-panel.tsx) expose consignments, correction, confirmed gate arrival, typed documents, PDF jobs, vendor-decline guidance and explicit adoption of the vendor date. Shared forms preserve the vendor flows. Nine focused inbound tests and the 25 vendor screen tests passed. |
| Portal screens and content | [Vendor screens](../../apps/web/src/features/vendor-portal/vendor-pages.tsx), [dealer screens](../../apps/web/src/features/dealer-portal/) and [catalogue publishing](../../apps/web/src/features/portals/) are now present. Their existence supersedes the historical claim that only portal APIs exist. They still participate in final combined verification. |
| Browser sessions and push | [Authentication](../../apps/web/src/features/auth/auth-context.tsx) aborts old login, MFA and logout requests before replacement authentication can set cookies. Failed replacement credentials clear the previous account. Push setup/cleanup uses bounded foreground waits while keeping native device mutations serialized; the worker caches no application responses or tokens. [Focused session regressions](../../apps/web/src/features/auth/auth-logout-races.test.tsx) cover abort ordering and delayed cleanup. |
| Database and storage recovery | The [isolated restore drill](restore-drill-20261025.md) matched all 137 public table counts, five trigger definitions, document hashes and storage permissions, with rolled-back mutation-refusal proofs. A metadata-order regression retains every named foreign key without the previous cyclic-sort warning; the unfiltered schema comparison reports no changes. Production off-box recovery remains separate. |
| Demonstration data | [Serial workflows](../../apps/api/src/core/demo_data/serial_workflows.py) add same-item partial/rework/QC stock, four same-label conversions and FINAL-inspected sealed packing through normal services. The [PostgreSQL regression](../../apps/api/tests/integration/test_demo_serial_workflows.py) passed with unchanged ledger, audit, identity and document counts on rerun. See [local seed instructions](../local-seed-data.md) for searchable tags. |

Focused checks do not replace the combined backend/frontend suites, migration
round trips, complete fresh-seed run, restricted-role browser journeys, worker
execution, restore drill or deployment evidence. Those results belong in the
release verification record once they actually finish. No production server or
domain has been supplied, so live-host verification remains separate.

## Verified software gaps in the full scope

| Package | Current code evidence | Required implementation |
|---|---|---|
| [D2 WhatsApp/SMS](specs/D2-whatsapp-sms-channels.md) | [NotificationChannel](../../apps/api/src/modules/notifications/models.py) contains only IN_APP, EMAIL and PUSH. No messaging providers, phone/consent routes, channel templates, delivery history, send workers or messaging administration screen exist. | Implement the configured, disabled-by-default channels, consent and preference gates, audited template management, provider adapters, claim/retry/recovery workers, delivery records and profile/admin UI. Verify with fake transports before enabling real accounts. This is software work, not just credential setup. |
| [D3 courier automation](specs/D3-courier-api-auto-tracking.md) | Manual [dispatch tracking](../../apps/api/src/modules/dispatch/service.py) and [inbound consignments](../../apps/api/src/modules/procurement/inbound_service.py) exist. There is no courier_tracking module, provider account/event storage, authenticated webhook, provider polling, refresh API or administration UI. | Implement the provider adapter, encrypted accounts, signed/replay-safe tenant-resolved webhooks, idempotent events, bounded polling and refresh, downstream notifications, administration and tracking UI. Exercise inbound and outbound behavior with fake provider fixtures. |
| [D4 DSC/PAdES](specs/D4-po-digital-signature.md) | PO PDFs exist, but certificate signing, signer identity/evidence storage, signature validation and a verification UI do not. | Follow the superseding [DSC/PAdES decision memo](d4-dsc-pades-compliance-20261019.md), rather than the old Ed25519 draft. The memo explicitly leaves signer model/authority, retention and remote-signing choices plus certificate procurement to be confirmed before implementation. Keep these external/design dependencies distinct from completed PDF generation. |
| [D1 PO amendments](specs/D1-duplicate-po-actions.md) | [PO updates](../../apps/api/src/modules/procurement/service.py) require a draft; an approved quantity change is refused with guidance to cancel and raise another order. No amendment/revision API or UI exists. | Implement the specified increase/decrease amendment rules, received-quantity bounds, reapproval, audit/revision trail and UI. Preserve the seam for the eventual signed PDF revision. Duplicate warning behavior already exists and must be retained. |
| [E3b planning outputs/scheduling](specs/E3b-mrp-outputs-scheduling.md) | [MrpSuggestionType](../../apps/api/src/modules/mrp/models.py) explicitly says TRANSFER is not emitted and has no REALLOCATION type. [Conversion hooks](../../apps/api/src/modules/mrp/service.py) target purchase requisitions, production and job-work orders; stock-transfer/assembly targets and the scheduled-run task are absent. | Complete warehouse balancing, reallocation suggestions, conversion targets, company-controlled scheduled runs, reorder and packing-material demand. Do not repeat the old E3a depth-netting finding: the current [engine](../../apps/api/src/modules/mrp/engine.py) already implements level-by-level netting. |
| [E10b actual profitability](specs/E10b-finance-reports-dashboard.md) | `product_profitability` in the [report repository](../../apps/api/src/modules/reports/repository.py) sums sales-order quantities/prices and multiplies by current `Item.standard_cost`. | Use issued invoice revenue net of credits and actual dispatch/FIFO costs, with the specified explicit fallback for uninvoiced dispatches. The current calculation must not be represented as posted actual margin. Other E10b deliverables require their own acceptance checks. |
| Vendor job work / rejected-stock reissue | The [vendor portal routes](../../apps/api/src/modules/vendor_portal/router.py) expose purchase-order, receipt and finance functions, not job-work orders or vendor job-work process events. Staff [job work](../../apps/api/src/modules/job_work/service.py) and rework draft creation exist, but the ordinary reservation/issue path does not issue DAMAGED stock. | Add the authorized vendor job-work views/events/reminders and a defined, secure reservation/issue policy for physically returned rejected stock. A rework draft alone does not move that stock. Preserve vendor-held rework's existing custody semantics. |

## Remaining serial-operation boundaries

[BinMoveLineCreate and InternalIssueLineCreate](../../apps/api/src/modules/inventory/schemas.py)
have quantity, batch and location fields but no named serial set.
`_assert_document_lines` in the [inventory service](../../apps/api/src/modules/inventory/service.py)
explicitly refuses serial-tracked items for bin moves, internal issues and scrap
disposal. Supporting those flows requires persisted identity selections,
whole-unit/count checks, tenant/item/position validation, shared inventory
posting, UI selectors and integration tests. Removing the refusal alone would
lose unit traceability.

Physical-count sheets also remain quantity-based. The
[PostgreSQL boundary regressions](../../apps/api/tests/integration/test_serial_inventory_operation_boundaries.py)
now reproduce both missing-unit and extra-unit refusals: an unnamed serial
variance leaves the count unconfirmed, with ledger, FIFO, balances, identities,
audit and unrelated tenant/warehouse rows unchanged. A matching serial count
confirms without a stock movement. Mixed bin-move, internal-issue and scrap
documents are also refused atomically. All six cases pass. These tests protect
the current traceability boundary; they do not implement named-unit variance
reconciliation or the three unsupported operations.

Label capabilities must be described precisely. The current
[serial field](../../apps/web/src/features/inventory/serial-numbers-field.tsx)
and register support bounded pattern/range generation as well as scanning or
pasting. The [packing screen](../../apps/web/src/features/packing/packing-order-dialog.tsx)
already prints carton barcode labels. The proposal's P12.M3 carton-label claim
must not be marked missing. A separate unit-label PDF/print service is absent;
it is an additional boundary to scope explicitly, not evidence that carton
label printing is absent.

The next packages must close these software gaps while retaining the corrected
stock, accounting, security and audit invariants. Passing the current release
checks alone cannot close the remaining full-scope requirements.

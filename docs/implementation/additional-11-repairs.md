# Additional 11 repairs

Completed 15 October 2026 (Asia/Calcutta), branch `fix/additional-11-20261014`.
Baseline: main `142f04d`. The eleven repairs were integrated as separate commits
in audit order. Main advanced to round-2 integration `768a3e7` during testing;
that newer release was merged into the repair branch and verified again.
The subsequent documentation checkpoint `0db0d62` was also merged intact;
it changes planning notes only, with no runtime code changes.
Existing main notes and all five development lanes are preserved.

| Issue | Verified repair | Integrated commit |
|---|---|---|
| A01 | Recovery email changes require credential and protected-admin authority; revoke sessions/reset grants while preserving MFA | `dc4ac2c` |
| A02 | Warehouse scope applies before transfer/adjustment/count pagination, detail and action lookup; both transfer endpoints are required | `6e756db` |
| A03 | Procurement/production destinations and legacy posting actions enforce assignment; the movement engine checks every touched grain | `a2e98b9` |
| A04 | Repeated SO line links are refused on create/PATCH and before legacy drafts consume stock/reservations | `59bfe17` |
| A05 | Selected cartons reconcile exact items, batches, demand, quantities and serials; valid mixed-item cartons remain supported | `79284a7` |
| A06 | Customer order/invoice/receipt credit flows use company currency; omitted currency resolves to the company; vendor FX remains supported | `0b5e7ed` |
| A07 | Owned valuation export access checks creator, tenant and task provenance; generation, job/file reads and document lists retain the captured warehouse scope | `ae2c8e7` plus integration closure |
| A08 | Packing PDF status failures stop polling, display the error and allow retry of the same job or dismissal | `49b0e10` |
| A09 | Batch selection uses bounded server search/paging and resolves saved selections outside the current page | `e139ce4` |
| A10 | Unsupported noncash ADJUSTMENT cannot fabricate a cash/bank posting; historical drafts can be corrected/cancelled and posted bank legs reversed | `b1e98a2` |
| A11 | Explicit null in all five required update identity fields returns field-level 422; omitted and nullable fields retain their semantics | `9d74f46` |

Integration review also corrected sales-line previews so new orders do not guess
INR and existing orders use their denomination. Legacy adjustment forms now
require an explicit supported payment method before saving, with no automatic
conversion to a bank transfer. These follow-ups have dedicated component checks.
Round-2 compatibility checks cover invoice issue/reversal currency boundaries,
dealer denial on export queuing/polling/downloads, push-device cleanup on recovery
email changes, and mixed cartons with two genuinely linked/reserved SO lines.
Malformed duplicate dispatch input retains main's validation-error contract;
legacy duplicate drafts are still refused before stock moves.

Final valuation review closed the asynchronous warehouse-scope gap: queued
workbooks use server-captured assignments and filters, including safe handling of
legacy queued jobs. Narrowed access also blocks general report readers and direct
document downloads/lists. Scope snapshots remain while their workbook exists;
recognizable orphaned legacy valuation files fail closed for restricted readers.
Regression checks inspect actual two-warehouse workbook rows and totals, access
changes, generic permission paths and cleanup retention.

Combined verification:

| Check | Result |
|---|---|
| Full backend unit suite | Final 15 October run: 3,568 passed; one pre-existing intentional QC-case skip; two existing dependency deprecation warnings |
| Affected backend API/integration files and direct neighbours | 496 unique checks passed with `CI=true` and real PostgreSQL; initial run 495 passed/one duplicate-input error-class mismatch; after correction the entire dispatch module and its rule tests passed (55 checks), with no skips |
| Final security closure and direct neighbours | 15 October run: 120 passed with `CI=true` and real PostgreSQL; report-job/workbook access, cleanup/orphans, report summaries, document storage, users and recovery-email HTTP checks; no skips |
| Affected frontend files and direct neighbours | 180 unique checks passed across ten files; initial run 179 passed/one existing five-second timeout; isolated rerun passed in 2.72 seconds, without relaxing the timeout or assertion |
| Live local HTTP checks | 33 passed: authenticated registers, malformed updates, currency/method/duplicate-dispatch refusals, batch lookup/search and served SPA entry points |
| Backend Ruff/type checks | Ruff check/format passed across 551 files; mypy passed all 240 source files |
| Frontend static/release checks | ESLint, TypeScript, contract types and production build with bundle/secrets checks passed |
| API contracts | OpenAPI, Postman and TypeScript regenerated; contract checks passed; 625 requests in 29 folders |
| Deployment configuration | Production compose configuration check passed |
| Database setup | Single round-2 Alembic head `7f3070625160`; existing main migrations and seed succeeded on the isolated development database. Baseline demo rerun created no duplicates; the round-2 run added its new demonstration records |

Tests used isolated repair databases, including `atcraft_erp_test_additional11`;
live checks used `atcraft_erp_additional11` and temporary local test-server
sessions, which were stopped after checks. No production deployment, business
data reset, dependency change or new repair migration was performed. Existing
main migrations were applied only to the isolated verification database.
Visual browser UAT was unavailable because the
Browser runtime had no connected browser; live HTTP and component checks do not
substitute for that visual review.

The conservative financial policies are documented in
[assumptions](../assumptions.md): foreign customer credit flows require audited
conversion before they can be enabled, and noncash adjustments require an
explicit counterpart journal workflow. Posted legacy foreign receipts/invoices require
reconciliation. Existing financial data was not silently converted or corrected.

This closes the eleven verified issue groups in the
[additional bug audit](reports/additional-bugs-2026-10-13.md). The broader proposal
checklist and separate administrator-removal concurrency investigation require
their own completion audit; this report does not certify the whole ERP as
production-ready.

A separate read-only finding remains for the broader access audit: the generic
catalogue `/reports/inventory-valuation` uses company-wide summary queries, and
generic report generation reconstructs a company-wide worker context. This
package's A07 closure covers the finance valuation export path. Catalogue stock
report permissions and warehouse scope need a separate policy and runtime review;
they are not included in the eleven-issue completion claim.

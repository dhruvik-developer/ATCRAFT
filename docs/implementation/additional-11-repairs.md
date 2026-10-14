# Additional 11 repairs

Completed 14 October 2026 (Asia/Calcutta), branch `fix/additional-11-20261014`.
Baseline: main `142f04d`. The eleven repairs were integrated as separate commits
in audit order. Existing main notes and all five development lanes are preserved.

| Issue | Verified repair | Integrated commit |
|---|---|---|
| A01 | Recovery email changes require credential and protected-admin authority; revoke sessions/reset grants while preserving MFA | `dc4ac2c` |
| A02 | Warehouse scope applies before transfer/adjustment/count pagination, detail and action lookup; both transfer endpoints are required | `6e756db` |
| A03 | Procurement/production destinations and legacy posting actions enforce assignment; the movement engine checks every touched grain | `a2e98b9` |
| A04 | Repeated SO line links are refused on create/PATCH and before legacy drafts consume stock/reservations | `59bfe17` |
| A05 | Selected cartons reconcile exact items, batches, demand, quantities and serials; valid mixed-item cartons remain supported | `79284a7` |
| A06 | Customer order/receipt credit flows use company currency; omitted currency resolves to the company; vendor FX remains supported | `0b5e7ed` |
| A07 | Restricted exporters can poll/download their own valuation job using creator, tenant and task provenance, without broad document access | `ae2c8e7` |
| A08 | Packing PDF status failures stop polling, display the error and allow retry of the same job or dismissal | `49b0e10` |
| A09 | Batch selection uses bounded server search/paging and resolves saved selections outside the current page | `e139ce4` |
| A10 | Unsupported noncash ADJUSTMENT cannot fabricate a cash/bank posting; historical drafts can be corrected/cancelled and posted bank legs reversed | `b1e98a2` |
| A11 | Explicit null in all five required update identity fields returns field-level 422; omitted and nullable fields retain their semantics | `9d74f46` |

Integration review also corrected sales-line previews so new orders do not guess
INR and existing orders use their denomination. Legacy adjustment forms now
require an explicit supported payment method before saving, with no automatic
conversion to a bank transfer. These follow-ups have dedicated component checks.

Combined verification:

| Check | Result |
|---|---|
| Full backend unit suite | 3,115 passed; one pre-existing intentional QC-case skip |
| Affected backend API/integration files and direct neighbours | 396 passed, zero skips, with `CI=true` and real PostgreSQL |
| Affected frontend files and direct neighbours | 171 unique checks passed across nine files, including the final 32 payment and 44 sales checks |
| Live local HTTP checks | 33 passed: authenticated registers, malformed updates, currency/method/duplicate-dispatch refusals, batch lookup/search and served SPA entry points |
| Backend Ruff/type checks | Ruff check/format passed; mypy passed all 207 source files |
| Frontend static/release checks | ESLint, TypeScript, contract types and production build with bundle/secrets checks passed |
| API contracts | OpenAPI, Postman and TypeScript regenerated; contract checks passed; 517 requests in 27 folders |
| Deployment configuration | Production compose configuration check passed |
| Database setup | Single existing Alembic head `159ffb5b9cb4`; migration and seed on the isolated development database succeeded; demo rerun created no duplicate records |

Tests used isolated repair databases, including `atcraft_erp_test_additional11`;
live checks used `atcraft_erp_additional11` and temporary local test-server
sessions. No production deployment, business data reset, dependency change or
schema migration was performed. Visual browser UAT was unavailable because the
Browser runtime had no connected browser; live HTTP and component checks do not
substitute for that visual review.

The conservative financial policies are documented in
[assumptions](../assumptions.md): foreign customer credit flows require audited
conversion before they can be enabled, and noncash adjustments require an
explicit counterpart journal workflow. Posted legacy foreign receipts require
reconciliation. Existing financial data was not silently converted or corrected.

This closes the eleven verified issue groups in the
[additional bug audit](reports/additional-bugs-2026-10-13.md). The earlier
proposal-feature gaps and the separately suspected administrator-removal race
remain separate from this repair package; this report does not certify the whole
ERP as production-ready.

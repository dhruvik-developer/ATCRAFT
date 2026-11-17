# Production hardening — 17 November 2026

The reported code defects and focused verification are complete. Production
commissioning still requires a target environment and the go-live checklist;
this checkpoint does not authorize a production deployment.

## Permission fixes

- Every dashboard business tile now requires its owning permission, including
  the original stock, purchase, sales, production, quality and dispatch tiles.
  Withheld values are null. Stock totals and refresh times respect warehouse
  assignments; cache version v4 separates both permissions and warehouse scope.
- Generic document lists, metadata and downloads enforce saved reports' domain
  and captured-column permissions. A redacted workbook remains readable without
  valuation rights, while missing or malformed column evidence fails closed.
  Grants and revocations apply to existing files as well as new exports.
- Snapshot evidence survives job expiry until its file is removed. Conservative
  legacy-file checks preserve the warehouse and permission boundaries after old
  cleanup removed provenance. Finance-owned valuation jobs retain their separate
  scope checks.
- Dead-stock, scrap-register and departmental-consumption exports now capture
  and restore warehouse assignments through the shared report path, matching
  their inline queries.

## Release checks repaired

- The dispatch regression patches the current fulfilled-order repository while
  preserving its checks for state-machine hops and audit actions.
- Dashboard controls follow API permissions, preserve authorized zero values,
  and withhold absent figures. A valuation-only reader receives no inaccessible
  inventory link; a failed read does not claim the reader lacks permissions.
- The 30-day trends panel loads separately from the first dashboard paint. The
  pinned Node 22 production image passes the unchanged 210 KiB first-load limit
  at 209.4 KiB; bundle credential scanning also passes.
- OpenAPI and generated frontend types reflect nullable dashboard fields.
  Handwritten dashboard/trend mirrors are registered with the contract guard.
- The existing TypeScript ESLint dependency family is updated to 8.73.0 to use
  patched minimatch 11.0.9 (GHSA-g3hv-75wg-m249). Only development dependency
  entries change; all 117 production dependency entries and package.json remain
  identical. This adds the upstream development-only glob helper needed by the
  patched matcher. npm audit reports no vulnerabilities at any severity.

## Verification

- Focused report/document PostgreSQL and HTTP suites: 81 passed.
- Additional legacy catalogue XLSX permission/scope regression: 1 passed.
- Dashboard HTTP regressions: 5 passed.
- Frontend suite: 2,114 tests passed across 157 files; the lint-only dependency
  refresh was additionally checked with 23 lint-rule tests, ESLint and TypeScript.
- Frontend contract mirror checks: 48 passed.
- Backend lint, formatting, mypy, runtime dependency audit, Compose parsing,
  Bash syntax and host-Nginx template validation passed.
- Both production images build successfully from their pinned Dockerfiles.
- Full backend coverage, final Git hooks, and production-stack browser/image
  gates are required for the pushed revision. Their authoritative result is
  recorded by [the CI workflow](../../.github/workflows/ci.yml) in
  [GitHub Actions](https://github.com/dhruvik-developer/ATCRAFT/actions).

The full backend gate runs in Linux with the current hash-locked dependencies:
Windows Code Integrity blocks the local psycopg native wrapper. No operating
system security settings or dependency security floors were weakened.

## Repository and commissioning

The abandoned `atcraft_erp_test_p2` and `atcraft_erp_test_p3` databases were
verified idle and test-only, then removed. Verification reuses the single
`atcraft-erp` stack and the default `atcraft_erp_test`; TEST_POSTGRES_DB remains
unset. Temporary migration databases must be removed by their fixture cleanup.

No ATCRAFT production server or domain is available. TLS and renewal, provider
accounts, encrypted off-box database and file recovery, representative production
load, restricted-role business acceptance and operator sign-off remain open in
[the commissioning checklist](../deployment.md#production-commissioning-sequence).
No production deployment has been performed.

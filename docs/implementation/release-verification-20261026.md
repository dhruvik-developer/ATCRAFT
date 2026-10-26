# Local release verification - 26 October 2026

This checkpoint covers the dealer/vendor interfaces, serial job work and QC,
packing, financial corrections, browser sessions and release hardening. It does
not close the [remaining full-scope packages](production-scope-20261025.md) or
approve deployment to a real host. The user kept the DSC/PAdES signer decision
pending while other work continues.

## Verified application behavior

| Check | Result |
|---|---|
| Browser journeys on patched web/proxy images | All 24 passed, including scoped portals at 375/768/1440 px, cookie restoration, documents without redirects, write paths, permissions and the order-to-receipt journey |
| Full responsive readiness | All 79 application routes; 474 screen/tab/viewport checks and 213 opened forms; zero failures, overflow or clipped controls |
| Accessibility | 66 screens in each theme; zero violations or render/API failures |
| Read-only browser guards | No business writes attempted during responsive/accessibility inspection; portal fixtures were prepared separately through normal APIs |
| Backend static checks | Ruff clean; mypy passed for 273 source files |
| API contracts | OpenAPI and Postman checks passed; generated frontend contract matched and TypeScript/build checks passed |
| Fresh local seed CLI | Passed in 230.23 seconds from an empty disposable database with a custom company/admin, generated PDFs, and unchanged table/file/password data on rerun |
| Full demo rerun | 342 references across 51 kinds; zero records created in every unchanged stage |
| Schema agreement | Unfiltered `alembic check` reports no upgrade operations and no cyclic-sort warning |
| Schema creation and rollback | Six dependency-order, identifier and fresh-baseline/round-trip checks passed after the metadata-order correction |
| Serial operation boundaries | Six PostgreSQL cases passed: three atomic document refusals, matching count confirmation, and both serial variance directions refused without partial posting |
| Database/storage recovery | [Restore drill](restore-drill-20261025.md) matched all 137 public table counts, five immutability triggers, 10 documents/files, hashes and permissions; nine immutable mutations refused and proof rolled back |

The broad backend run recorded 6,110 passes and 81.34% branch coverage, with
one failure caused by an obsolete hardcoded migration-head expectation. The
test now resolves the actual head and its fresh-install/rollback regression
passes. Subsequent financial, PDF, serial and schema fixes passed their focused
PostgreSQL/unit checks. The complete frontend run recorded 1,396 passing tests;
later session/MFA corrections passed 157 focused cases, and credit-note/dealer
settlement checks passed 21. The Git pre-push hooks and CI rerun the current
whole-tree checks; these earlier counts are not a claim that every function has
complete coverage.

The responsive run found a real tablet catalogue overflow. Long offer labels
and quantity/action rows now wrap inside their cards, and the 768 px browser
regression passes. Portal reload checks explicitly verify the expected initial
401 followed by successful refresh, profile and scoped identity reads; other
HTTP/render failures remain failures.

## Container security and resource use

The frontend and edge proxy share the digest-pinned official
`nginx:1.30.6-alpine3.24-slim` runtime. Its 21 packages omit unused image/njs
modules and their Brotli, X11 and expat dependency chain. This removes the
reported Brotli vulnerability and the need for separate frontend APK patches.
The frontend runs as UID 101; its health probe and `nginx -t` passed. The edge
configuration also passed validation against the same runtime.

Trivy vulnerability and secret scans reported zero HIGH/CRITICAL findings for
both web and edge images, without ignoring unfixed vulnerabilities or using an
ignore list. The backend image passed the existing HIGH/CRITICAL scan policy,
which excludes unfixed vulnerabilities. CI now explicitly scans all three
images for vulnerabilities and secrets and resolves the edge image from the
deployed Compose configuration.

The broad responsive/accessibility runs used the preceding web image. Its
206 static files are byte-identical to the patched runtime candidate, verified
by path, size and SHA-256. The complete 24-test journey suite then passed on
the patched web and edge stack. The shared asset-manifest digest is
`8069835635e75e7359199f63c1da48485658eec2aee9635adbb1390d01c5fb8d`.

The production build passed all bundle and embedded-credential checks. Initial
load is 203.2 KB gzip against the 210 KB budget; the heaviest screen closure is
37.9 KB against 40 KB. The shared Decimal/date-formatting chunk has its own
22 KB budget rather than being mistaken for a route.

## Evidence and limits

Private machine-readable reports, logs and restore archives remain outside Git
under `%TEMP%\atcraft-release-20261024`. Browser reports and handbook outputs
follow the repository's ignored-artifact rules. Existing account passwords,
source database data and protected environment files were preserved.

Git hooks remain enabled. The repository's
[CI workflow](../../.github/workflows/ci.yml) supplies the result for each pushed
commit; local candidate checks do not substitute for that commit's CI outcome.

There is no production server/domain. Public TLS, real delivery/provider
accounts, encrypted off-box backups, production-sized load/recovery and
operational ownership still require the [go-live gate](../deployment.md#go-live-gate).
Remaining software packages are listed in the current scope checkpoint.

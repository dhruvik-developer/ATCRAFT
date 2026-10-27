# Local release verification - 26 October 2026

This checkpoint covers the dealer/vendor interfaces, serial job work and QC,
packing, financial corrections, browser sessions and release hardening. It does
not close the [remaining full-scope packages](production-scope-20261025.md) or
approve deployment to a real host. The user kept the DSC/PAdES signer decision
pending while other work continues.

The shared `b4804d9` checkpoint passed all five CI jobs on both
[develop](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/40378297997)
and [main](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/40378299144):
backend quality, backend integration, frontend, production configuration/image
scans and browser journeys. The develop run recorded 6,338 backend passes with
81.63% combined statement/branch coverage and 30 browser tests. Accessibility
checks covered 66 screens in each theme with zero violated rules or render
failures. The frontend suite passed 1,493 tests across 113 files. These combined
checks include D5 and transient session recovery.

## Canonical local installation

The owner's [single-checkout instructions](../../AGENTS.md) now govern local
work. Only `C:\Project\Other\ATCRAFT` remains as a working copy. All 56 changed
or new D5 files from the former checkout were checked against the main copy;
the four differences were retained improvements to identifier naming, export
ordering and the category-retirement regression. The obsolete dependency
junctions were detached before removing the old worktree, preserving the main
virtual environment and both Node installations.

The sole project stack is `atcraft-erp`. Its development database is
`atcraft_erp`; tests use `atcraft_erp_test` with `TEST_POSTGRES_DB` unset. Twenty-nine
disposable databases and seven extra volumes were archived and verified before
removal. The extra rehearsal containers and networks were removed. The primary
database was archived and preserved; other projects were not changed.
The cleanup manifest and 64,711,664 bytes of archives are outside Git under
`%TEMP%\atcraft-single-checkout-20261026`.

The primary database upgraded from `20261027_0100` to `20261101_0100` with every
existing table's row count preserved: 136 existing tables and five added tables.
An unfiltered `alembic check` found no changes. The complete local seed now
registers 351 references across 52 kinds, including D5 descriptions/accessories;
its rerun created no replacement PDFs. Existing account passwords were retained.
The current local UI/API use ports 5173/8000 rather than the removed rehearsal's
18081 port.

## D5 and session-recovery verification

The consolidated D5 backend run passed 167 tests with no skips, including
category-lock concurrency, retained archived references, typed values,
permission and tenant boundaries, preserving seed behavior and migration
round trips. Ruff passed 634 files and mypy passed 278 source files. The
frontend candidate passed 222 relevant cases, generated-contract checks and
the unchanged production budgets. Three new browser journeys passed at
375/768/1440 pixels using actual staff and bound dealer principals. Handbook
validation covers 97 views; product-detail capture can select the populated
`DM-FG-LEVER-AB` fixture through normal read-only list navigation.

The complete backend coverage run passed 6,338 tests with two deprecation
warnings and 81.64% combined statement/branch coverage. The standard test
database was used with no `TEST_POSTGRES_DB` override.

The local browser run exposed transient authentication failures under rate
limiting. Refresh/profile recovery now preserves the session and presents an
explicit retry while protected content remains blocked until identity is
verified. All 179 focused authentication/client/router tests passed, as did two
real-cookie Chromium recovery cases plus their setup. The pushed checkpoint's
30-test browser run and both accessibility sweeps passed as recorded above.
The 27 October read-only capture passed all 97 views with zero failures using
staff, bound vendor and bound dealer accounts. The rebuilt 199-page handbook
has an image for every view and no out-of-page text. All pages were rendered;
contact sheets and selected product, job-work, packing and dealer pages were
reviewed visually. The selected demo handle shows its actual zero/false values
and both accessory relations. The capture now checks the requested code in
the rendered link, rather than trusting an ineffective URL filter. Optional
pacing respects the local API limits without relaxing HTTP-error checks.
All 45 offline capture/navigation/portal tests pass.

The owner now requires only `main` and `develop`, synchronized to the same
verified revision locally and remotely. The consolidation synchronized both
branches locally and on origin to `b4804d9`. Superseded snapshot histories were
retained with an ordinary merge whose application tree was unchanged. Four obsolete
local branches and eight remote dependency-update branches were removed; no
open pull requests remain. Dependency version-update PR generation is paused
through the seven update configurations; CI audits remain enabled.
The deployment named `dev` now tracks `develop`, so no third Git branch is
required. The branch mapping passed 36 release/configuration unit cases and
shell syntax validation.

## Previously verified application behavior

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
| Handbook | All 93 views recaptured with bound portal principals; rebuilt 195-page PDF has no missing images or out-of-page text, with contact-sheet and selected-page visual review |

The broad backend run recorded 6,110 passes and 81.34% branch coverage, with
one failure caused by an obsolete hardcoded migration-head expectation. The
test now resolves the actual head and its fresh-install/rollback regression
passes. Subsequent financial, PDF, serial and schema fixes passed their focused
PostgreSQL/unit checks. The complete frontend run recorded 1,396 passing tests;
later session/MFA corrections passed 157 focused cases, and credit-note/dealer
settlement checks passed 21. The Git pre-push hooks and CI rerun the current
whole-tree checks; these earlier counts are not a claim that every function has
complete coverage.

The [first pushed checkpoint's CI run](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/40256351834)
passed frontend tests/builds, backend quality checks, all three image scans and
24 browser journeys. Its full backend run recorded 6,169 passes, two failures
and 81.36% branch coverage. The failures were a dealer credit assertion that
omitted the allocated one-paisa invoice round-off and an audit assertion that
did not order tied timestamps. Both regressions now pass within their complete
19-case PostgreSQL modules. Fresh-seed accessibility also exposed an unfocusable
finance table scroll area. The shared table now provides a named, focusable
group with a visible keyboard focus indicator; Chromium verified Tab access and
ArrowRight scrolling at 375 px. Using a group avoids duplicate page landmarks
when two tables are present. The completed CI runs above verify these
corrections.

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

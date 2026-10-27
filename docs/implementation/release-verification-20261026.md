# Local release verification - 26-27 October 2026

This checkpoint covers the dealer/vendor interfaces, serial job work and QC,
packing, financial corrections, browser sessions and release hardening. It does
not close the [remaining full-scope packages](production-scope-20261025.md) or
approve deployment to a real host. The user kept the DSC/PAdES signer decision
pending while other work continues.

The shared `a3a6673` checkpoint passed all five CI jobs on both
[develop](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/40520512990)
and [main](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/40520512859):
backend quality, backend integration, frontend, production configuration/image
scans and browser journeys. The develop run recorded 6,600 backend passes with
81.82% combined statement/branch coverage and 34 browser tests. Accessibility
checks covered 66 screens in each theme with zero violated rules or render
failures. The frontend suite passed 1,544 tests across 115 files. These combined
checks include D1, D5, transient session recovery, planning provenance and strict
E2E Node/browser typing. The D1 section below records its focused verification.

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

## Planning demand links - 27 October follow-up

The planning follow-up retains canonical sales/manual demand identities through
multi-level BOM expansion, independently of the bounded display listing.
Purchase conversion stores complete references and refuses missing, malformed
or over-bound history without closing the suggestion, creating a requisition
or writing conversion audit. Shared component quantities remain labelled as
aggregate in the sales fulfilment trail. This completes the provenance
foundation, not the remaining PO duplicate actions or amendment workflow.

Local verification passed all 4,224 backend unit tests, 48 PostgreSQL MRP cases,
15 fulfilment/conversion HTTP cases and 108 procurement/guard cases. The latest
cross-module validation changes also passed their nine-case focused rerun.
Ruff passed with 634 formatted files and mypy passed 280 source files. Frontend
typing and the two focused MRP regressions passed. The production build retained
the existing budgets: 203.8 KB initial load against 210 KB, with all 207 bundled
files passing the credential scan. OpenAPI has 541 paths and 1,160 schemas;
generated types and all 717 Postman requests match.

Revision `20261102_0100` adds the JSONB GIN lookup index. Its transactional
build bounds lock acquisition to five seconds; large histories require a
maintenance window because table writes conflict with the build lock. Index
round-trip and full fresh-install/downgrade/rebuild tests passed. The canonical
database was upgraded with every table's row count and the complete saved MRP
suggestion hash preserved; unfiltered `alembic check` found no drift.

The complete fresh seed CLI regression passed in 164.48 seconds, including
generated files, all seeded workflow areas and unchanged rows/files/passwords
on its second invocation. Its temporary database is dropped by the fixture.
The canonical API readiness checks and UI returned success. The handbook's
planning rules were updated and the 199-page output was rendered again with
zero out-of-page text and visual review of the affected pages.

The Git hooks and pushed-revision CI repeat the whole-tree gates; each workflow
run is evidence for its own commit. The completed checkpoint's CI above does
not substitute for the workflow of a later source revision.

The E2E folder now has an explicit strict TypeScript project with Node.js and
browser types. Its compiler matches the frontend's locked 5.9.3 version. The
full E2E typecheck passes; the unit-of-measure lookup uses the generated API
schema, all 30 browser tests are discoverable and the test dependencies have
zero audit findings. CI checks these types before running the journeys, and
both `make test-e2e` variants use the same npm pretest check.

## D1 purchase-order actions and amendment verification

The D1 follow-up implements merge, increase, a separate draft requiring duplicate
approval, and cancellation of explicitly selected outstanding requisition demand.
Issued orders can be amended before any receipt history exists. Revisions retain
line identities, agreed taxes and durable source allocations, return to approval,
and require resend. Complete before/after snapshots are available through paged
staff history. Previously sent orders expose only a status notice to their scoped
vendor while revised commercial content remains private. The creator and latest
amendment author cannot approve their own work except through the existing
explicit platform-superuser exception.

The complete backend unit suite passed 4,355 tests with two third-party
deprecation warnings. Local PostgreSQL verification includes a 61-case
service/HTTP/search/fulfilment and fresh-seed run, six concurrency and revision
cases, and six migration/tenant cases. The final 47-case run passed after adding
warehouse-scoped demand
cancellation and atomic numeric-limit refusals. Twenty pure arithmetic boundary
cases also pass. These runs overlap; their counts are not summed into a claimed
whole-suite total. Deterministic receipt/amendment races prove that both locking
orders serialize and that a draft receipt prevents a subsequent amendment.
Overflowing derived amounts and merged quantities produce controlled input
errors without changing the original document or exposing database diagnostics.
Final review added a passing pooled-counter regression: individually valid
allocations cannot overflow a requisition's stored ordered quantity. Both numeric
integration cases and all 15 D1 HTTP cases pass, including preserved counters
and no partially created documents after refusal.

The canonical database is at `20261104_0100` (revision 35). Revision 34 preserved
every value in the 140 existing base tables and backfilled two source mappings.
Revision 35 preserved all 141 table snapshots, including allocation identities,
while shortening the two new composite foreign-key names. Unfiltered
`alembic check` reports no drift. Fresh-install, safe rollback and refusal cases
passed. Both revisions use a five-second lock acquisition bound; large histories
still require the maintenance window described in the migration. The only local
application databases remain `atcraft_erp` and `atcraft_erp_test`, with
`TEST_POSTGRES_DB` unset.

A final six-case migration rerun also covers a concurrent amendment attempting
to commit after rollback preflight but before its first destructive statement.
The downgrade now freezes all affected writes before checking retained history;
the independent writer times out, and the test rolls back the schema changes.

The frontend suite passed 1,543 tests in 115 files. All four new live browser
journeys passed: actual amendments and saved history at 375, 768 and 1440 pixels,
plus explicit duplicate approval confirmation. Strict frontend and E2E typing,
formatting, lint and generated-contract checks passed. The production build
retained the 203.8 KB initial-load budget result against 210 KB, the 38.0 KB
largest screen closure against 40 KB, and a clean credential scan of 210 assets.
OpenAPI contains 545 paths and 1,176 schemas; all 721 Postman requests match.
Final review added a seventeenth D1 UI case proving that a background refetch
cannot replace the version attached to already edited lines. The opening order
snapshot stays fixed until the editor closes; a stale submission receives a
conflict without discarding the user's edits. All 17 focused cases pass.

The single local seed entrypoint now adds four D1 documents, including a pooled
order with two retained quantities and a separate order awaiting duplicate
approval. The complete fresh CLI test verifies that its second invocation leaves
the document rows, allocations, files and existing passwords unchanged. On the
canonical installation the additive run created only these four documents and
registered 357 references across 52 kinds; it replaced no PDFs or passwords.

All 97 handbook views were recaptured with their correctly bound principals and
zero failures. Staff and vendor captures select the same pending revision through
normal read-only lists. The rebuilt 199-page guide was rendered in full, with
zero out-of-page text, and its contact sheets and affected pages were visually
reviewed. The offline browser/capture helpers passed all 46 tests; their guard
recognizes 403 non-session API writes and blocks them before dispatch.

The mandatory hooks and the workflow of the pushed D1 commit are the whole-tree
gates for this follow-up. The earlier `f779a2e` CI result above remains evidence
for that earlier checkpoint. D2 messaging, D3 courier automation, the remaining
planning/profitability/serial packages, the deferred D4 signer setup and the real
deployment gate remain open in the current scope document.

## Customer-return FIFO costing follow-up

The next source review reproduced a stock-valuation error: ten units dispatched
at a recorded FIFO value of 100 were received back at 990 after standard cost
changed to 99. The same return of recorded zero-cost goods was also valued at
990. Customer returns now carry the source dispatch layers through the shared
inventory engine. Matching ordinary movement anchors and individual serials
keep their own costs, while unanchored legacy lines use only the remaining
dispatch/item/batch FIFO queue. Partial returns advance their source counters;
missing source history refuses receipt atomically. Locked return and dispatch
reads refresh state before posting. Existing ledger rows are preserved.

All 75 focused checks passed in 226.30 seconds: pure return-cost boundaries,
real ordinary/serial/legacy returns, invoice/credit regressions, the complete
inventory FIFO module and the complete fresh-seed rerun. The seed fixture
verified preserved rows, files and passwords on its second invocation and
dropped its temporary database. Mypy passed all 289 source files and Ruff
passed. No migration, response shape, frontend code or runtime configuration
changed in this correction.

The 199-page handbook was rebuilt with the corrected receipt rules, rendered
in full with zero out-of-page text, and its changed pages were visually reviewed.
Its 97 UI images retain their recorded D1 capture time because the interface
has not changed. The pushed follow-up's hooks and CI provide its whole-tree
results; the `a3a6673` results above describe the completed D1 checkpoint.
Actual product profitability remains open: correcting return source values
does not replace the existing sales-order/standard-cost report calculation.

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

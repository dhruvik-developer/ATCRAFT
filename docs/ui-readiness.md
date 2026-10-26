# UI readiness verification

Use a populated development or test environment. The checks exercise the real
API, including authentication, tenant permissions, validation and pagination.
The browser journeys create their own test records. Run write journeys only
against a disposable test database. The readiness and accessibility sweeps do not
submit business forms; their shared authentication bootstrap can replace an
initial temporary password, so configure a dedicated test account.

The [26 October release verification](implementation/release-verification-20261026.md)
records the latest populated route, theme and error-contract checks. Earlier
dated results below remain historical evidence for their stated source.

## D5 specifications and accessories candidate

The D5 candidate adds four documented views on existing routes:
`/masters?tab=specifications`,
`/masters?tab=specifications&view=templates`,
`/items/:itemId?tab=specifications` and
`/items/:itemId?tab=accessories`. The maintained handbook now has 97 entries.
The 93-view capture below predates these additions; a text update does not
establish fresh browser or screenshot evidence for D5.

The frontend uses generated API schemas and lazy-loads the new master tab and
item panels. Its focused tests cover typed zero/false values, exact decimal
strings, full replacements, warning-only completeness, permitted Option values,
metadata pagination, accessory quantity/relation/ordering/removal, audit reasons,
pending/error behavior, read-only permissions and dealer-safe descriptions and
filters. Item-value editing uses item-authorized metadata; template category
choices do not require an unrelated category-view permission.

Candidate checks on 26 October passed 69 focused frontend cases, 103 other
affected item/dealer cases and all 50 master-screen cases. Type checking,
lint, formatting, generated-contract comparison and handbook validation passed.
The production build meets the unchanged limits: 203.3 KB gzip initial load
against 210 KB, and 37.9 KB for the heaviest screen against 40 KB; its asset
scan found no credentials. These checks do not replace the populated browser
and capture work below.

Before promoting this candidate, run the unchanged production build budgets and
the full checks, then use populated D5 demo data for these browser checks at
375, 768 and 1440 pixels in both themes:

- Create/edit an Option master, inspect required options and a refused in-use
  type/option change, then inspect the reasoned retirement dialog.
- Choose a category, attach attributes, change required warnings and save its
  template with only the existing item-attribute permissions.
- Open an item's Specifications panel with item-view/update permissions and no
  specification-master permission. Verify that zero and No remain recorded,
  missing required values warn without blocking saves, and field refusals keep
  the draft available.
- Add/edit/reorder/remove included and optional accessories. Included choices
  may be non-sellable and need no stock; optional choices must be sellable.
  Inspect long labels, keyboard focus, scrolling dialogs and pending controls.
- Browse the dealer catalog with its own bound principal. Check paginated
  Option/Boolean filters, false filters, combined filters and accessories
  filtering. Included items have no separate price/cart/link requirement;
  price-on-request optional items cannot be added. A browse-only dealer sees
  descriptions without order actions.

Refresh all 97 handbook captures on that candidate before describing its images
as current. Follow the existing capture guard and preserve the dated release
evidence below rather than attributing it to these new views.

## Current code and the latest recorded browser evidence

The current loader registry defines 79 application routes, including the vendor
and dealer portals and inbound tracking, plus three public authentication pages.
The readiness sweep discovers every route at runtime. The accessibility sweep
derives its 65 static application screens from the same registry and also checks
the login page per theme. Dynamic detail screens and the restricted password
change screen are covered separately by the readiness sweep.

The latest populated checks cover all 79 registered application routes:
474 screen/tab/viewport checks and 213 opened forms across 375, 768 and 1440
pixels, with no overflow, clipping, render/API failures or blocked business
writes. Each accessibility theme checked 66 screens with zero violations.
All 24 browser journeys passed again after publishing the patched web and edge
images. Portal checks use populated, bound vendor/dealer principals and verify
cookie-backed restoration and staff-route isolation.

CI runs the browser journeys against the production-style Docker stack and runs
the static accessibility sweep in both themes, retaining its reports. It also
checks handbook route coverage offline with `npm run handbook:check` from
`tests/e2e`, without starting a capture or changing application data. The
populated readiness, restricted-permission and data sweeps remain explicit
checks to run against suitable test fixtures. A passing CI journey or static
accessibility scan does not replace those checks.

## Handbook refresh verified on 26 October 2026

The capture recorded at `2026-10-26T01:15:24.066Z` contains all 93 documented
views across eleven chapters and all 79 application routes. Each portal used
its own bound principal and passed populated order/invoice preflight. No views
were skipped or failed; business writes remained blocked.

The rebuilt PDF has 195 pages and an image for every view. All pages passed
text-bound checks; the complete contact sheet and twelve selected full-size
pages were visually reviewed. Final review confirmed the corrected empty-action
heading and legacy-carton recovery wording. These generated files remain local,
ignored artifacts as described in the [handbook guide](handbook/README.md).

## Handbook refresh verified on 24 October 2026

The complete handbook capture recorded on 23 October at
`2026-10-23T13:50:34.511Z` contains all 70 documented screen/tab views at a
1600-by-1000 desktop viewport, with no missing views or capture failures. It
includes all 58 application routes and the three public authentication pages.
The generated 165-page PDF was reviewed after rebuilding with this image set;
page text stays within its bounds and all documented views have an image.

The capture used a disposable snapshot of the populated local database, an
isolated API on port 8001 and a web server on port 5174. Only authentication
session writes were allowed by the capture guard. The password-reset form used
an unsubmitted preview token, and the forced-change screen used a browser-only
identity override; their image captions record those conditions. The original
users/passwords, configuration and stored files were verified unchanged, and
the isolated servers and database were removed afterwards.

This desktop documentation refresh does not replace the dated responsive,
accessibility or business-journey evidence above.

## Verified on 16 October 2026

The audit used a separate, populated test database and the production web build.

| Check                                                                                                                     | Result                                                     |
| ------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| All 52 registered application routes and three public pages at 375, 768 and 1440 pixels, including tabs and primary forms | 357 checks passed                                          |
| Restricted-user navigation and sidebar permissions                                                                        | 44 routes passed                                           |
| List rows compared with API responses, including pagination                                                               | 35 populated screens passed; no empty fixtures             |
| Light/dark appearance and page text contrast                                                                              | 10 screen/theme checks passed; login theme controls passed |
| Frontend feature and component tests                                                                                      | 1,042 tests passed in 77 files                             |
| Existing browser journeys and new layout regressions                                                                      | 12 existing checks and four new cases passed               |
| Backend guards for frontend contracts, permissions, validation and sweep routes                                           | 34 tests passed                                            |
| Lint, formatting, TypeScript, production build, bundle budgets and credential scan                                        | Passed                                                     |
| OpenAPI and Postman contract checks                                                                                       | Passed                                                     |

## Repeat the checks

Start the API and worker using the README's development setup. Build the web
application with `npm run build` in `apps/web`, then run `npm run preview`.
The preview normally serves port 4173; its API proxy must point to the API being
tested through `VITE_API_PROXY_TARGET`.

Configure `tests/e2e/.env` from [its example](../tests/e2e/.env.example) with the matching `E2E_BASE_URL`,
`E2E_API_URL` and seeded administrator credentials. The existing authentication
helper completes the initial password change when the seed requires it.

Run the complete [demonstration seed](local-seed-data.md) after baseline setup.
Configure the vendor/dealer test credentials from the example as well as the
administrator. Browser setup creates absent test users against populated
`DM-VEN-PACK` (with its qualified PACKING category) and `DM-DLR-VASTU` records.
It preserves every existing account's binding and password. Before rendering
portal screens it verifies non-superuser identities, nonempty order/invoice
lists and matching detail reads using each portal's own token. Missing or
incompatible fixtures fail explicitly; an empty portal is not passing coverage.
The readiness script performs that same audited test-user preparation for the
portal audiences selected by its route filters. A staff-only subset does not
require portal credentials. Business forms are inspected without submission.
The readiness, accessibility and handbook browser contexts share a request guard
that permits reads and login, refresh, logout and MFA verification only on the
configured API or web origin, supporting the development API proxy. Other writes
are blocked before dispatch and fail the check, including
cart changes and notification updates. Service workers are disabled for these
read-only inspections so their requests cannot bypass interception. Audited
fixture preparation and password bootstrap happen separately before inspection.

From `tests/e2e`:

```sh
npm ci
npm run test:readiness
npx playwright test
node scripts/permission-sweep.mjs
node scripts/data-sweep.mjs
node scripts/theme-sweep.mjs
node scripts/a11y-sweep.mjs
```

The permission sweep uses a separate restricted account. Its five explicit
permissions are listed in the script; configure `SWEEP_USER` and
`SWEEP_PASSWORD` for a matching test account.

`test:readiness` discovers every route in `apps/web/src/app/route-loaders.ts`, resolves detail
pages from API data, and checks public authentication pages, visible top-level
tabs and the first enabled primary form on each screen/tab. It tests 375, 768
and 1440 pixel widths. A missing detail fixture, failed request, API error,
render failure, horizontal page overflow or clipped form fails the check.
Tables may scroll within their own containers.

Rendered error notifications also fail the sweep, including a malformed HTTP 200
response rejected by the client. The forced-password screen is checked in a
separate browser session with an intercepted identity response; no stored
account password is changed. Keep application source and test files unchanged
while a development-server sweep runs: a file-watcher reload invalidates its
navigation and in-flight request evidence.

The full route sweep also requires populated RFQ and MRP-run details; the
[local seed guide](local-seed-data.md) describes the comprehensive fixtures. A
filtered diagnostic run does not establish coverage for omitted routes.

The JSON results are written to `tests/e2e/sweep-ui-readiness.json`; screenshots
are kept in `tests/e2e/readiness-artifacts/`. These generated files are ignored
by Git. `READINESS_WIDTHS`, `READINESS_ROUTES` and `SWEEP_OUT` can narrow a
reproduction or name another report. For example, in PowerShell:

```powershell
$env:READINESS_WIDTHS = '320,667'
$env:READINESS_ROUTES = '/items,/finance'
npm run test:readiness
```

The accessibility sweep includes login and all configured static screens and
checks API, network and render failures as well as WCAG rules. Run it once per
theme with `A11Y_COLOR_SCHEME=light` and `A11Y_COLOR_SCHEME=dark`, using separate
`SWEEP_OUT` reports. `A11Y_ROUTES=/items,/warehouses` narrows a reproduction; a
filtered run does not verify the omitted screens. JSON reports include each
screen's failures and aggregated accessibility rules.

The additional `ui-layout.spec.ts` browser regressions cover phone navigation
focus and resizing, long account names, short-screen dropdown/select scrolling,
and the Ageing chart's hidden accessible table at 320 pixels.

## Fixes covered by the audit

- Mobile navigation contains keyboard focus, closes with Escape or navigation,
  and releases its modal state when the desktop sidebar becomes visible.
- Menus and selects fit the available viewport; long names and breadcrumbs
  wrap or truncate within the layout.
- Table search follows external URL filters. Assembly board line loads and
  late orders have independent server pagination.
- Changing a sales-order customer clears the previous customer's addresses;
  address lookup and field errors remain visible.
- Invoice actions require the same preview/read and write rights as the API.
  Receipt allocations preserve settlement notes and validate known balances.
- Dispatch operators can hand packing orders over on their own endpoint rights;
  the consignment picker exposes loading and failure states.
- Production completion/closure validation failures are surfaced to the user.
- Finance Ageing's accessible chart table stays inside its clipping wrapper,
  and report job actions wrap on narrow screens.

## Scope

The sweep checks navigation, reads, layout and primary-form access. Business
transitions and writes are additionally exercised by the browser journeys and
feature tests; the sweep does not submit every lifecycle dialog. Backend
services remain responsible for stock, money, state transitions and permission
enforcement.

## Historical re-verification after the transfer

This earlier record predates the 22-23 October standards verification above.
Its revision and migration references describe the environment at that time,
rather than the current migration head.

The transferred tree re-ran green against the main development database
(migrated to `20261016_1100`) with the API on 127.0.0.1:8000 and Vite on
127.0.0.1:5173: `browser-sweep.mjs` 44 routes, broken=0 http-failures=0;
`npm run test:readiness` 360 screen/tab/viewport checks, 0 failed (including
the new WIP-by-order tab at all three widths); `ui-layout.spec.ts` 5 passed.

The refreshed audit includes the committed production completion and costing
changes through `ce8f7b1`, plus the production-cost report (`b9069ab`) and the
WIP valuation committed alongside this document.

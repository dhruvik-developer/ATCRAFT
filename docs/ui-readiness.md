# UI readiness verification

Use a populated development or test environment. The checks exercise the real
API, including authentication, tenant permissions, validation and pagination.
The browser journeys create their own test records.

The [22–23 October standards verification](implementation/development-standards-20261022.md)
records the latest populated route, theme and error-contract checks. Earlier
dated results below remain historical evidence for their stated source.

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

Configure `tests/e2e/.env` from its example with the matching `E2E_BASE_URL`,
`E2E_API_URL` and seeded administrator credentials. The existing authentication
helper completes the initial password change when the seed requires it.

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

`test:readiness` discovers every route in `app/route-loaders.ts`, resolves detail
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

## Re-verified on develop after the transfer

The transferred tree re-ran green against the main development database
(migrated to `20261016_1100`) with the API on 127.0.0.1:8000 and Vite on
127.0.0.1:5173: `browser-sweep.mjs` 44 routes, broken=0 http-failures=0;
`npm run test:readiness` 360 screen/tab/viewport checks, 0 failed (including
the new WIP-by-order tab at all three widths); `ui-layout.spec.ts` 5 passed.

The refreshed audit includes the committed production completion and costing
changes through `ce8f7b1`, plus the production-cost report (`b9069ab`) and the
WIP valuation committed alongside this document.

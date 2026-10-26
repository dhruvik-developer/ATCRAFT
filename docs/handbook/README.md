# The client handbook

`content.json` is the maintained handbook source. It describes all 79 current
application routes, three public authentication routes and selected master-data
tabs: 97 screen/tab entries across eleven chapters. Parameter names such as
`:itemId` and `:id` describe the same detail route.

`Atcraft-ERP-Handbook.pdf`, `handbook.html` and `screens/` are generated local
outputs ignored by Git. A fixed page count is not part of the contract: it
changes with the text and the available images. The cover states when the text
was rebuilt, what capture dates are known, and which views have no stored image.
A text-only rebuild does not establish that the screenshots match current code.

## Check the source without a running stack

From the repository root:

```sh
node tests/e2e/scripts/capture-screens.mjs --check
```

This checks the handbook against `apps/web/src/app/route-loaders.ts`, the public
authentication routes and populated-detail resolvers. It also checks the capture
request guard against the OpenAPI write endpoints. It starts no browser and
performs no API requests. CI runs this validation through
`npm run handbook:check` in `tests/e2e` before the browser journeys.

## Refresh the screenshots

Use a populated, disposable development/test stack. Follow the root
[development setup](../../README.md) and [local seed guide](../local-seed-data.md)
for the selected database; do not reset a database to refresh a handbook.

Configure `tests/e2e/.env` from its example with the actual UI/API URLs, an
administrator whose initial password change is already complete, and dedicated
vendor and dealer accounts bound to populated partners. Each portal is captured
using its own principal; an administrator session cannot substitute for one. For the
standard host setup, the UI is `http://localhost:5173` and the API is
`http://localhost:8000/api/v1`. A production-build preview normally uses UI port
4173. Use the actual published nginx URL when capturing a Docker stack.

From `tests/e2e`:

```sh
npm ci
npx playwright install chromium
node scripts/capture-screens.mjs --check
node scripts/capture-screens.mjs
node scripts/build-handbook.mjs
```

The capture signs in through the shared form helper, opens detail records from
populated lists and navigates within the application. RFQs use their first data
row, whose click handler opens the detail page without submitting a document. It captures every
handbook entry and writes the capture date, route templates, image files and
failures to `screens/index.json`. Missing detail data, redirects, API/render
errors, error notifications or three or more identical images fail the command.
Review failures before treating the capture as complete.

Only session login, refresh and logout writes are permitted during capture.
Business writes, marking notifications read, password resets and password
changes are blocked. The forced-change view is shown using a browser-only
identity override in a separate session, and the reset-password form uses an
unsubmitted, non-secret preview token. Neither changes a stored password. A
capture account still requiring its initial change must be set up before running
this script; capture never bootstraps or resets it.

## Rebuild the text using preserved screenshots

After editing `content.json`, this command uses the existing local image set and
needs no running application or database:

```sh
node tests/e2e/scripts/build-handbook.mjs
```

The builder requires a screenshot of the requested tab before illustrating an
explicit query-string view; an image of another tab is not reused. It embeds
available images, labels missing views and records capture
provenance on the cover. Older indexes without a capture timestamp are identified
as such; image file modification dates are reported as file dates, not invented
capture dates. Inspect the rendered PDF after a rebuild, particularly long rules,
new screens and chapter transitions. Refresh the screenshots separately before
presenting an edition as current visual evidence.

## Inbound delivery tracking

The procurement API records vendor-accepted inbound consignments: carrier,
tracking/LR reference, dispatch and expected-delivery dates, boxes, weight,
delivery confirmation and associated documents. Vendor commitment remains
separate from the buyer planning date until accepted. Staff use the inbound
tracking screen and the purchase-order detail panel. Vendors use their scoped
orders, shipments, receipts, invoices and payments screens. The maintained
handbook includes these routes and the dealer storefront, account and return
flows. A new edition needs a fresh capture before its images can be described
as current visual evidence.

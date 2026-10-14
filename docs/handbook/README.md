# The client handbook

`Atcraft-ERP-Handbook.pdf` — 134 landscape pages, every screen of the ERP with a
screenshot taken from the running system, what it is for, how to use it, and the
rules it enforces.

## Rebuilding it

The stack must be up and carrying demo data, because the screenshots are of real
records rather than empty tables:

```bash
make dev-docker                      # or: docker compose ... up -d
cd tests/e2e
E2E_BASE_URL=http://127.0.0.1 node scripts/capture-screens.mjs
node scripts/build-handbook.mjs
```

`capture-screens.mjs` signs in once and walks every route, writing a PNG per
screen into `screens/` plus an `index.json`. `build-handbook.mjs` pairs those
with `content.json` and prints the PDF through Chromium.

## Two things worth knowing before you change the capture

**Navigate inside the application, never with `page.goto`.** Every full page load
restores the session from the refresh cookie, and refresh tokens rotate. Fifty
loads in a row is fifty rotations, and the moment two overlap the token family is
revoked as a replay and the app lands on `/login`. Two runs reported
"53 captured, 0 skipped" while more than half the files were byte-identical
pictures of the sign-in screen. The capture pushes a history entry and dispatches
`popstate`, which React Router treats as a real navigation - one page load for
the whole run.

**The run fails if captures come out byte-identical.** That is the only automatic
tell for the failure above, and without it the handbook ships with the same
picture on thirty pages and nobody notices until the client opens it.

## Editing the words

`content.json` holds the chapters. Each screen carries a purpose, what you can do
there, a step-by-step flow, the rules it enforces and who may use it. Edit it and
re-run `build-handbook.mjs`; no screenshots need retaking.

Three screens have no picture, each for a stated reason printed in the PDF: the
forced password-change screen only exists when a change is actually required, and
the demonstration data holds no request for quotation to open.

## Inbound delivery tracking

The buyer's inbound workflow records each consignment after vendor acceptance:
carrier, tracking/LR reference, dispatch date, expected delivery, boxes and weight.
Buyers can enter details phoned in by a vendor, correct tracking, mark delivery,
and file LR copies, photographs, challans and invoices against the PO/consignment.
All are visible to the vendor of that PO; creator, approvers and administrators
receive a grouped shipment update. Vendor commitment is shown separately from
the buyer planning date until a buyer accepts it. Tracking distinguishes ready,
dispatched, in transit, delivered, warehouse receipt, QC pending and GRN completed.
The staff board and vendor app screens arrive in C2; these operations are available
through the procurement API in the C1 backend package. Regenerate screenshots/PDF
when those screens land, rather than depicting screens that do not yet exist.

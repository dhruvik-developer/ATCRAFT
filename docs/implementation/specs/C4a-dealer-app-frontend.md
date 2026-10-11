# WP C4a: Dealer app (mobile-first storefront for dealer users)

Lane C (portals), round 5, step 1. Placeholder prefix `§C4-n`.

**What the proposal promised:** "Mobile API — browsing, cart, wishlist, orders, tracking, returns", "Banner & offer management", "Dealer pricing & discount management", plus the dealer notifications.

**What is already on main:** the backend from C3a/C3b. Read `apps/api/src/modules/dealer_portal/router.py` and the generated types. The vendor app (C2a) is the structural precedent: `VendorLayout`, guard, bottom tab bar, sheets. Copy its patterns. If something is truly shared, extract it into a common component rather than duplicating it.

## Decisions (binding)

### 1. Shell

- When `isDealerUser`, mount `DealerLayout` for `/dealer/*`. Every other path redirects to `/dealer`, except `/notifications`, `/profile`, `/change-password` and the auth pages. Staff and vendors never see `/dealer/*`.
- Phones get a bottom tab bar: Home, Catalog, Cart (with a count badge), Orders, Account. Larger screens get a top navigation bar or sidebar.
- The header shows the company logo, a search box (global catalogue search), and the notification bell.

### 2. Pages (lazy, responsive, `SchemaOf` types, keys under `queryKeys.dealerPortal.*`)

| Route | Contents |
|---|---|
| `/dealer` (Home) | Banner carousel (accessible: pause control, reduced-motion respected; no autoplay without pause). Offers strip. Category tiles. Brand tiles. Credit tiles: limit, outstanding, available headroom, overdue. Recent orders. "Reorder" shortcuts from the last order are optional. |
| `/dealer/catalog` | Server-paginated grid (cards) with image (blob fetch via the image route, `loading="lazy"`), name, code, brand, price (or "Price on request"), offer badge, availability band chip, wishlist heart, and an add-to-cart stepper. Filters (category tree, brand, in stock, on offer) live in the URL; on phones they open in a sheet. Sort: code, name, and price only if the server allows it. |
| `/dealer/catalog/:itemId` | Images, details, variant select, slab table for the dealer's channel, best offer, a live price for the entered quantity (`/price`, debounced with `SEARCH_DEBOUNCE_MS`), add to cart, and wishlist. A **Specifications** table (name, value, unit). An **Accessories** section: OPTIONAL accessories as small priced cards with "Add" (into the cart), INCLUDED ones as an "In the box" list. Catalogue filters include the filterable specifications the backend exposes. |
| `/dealer/cart` | Lines with quantity steppers. Priced preview: channel price, offer discount, tax split, totals. Warnings. Credit preview. A **Checkout** sheet (ship-to and bill-to address selects, required date ≥ today, PO reference, notes) posts `/checkout` with `cart_version`. On success: "Order SO/… placed for review", then go to the order. A 409 version conflict refreshes the cart and explains. |
| `/dealer/wishlist` | Cards with move-to-cart and remove. |
| `/dealer/orders`, `/dealer/orders/:orderId` | Status timeline (Placed → Confirmed → Allocated → Dispatched → Delivered → Invoiced). Lines with ordered / dispatched / invoiced quantities. Dispatches with tracking (courier, tracking ID, LR, expected date, events). Cancel only on DRAFT, with a reason. |
| `/dealer/invoices`, `/dealer/invoices/:invoiceId` | List with balance and overdue chips. Detail with lines, tax, payments and credits. PDF download through the portal job route. |
| `/dealer/returns`, plus a "Request return" flow | Pick an eligible dispatch, then lines with quantity, reason and note, then submit. The list shows each request's status, QC outcome and credited amount. |
| `/dealer/account` | Statement with a date range, outstanding and ageing, overdue invoices, and the credit-notes list with PDFs. Profile, password and MFA, the push device card from C2b, and notification preferences. |

Notifications: reuse `/notifications`, inside the dealer shell.

### 3. UX rules

The C2a rules apply: messages and constants come from the catalogue files, touch targets are at least 44 px, layouts work at 360 px, dialogs become sheets on phones, and gates use the exact `P.DEALER_PORTAL_*` rights per endpoint.

Never show stock quantities, only the band. Prices are formatted as INR with Indian grouping: add or reuse a shared `formatMoney` with the `en-IN` locale from `lib/format.ts`.

### 4. Wiring

route-loaders, router (dealer route group), the four sweeps (sign in as the demo dealer), capture-screens, and a handbook "Dealer app" chapter (install, push, every screen).

### 5. Tests

- Router: dealer users are redirected; staff and vendors are kept out.
- Catalogue: filters reach the URL and the request; price on request; add to cart.
- Cart: the checkout body carries `cart_version`, with no prices or discounts sent.
- Order detail: cancel is shown only on DRAFT.
- Return request body.
- Invoices: PDF job flow.
- Mobile tab bar smoke test.

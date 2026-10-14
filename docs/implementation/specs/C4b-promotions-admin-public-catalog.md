# WP C4b: Staff promotions admin, item images, dealer user binding, optional public catalogue pages

Lane C (portals), round 5, step 2. Placeholder `§C4-n`, continuing from C4a.

## Part 1: Staff screens

1. **New page `/sales/promotions`** ("Promotions", Sales nav; route gate `[P.PROMOTION_VIEW]`).
   - **Banners tab**
     - List with image thumbnails.
     - Form dialog:
       - upload the image through `/documents` with the right `entity_type`;
       - title and subtitle;
       - link kind and target picker (item / category / brand / offer);
       - audience;
       - validity window;
       - sort order.
     - Activate, deactivate and retire, each with a reason.
   - **Offers tab**
     - List.
     - Form dialog: scope kind and target picker, audience, minimum quantity, discount %, validity, sort.
     - Preview: "applies to N items".
   - Gate every mutation on `P.PROMOTION_MANAGE`.
   - Add the page to the sweeps and the handbook.
2. **Item detail "Images" panel** (gated `P.ITEM_UPDATE` for changes).
   - Upload an image with `entity_type="item"`, then set it as primary through `PUT /products/items/{id}/image`.
   - Show the gallery.
   - Remove the primary image.
3. **User dialog** (`features/admin/user-dialogs.tsx`): dealer binding, mirroring the vendor binding.
   - Account kind: Staff / Vendor / Dealer.
   - Dealer uses `CustomerPicker` with a new `channels` prop limited to DEALER and DISTRIBUTOR.
   - The role list is filtered to `is_dealer_role`.
   - The user list shows a "Dealer" badge and filter.
4. **Sales order list and detail.**
   - A "Source" column and filter (ERP / Dealer portal).
   - A "Dealer portal" badge on portal orders.
5. **Dealers page.** A "Portal users" count or link per dealer, if the users API supports filtering by `customer_id`.

## Part 2: Optional public catalogue pages (only when `/public/catalog/config` says enabled)

- **Unauthenticated routes** `/catalog` and `/catalog/:itemId`, rendered without the app shell in a public layout.
  - Contents: company header, banners, category and brand browse, item grid and detail with **no prices**.
  - Call to action: "Dealer login" leading to the login page with a redirect back.
  - When disabled, `/catalog` shows NotFound.
- **The login page** shows a "Browse catalogue" link only when enabled.
- **Public API calls** use the plain fetch path in `lib/api-client.ts` without auth. Add an explicit `publicApi` helper; `api-client.ts` remains the only `fetch` site.
- **Sweeps:**
  - the public routes go in the browser, a11y and responsive sweeps;
  - the permission sweep records `null` (ungated);
  - `test_e2e_sweep_routes.py` must pass.

## Part 3: Tests

- Promotions: page gating, plus the banner and offer form request bodies.
- Item image panel.
- User dialog: the dealer binding body includes `customer_id` and a dealer role.
- Sales order source filter.
- Public catalogue: renders when enabled; NotFound when disabled; no auth header sent.

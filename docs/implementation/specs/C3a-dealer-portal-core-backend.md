# WP C3a: Dealer portal backend, core

Lane C (portals), round 4, step 1. Placeholder prefix `§C3-n`.

**Scope:** dealer principal and isolation, catalogue with dealer pricing and offers, cart, wishlist, checkout, orders, tracking, statement.

**Proposal:**
- p.3: "public website / catalog platform", "Mobile API — browsing, cart, wishlist, orders, tracking, returns", "Dealer pricing & discount management", "Banner & offer management".
- Mobile notifications: "Order dispatched", "Courier name & tracking ID", "Delivery expected date", "Payment reminder", "New order request".

**Audit:** "The public dealer site: catalog browsing, cart, wishlist, banners, and offers" is absent.

**Delivery:** a dealer app inside the same ERP and SPA, at `/dealer/*`. The frontend comes in round 5 (WP C4). It is installable as the same PWA with push (C1b/C2b). There is no new stack.

**Background:** read `reports/dealers-reports-docs.md` in full, especially §1.3–§1.4 and §7 (follow §7 except where this spec says otherwise), and the vendor-portal precedent on main (C1a: allow-list gate, `require_vendor_user`, `vendor_portal` module). Mirror C1a's structure closely, so vendor and dealer isolation work the same way.

## Decisions (binding)

### 1. Dealer principal

**Schema and context**
- `users.customer_id`: FK customers, ON DELETE CASCADE, nullable, index `ix_users_customer (company_id, customer_id)`.
- CHECK `vendor_id IS NULL OR customer_id IS NULL`, short name.
- `roles.is_dealer_role`: bool, default false.
- `Principal.customer_id` and `is_dealer_user`; `TenantContext.customer_id`; `MeResponse.customer_id`. Frontend `CurrentUser.customer_id` and `isDealerUser` go in `auth-context.tsx` (types only now; the UI comes in C4).

**Users service**
- Generalise the vendor rule into `assert_portal_role_consistency(vendor_id, customer_id, roles)`: dealer roles only on dealer-bound accounts, and only dealer roles on them.
- `_assert_references` checks the customer through the tenant-scoped repository. It must be active and of type DEALER or DISTRIBUTOR (`§C3-1`).
- `UserCreate`/`Update` gain `customer_id` and `clear_customer`. `UserFilters` and `UserSummary` gain `customer_id` and `is_dealer_user`.
- `UsersRepository._scoped` adds `User.customer_id == tenant.customer_id` for dealer principals.

**Allow-list gate.** Add `dealer_route_allowed(path)` next to `vendor_route_allowed`. Dealers may call only:
- `{prefix}/dealer-portal/**`
- `{prefix}/notifications**`
- the auth self-service paths

Everything else answers 403 SCOPE_DENIED. Unit-test the predicate, and test it through the real dependency chain.

**`require_dealer_user` dependency.** Every `/dealer-portal` route uses it, so staff get 403 SCOPE_DENIED. ADMIN holds every permission, so the permission alone is not enough.

**Roles** (system, `is_dealer_role`):

| Role | Rights |
|---|---|
| DEALER_OWNER | browse, order, account, return + `notification.view` |
| DEALER_BUYER | browse, order, return + `notification.view` |
| DEALER_ACCOUNTS | browse, account + `notification.view` |

- New rights: `dealer_portal.browse`, `dealer_portal.order`, `dealer_portal.account`, `dealer_portal.return`, in group "Dealer Portal". Avoid `.view`, as C1a did.
- Update the pinned system-role list in `test_role_presets_reach_roles.py` deliberately.
- Write a migration that inserts the three roles for **every existing company** with literal permission arrays (idempotent), and grants the new rights to SUPER_ADMIN and ADMIN.
- Make `seed.py` create them for new companies.

### 2. Catalogue

Module `dealer_portal`, prefix `/dealer-portal`, tag "Dealer Portal". Every list is paginated.

| Route | Right | Notes |
|---|---|---|
| `GET /me` | browse | account, channel, `price_list_type`, credit position (`evaluate_credit`), ship-to addresses, primary contact, assigned sales person (name and phone only) |
| `GET /catalog/categories` | browse | categories that hold sellable active items (tree) |
| `GET /catalog/brands` | browse | active brands that hold sellable items (A1) |
| `GET /catalog/items` | browse | see below |
| `GET /catalog/items/{item_id}` | browse | variants with their own price, current slabs for the dealer's channel, active offers, `can_return`, HSN, GST rate, description, up to N images, **specifications** (D5's batch helper: name, value, unit) and **accessories** (OPTIONAL ones are priced and addable to the cart; INCLUDED ones are listed as "In the box") |
| `GET /catalog/items/{item_id}/price?quantity&variant_id` | browse | `products.service.resolve_price(price_type=customer.price_list_type)` plus the best offer, giving gross, offer discount, net unit and tax estimate |

**`GET /catalog/items`**
- Query: `search`, `category_id` (subtree), `brand_id`, `in_stock_only`, `on_offer_only`, `sort_by` = `code | name | price`.
- `price` sorting is only allowed if it can be done in SQL. Otherwise refuse it with a 422 listing the allowed fields.
- Items are `can_sell AND is_active`, with the channel price batched (`PriceListRepository.candidates_for_items` + `select_price_row` + fallback).
- DISTRIBUTOR with no price row returns `price: null`, `price_on_request: true`, and is not orderable.
- `availability_band` is IN_STOCK, LIMITED or MADE_TO_ORDER. It comes from live `available_by_item` summed across sellable warehouses, is **never cached**, uses the thresholds as named constants (`§C3-2`), and never exposes exact quantities.
- Also `best_offer`, `in_wishlist`, `in_cart_quantity`, and the primary image id.
- Use **one query per concern per page**: no N+1.

**Offers.** C3b builds offer administration. Define the table here:

`dealer_offers` (TenantMasterEntity):
- `code`, `title`, `description`
- `scope_kind` = `ALL | CATEGORY | BRAND | ITEM`, with `category_id` / `brand_id` / `item_id` matching it (CHECK)
- `audience_price_type` NULL (meaning all channels) or a `PriceType`
- `min_quantity`
- `discount_percentage` (0 < x ≤ 100)
- `valid_from` / `valid_to`, `is_active`, `sort_order`

The rule is percentage only, no stacking, best single offer per line (`§C3-3`). It lives in one pure function used by the catalogue, the cart and checkout.

**Dealer-specific schemes (proposal "Dealer pricing & discount management").** `dealer_offers` also carries an optional `customer_id` (FK customers RESTRICT, tenant-validated, must be a dealer channel). An offer with a customer applies only to that dealer (a negotiated scheme); without one it applies to its audience. The same best-offer function is **also used by ERP sales-order pricing** (`sales.service.price_line` / `_build_pricing_context`, batched, no N+1) as the line's default `discount_percentage` when the user gives **no** explicit discount, storing `offer_id` on the line. An explicit discount still wins and keeps today's authority rules. With no offers configured nothing changes for existing ERP orders. Test both paths (ERP and portal), and document it in assumptions and the handbook ("Schemes and offers").

### 3. Cart and wishlist

**Tables**
- `dealer_carts` (TenantEntity + OptimisticLock): one per dealer **user** (`§C3-4`), unique `(company_id, user_id)`, plus `customer_id`.
- `dealer_cart_lines`: `item_id`, `item_variant_id`, `quantity > 0`. Unique `(cart_id, item_id, COALESCE(item_variant_id, ATTRIBUTE_NULL_SENTINEL))`. At most `MAX_LINES_PER_ORDER`. **No stored price.**
- `dealer_wishlist_items`: same shape per user. Unique per user + item + variant.

**Routes**

| Route | Right |
|---|---|
| `GET /cart` | order |
| `POST /cart/lines`, `PATCH /cart/lines/{id}`, `DELETE /cart/lines/{id}`, `DELETE /cart` | order |
| `GET /wishlist`, `POST /wishlist`, `DELETE /wishlist/{id}`, `POST /wishlist/{id}/move-to-cart` | browse; move-to-cart needs `order` |

**`GET /cart`** returns a priced preview, computed live:
- per-line channel price and offer
- tax split using `resolve_tax_treatment` with the chosen or default ship-to
- totals
- a credit preview from `evaluate_credit`
- warnings: inactive item, no price, below minimum quantity, over credit limit

Every item id is validated for tenant, `can_sell` and active.

### 4. Checkout: `POST /dealer-portal/checkout` (order)

**Request:** `{cart_version, required_date, shipping_address_id?, billing_address_id?, customer_po_number?, notes?}`

**Response:** 201 with the order.

**First outermost transaction**
1. Lock the cart and check `cart_version`.
2. Re-validate the lines.
3. Resolve the warehouse (`warehouses.is_default`; add a getter in warehouses, `§C3-5`) and the addresses, which must belong to the dealer.
4. Build `SalesOrderCreate` with **no `unit_price` and no client discount**. The server sets each line's `discount_percentage` from the best offer.
5. Call `sales.service.create_order`.
6. Set `source=DEALER_PORTAL`. This is a new column `sales_orders.source` String(16), ERP | DEALER_PORTAL, server default ERP, CHECK, set by the server only, exposed in summary and detail, with a filter.
7. Store `offer_id` per line as `sales_order_lines.offer_id`, a bare UUID, for traceability.
8. Clear the cart and audit.
9. Keep `order.id` in a plain variable.

**Order lifecycle.** Orders stay **DRAFT** for sales review (`§C3-6`: conservative; sales staff confirm, which runs the credit check and reservation). The dealer can cancel their own DRAFT with a reason through `POST /orders/{id}/cancel`.

**Deletion.** `delete_draft_order` must refuse portal-sourced drafts (dealers report trap 4). Use cancel instead.

**Notification.** Raise "New order request from <dealer>" for the assigned sales user (else `sales_order.approve` holders), in the same transaction. Link to the SO detail page.

### 5. Orders, tracking, account

| Route | Right | Notes |
|---|---|---|
| `GET /orders` | order | status, `open_only`, search; customer forced from the tenant |
| `GET /orders/{id}` | order | header, lines (ordered, allocated, dispatched, invoiced), allocation status. Ownership is checked, and a foreign order answers 404 |
| `GET /orders/{id}/dispatches` | order | |
| `GET /dispatches/{id}/tracking` | order | dealer-safe projection: courier or transporter name, tracking, LR, dispatched and expected delivery dates, events. **Driver mobile and freight cost are dropped** (`§C3-7`) |
| `GET /statement?date_from&date_to` | account | `party_statement` with the forced `party_id` |
| `GET /outstanding` | account | `ageing_report(party_type=CUSTOMER, party_id=tenant.customer_id)` plus the overdue invoices list |

Defence in depth: add a customer predicate to `SalesOrderRepository._scoped` and `DispatchRepository._scoped` for dealer principals. The gate already blocks dealers from staff routes, but scoping protects against any future route.

### 6. Demo data

- Two demo dealer users per demo dealer (owner and buyer), with valid emails and the demo password mechanism.
- A cart with lines, a wishlist, and one portal-sourced DRAFT order.
- Two demo offers: one CATEGORY offer and one BRAND offer.
- Idempotent.

### 7. Docs

- `database.md`: §2 new module, the `users.customer_id` and `sales_orders.source` columns, §8 checkout event.
- `security.md` §4: dealer isolation, mirroring vendor.
- `assumptions.md` `§C3-n`: who may hold portal users, cart per user, draft orders, default warehouse, offer rule, availability bands, dealer-safe tracking projection.
- `architecture.md`, `api.md`.

## Tests

**Unit**
- `dealer_route_allowed`
- the best-offer function (scopes, audience, minimum quantity, validity, tie-break)
- availability band
- portal role consistency

**Integration**
- Catalogue prices per channel: DEALER price list, item-master fallback, DISTRIBUTOR price on request.
- Offers applied.
- Cart: CRUD, version conflict, priced preview.
- Wishlist: CRUD.
- Checkout:
  - creates a DRAFT SO with source DEALER_PORTAL and server prices and discounts;
  - clears the cart;
  - notifies;
  - refuses a foreign address;
  - refuses a stale `cart_version`.
- The dealer can cancel their own DRAFT; deleting a portal draft is refused.
- Orders, tracking and statement are scoped: dealer A cannot read dealer B's data (404).
- `users.customer_id` validation: a non-dealer customer is refused; vendor and customer together are refused.

**API (real chain)**
- A dealer gets 403 on staff routes and 200 on portal routes.
- Staff get 403 on portal routes.

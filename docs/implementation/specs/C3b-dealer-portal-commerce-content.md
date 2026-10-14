# WP C3b: Dealer portal backend, part 2

Lane C (portals), round 4, step 2. It follows C3a in the same lane. Assumption placeholders continue as `§C3-n`.

**Scope:** invoices, credit notes and returns for dealers; banners and offers administration; item images; and an optional public catalogue.

**Already on main:**
- A2: sales invoices and the invoice PDF pipeline.
- A3: sales returns (with the `source` column) and credit notes.
- A1: brands.

**Background:** `reports/dealers-reports-docs.md` §1.8 (documents), §7.4, and traps 25–27.

## Part 1: Dealer commerce endpoints

All routes live under `/dealer-portal`. Each uses `require_dealer_user`, a right written literally, and an ownership check that answers 404 for another dealer's data. Lists are paginated.

| Route | Right | Notes |
|---|---|---|
| `GET /invoices`, `GET /invoices/{id}` | account | Own issued invoices: number, date, due date, total, paid, credited, balance, status, overdue days, lines, tax summary. |
| `POST /invoices/{id}/pdf`, `GET /jobs/{job_id}`, `GET /documents/{document_id}/download` | account | Reuses A2's job pipeline. The portal job status only shows jobs the dealer started; the download only serves documents of their own invoices and credit notes. Vendors already have the same pattern in C1a; follow it. |
| `GET /credit-notes`, `GET /credit-notes/{id}`, `POST /credit-notes/{id}/pdf` | account | |
| `GET /returns`, `GET /returns/{id}` | return | Own sales returns of any source. Show status, lines, QC outcome summary, and the credited amount. |
| `POST /returns` | return | **Return request.** Body: `{dispatch_id, lines:[{dispatch_line_id, quantity, reason_code, note}], notes}`. Calls `sales_returns.service.create_return(...)` with `source=DEALER_PORTAL` and `requested_by_user_id`, so validation is identical to staff creation. The dispatch must be the dealer's own SALES dispatch, and must have left. Serial-tracked lines are refused for dealers; staff handle those (`§C3-n`). Notify the assigned sales user and holders of `sales_return.create`: "Return request from <dealer>". |
| `GET /returns/eligible-dispatches` | return | Own dispatches that have left and still have returnable quantity. |

## Part 2: Banners, offers and images (staff administration)

The table `dealer_offers` comes from C3a. Add `dealer_banners` (TenantMasterEntity):

- `code`, `title`, `subtitle`
- `image_document_id`: a bare UUID, validated as an image document in the tenant
- `link_kind` (`NONE | ITEM | CATEGORY | BRAND | OFFER`) and `link_id`, with a CHECK that `link_id` is present exactly when `link_kind <> 'NONE'`. Validate the target in the tenant.
- `audience_price_type` (NULL means all)
- `sort_order`
- `valid_from`, `valid_to` (ordered, with a CHECK)
- `is_active`

**Lifecycle.** Banners and offers move between ACTIVE, INACTIVE and ARCHIVED through state machines ("Dealer banner", "Dealer offer").

**Staff routes** (module `dealer_portal`, prefix `/promotions`, tag "Promotions"; each with `require_permission(Permission.PROMOTION_...)` and `forbid_vendor_users`):

| Route | Notes |
|---|---|
| `GET/POST /promotions/banners`, `GET/PATCH/DELETE /promotions/banners/{id}` | Retire requires a reason. |
| The same set for `/promotions/offers` | Validates the scope target, the discount bound and the validity window. Overlapping offers are allowed; the best one wins. |

New permissions: `promotion.view` and `promotion.manage` (group "Sales"). Presets: `_SALES` gets view and manage. Write a grant migration.

**Dealer-facing reads:**

- `GET /dealer-portal/banners`: active, inside today's window, for the dealer's audience.
- `GET /dealer-portal/offers`: the same filtering.

**Item images:**

- `items.image_document_id`: a bare UUID, like `companies.logo_document_id`.
- `PUT /products/items/{item_id}/image` with `{document_id | null}`, under `item.update`. The document must be an image (jpeg, png or webp), in the tenant, with `entity_type="item"` and `entity_id` equal to that item.
- A gallery is optional and reads documents with `entity_type='item'`.
- Image bytes go through `GET /dealer-portal/images/{document_id}`. It serves **only** documents that are the primary image or gallery of a sellable item, or the image of an active banner, and only with image MIME types. Send `Cache-Control: private, max-age=300` and the `DOWNLOAD_HEADERS` safety headers with an inline disposition for images.
- Bound the number of images per catalogue page with a named constant.

## Part 3: Optional public catalogue (disabled by default)

The proposal mentions a public website/catalogue. CLAUDE.md requires that the tenant never comes from the request, so:

- A new setting `PUBLIC_CATALOG_COMPANY_CODE`, empty by default, which disables the feature. When set, it resolves the tenant **from server configuration**.
- Add it to `.env.example`, compose, and the `test_env_template` expectations.
- Unauthenticated read-only routes under `/public/catalog`:
  - `/config`: enabled flag, company display name, logo document id
  - `/categories`, `/brands`
  - `/items`: search, category, brand; **no prices, no availability**
  - `/items/{id}`
  - `/banners`, filtered to `audience_price_type IS NULL` only
  - `/images/{document_id}`: same rules as the dealer image route
- When disabled, every route answers 404.
- Each route must be explicitly unauthenticated, and the auth middleware must allow it. Check how `/auth/login` is exempted, and follow that.
- The existing per-IP limits apply. Check that `src/core/ratelimit.py` covers the new prefix as a flood guard.
- Document it in `security.md`: the public surface, what is never exposed (prices, stock, customers), and how to enable it. Record `§C3-n`.

## Part 4: Dealer notification types

Add the `NotificationType` members dealer events need (order request received, order confirmed, dispatched, delivered, invoice issued, payment reminder, return received, credit note issued) and mirror them in the frontend. **Raise only the request-side events in this work package:** new order request and return request, both to staff. Dealer-facing events are WP A5 (round 5). The members exist now so A5 and C4 agree on names.

## Demo data

- A primary image for a handful of demo items. Generate small placeholder product images with the global Python + PIL at dev time, store them through the documents service in the demo loader, and keep it idempotent.
- Two banners and the offers from C3a.
- One dealer-raised return request (DRAFT, source DEALER_PORTAL).

## Docs

- `database.md`: tables, columns, §8.
- `security.md`: public catalogue and image serving.
- `assumptions.md`.
- `api.md`.
- Handbook, staff side: the Promotions screen arrives in C4, so describe it there.

## Tests

**Integration**

- Invoices, credit notes and returns are scoped to the dealer (a foreign id answers 404).
- A dealer return request creates a DRAFT with source DEALER_PORTAL. A serial line is refused. Another dealer's dispatch answers 404.
- The PDF job is visible only to its requester. The download only serves the dealer's own documents.
- Banners and offers: CRUD, validation, active-window filtering, audience filtering.
- Item image: a non-image is refused, a foreign document is refused, and the image route serves only allowed documents.

**Public catalogue**

- Disabled: 404.
- Enabled through a settings override: no prices or stock leak, and inactive or unsellable items are hidden.

**API**

- `promotion.*` gating.
- Dealers get 403 on `/promotions`.

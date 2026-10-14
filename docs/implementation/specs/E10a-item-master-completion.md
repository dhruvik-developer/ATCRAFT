# WP E10a: Item master completion: variants, prices, planning fields, vendor links, discount authority

Lane B, round 3, step 3 (after D5). Placeholder prefix `§E10-n`.

## Checklist IDs to close

- S02.3 (item master / product catalog)
- S02.5 (variant authoring; accessories are D5)
- S02.6 (discount authority; offers are C3a)
- P04.M7 (variant)
- P04.M9 and P04.M10 (sale and dealer price editable)
- P06.F5 (planning inputs editable)
- G1 note (i): dealer address and contact writes need `dealer.manage`

Read each one in `trace/trace.json`.

## Decisions (binding)

### 1. Variants

**Variant dialog.** Add a `VariantFormDialog` on the item detail Variants tab, with Create and Edit row actions. Fields:
- `code` (create only), `name`
- finish / size / colour selects (`useItemAttributeOptions`)
- `price_delta`
- `cost_delta`, shown only with `item.cost_view`
- `barcode`, `is_active`

It uses the existing `POST /products/items/{id}/variants` and `PATCH /products/variants/{id}`. Gates: `P.ITEM_CREATE` / `P.ITEM_UPDATE`. On success, invalidate the item detail and the variant keys.

**Sales line.** The SO line editor offers a **variant select** when the item has variants. Pricing already honours `variant_id`.

**Record `§E10-1`** in `docs/assumptions.md`: variants share the parent's stock and BOM, and differ only in price and identity. A separately stocked finish must be modelled as its own item. Explain this in the handbook.

### 2. Item form

Add to `ItemFormDialog`:
- Sale price and Dealer price (decimal inputs; editing them requires `price.manage`, enforced server-side in `update_item` for those two fields)
- Default warehouse (and default bin, from E8a, if present)
- Min order quantity, Reorder level, Reorder quantity, Safety stock, Shelf life days
- Batch-tracked and Serial-tracked flags

For the tracking flags, **changing a flag on an item with stock or open documents is refused server-side** if it is not already refused; check, and add the rule if missing (`§E10-2`).

Keep the `ItemDetail` mirror registered and correct in `test_frontend_contract_types.py`.

### 3. Item-vendor links editor

Add a Vendor-link dialog on `VendorsPanel`, with add / edit / remove / set-preferred actions. Fields:
- vendor picker
- vendor item code and name
- manufacturer and MPN (A1)
- preferred toggle (demote-before-promote is already in the service)
- lead time, MOQ, order multiple
- last purchase price, read-only

Gate on `P.ITEM_UPDATE`. Use the existing routes.

### 4. Discount authority (S02.6)

Today any line discount of 0–100% is ungated, and the below-cost check uses the price before discount. Close this:

- **Setting:** add `SALES_DISCOUNT_APPROVAL_PERCENT`, a company setting via a sparse company settings row or a config default (`§E10-n`; default 10%).
- **New right:** `sales_order.discount_override`, with a grant migration. `_SALES` does **not** get it; ADMIN does.
- **Approval trigger:** a line whose **net** unit price (after discount) is below `standard_cost`, or whose discount exceeds the threshold, sets `requires_approval`. The order then goes through the existing `approve_order` path, and B4's approval inbox picks it up.
- **Refusal:** a discount above the threshold is refused outright without the new right.
- **Offers:** offer-derived discounts (C3a) are pre-authorised. They are exempt from the threshold but still subject to the below-cost check.

### 5. Dealer address and contact writes

`customers` address and contact service methods must call `assert_channel_write_allowed(principal, customer.customer_type)`. Thread the principal through from the router, so `customer.update` alone can no longer change a DEALER's GSTIN, state code or ship-to.

### 6. Schema drift repair (found in round 1)

A metadata compare shows `item_attributes` lacks `ix_item_attributes_created_at`, `ix_item_attributes_deleted_at` and the `created_by_id` / `updated_by_id` foreign keys to `users`. This comes from migration `4c1d70b8ea92` (masters report, trap 5).

Write a migration that:
- adds both indexes and both foreign keys;
- adds the foreign keys `NOT VALID` first, after nulling any actor id that matches no user, logged;
- then validates them.

Then run an autogenerate compare and confirm zero drift on the products tables.

### 7. Small carry-overs from round 1 (WP A1)

- The items list has a brand filter but no **Brand** column. Add one (label through `use-brand-options`, gated like the filter). Sort it only if `sortable_fields` allows `brand_id`; otherwise leave it unsorted.
- `tests/unit/test_frontend_contract_types.py` still lists `ItemVendorLink` in `not_a_response_mirror`, although it is now a `SchemaOf<'VendorLinkOut'>` alias. Remove the stale entry.
- An item-vendor link's manufacturer and MPN (A1 columns) are editable only through the API today. Section 3's link editor is where they become editable; make sure both fields are in it.

## Tests

- **Integration:**
  - variant CRUD through the API used by the dialog
  - SO with a variant prices with its delta
  - price-field permission
  - tracking-flag change refused with stock
  - discount threshold: refusal and approval
  - net-below-cost approval
  - offer exemption
  - dealer address write needs `dealer.manage`
- **Frontend:** request bodies for each dialog, plus the gates.

## Docs

- Assumptions
- Handbook: the Items chapter (variants, vendor links, prices) and the Sales chapter (discount authority)

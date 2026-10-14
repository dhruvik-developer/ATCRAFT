# WP A1: Brand and Manufacturer masters

Lane A (commercial), round 1. Placeholder prefix for assumptions: `§A1-n`.

**Proposal reference.** "Category, brand, manufacturer & specification masters" (p.3). The audit found: "Brand and manufacturer as their own master lists" are absent.

**Background.** Read `reports/masters-pricing.md`, sections 3, 4, 5 and 6 and "Conclusion A". The **item-attribute master** (finish, size, colour) is the precedent. Copy its shape, but use the *organisation* master lifecycle (a `StateMachine` with `record_status_change`), not the item-attribute's direct `is_active` assignment.

## Decisions (binding)

### 1. Separate tables, in `products`

Use two tables, not new `ItemAttributeType` kinds.

**`manufacturers`** (`TenantMasterEntity`)
- `code` String(32), passed through `normalise_code`, unique on (company_id, code) as `uq_manufacturers_company_id_code`.
- `name` String(255); `legal_name` String(255) NULL; `country` String(80) NOT NULL default 'India'; `website` String(255) NULL; `notes` Text NULL; `is_active` bool default true.
- Partial picker index `(company_id, name) WHERE is_active AND deleted_at IS NULL`.

**`brands`** (`TenantMasterEntity`)
- `code` String(32), unique on (company_id, code).
- `name` String(120); `description` Text NULL; `sort_order` int default 0 (CHECK 0..9999); `is_active`.
- `manufacturer_id` FK manufacturers RESTRICT NULL, indexed. This is the brand owner.
- Partial picker index `(company_id, sort_order) WHERE is_active AND deleted_at IS NULL`.

**`items`**
- `brand_id` FK brands RESTRICT NULL, indexed.
- `manufacturer_id` FK manufacturers RESTRICT NULL, indexed.
- `manufacturer_part_number` String(64) NULL.
- Index `ix_items_company_brand (company_id, brand_id) WHERE deleted_at IS NULL`, used later by the dealer catalogue.

**`item_vendor_links`**: `manufacturer_id` FK RESTRICT NULL and `manufacturer_part_number` String(64) NULL.

**The rule** (state it in `docs/database.md` §3 as a "Brands and manufacturers" subsection): nothing may branch on a brand or manufacturer value. They are client-editable classification and traceability masters.

### 2. Lifecycle

- `BRAND_LIFECYCLE` and `MANUFACTURER_LIFECYCLE` (entities "Brand" and "Manufacturer"). Use ACTIVE ↔ INACTIVE, with ARCHIVED terminal, as in `organization/service.py:_lifecycle`. Reuse that factory if you can import it without breaking the dependency direction (products sits above organization, so it may). Otherwise replicate it.
- Every `is_active` change goes through `assert_transition` and `record_status_change`.
- Deactivating or archiving is **refused while referenced**, with `ErrorCode.REFERENCED_RECORD_IN_USE`:
  - a brand is referenced by live items;
  - a manufacturer is referenced by live items, live vendor links and live brands.

### 3. Routes

Prefix `/products`. Declare static paths before `/{id}`.

| Route | Permission | Notes |
|---|---|---|
| `GET /products/brands` | brand.view | paginated; search, `is_active`, `manufacturer_id`; sort code, name, sort_order |
| `POST /products/brands` | brand.manage | returns 201 |
| `GET /products/brands/{brand_id}` | brand.view | |
| `PATCH /products/brands/{brand_id}` | brand.manage | no `code`; an `is_active` change goes through the machine |
| `DELETE /products/brands/{brand_id}?reason=` | brand.manage | archive = soft delete; refused while in use |
| the same five under `/products/manufacturers` | manufacturer.view / manufacturer.manage | |

### 4. Validation

- `_require_brand(session, tenant, brand_id)` and `_require_manufacturer(...)`: read through the tenant-scoped repository (another tenant's id gives `NotFoundError`), and refuse inactive values on the way in.
- Call them in `create_item` and `update_item`, and in `link_vendor` / `update_vendor_link` for the manufacturer.
- **Also fix trap 1:** `create_item` never calls `_require_attributes` for finish, size and colour. Add the call, and add tests proving `create_item` refuses another tenant's attribute and a colour id in `finish_id`.

### 5. Item list and search

- `ItemFilters` and `item_filters()` gain `brand_id` and `manufacturer_id`.
- `ItemSummary` gains `brand_id`, which **must** also be added to `ITEM_SUMMARY_COLUMNS` (trap 8, the `raiseload` 500).
- `ItemDetail` and the `ItemDetailWithCost` mirror gain `brand_id`, `manufacturer_id` and `manufacturer_part_number`.
- Add the fields to `ITEM_AUDITED_FIELDS`.

### 6. Permissions

- New: `brand.view`, `brand.manage`, `manufacturer.view`, `manufacturer.manage`, in group "Item Master".
- Presets:
  - DESIGN_ENGINEER: all four
  - `_SALES`: `brand.view`
  - `_PURCHASE`: `brand.view`, `manufacturer.view`
  - PLANNER: `brand.view`
- A grant migration for SUPER_ADMIN and ADMIN.

### 7. Frontend

- `features/masters`: new tabs **Brands** and **Manufacturers** (copy `AttributesTab`), with form dialogs (`brand-dialogs.tsx`, `manufacturer-dialogs.tsx`) and tests. Retire actions use a reason, and the in-use refusal message is shown verbatim.
- `/masters` route and nav permissions become `[CATEGORY_VIEW, UOM_VIEW, PRICE_VIEW, ITEM_ATTRIBUTE_VIEW, BRAND_VIEW, MANUFACTURER_VIEW]`. This also fixes the missing `ITEM_ATTRIBUTE_VIEW` (trap 19). Update the empty-state text.
- Hooks `hooks/use-brand-options.ts` and `hooks/use-manufacturer-options.ts`, copying `use-item-attribute-options.ts` (`LOOKUP_LIST_LIMIT`, `emptyOnPermissionError`, `staleTimes.reference`). The labeller falls back gracefully for a retired value.
- Item form: Brand select, Manufacturer select, MPN input. "None" posts `null`.
- Item detail: labels.
- Items list: a Brand filter in the URL.
- Vendor links panel: an MPN column (display only).
- Mutations invalidate the masters keys and the options keys.

### 8. Demo data (`masters.py`, idempotent by code)

- Manufacturers: ATCRAFT (own manufacturing), plus two outside makers, for example a cylinder maker and a spring/fastener maker (your own realistic names, marked as demo).
- Brands: "ATCRAFT", "ATCRAFT PRO" (owner ATCRAFT), and one partner brand (owner = a cylinder maker).
- Assign brand and manufacturer to a sensible subset of the demo items. Finished goods are ATCRAFT; bought-out cylinders get the partner brand and maker.

### 9. Docs

- `docs/database.md`: §2 add the tables to products; §3 add the subsection.
- `docs/assumptions.md`:
  - `§A1-1` covers the code normalisation and retirement rules.
  - Brand belongs to at most one manufacturer, an MPN is free text, and nothing branches on either: state this.
- `docs/api.md`: count and endpoint map.
- Handbook: the Masters chapter lists the two new tabs.

### 10. Tests

**Integration** (`tests/integration/test_products_service.py`, or a new module-schema file):
- create, list (the list returns your row), update and rename;
- duplicate code refused;
- code normalised;
- deactivate refused while in use (items, vendor links, brands);
- archive refused while in use, and allowed when unused;
- a cross-tenant brand or manufacturer is refused on item **create** and **update**;
- an inactive one is refused;
- the item list filtered by brand returns only matching items;
- an item list containing a brand-bearing item serialises (no 500).

**Unit:** lifecycle `validate()`.

**Frontend:** dialog tests (POST and PATCH bodies; the edit sends no `code`), a masters tab test, and the item form posting `brand_id` and `manufacturer_id` (or `null`).

## Out of scope

Dealer catalogue, images and pricing changes belong to other packages. Do not change price resolution.

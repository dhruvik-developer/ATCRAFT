# WP C0: Split purchase requirements into category-wise POs, with vendor manager assignment

Lane C (portals), round 1. Placeholder prefix `§C0-n`.

**Proposal.** Add-on §02 "Category-wise PO separation": *a requirement of 100 lock bodies, 200 handles and 500 packing boxes splits into separate POs, each visible only to its assigned manager*. The PO status flow runs Requirement → Vendor selection → Category-wise PO → Manager assignment → Vendor notification. The audit lists "Automatic split of one purchase requirement into one purchase order per category" as absent.

**Background.** Read `reports/procurement-vendor-portal.md` in full: E1–E7, Traps T1–T20, and "Conclusion (a)". Follow Conclusion (a), with the decisions below.

**This round you own procurement.** Lane B (cost centres) will not touch procurement this round.

## Decisions (binding)

### 1. Procurement category of a line

A line's procurement category is the **nearest ancestor-or-self of the item's category that appears in the resolved vendor's active `vendor_categories` rows**. If none appears, it is the item's own category (`ASSUMPTION §C0-1`).

This is the granularity at which the vendor is engaged, and it is the same set vendor managers are scoped to, so grouping and visibility cannot disagree.

Use `item_categories.path` / `depth` to resolve ancestors. Batch-load them; never query per line.

### 2. Vendor resolution, a pure rule in `service_rules.py`

Use the first source that yields a vendor:
1. request override;
2. `requisition_line.suggested_vendor_id`;
3. the item's preferred `ItemVendorLink`;
4. the preferred `VendorCategory` for the item's category or its nearest ancestor that has one.

If no source yields a vendor, the line is **unresolved**. If any line is unresolved, the whole conversion is refused with a `BusinessRuleError` listing every unresolved line and its reason (`§C0-2`).

The vendor must also pass `can_receive_purchase_orders`.

### 3. Price resolution, a pure rule

Use the first source that yields a price:
1. override;
2. `estimated_unit_price`;
3. `ItemVendorLink.last_purchase_price` for that (item, vendor).

Otherwise the line is unresolved. **Never** use `standard_cost` (`§C0-3`).

### 4. Grouping

- One PO per **(vendor_id, procurement_category_id, warehouse_id)**.
  - Single-requisition conversion has one warehouse.
  - Batch conversion may span several.
- Sort groups deterministically by `(str(vendor_id), str(category_id or ""), str(warehouse_id))` so concurrent conversions take the per-vendor advisory locks in the same order (T9).
- Bound the number of groups with the named constant `MAX_ORDERS_PER_CONVERSION` (20).

### 5. PO category column

`purchase_orders.item_category_id`:
- nullable FK item_categories RESTRICT;
- index `ix_purchase_orders_vendor_category (company_id, vendor_id, item_category_id)`.

It is **derived, never accepted from the client**:
- On convert, it is the group's procurement category.
- On manual create or update, set it to the single distinct procurement category of the lines (resolved against the PO's vendor), or NULL when the lines span several.
- Backfill existing POs in the migration with one set-based UPDATE, using the item's own category where every line shares one. Document locking and rollback.

### 6. Assignee, a pure rule `choose_vendor_assignee`

Eligible candidates are active, non-deleted users with `vendor_id = PO.vendor_id` who hold the vendor PO-respond right. **For now that right is `purchase_order.update`.** WP C1 later switches it to `vendor_portal.respond`, so keep the permission in one named constant.

Match a candidate when its `allowed_category_ids` contains the PO category **or an ancestor of it** (subtree semantics, via `path`). Then:
1. exactly one restricted match → assign it;
2. several → unassigned, reason `"ambiguous"`;
3. none → if exactly one unrestricted user (empty `allowed_category_ids`) exists, assign it;
4. otherwise → unassigned.

A NULL PO category matches only unrestricted users. Return `(user_id | None, reason)`.

### 7. Validate `assigned_vendor_user_id` everywhere (T3)

On create, update and send, the user must:
- exist in this tenant;
- be active;
- have `vendor_id` equal to the PO's vendor;
- match the PO category, or be unrestricted.

On failure answer `NotFoundError` (foreign id) or `ValidationError`. On **Send**, if none is given, default it with `choose_vendor_assignee`.

Add `GET /procurement/purchase-orders/{order_id}/vendor-assignees` (`require_permission(Permission.PURCHASE_ORDER_SEND)`). It returns eligible users (`id`, `full_name`, `email`, `allowed category codes`, `is_suggested`), because buyers lack `user.view`.

### 8. Endpoints

All take `require_permission(Permission.PURCHASE_ORDER_CREATE)` + `Depends(forbid_vendor_users)`. Declare static paths before `/{requisition_id}`.

| Endpoint | Behaviour |
|---|---|
| `POST /procurement/purchase-requisitions/{requisition_id}/conversion-preview` | read-only proposal |
| `POST /procurement/purchase-requisitions/{requisition_id}/convert-to-orders` | creates DRAFT POs |
| `POST /procurement/purchase-requisitions/conversion-preview` and `.../convert-to-orders`, with body `{requisition_ids: [...] (max 50), ...}` | batch form for many requisitions (MRP creates single-line ones) |

- Schemas follow Conclusion (a) (`ConversionLineOverride`, `RequisitionConversionRequest`, `ConversionGroupRead`, `RequisitionConversionResult`). Use **module-specific names**; check for OpenAPI collisions (anatomy trap 9).
- Service, inside ONE `transaction()`:
  1. Lock the requisitions (header lock in id order), then lock their lines (`lock_many`, id order).
  2. Compute outstanding quantity = `quantity − ordered_quantity`, skipping CANCELLED, fully ordered and excluded lines.
  3. Batch-load everything: items with `category_id` (add it to `PurchasableItem` / `PurchasableItemRepository.load`), category paths, vendor facts, purchasing terms, vendor categories, assignee candidates (users ⨝ roles, effective permissions folded in Python via a pure resolver extracted from `deps._resolve_permissions`).
  4. Resolve and group.
  5. Create each PO through a refactored shared body `_create_purchase_order(...)`, also used by `create_purchase_order`, which keeps the duplicate check with its 409 `acknowledge_duplicates` + reason + `po.approve` rule.
  6. Link each line through `_link_source_documents` (that moves APPROVED → CONVERTED).
  7. Write one audit row per PO and one summary audit row per requisition ("Split into PO-…, PO-…" with group context).
- POs stay **DRAFT** and go through submit → approve → send (`§C0-4`).
- `source_type = PURCHASE_REQUISITION`. Set `source_id` to the requisition id when the PO's lines all come from one requisition, otherwise NULL. Check the existing source CHECK constraints first.
- The preview returns the same groups with no writes, plus `duplicate_candidates` per group.

### 9. Duplicate rule

Matching on `requisition_line_id` ("same pending PR") becomes an **EXACT** match:
- add `requisition_line_ids` as an extra OR branch to `open_lines_for_duplicate_check`;
- add the field to `CandidateInput`.

`POST /purchase-orders/check-duplicates` gets `Depends(forbid_vendor_users)` (T2), and its query is no longer reachable by a vendor.

### 10. Validate unvalidated foreign keys (T3 + masters trap 3)

- `requisition_line_id` and `quotation_line_id` on PO lines must be in tenant, from an APPROVED/CONVERTED requisition or a SELECTED quotation, with the same item.
- `purchase_requisitions.department_id` / `branch_id` and `purchase_orders.branch_id` must be in tenant. Use the organization seam; organization is below procurement.

### 11. Vendor visibility (T4)

`PurchaseOrderRepository._scoped`, for vendor principals, adds:
- `status IN VENDOR_VISIBLE_PO_STATUSES`, i.e. SENT and every later status, plus CANCELLED only when `sent_at IS NOT NULL`;
- when the user's `allowed_category_ids` is non-empty: `item_category_id` within the allowed subtrees (path prefix) **OR** `assigned_vendor_user_id = tenant.user_id`.

An **empty** allowed set means unrestricted, which is the documented contract.

Update `tests/api/test_vendor_portal_scope_http.py` deliberately: a vendor's own DRAFT is now 404. Add a category-scope test.

### 12. Close a requisition

Add `POST /procurement/purchase-requisitions/{id}/close` with a `ReasonRequest` body, for APPROVED→CLOSED and CONVERTED→CLOSED. Use the permission declared on that machine edge; if none is declared, use `purchase_requisition.approve`. Today no route reaches CLOSED.

### 13. Vendor user category scope

- **Server:** in `users/service.py`, `allowed_category_ids` must exist in the tenant, be non-deleted, and be a subset of the vendor's active `vendor_categories`. Test it.
- **UI:** in `features/admin/user-dialogs.tsx`, a vendor account gets an "Item categories" multi-select limited to that vendor's categories. Find or add a buyer/admin-readable endpoint for a vendor's categories, gated on the right the users screen already needs.

### 14. Frontend

- **`requisitions-page.tsx`**
  - Row action "Raise purchase orders" when the status is APPROVED or CONVERTED with open quantity and `can(P.PURCHASE_ORDER_CREATE)`.
  - Header action "Raise POs from approved requisitions", a batch picker over approved open requisitions: server-paginated, with checkboxes.
  - Row action "Close" with a reason.
- **New `requisition-conversion-dialog.tsx`** (lazy chunk)
  - Calls the preview and shows one card per (vendor, category): lines, quantity, price and its source, vendor and its source, the assignee with its reason, and duplicate warnings.
  - Per-line overrides: vendor picker, price, quantity, exclude. Re-preview on change.
  - Convert. On success, toast the PO codes from `lib/messages.ts` and invalidate `queryKeys.procurement.all`.
  - On a 409 duplicate, show the candidates and, when `can(P.PURCHASE_ORDER_APPROVE)`, an acknowledge-with-reason resubmit. Reuse the existing duplicate dialog's presentation.
- **PO list:** a Category column (label from the category options; not sortable unless you add it to `sortable_fields`) and a category filter in the URL.
- **PO detail:** show the category and the assignee. "Send to vendor" opens a dialog with an assignee picker from `/vendor-assignees`, preselecting the suggested user.
- **Admin user dialog:** the vendor categories multi-select.

### 15. Demo data

- Give two demo vendors several `vendor_categories`.
- Create vendor-portal demo users per vendor, with category scopes: Manager 1 → Lock Body, Manager 2 → Handles, Manager 3 → Packaging, as in the proposal's own table. Use **valid** demo emails per `seed.py`'s rules. Give them a known demo password via the same mechanism the seed uses for staff demo users (check `masters.py` `STAFF`), and record it in the handbook or README demo-credentials section if one exists.
- Raise one approved demo requisition mixing lock bodies, handles and packing boxes, ready to convert. Leave it unconverted so the demo shows the action.

### 16. Docs

- `database.md`: the PO category column, §8 "Requisition converted" event.
- `assumptions.md` `§C0-1..n`: procurement category rule, vendor resolution order, price source, an unresolved line refuses all, ambiguous assignee means unassigned, conversion POs are DRAFT, vendors see SENT+ only.
- `security.md` §4: the vendor visibility rule.
- `api.md`: count and map.
- Handbook: Requisitions (Raise POs and Close), PO (Send with assignee), Users (vendor categories).

## Tests

**Unit**
- vendor resolution;
- price resolution;
- procurement-category resolution, including nearest-ancestor;
- grouping and sort order;
- assignee choice (restricted, ambiguous, unrestricted fallback, NULL category);
- the requisition-line EXACT duplicate.

**Integration**
- converting a three-category requisition creates three DRAFT POs with the right category and assignee;
- `ordered_quantity` rises and the requisition becomes CONVERTED;
- partial override and exclude;
- an unresolved line refuses everything and writes nothing;
- the duplicate 409 and the acknowledged path;
- batch conversion across requisitions and warehouses;
- foreign ids refused;
- `close`;
- a second conversion of the same requisition sees the ordered quantity (nothing left to order).

**API**
- vendor: DRAFT gives 404, SENT gives 200, an out-of-category PO gives 404, and an assigned out-of-category PO gives 200;
- `check-duplicates` gives 403 to a vendor;
- vendor-assignees permission.

**Frontend**
- conversion dialog: preview render, override re-preview, convert body, 409 path;
- requisitions page actions gating;
- send dialog body includes `assigned_vendor_user_id`;
- user dialog categories.

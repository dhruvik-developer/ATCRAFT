# WP D5: Specification masters, item specifications, and product accessories

Lane B, round 3, step 3 (after B3a and B3b). Assumption placeholders use `§D5-n`.

**Proposal (p.3, existing-system modules to keep):**
- "Category, brand, manufacturer & **specification** masters"
- "Product variants — size, finish, **accessories**"

Brand and manufacturer are covered by A1, and finish, size and colour by item attributes. Two pieces are missing:
- a governed **technical specification** vocabulary per product (material, lock type, backset, finish grade, key count, warranty years, and so on);
- **accessories** linked to a product.

**Background:** `reports/masters-pricing.md` §3 (the item-attribute precedent) and §5 (where specifications stand today). A1's brand and manufacturer pattern on main is the closest precedent; copy it.

## Decisions (binding)

### 1. Specification master: `specification_attributes`

Products module, `TenantMasterEntity`. Columns:
- `code` String(32), normalised, unique per company
- `name` String(120)
- `value_type` String(16): `TEXT | NUMBER | BOOLEAN | OPTION`, with a CHECK
- `unit` String(16), nullable (for example `mm`, `kg`)
- `options` ARRAY(String(64)): required non-empty exactly when the type is OPTION (CHECK)
- `is_filterable` bool, default false. When true, the dealer catalogue may filter on it.
- `sort_order`
- `is_active`

Lifecycle is a StateMachine ("Specification attribute"). Retirement is refused while any item holds a value.

### 2. Category templates: `category_specifications`

`TenantEntity` with `category_id`, `specification_attribute_id`, `is_required`, `sort_order`, unique on (category, attribute). An item **inherits** the templates of its category and its ancestors.

ASSUMPTION `§D5-1`: "required" produces a completeness **warning** on the item, not a block, so existing items do not become unsaveable.

### 3. Values: `item_specifications`

`TenantEntity`:
- `item_id`, `specification_attribute_id`
- exactly one of `value_text`, `value_number` (Numeric 18,4), `value_bool`, `value_option`, enforced by a CHECK on the value type and validated in the service
- unique on (item, attribute)

Write path: `PUT /products/items/{item_id}/specifications` replaces the full set.
- Validate tenant, active status and value types.
- An OPTION value must be one of the attribute's options.
- Audit the change as an UPDATE with old and new values.

Read path: `GET /products/items/{item_id}/specifications` returns:
- the values
- the inherited template with missing-required flags

### 4. Accessories: `item_accessories`

`TenantEntity`:
- `item_id`, `accessory_item_id` (FK items, RESTRICT)
- `quantity` (Quantity > 0, default 1)
- `relation` String(16): `OPTIONAL | INCLUDED`. OPTIONAL is an add-on the buyer may add; INCLUDED is in the box (informational).
- `sort_order`, `notes`
- CHECK not self; unique on (item, accessory)

Routes:
- `GET /products/items/{item_id}/accessories`
- `POST` on the same path, validating tenant and active status. An OPTIONAL accessory must be `can_sell` so it can be bought.
- `PATCH /products/item-accessories/{id}`
- `DELETE /products/item-accessories/{id}`

Validation and safety:
- Accessory cycles (A ↔ B) are allowed, because accessories are not BOMs.
- An accessory relation never affects stock, BOM or MRP. Record this as `§D5-2`, and state it in `database.md` so nobody mistakes it for a kit.

### 5. Permissions

Reuse `item_attribute.view` and `item_attribute.manage` for the specification master and the category templates. Specifications belong to the same "describe the item" family. ASSUMPTION `§D5-3`: no new permission, no grant migration.

Item values and accessories use `item.update`, and are readable with `item.view`. Write every permission literally in the decorators.

### 6. Item filters and search

`ItemFilters` gains:
- `spec` filters for filterable OPTION and BOOLEAN attributes (`spec_code=value`), as a bounded list
- `has_accessories`

Rule: an empty filter value returns nothing, per the CLAUDE.md empty-list trap.

### 7. Frontend

**Masters → new "Specifications" tab**
- attribute CRUD dialog: type, unit, options editor, filterable flag
- a "Category templates" sub-view: pick a category, attach attributes and mark them required

**Item detail → "Specifications" panel**
- edits values with type-appropriate inputs
- shows inherited required attributes that are missing as warnings

**Item detail → "Accessories" panel**
- add accessories with an item picker, quantity and relation
- reorder and remove
- show stock-free "INCLUDED" items as "In the box"

**Items list:** a "Has accessories" filter, plus spec filters only if simple. Otherwise leave spec filters for the dealer catalogue.

All gates use the exact rights. Use `SchemaOf` types, `lib/messages.ts`, and responsive layouts.

### 8. Dealer catalogue hook

C3a, next round, will add specifications and accessories to the dealer item detail and catalogue filters. Expose a batch-friendly service function now:

`specifications_for_items(session, tenant, item_ids)` and `accessories_for_items(...)`, each **one query**.

### 9. Demo data

- Attributes: Material (OPTION: Brass, Stainless Steel 304, Zinc Alloy, Aluminium), Lock Type (OPTION: Mortise, Cylindrical, Rim), Backset (NUMBER, mm), Keys Included (NUMBER), Warranty (NUMBER, years), Fire Rated (BOOLEAN).
- Category templates for lock bodies and handles.
- Values on demo finished goods.
- Accessories: a handle → screw kit (INCLUDED) and a matching escutcheon or rose (OPTIONAL).
- Idempotent.

### 10. Docs

- `database.md`: §2 and §3 (a "Specifications and accessories" subsection, with the rule that nothing branches on a specification value)
- `assumptions.md`
- `api.md`
- handbook: the Masters and Item detail chapters

## Tests

**Unit:** value-type validation; OPTION membership; template inheritance through ancestors; lifecycle `validate()`.

**Integration:**
- CRUD, and the list actually lists
- a cross-tenant attribute or accessory is refused
- a retired attribute is refused on the way in
- PUT replaces the set and audits it
- required warnings are computed
- accessory self-reference is refused, and a non-sellable OPTIONAL accessory is refused
- filters work, and an empty filter value returns nothing
- the batch helpers issue one query each (assert the query count)

**Frontend:** the masters tab dialog body; the specifications panel body; the accessories panel body.

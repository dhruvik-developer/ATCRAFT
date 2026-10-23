# Customers/dealers, reports, dashboard and documents: map for audit gaps 1 and 10

> Historical implementation research from the proposal build. Source references
> and status descriptions apply to that checkpoint. Consult the
> [current guides](../../README.md) and source before following operational advice.

This is a read-only survey; no file was changed. Line numbers are as of HEAD `cdac4e0`. I could not read the proposal PDF here: it is image-only and no page renderer is available. The proposal mapping below therefore uses the task's own list.

**Path aliases (all absolute):**
- `API` = `C:\Project\Other\ATCRAFT\apps\api\src`
- `TESTS` = `C:\Project\Other\ATCRAFT\apps\api\tests`
- `MIG` = `C:\Project\Other\ATCRAFT\apps\api\migrations\versions`
- `WEB` = `C:\Project\Other\ATCRAFT\apps\web\src`
- `DOCS` = `C:\Project\Other\ATCRAFT\docs`
- `E2E` = `C:\Project\Other\ATCRAFT\tests\e2e`

---

## 0. Verdict on the audit's claims in this area

| Audit claim | Verdict | Evidence |
|---|---|---|
| No public dealer site (catalog, cart, wishlist, banners, offers) | **Confirmed absent.** The foundations exist (see below). | No table, route or screen exists. Searching for `cart|wishlist|banner|storefront|checkout|promotion|coupon` across `API` and `WEB` finds only unrelated words. There is no dealer principal: `users` has only `vendor_id` (`API/modules/users/models.py:150`). `Principal` and `TenantContext` carry only `vendor_id` (`API/core/deps.py:83,338`). There is no dealer role preset (`API/core/permissions.py:460-677`). Items have no image column. |
| Mobile API (browse, cart, wishlist, orders, tracking, returns) | **Confirmed absent.** | Nothing is mobile-specific. A native client would have to keep a cookie jar and send `X-CSRF-Token` to refresh, because there is no refresh in a request body (`DOCS/assumptions.md` §14, L267-294). `NotificationChannel` is `IN_APP`/`EMAIL` only (`API/modules/notifications/models.py:82-90`). |
| No dealer-performance report | **Confirmed.** | `ReportKey` has 19 tabular reports plus `PACKING_LIST` (`API/modules/reports/models.py:62-95`). None aggregates per customer or dealer. Partial help: `customer_id` filters on sales-order-status, order-delay, product-profitability, dispatch and courier-tracking. |
| No payment-follow-up workflow | **Confirmed.** | There is no follow-up, promise-to-pay or reminder table or route. `NotificationType.PAYMENT_DUE` is declared (`API/modules/notifications/models.py:125`) but **never emitted**. Its only other appearance is `WEB/features/notifications/notification-types.ts:42`. `SALES_ORDER_AT_RISK` is also unused. |
| "Outstanding and ageing are there" | **Partly true. Caveats below.** | `/finance/outstanding` covers **payables only** (`API/modules/finance/router.py:706-746`). Customer outstanding is the cached `customers.outstanding_amount`, plus `/finance/ageing?party_type=CUSTOMER`, plus `/finance/ledger/customer/{id}`. Receivable ageing runs from the **posting date**, so `credit_days` is ignored and `not_due` is always 0. The receivable only rises through manual CHARGE customer adjustments, because dispatch and sales post nothing to the ledger. |

**Things that partly exist and that the audit did not mention**

- The dealer channel is first-class:
  - `CustomerType.DEALER/DISTRIBUTOR`
  - `customers.price_list_type`
  - `PriceType.DEALER`, `items.dealer_price` and `item_prices` slabs with validity dates
  - a staff-only read view at `/customers/dealers`
  - `dealer.view` / `dealer.manage` permissions
  - demo dealers `DLR-VASTU` and `DLR-METRO` (`API/core/demo_data/masters.py:690-720`)
- Sales-invoice plumbing exists but nothing is behind it:
  - permissions `sales_invoice.view/create` (`API/core/permissions.py:116-117`)
  - `DocTypes.SALES_INVOICE = ("sales_invoice","INV")` (`API/common/codes.py:67`)
  - `SalesOrderStatus.INVOICED` with transition permission `sales_invoice.create` (`API/modules/sales/service.py:207-212`)
  - There is **no invoice table, and nothing ever moves an order to INVOICED or CLOSED.**
- Customer-return seams exist:
  - `TransactionType.CUSTOMER_RETURN`, an inflow to AVAILABLE (`API/modules/inventory/models.py:118,148`; `API/modules/inventory/service.py:373`)
  - QC maps `"sales_return"` and `"customer_return"` to `QcStage.RETURN` (`API/modules/quality/service.py:191-192`)
  - `items.can_return` exists.
- `dispatches.invoice_document_id` is a foreign key to `documents` (`API/modules/dispatch/models.py:300-302`). It is validated by `DocumentLookupRepository.exists_in_tenant`, but no screen sets it. It appears only as a type field in `WEB/features/dispatch/dispatch-types.ts:158`.
- A partner scorecard precedent exists: vendor rating, `GET` and `POST /vendors/{id}/rating` (`API/modules/vendors/router.py:520-565`).

---

## 1. Exists today

### 1.1 Customer / dealer master (backend)

**Model: `API/modules/customers/models.py`**
- The module docstring (L1-34) states the design: "A dealer is a customer with different commercial terms." It is one `customers` table. `customer_type` classifies the account; `price_list_type` chooses the `item_prices.price_type`.
- `CustomerType` (L66-78) has CUSTOMER, DEALER, DISTRIBUTOR and OEM. `AddressType` (L81-92) has BILLING and SHIPPING.

`class Customer(TenantMasterEntity)`, table `customers` (L100-239):

| Field | Line | Meaning / notes |
|---|---|---|
| `code` String(32) | 105 | Unique per company, `uq_customers_company_id_code` (L194) |
| `name`, `legal_name` | 106, 109 | |
| `customer_type` | 111 | CHECK `customer_type_known` |
| `gstin`, `pan` | 114-115 | Partial unique `uq_customers_company_gstin` (L218-224) |
| `price_list_type: PriceType` | 122-124 | The "price tier". Default SALE. `PriceType` is SALE / DEALER / DISTRIBUTOR (`API/modules/products/models.py:101-106`). |
| `credit_limit` Money, nullable | 128 | NULL means unlimited; 0 means no credit |
| `credit_days` int ≥ 0 | 131 | The "payment terms days"; schema caps it at 365 |
| `outstanding_amount` Money | 136-138 | Owned by finance; no ≥ 0 CHECK |
| `is_active`, `is_blocked`, `block_reason`, `blocked_at`, `blocked_by_id` | 141-156 | Credit hold. CHECKs `block_has_reason` and `block_has_timestamp` |
| `assigned_sales_user_id` | 162-164 | FK users, SET NULL. Partial index `ix_customers_sales_user` |
| `default_transporter_id` | 178 | Bare UUID |
| `addresses`, `contacts` | 186-191 | `lazy="raise"` |

**Not present on customers:** priority, discount % or tier discount, any user/portal link, segment, territory or region, rating or score, and header-level email or phone (these exist only on contacts and addresses).

- `CustomerAddress` (L242-337) has these fields:
  - `address_type`, `label`, address lines, `city`, `state`
  - `state_code`: 2 digits, mandatory when country is India (CHECK L327-336)
  - `postal_code`, `country`, `gstin` (per address), `contact_person`, `phone`, `email`
  - `is_default`: one per (customer, type), partial unique `uq_customer_addresses_one_default` (L315-322)
  - `is_active`
- `CustomerContact` (L340-381) has `name`, `designation`, `email`, `phone` (at least one required, CHECK L377-380), and `is_primary` (one per customer, L368-374).

**Schemas: `API/modules/customers/schemas.py`**
- `CustomerCreate` (L63-97): `customer_type` defaults to CUSTOMER, `price_list_type` to SALE, `credit_days` must be 0..365.
- `CustomerUpdate` (L100-139): code is immutable, `is_blocked` cannot be set here, and `clear_credit_limit`, `clear_assigned_sales_user` and `clear_default_transporter` flags exist.
- `CustomerSummary` (L142-160) and `CustomerDetail` (L163-178), which adds a derived `credit_headroom`.
- `CustomerFilters` (L181-197): search, customer_type, price_list_type, assigned_sales_user_id, is_active, is_blocked, over_credit_limit, state_code, include_deleted.
- `OutstandingAdjustment` (L217-243) has no HTTP route; only finance uses it.
- `CreditStatus` (L246-264).

**Routes: `API/modules/customers/router.py`** (prefix `/customers`)

| Route | Permission | Line |
|---|---|---|
| `GET /customers` | customer.view | 112 |
| `GET /customers/dealers` (declared before `/{id}`) | **dealer.view OR customer.view** | 129 |
| `POST /customers` | customer.create, plus `dealer.manage` checked in the service for DEALER/DISTRIBUTOR | 153 |
| `GET /customers/{id}` | customer.view | 170 |
| `PATCH /customers/{id}` | customer.update, plus dealer.manage if the old or new type is a dealer channel | 182 |
| `POST /{id}/activate`, `/deactivate`, `/block`, `/unblock`, `/restore` | customer.update | 201-323 |
| `GET /{id}/credit-status?order_value=` | customer.view | 277 |
| `DELETE /{id}?reason=` | customer.delete | 296 |
| addresses: GET / POST / PATCH / DELETE / set-default | view / update | 331-411 |
| contacts: GET / POST / PATCH / DELETE / set-primary | view / update | 419-498 |

**Service: `API/modules/customers/service.py`**
- Recorded assumptions:
  - `CREDIT_LIMIT_NULL_MEANS_UNLIMITED` (L100-107)
  - `INACTIVE_CUSTOMER_MAY_NOT_ORDER` (L109-112)
  - `DEALER_CHANNELS` plus the "requires dealer.manage" rule (L114-122)
  - `ENFORCE_GSTIN_STATE_CODE_MATCH` (L124-130)
  - `BLOCK_RETIREMENT_WITH_OUTSTANDING` (L132-136)
- State machines (no status column; state is derived from the flags):
  - `CUSTOMER_LIFECYCLE` (L248): ACTIVE↔INACTIVE, both → ARCHIVED, ARCHIVED → INACTIVE.
  - `CREDIT_STANDING` (L289): GOOD_STANDING↔ON_HOLD.
- Pure credit rules:
  - `evaluate_credit(customer, order_value) -> CreditAssessment` (L380-441). It checks blocked, then inactive, then `outstanding + order <= limit`. **There is no overdue or ageing input.**
  - `assert_within_credit_limit` (L444) and `credit_status_of` (L479).
- `assert_channel_write_allowed(principal, *types)` (L534-547).
- `CustomerService` (L554) methods: `list_customers` (566), `list_dealers` (573), `get_customer` (599), `get_credit_status` (612), `create_customer` (621), `update_customer` (659), `set_active` (728), `block` (759), `unblock` (796), `retire_customer` (834), `restore_customer` (882), addresses (931-1118), contacts (1120-1284), and `_to_detail` (L1350-1376, which assembles the detail from a dict because of `lazy="raise"`).
- Cross-module entry points:
  - `get_customer_for_order(session, tenant, customer_id, *, lock=False) -> Customer` (L1379)
  - `assert_customer_can_order(session, tenant, customer_id, order_value, *, lock=True) -> CreditAssessment` (L1396)
  - `apply_outstanding_delta(session, tenant, customer_id, OutstandingAdjustment) -> Customer` (L1414). Only finance calls it: `API/modules/finance/service.py:2653` (receipt approve), `:2799` (receipt cancel) and `:3259` (customer adjustment).
- **Finding:** address and contact writes never call `assert_channel_write_allowed`. The service methods take no principal (L946, 987, 1036, 1078, 1132-1284), and the routes at L351-498 pass none. So `customer.update` alone can change a DEALER account's GSTIN, state code or ship-to. This contradicts the assumption at L114-119 and the handbook text "amending one ... needs the dealer-management right".

**Permissions and presets: `API/core/permissions.py`**
- The `customer.*` and `dealer.*` permissions are at L42-47. The group "Business Partners" is at L213.
- `_SALES` (L400-420, used by the SALES_EXECUTIVE preset) has customer view/create/update and `dealer.view`. It does not have `dealer.manage` or `customer.delete`. It also has `sales_invoice.view`, `price.view`, `report.view` and `dashboard.view`.
- `_ACCOUNTS` (L422+) has `customer.view`, `sales_invoice.view/create` and `payment.*`.
- AUDITOR is `VIEW_ONLY` (L257-262, every `*.view`, `*.ledger_view`, `*.cost_view` and `*.valuation_view`).
- Only ADMIN and SUPER_ADMIN hold `dealer.manage`.

**Tests:**
- `TESTS/unit/test_customers_credit.py`, `test_customers_lifecycle.py`, `test_customers_schemas.py`
- `TESTS/integration/test_customers_service.py`
- `TESTS/unit/test_finance_customer_adjustment.py`, `TESTS/api/test_finance_customer_adjustments_http.py`

### 1.2 What a "dealer" is in the UI

- `WEB/features/customers/dealer-model.ts`:
  - The docstring (L1-37) says: "A dealer is not a separate table … There is no dealer write endpoint."
  - `Dealer = SchemaOf<'CustomerSummary'>` (L69); `DealerChannel = 'DEALER'|'DISTRIBUTOR'` (L80); `DEALER_CHANNEL_LABELS` (L87).
  - It has its own `dealerKeys` (L55-58), outside `lib/query-client` on purpose.
  - Helpers: `creditHeadroom` (L134), `isOverCreditLimit` (L151), `isCompleteStateCode` (L114).
- `WEB/features/customers/dealers-page.tsx` is a **read-only staff list** of DEALER and DISTRIBUTOR accounts showing their credit position:
  - It calls `useListQuery({path:'/customers/dealers', queryKey: dealerKeys.list, defaultSortBy:'code', staleTime: staleTimes.master, filters})` (L98-119).
  - Filters: channel, price list, standing (blocked / clear / over_limit), active status, state code, and "mine" (`assigned_sales_user_id` equals the signed-in user).
  - Columns (L160-290): code, name, channel, GSTIN, credit limit, outstanding with headroom, credit days, standing, status, opened.
  - The page gate is `canAny(P.DEALER_VIEW, P.CUSTOMER_VIEW)` (L84). The route gate is only `[P.DEALER_VIEW]` (`WEB/app/router.tsx:345`), and so is the nav entry (`WEB/app/navigation.ts:79`). This is a minor inconsistency.
- `WEB/features/customers/customers-page.tsx` is the master list.
  - `canManageDealers = can(P.DEALER_MANAGE)` (L73) gates row actions on dealer rows (L237).
  - `customer-form-dialog.tsx` locks dealer records and hides the dealer channels without `dealer.manage` (L406, L536-538).
  - `customer-relations.tsx` holds the address and contact panels on their own endpoints (L252-330, L742-803).
  - `customer-types.ts` has hand-written mirrors `CustomerRow`, `CustomerDetail`, `CustomerAddress`, `CustomerContact` and `CreditStatus` (L43-138), and `isDealerChannel` (L197).
  - `customer-picker.tsx` is a server-searched picker on `/customers` with `is_active=true` (L54-68). It has **no channel prop**.
- Tests: `customers-page.test.tsx` and `dealers-page.test.tsx`. The latter pins the read-only property and the either-permission property.

### 1.3 Non-staff principals: the vendor-portal pattern, and the missing dealer binding

There is **no dealer or customer binding anywhere.** `customer_id` and `dealer` have no hits in `API/modules/auth`, `API/modules/users`, `API/core/deps.py` or `API/core/security.py`. There is no dealer role and no `is_dealer_role` flag. `MeResponse` and the frontend `CurrentUser` have no `customer_id`.

The vendor binding is the pattern to mirror. Every place that special-cases a vendor principal:

| Layer | Location |
|---|---|
| DB column | `users.vendor_id` FK vendors, ON DELETE CASCADE (`API/modules/users/models.py:150-152`). Also `allowed_category_ids` ARRAY (L156) and `allowed_warehouse_ids`. Index `ix_users_vendor` (L183). `User.is_vendor_user` (L187-189). |
| Role flag | `roles.is_vendor_role` (`API/modules/users/models.py:79`) |
| Presets | `_VENDOR_BASE` = purchase_order.view, dispatch.view, document.view, notification.view (`API/core/permissions.py:451-458`). Roles VENDOR_OWNER, VENDOR_MANAGER, VENDOR_DISPATCH_MANAGER, VENDOR_QC_CONTACT and VENDOR_ACCOUNT_MANAGER, all `is_system` and `is_vendor_role` (L611-676). `VENDOR_ROLE_CODES` (L679-681). The seed copies the flag (`API/core/seed.py:239`). |
| Principal | `Principal.vendor_id`, `allowed_category_ids`, `is_vendor_user`, `can_access_category` (`API/core/deps.py:83-84,107-119`). `_load_principal` copies them (L209-211). `TenantContext.vendor_id` (L338) is set by `get_tenant_context` (L353). |
| Hard gate | `forbid_vendor_users` raises `AuthorizationError(code=SCOPE_DENIED)` (`API/core/deps.py:319-329`). It is used on **two routes only**: `API/modules/procurement/router.py:830` (PATCH PO) and `:855` (submit PO). |
| Repository scoping (`_scoped` overrides) | users (`API/modules/users/repository.py:101-121`); vendors, contacts, categories (`API/modules/vendors/repository.py:122-133`, ~365, ~445); procurement quotations (`repository.py:675`), POs (`:898-911`), GRNs (`:1216`), purchase returns (`:1320`); item search by vendor categories (`API/modules/products/repository.py:532-535`) |
| Second check in the service | `API/modules/procurement/service.py:1521,1557,2552,2628` |
| Users service | `assert_vendor_role_consistency` (`API/modules/users/service.py:466-495`), called at L796 (create), L934 (update) and L1043 (role replace). `_assert_references` checks the vendor exists (L1314-1332). `_assert_scope_is_meaningful` (L1335-1350). `"vendor_id"` is an audited field (L186). The summary sets `is_vendor_user` (L661). |
| Users schemas, router, repository | `UserCreate.vendor_id` (schemas L152), `UserUpdate.vendor_id` and `clear_vendor` (L208, L227), the read field (L321), `UserSummary.is_vendor_user` (L362), `UserFilters` (L374-375); router filters (`router.py:87-105`); `vendor_exists` (`repository.py:395`) |
| Auth | `MeResponse.vendor_id` (`API/modules/auth/schemas.py:174`), set in `AuthService.me` (`API/modules/auth/service.py:775`). Login has no vendor-specific rule. |
| Frontend | `auth-context.tsx` `vendor_id` (L54, 84, 106) and `isVendorUser` (L164, L351). `admin/user-dialogs.tsx`: VendorPicker binding and role list filtered by `is_vendor_role` (L54, 97, 224, 254-256, 470, 633, 687-739). `profile-page.tsx` shows a "Vendor portal" badge (L759-760). There is **no separate vendor shell**: it is the same SPA, the sidebar is filtered by permission, and the index route is the dashboard (`WEB/app/router.tsx:214-221`). |
| Tests | `TESTS/api/test_vendor_portal_scope_http.py` covers **purchase orders only**. `_as_vendor_owner` (L119-131) builds the Principal from `ROLE_PRESETS`; `_sign_in_as_vendor_owner` (L134-138) overrides `get_current_principal`. Also `TESTS/unit/test_procurement_vendor_scope.py`. `TESTS/unit/test_role_presets_reach_roles.py:63-84` pins the system-role list, which includes all five vendor roles. |
| Docs | `DOCS/security.md:177-189` and `DOCS/assumptions.md` §33 (L746-770) plus open question 5 |

**Existing defect: do not copy this model.** Vendor scoping is opt-in per repository.
- It is **absent** from finance, dispatch, job_work, quality, documents and reports. Searching those modules for `tenant.vendor_id|principal.vendor_id|is_vendor_user|forbid_vendor` returns nothing.
- Yet vendor presets hold `dispatch.view` and `document.view` (all five roles), `job_work.view`, `qc.view`, `vendor_invoice.view`, `payment.view` and `ledger.view`.
- A vendor account can therefore:
  - list every dispatch in the tenant, including customer consignments
  - list and download every stored document, including report exports and other vendors' files
  - list every job-work order and QC inspection
  - read `/finance/ledger/vendor/{any}` and `/finance/ledger/customer/{any}`
- `DOCS/security.md:183` ("scoped to their own vendor's records") holds only for procurement, vendors, users and categories.

### 1.4 Sales order creation: what a dealer checkout would call

**Model: `API/modules/sales/models.py`**
- `SalesOrder(TenantEntity, OptimisticLockMixin)` (L135-287):
  - `customer_id` (L142), `customer_po_number` (L147)
  - `order_date`, `required_date`, `promised_date` (L154-156)
  - `branch_id`; `warehouse_id` is **NOT NULL** (L164)
  - `status` (L169)
  - `priority` int 1..9, default 5. This is a recorded assumption at L66-76; CHECK `priority_in_range`.
  - `is_urgent` (L181)
  - money columns (L186-203)
  - `place_of_supply_state_code` and `is_interstate`, frozen at creation (L209-214)
  - `billing_address_id` and `shipping_address_id`, FK customer_addresses (L217-222)
  - `transporter_id`, bare UUID (L226)
  - `requires_approval`, `approved_by_id`, `approved_at` (L232-238)
  - **There is no `source` or channel column.** Only `created_by_id` from `ActorMixin` says who keyed the order.
- `SalesOrderLine` (L290-431): `unit_price`, `discount_percentage`, `discount_amount`, the GST split, `allocated_quantity`, `dispatched_quantity`, `invoiced_quantity`, a derived `status`, and `reservation_id`.

**Request: `SalesOrderCreate`** (`API/modules/sales/schemas.py:149-182`, `extra="forbid"`)
- Required: `customer_id`, `required_date`, `warehouse_id`, and `lines` (1..200, `MAX_LINES_PER_ORDER` L54).
- Optional: `customer_po_number`, `order_date` (defaults to today), `promised_date`, `branch_id`, `priority`, `is_urgent`, `currency` (default INR), `billing_address_id`, `shipping_address_id`, `transporter_id`, `notes`.
- `SalesOrderLineIn` (L62-96): `line_number`, `item_id` and `quantity > 0`. Optional `item_variant_id`, `description`, `uom_id`, `unit_price` (an override), `discount_percentage` **or** `discount_amount`, `required_date`, `notes`.

**Pricing** (`API/modules/sales/service.py`)
- `_build_pricing_context` (L709-764) enforces `can_sell` and `is_active` (L737-748). It loads channel price rows for `PriceType(customer.price_list_type)` through `PriceListRepository.candidates_for_items(item_ids, *, price_type, variant_ids, on_date)` (`API/modules/sales/repository.py:175-219`).
- `resolve_unit_price` (L592-624) uses `products.service.select_price_row` (`API/modules/products/service.py:467-501`). The order of preference is: variant-specific row, then the highest slab the quantity reaches, then the newest `valid_from`.
  - If no row applies, it falls back to `ITEM_MASTER_FALLBACK_FIELD` (`products/service.py:1779-1783`): SALE uses `sale_price`, DEALER uses `dealer_price`, and **DISTRIBUTOR has no fallback, so pricing raises `BusinessRuleError`.**
- **Dealer price is therefore automatic** for a customer with `price_list_type=DEALER`.
- `price_line` (L627-706): a `unit_price` override needs `price.manage`, and an override below `items.standard_cost` sets `requires_approval` (`is_below_cost` L153-169).
- **Discounts are not permission-gated.** `discount_percentage` (0-100) or `discount_amount` flows straight into `compute_line_amounts` (L288-355). The only refusal is a discount larger than the gross line value, and a discount never sets `requires_approval`.
- Tax: `_resolve_shipping_context` (L767-802) only accepts addresses belonging to that customer and falls back to the default shipping address. Then `resolve_tax_treatment` (L260) and `_assert_seller_state_known` (L805).

**Lifecycle: `SALES_ORDER_LIFECYCLE`** (L174-245)

| From → To | Permission | Notes |
|---|---|---|
| DRAFT → CONFIRMED | sales_order.update | |
| CONFIRMED → ALLOCATED | sales_order.update | |
| ALLOCATED → PARTIALLY_DISPATCHED / DISPATCHED | dispatch.create | Driven by `API/modules/dispatch/service.py:414-435` |
| DISPATCHED → INVOICED | sales_invoice.create | **No caller exists** |
| INVOICED → CLOSED | sales_order.update | |
| DRAFT / CONFIRMED / ALLOCATED → CANCELLED | sales_order.cancel | |

**Service functions**
- `create_order(session, tenant, principal, payload) -> (SalesOrder, lines)` (L1023-1136):
  - Refuses a deleted or inactive customer (L1041-1045). **It does not check the block flag or credit.**
  - Numbers the order with `next_document_number(DocTypes.SALES_ORDER)` (L1078) and leaves it in DRAFT.
  - Writes an audit CREATE row (L1116-1134). Nothing is reserved.
- `confirm_order(session, tenant, principal, order_id, *, reason, allocation=None) -> ConfirmResult` (L1608-1752):
  - Locks the order header and checks the approval gate.
  - Calls `assert_customer_can_order(..., lock=True)` (L1659), which checks block, inactive and limit, and **nothing about overdue amounts.**
  - Takes an advisory lock per item and reads `AvailabilityRepository.available_by_item(item_ids, warehouse_id)` (`repository.py:222-253`, never cached).
  - Reserves `min(demand, available)` through the AllocationPort. Partial reservation counts as success.
  - Moves to CONFIRMED, and to ALLOCATED as well if fully covered. Writes an audit RESERVATION_CHANGE.
  - Returns `ConfirmResult(order, allocation, credit_headroom)` (schemas L344-352).
  - `principal` is unused inside; authority comes only from the route (`router.py:234`).
- Other functions:
  - `_update_confirmed` (L1322) re-checks credit only when the total grows (L1411-1418).
  - `approve_order` (L1554) enforces segregation through `can_approve` (L253).
  - `cancel_order` (L1755) and `delete_draft_order` (L1518). The latter is a **hard delete**, justified by "no number was reported to the customer".
  - Reads: `list_orders` (L900), `get_order_detail` (L913), `allocation_status` (L922), `order_detail` (L828).

**Routes** (`API/modules/sales/router.py`, prefix `/sales`)
- `GET /demand-summary` (L106), `GET ""` (L129), `POST ""` (L146), `GET /{id}` (L167), `PATCH` (L181), `DELETE` (L205)
- `POST /{id}/confirm` (L224), `/cancel` (L254), `/approve` (L269)
- `GET /{id}/allocation-status` (L292)
- **`SalesOrderRepository` (repository L397-454) has no `_scoped` override.** Any `sales_order.view` holder sees every customer's orders.
- `OrderPartyRepository.customer_price_type` (repository L340) is defined and **never called**.

**What a dealer checkout must call, in order:**
1. `customers.service.get_customer_for_order`
2. `OrderPartyRepository.addresses_of` / `default_shipping_address`
3. A default-warehouse lookup. `warehouses.is_default` exists (`API/modules/warehouses/models.py:120-122,160-168`), but there is no getter.
4. Build a `SalesOrderCreate` with **no `unit_price`** and **no client-supplied discount**.
5. `sales.service.create_order`
6. Optionally `sales.service.confirm_order`, in a separate outer transaction.

### 1.5 Reports module: every report

**How a report works**
- **Catalogue:** `ReportKey` (`API/modules/reports/models.py:62-95`, with `DOCUMENT_ONLY_REPORT_KEYS = {PACKING_LIST}` at L105) plus `REPORT_CATALOGUE: dict[ReportKey, ReportDefinition]` (`API/modules/reports/service.py:538-745`).
  - `ReportDefinition(key, title, description, row_model, build, requires_period, cacheable, derive)` (L351-364).
  - An import-time assertion checks the catalogue is complete (L747-757).
- **SQL:** each report is a `Select` built in `ReportQueryRepository` (`API/modules/reports/repository.py:274+`). Columns are labelled with the row-schema field names. Derived values come from pure `_derive_*` functions (`service.py:408-535`).
- **Runner:** `run_report` (`service.py:906-965`).
  1. `count_rows(cap=REPORT_INLINE_ROW_LIMIT=5000)`.
  2. Above 5000 rows, `_queue_report` (L968) runs, which **needs `report.export`** (`may_export` comes from `router.py:176`). It creates a `report_snapshots` job and the Celery task `reports.generate`, which calls `render_report_xlsx` (`API/workers/tasks/reports.py:809`) and stores the result as `Document(category=REPORT)`. The client then uses `GET /reports/jobs/{id}` followed by `/documents/{id}/download`.
  3. Otherwise the rows are paginated. Reports with `cacheable=True` go through a Redis cache (`CacheNamespace.REPORT`, 300 s).
- **Period:** `ReportPeriod` allows at most 366 days with both ends required (`API/modules/reports/schemas.py:55-92`; `report_period` in `router.py:89-109`).
- **Filters:** `ReportFilters` is shared by all reports: item_id, warehouse_id, vendor_id, customer_id, category_id, status (≤ 32 characters), min_days, search (`schemas.py:95-118`). **There is no `customer_type`.**
- **Authorisation:** every route requires only `report.view`; there is no per-report permission.
  - `GET /reports/catalogue` returns **all** descriptors whoever calls it (`service.descriptors` L764-776). The comment at `WEB/features/reports/reports-page.tsx:4` says it is "filtered to what this user may run", which is wrong.
  - PRODUCT_PROFITABILITY exposes `standard_cost` arithmetic to any `report.view` holder.
- **Exports:**
  - XLSX is produced only for results over 5000 rows. Small reports have no export button.
  - The only PDF is the packing list (`render_packing_list_pdf`, `reports.py:248`; task `generate_packing_list`, L329).
  - The full valuation XLSX is `POST /finance/inventory-valuation/export` (`API/modules/finance/router.py:835-862`; task at `reports.py:565`).
- **`inventory_summary`** (`models.py:224-290`) is refreshed by `maintenance.refresh_summary_views` every 900 s (`API/workers/celery_app.py:73-77`) through `refresh_inventory_summary` (`service.py:1338`).

**All reports.** Routes are `GET /api/v1/reports/<path>`, every one gated on `report.view`. Router and repository columns give line numbers.

| Key | Path | Router | Repo builder | Period | Cached | Notes |
|---|---|---|---|---|---|---|
| SALES_ORDER_STATUS | /sales-order-status | 253 | sales_order_status 423 | yes | yes | filters customer_id, warehouse_id, status, search |
| STOCK_AVAILABILITY | /stock-availability | 328 | 478 (summary) | no | no | on_hand, reserved, available, `below_reorder_level` = "reserved vs available" |
| MRP_SHORTAGE | /mrp-shortage | 440 | 483 | yes | no | |
| PURCHASE_PENDING | /purchase-pending | 466 | 519 | yes | yes | |
| PRODUCTION_PENDING | /production-pending | 487 | 577 | yes | yes | |
| JOB_WORK_PENDING | /job-work-pending | 508 | 617 | yes | yes | |
| QC_REJECTION | /qc-rejection | 588 | 670 | yes | yes | |
| VENDOR_PERFORMANCE | /vendor-performance | 609 | 716 | yes | yes | on-time % and rejection %, weighted equally (assumption at `service.py:303`) |
| PRODUCT_PROFITABILITY | /product-profitability | 295 | 774 | yes | yes | revenue net of discount, before tax, against qty × standard_cost; `customer_id` filter |
| FAST_MOVING | /fast-moving | 398 | 851 | yes | yes | |
| SLOW_MOVING | /slow-moving | 419 | 871 | yes | no | |
| DEAD_STOCK | /dead-stock | 374 | 910 | no | no | default 180 days (`DEAD_STOCK_DAYS` `service.py:121-126`) |
| ORDER_DELAY | /order-delay | 274 | 928 | yes | yes | open orders with `required_date < today`; `customer_id` filter |
| PRODUCTION_EFFICIENCY | /production-efficiency | 529 | 980 | yes | yes | |
| MACHINE_UTILISATION | /machine-utilisation | 557 | 1032 | yes | yes | "no shift calendar" |
| INVENTORY_VALUATION | /inventory-valuation | 354 | 1095 | no | no | |
| DISPATCH | /dispatch | 640 | 1104 | yes | yes | `customer_id` filter |
| COURIER_TRACKING | /courier-tracking | 661 | 1146 | yes | no | |
| VENDOR_DISPATCH_HISTORY | /vendor-dispatch-history | 682 | 1212 | yes | yes | |
| PACKING_LIST (document only) | `GET /packing/{id}/packing-list` → 202 | `API/modules/packing/router.py:474-494` | `packing/service.py:2029-2083` | – | – | PDF |

The router also serves `/catalogue` (L202), `/dashboard` (L216, `dashboard.view`) and `/jobs/{job_id}` (L232).

**Report-like endpoints outside this module**
- finance: `/finance/ageing`, `/finance/outstanding` (payables), `/finance/gst-summary`, `/finance/inventory-valuation` plus `/export`, `/finance/ledger/{customer|vendor}/{id}`
- `/mrp/shortage-report`
- `/quality/pending`, `/quality/reports/item/{id}`, `/quality/reports/vendor/{id}`
- `/job-work/pending-at-vendor`
- `/dispatch/tracking`, `/dispatch/delayed`
- `/procurement/pending-approvals`
- `/allocation/available-to-promise`
- `/sales/demand-summary`, `/sales/{id}/allocation-status`
- `/vendors/{id}/rating`

**Proposal report list against what exists**

| Proposal item | Status |
|---|---|
| sales order status, stock availability, reserved vs available, MRP shortage, purchase pending, production pending, job work pending, QC rejection, vendor performance, product profitability, fast moving, slow moving, dead stock, order delay, machine utilisation, production efficiency, inventory valuation | **All present** (table above) |
| Excel export | Only above 5000 rows |
| PDF export of reports | **None** |
| **DEALER PERFORMANCE** | **Absent** |

**Frontend: `WEB/features/reports/reports-page.tsx`.** This is a generic runner driven by the catalogue; it has no per-report tabs.
- The picker groups reports by `requires_period` (`reports-types.ts:92-97`).
- `FILTERS` (≈L125-140) offers warehouse, item, vendor, customer, status and min_days, using CustomerPicker, VendorPicker and ItemPicker.
- Grid columns come from the descriptor and are formatted by name suffix (L692-695):
  - `_percent` → percent
  - `_quantity` or `_qty` → quantity
  - `_amount`, `_value`, `_cost`, `_price`, `_total` → money
- A 202 response goes to `useReportJob` (`report-jobs.ts`) and `ReportJobPanel` (`job-panel.tsx`).
- The `ReportKey` union is hand-written (`reports-types.ts:23-43`). The route is derived as `reportPath(key)` = `/reports/<kebab>` (L80-82).

**Report tests:**
- `TESTS/unit/test_reports_catalogue.py`, whose `test_the_master_rules_reports_are_all_present` (L78-100) lists 19 keys and has no dealer report; cache-policy pins are at L185-207.
- `test_reports_export.py`, `test_reports_kpi.py`, `test_reports_ageing.py`, `test_report_job_dispatch.py`, `test_report_job_urls.py`
- `TESTS/integration/test_reports_summary.py`, `TESTS/api/test_report_jobs_for_documents.py`
- `WEB/features/reports/reports-page.test.tsx`

### 1.6 Dashboard

**Backend.** `GET /reports/dashboard` (`router.py:216-229`, `dashboard.view`) calls `service.dashboard` (`service.py:1297-1327`). That function uses a Redis cache (`CacheNamespace.DASHBOARD`, 60 s, key `dash:<company>:tiles`) and `ReportQueryRepository.dashboard(as_of)` (`repository.py:1247-1338`), which is one SELECT of scalar subqueries. The result is `DashboardRead` (`schemas.py:421-444`):
- `open_sales_orders`
- `overdue_sales_orders`
- `stock_value`, `stock_refreshed_at`
- `pending_purchase_orders`, `pending_purchase_value`
- `production_in_progress`
- `qc_pending`
- `dispatches_in_transit`
- `overdue_deliveries`
- `generated_at`

**Frontend: `WEB/features/dashboard/dashboard-page.tsx`**
- `DashboardPayload` is a hand-written interface (L60-73). It is **not** in the contract-mirror test.
- There are 8 tiles (L200-286). Each has `to` and `permissions`.
- An approvals panel reads `/audit/approvals` and an activity panel reads `/audit/logs`. The summary query is gated on `dashboard.view` (L155-164).
- **There is no receivables, collections or dealer tile.**
- The index route `/` is the dashboard (`WEB/app/router.tsx:214-221`), so every user, a portal user included, lands there.

### 1.7 Receivables: outstanding, ageing, statement; follow-up concepts

- **There is no customer invoice.** A receivable is created in two ways:
  - **Customer adjustments** (CHARGE or CREDIT): `post_customer_adjustment` (`API/modules/finance/service.py:3189`), route `POST /finance/customer-adjustments` (`router.py:674-703`), permission `payment.approve`, document type `CUSTOMER_ADJUSTMENT` "CADJ" (`API/common/codes.py:89`).
  - **Receipts**: `Payment(payment_type=RECEIPT, party_type=CUSTOMER)` (`API/modules/finance/schemas.py:502-545`). Approving one (`service.py:2598`) posts the journal and calls `apply_outstanding_delta(-amount)` (L2653). Cancelling one (`service.py:2729`) reverses it and adds the amount back (L2799).
  - Dispatch, sales and packing post **nothing** to the ledger. So `customers.outstanding_amount` grows only through manual CHARGE adjustments (`DOCS/assumptions.md` §32, L720-744).
- **Ageing:** `GET /finance/ageing?party_type=CUSTOMER&party_id&as_of&limit&currency` (`router.py:748-805`, `finance.view`) calls `ageing_report(session, tenant, *, party_type, as_of=None, party_id=None, limit=200, currency=None) -> AgeingReport` (`service.py:3477`).
  - That uses `LedgerEntryRepository.receivable_ageing_stmt` (`API/modules/finance/repository.py:674-730`) over `_open_receivable_items` (L549-672), which nets FIFO per (party, currency) and ages from the **posting date** (assumptions at L558-592).
  - Buckets come from `AGEING_BUCKET_BOUNDARIES` (`API/modules/finance/models.py` ≈L106-110) and `_bucket_columns` (`repository.py:738-760`).
  - For receivables, `not_due` is always 0, because every entry is dated on or before `as_of`.
  - Shapes: `AgeingRow`, `AgeingCurrencyBlock` and `AgeingReport` (`schemas.py:722-800`). See `DOCS/assumptions.md` §27-28.
- **Statement:** `GET /finance/ledger/customer/{customer_id}` (`router.py:227-249`, `ledger.view`) calls `party_statement(session, tenant, *, party_type, party_id, filters, params) -> (LedgerStatement, OffsetPagination)` (`service.py:1332`). The running balance comes from a window function.
- **Credit-controller worklist:** `GET /customers?over_credit_limit=true` (`API/modules/customers/repository.py:113-121`).
- **Frontend:**
  - `WEB/features/finance/finance-page.tsx` has tabs outstanding, ageing, ledger, valuation and gst (L43-63).
  - `outstanding-tab.tsx` states it is payables only (L1-19).
  - `ageing-tab.tsx` offers both the customer and vendor side.
  - `party-ledger-tab.tsx` and `party-ledger-panel.tsx`.
  - `payments-page.tsx` and `payment-dialogs.tsx` handle receipts.
- **Follow-up concepts: none.** The only traces are:
  - the `CustomerContact.is_primary` docstring, "payment chase goes to by default" (`customers/models.py:354-358`)
  - the unused `PAYMENT_DUE`
  - the handbook (`DOCS/handbook/content.json`), which lists "payments due" among notification events. The docs promise an alert that nothing raises.
  - There is no automatic credit hold and no overdue gate when an order is confirmed.
- **Celery beat today** (`API/workers/celery_app.py:71-111`): summary refresh every 900 s, stale reservations every 3600 s, dispatch delay alerts every 3600 s, daily purges, and the password-reset outbox every 60 s. **There is no receivable sweep.**

### 1.8 Documents

- **Model:** `Document(TenantEntity)` (`API/modules/documents/models.py:75-171`):
  - `file_name` (display only), `storage_key` (generated by the server, never returned), `mime_type` (verified), `size_bytes`, `checksum_sha256`
  - `category: DocumentCategory` = DOCUMENT, REPORT, ATTACHMENT, EXPORT, TEMP (L59-73)
  - `entity_type` and `entity_id`: both or neither (CHECK), bare UUID, **no FK**
  - `uploaded_by_id`, `is_public`, `expires_at`
  - No soft delete. Index `(company_id, entity_type, entity_id, created_at DESC)`.
  - The docstring is explicit: "`is_public` does NOT relax authentication."
- **Storage:** local disk under `settings.storage_path` (default `REPO_ROOT/storage`) with sub-trees documents, reports, attachments, exports and temp (`API/core/config.py:229-235,433-436`).
  - Keys look like `<dir>/<company>/<yyyy>/<mm>/<uuid7><ext>` (`build_storage_key`, `service.py:349`).
  - Path containment is enforced by `resolve_storage_path` (L370). **There is no S3.**
- **Limits:** `max_upload_size_mb = 25` (≤ 500), enforced while streaming (`service.py:673-718`).
  - Allowed types (`config.py:232-235`): jpeg, png, webp, pdf, csv, xlsx.
  - `KNOWN_TYPES` (`service.py:177-198`) checks magic bytes, and `BLOCKED_SIGNATURES` (L199) refuses executables.
  - Default retention: TEMP 24 h, EXPORT 7 days (L323-326). REPORT has none.
- **Routes** (`API/modules/documents/router.py`, prefix `/documents`):

  | Route | Permission | Line |
  |---|---|---|
  | `POST ""` (multipart: file, category, entity_type, entity_id, is_public, expires_at) | document.upload | 84 |
  | `GET ""` (filters search, category, entity_type, entity_id requiring entity_type, mime_type, uploaded_by_id, is_public, include_expired) | document.view | 120 |
  | `GET /{id}` | document.view | 137 |
  | `GET /{id}/download` (StreamingResponse, `Content-Disposition: attachment`, `nosniff`, `Cache-Control: private, no-store`) | document.view | 150 |
  | `DELETE /{id}?reason=` | document.delete | 178 |

  - Scoping is **company only** (`DocumentRepository._scoped`, `repository.py:56-85`). There is no per-entity authorisation: any `document.view` holder can list and download every file in the tenant.
- **How other modules attach files:**
  - QC photo: upload with `entity_type='qc_inspection'` (`WEB/features/quality/inspection-dialog.tsx:419-430`), then send the id as `photo_document_id` (bare UUID, `API/modules/quality/models.py:673-677`).
  - Company logo: `companies.logo_document_id` (bare UUID).
  - Dispatch invoice: `dispatches.invoice_document_id` FK (validated by `DocumentLookupRepository.exists_in_tenant`, `API/modules/dispatch/repository.py:284-291`; `service.py:942-945`).
  - Generated artefacts: the packing list (`entity_type="packing_order"`) and the valuation workbook.
  - Vendor invoices have **no** attachment flow in the UI.
- **Items have no image** of any kind: no column on items or variants, and no `entity_type="item"` usage.
- **Frontend:** `WEB/features/documents/documents-page.tsx` uploads at L211 and downloads at L688 through `api.download`, which fetches a blob into an object URL (`WEB/lib/api-client.ts:299-318`). `document-model.ts` is at L1-35.
- **Tests:** `TESTS/unit/test_documents_*.py` and `TESTS/integration/test_documents_service.py`.

### 1.9 Auth transport (relevant to a dealer web or mobile client)

- **Login:** `POST /auth/login` with `{email, password, company_code?, device_label?}` (`API/modules/auth/schemas.py:60-82`).
  - The body returns `BrowserLoginResponse`: the access token plus `user` = id, email, full_name, company_id, is_superuser, must_change_password, roles (L85-139).
  - The refresh token lives **only** in the `atcraft_refresh` cookie: HttpOnly, SameSite=Strict, path `/api/v1/auth`.
  - A readable `atcraft_csrf` cookie (an HMAC) must be echoed as `X-CSRF-Token` on refresh and logout (`API/modules/auth/cookies.py:21-98`; `router.py:131-153`).
- **Access JWT** claims are `sub`, `cid`, `typ`, `jti`, `sid` (`API/core/security.py:239-254`). It carries **no permissions or scope**. The principal is rebuilt from the database on every request (`deps.py:171-215`), and sessions can be revoked (L218-241).
- **Precedent for a portal route allow-list:** `get_current_principal` already restricts temporary-password principals to `password_change_route_allowed` (`deps.py:54-63,265-270`).
- **Bearer header only** (`deps.py:49,249-258`). An `<img src>` cannot authenticate, so images must be fetched as blobs.
- **No invitation flow.** `security.py:361` mentions "vendor invitations" in a comment but nothing implements it. Staff create portal users with `POST /users` and a temporary password (`must_change_password` defaults to True).

---

## 2. Missing vs proposal

### Gap 1: dealer site, portal and mobile API

1. **Dealer principal.** Missing: `users.customer_id`, a dealer role flag, `Principal`/`TenantContext.customer_id`, `MeResponse.customer_id`, and the frontend `isDealerUser`.
2. **Dealer roles and permissions.** No dealer preset and no portal permissions.
3. **Isolation.** There is no portal router and no route allow-list. Existing staff endpoints are not scoped by customer. Sales, dispatch, finance, customers and documents have no `_scoped` override. Without a gate, a dealer given `sales_order.view` would see every customer's orders.
4. **Catalog with dealer price.** No endpoint returns the sellable catalog priced for the caller's channel.
   - `GET /products/items` needs `item.view` and returns `sale_price` (`ItemSummary`, `API/modules/products/schemas.py:295-314`).
   - `ItemDetail` exposes `sale_price`, `dealer_price`, reorder levels, lead time and more (L323-370).
   - `/products/items/{id}/resolved-price` needs `price.view` and lets the caller choose `price_type` (`router.py:605-633`).
5. **Images.** None (see 1.8).
6. **Cart.** None.
7. **Wishlist.** None.
8. **Banners.** None; there is no content management.
9. **Offers or schemes.** None. Dated slabs exist, but `create_price` refuses overlapping rows for the same (item, variant, channel, slab) (`products/service.py:1800-1812`). So an offer **cannot** be layered on as a second price row.
10. **Checkout.** No endpoint creates an order for the caller's own customer. `SalesOrderCreate` accepts any `customer_id` and has no `source` field. Client-supplied discounts are unguarded.
11. **My orders and tracking.** No reads scoped to the customer. Dispatch filters `customer_id` and `sales_order_id` exist (`API/modules/dispatch/schemas.py:454-473`), as do `list_dispatches` (`service.py:1023`) and `list_tracking_events` (L1075).
12. **My invoices.** None. This depends on gap 3.
13. **My returns.** None. This depends on gap 4; the seams are listed in section 0.
14. **My outstanding.** The finance endpoints need `finance.view` / `ledger.view` and accept any `party_id`.
15. **Dealer notifications.** There are no types for "order placed, confirmed or dispatched" addressed to a customer's users.
16. **Mobile.** No body-based refresh and no push channel.
17. **Anonymous public browsing is impossible by design**, because `company_id` must come from a verified token (CLAUDE.md). Any public catalog needs an explicit tenant-resolution decision.

### Gap 10: dealer performance and payment follow-up

1. **No dealer-performance report** and no dealer score. The vendor rating is the precedent.
2. **No follow-up workflow:** no follow-up record, notes, promise-to-pay, assignee, next-action date, escalation, or "my collections" worklist.
3. **No due-date ageing.** Receivables age from the posting date and ignore `credit_days`.
4. **`PAYMENT_DUE` is never emitted,** and no beat sweep exists for overdue receivables or broken promises.
5. **No receivables or collections dashboard tile.**
6. **No overdue gate** at order confirmation (only block, inactive and limit).
7. **No collection view per salesperson.** `assigned_sales_user_id` is used only as a filter on the customer and dealer lists.

---

## 3. Patterns to copy

| New piece | Closest precedent | Files |
|---|---|---|
| Dealer principal binding | Vendor binding (section 1.3) | `API/modules/users/models.py:79,150-189`; `API/core/deps.py:66-141,171-215,332-357`; `API/modules/users/service.py:466-495,1314-1350` |
| Dealer route isolation | Temporary-password allow-list in the shared auth dependency; `forbid_vendor_users` | `API/core/deps.py:54-63,265-270,319-329` |
| Customer-scoped repositories (defence in depth) | `PurchaseOrderRepository._scoped`; second-line "not found, never forbidden" check | `API/modules/procurement/repository.py:898-911`; `procurement/service.py:2545-2553` |
| Master tables (banners, offers) | customers module: `TenantMasterEntity`, code unique per company, lifecycle derived from flags through `StateMachine`, `_to_detail` dict assembly, `clear_*` flags, reason in DELETE query | `API/modules/customers/*` |
| Documents with lines, status and numbering | sales: `OptimisticLockMixin`, `SALES_ORDER_LIFECYCLE`, `_transition` (L1825-1856), `next_document_number`, line_number unique, `MAX_LINES_PER_ORDER` | `API/modules/sales/*` |
| "One default/live per parent" flag | `uq_customer_addresses_one_default` plus `clear_default` **before** insert | `customers/models.py:315-322`; `customers/service.py:956-966` |
| NULL-safe unique on variant | `ATTRIBUTE_NULL_SENTINEL` COALESCE index | `API/modules/products/models.py:123-126` |
| Catalogue report | `ReportKey` + `ReportDefinition` + builder + row model + route + migration widening the CHECK + TS union | `reports/models.py`, `service.py`, `repository.py`, `schemas.py`, `router.py`; `MIG/20260905_0100_report_snapshots_packing_list_key.py` |
| Partner scorecard | Vendor rating: GET preview / POST recompute, stored `rating`, `rating_updated_at`, `rating_sample_size` | `API/modules/vendors/router.py:520-565`; `vendors/models.py:100-171` |
| Overdue sweep and alerts | `dispatch.service.scan_delays` (cross-tenant read, then a nested transaction per company, failure isolation); Celery adapter; beat entry; `notify_role(..., group_key=...)`; flood-control column | `API/modules/dispatch/service.py:1846-1902`; `API/workers/tasks/notifications.py:60-95`; `API/workers/celery_app.py:86-91`; `API/modules/notifications/service.py:364,519`; `dispatch/models.py:320-322` |
| PDF artefact (invoice PDF for "my invoices") | packing list: queue the job, render on the worker, store as Document, complete the snapshot, client polls `/reports/jobs/{id}` | `packing/service.py:2013-2083`; `packing/router.py:474-494`; `API/workers/tasks/reports.py:248-470` |
| File attached to an entity | QC photo (upload, then bare UUID); dispatch invoice (FK plus tenant existence check) | see section 1.8 |
| New permission | enum + `PERMISSION_GROUPS` + presets + `WEB/lib/permissions.ts` + grant migration + `_seed_roles` refresh | `API/core/permissions.py`; `MIG/20260906_0902_grant_item_attribute_permissions.py`; `API/core/seed.py:200-248` |
| List page | `dealers-page.tsx` (`useListQuery`, URL-state filters, `DataTable`, `PageHeader`, `EmptyState`, `StatusBadge`) | `WEB/features/customers/dealers-page.tsx` |
| Model file | `dealer-model.ts` (`SchemaOf<>` types, local keys) — but CLAUDE.md asks for keys in `queryKeys` | `WEB/lib/query-client.ts` (`queryKeys` L120-245) |
| Form dialog | zod + `zodResolver` + react-hook-form + `applyFieldErrors` / `reportFieldErrors` + `messages` / `success` / `toast` | `WEB/features/customers/customer-form-dialog.tsx` |
| Relation panels | own endpoints, reason-on-delete confirm | `WEB/features/customers/customer-relations.tsx` |
| Authenticated download | `api.download(path, filename)` | `WEB/lib/api-client.ts:299-318` |
| Background job UI | `useReportJob` + `ReportJobPanel` | `WEB/features/reports/report-jobs.ts`, `job-panel.tsx` |
| Frontend tests | `renderScreen`, `ok`, `page`, `fail`, `FetchRecorder` | `WEB/test/harness.tsx:70-227`; `WEB/features/customers/dealers-page.test.tsx` |
| Backend unit tests | pure rules without a DB | `TESTS/unit/test_customers_credit.py`, `test_reports_catalogue.py` |
| Backend integration tests | `session` fixture + `tests/seed_support` (ORM builders) | `TESTS/integration/test_customers_service.py`, `test_reports_summary.py` |
| Backend API tests | `api_client` + principal override | `TESTS/api/test_vendor_portal_scope_http.py:119-138` |
| Real-chain write persistence | No override of `get_db` | `TESTS/api/test_route_writes_persist.py` |

**Exact backend conventions**
- **Bases:** `TenantMasterEntity` for masters, `TenantEntity` for documents and carts, `ImmutableLedgerEntity` for append-only logs, `OptimisticLockMixin` for documents several people edit (`API/core/models.py:168-217`). The UUIDv7 primary key is automatic.
- **Types:** `Money`, `UnitPrice`, `Quantity`, `Percentage` and `GUID` from `src.core.types`, with `to_money` / `to_quantity` / `to_unit_price`.
- **Enum columns:** `String(16|24)` plus `CheckConstraint(f"col = ANY({sql_array_literal(E)})")` or `f"status IN ({sql_in_list(E)})"`.
- **Router:**
  - `APIRouter(prefix=..., tags=[...], responses=COMMON_ERROR_RESPONSES)`
  - `dependencies=[require_permission(Permission.X)]`
  - `DbSession`, `Tenant`, `CurrentUser`
  - `PageDep` / `SortDep`
  - a filter-builder dependency (example `customer_filters`, `customers/router.py:74-101`)
  - literal paths before `/{id}`
  - return `ApiResponse.ok(data, "msg")` or `PaginatedResponse[T](data=, pagination=)`
- **Service:**
  - `async with transaction(session):`
  - `repo.get_or_404`
  - `record_audit(session, company_id=, action=AuditAction.X, entity_type=, entity_id=, entity_code=, summary=, user_id=tenant.user_id, old_values=, new_values=snapshot(obj, FIELDS), context=)` (`API/modules/audit/service.py:104-155`)
  - `record_status_change(...)` (L158-190) after `SM.assert_transition`
  - `next_document_number(session, tenant.company_id, DocTypes.X, on=date)` with a new `DocType` added in `API/common/codes.py:65-91`
- **Repository:** `BaseRepository[Model]` with `sortable_fields`, `searchable_fields`, an `entity_label`, `_scoped` overrides, and ordering ending in `Model.id` as tiebreak.
- **Errors:**
  - `NotFoundError` for foreign rows (404, never 403)
  - `BusinessRuleError(msg, details=...)`
  - `PermissionDeniedError`
  - `AuthorizationError(code=ErrorCode.SCOPE_DENIED)`
  - `ApprovalRequiredError`

---

## 4. Integration points a new feature must call or update

- **customers**
  - `get_customer_for_order`, `assert_customer_can_order`, `evaluate_credit`, `credit_status_of`
  - `CustomerService.get_customer`, `.list_addresses`, `.block`
  - `DEALER_CHANNELS`, `requires_dealer_permission` (`customers/service.py:529`)
  - `apply_outstanding_delta` is **finance-only**
  - `CustomerRepository` for tenant-scoped existence and kind checks when binding a user
- **sales**
  - `create_order`, `confirm_order`, `cancel_order`, `list_orders`, `get_order_detail`, `allocation_status`, `order_detail`
  - pure functions `price_line`, `compute_line_amounts`, `sum_order_totals`, `resolve_tax_treatment`
  - repositories `PriceListRepository.candidates_for_items`, `AvailabilityRepository.available_by_item`, `OrderPartyRepository.{addresses_of, default_shipping_address, seller_state_code, warehouse_in_tenant, customer_price_type}`, `ItemLookupRepository.load`
  - add `SalesOrderRepository._scoped` scoping if dealers ever reach the staff routes
- **products**
  - `select_price_row` (`service.py:467`), `ITEM_MASTER_FALLBACK_FIELD` (L1779)
  - `resolve_price(session, tenant, *, item_id, price_type, quantity, variant_id, on_date) -> ResolvedPrice` (L1944)
  - the item search statement with `can_sell=True, is_active=True` (sellable partial index `ix_items_sellable`, `models.py:497-503`)
  - the categories repository
- **allocation / inventory:** `AllocationService.availability` (`API/modules/allocation/router.py:109-128`). Never cache it.
- **warehouses:** a default-warehouse lookup (`is_default` with its partial unique index, `warehouses/models.py:120-122,160-168`). No getter exists yet.
- **dispatch:** `list_dispatches(filters=DispatchFilters(customer_id=..., sales_order_id=...))`, `get_dispatch_detail`, `list_tracking_events` (keyset). Ownership must be checked before reading tracking.
- **finance:** `party_statement(party_type=CUSTOMER, party_id=tenant.customer_id)`, `ageing_report(party_type=CUSTOMER, party_id=...)`, `LedgerEntryRepository._open_receivable_items`, the payments repository (receipts per customer), `post_customer_adjustment` and `ensure_system_accounts`.
- **documents:** `DocumentService.upload` / `open_download`, `stream_file`, `DOWNLOAD_HEADERS` (`service.py:132`), `content_disposition`, `KNOWN_TYPES`, `build_storage_key`, `resolve_storage_path`, `default_expiry`, `DocumentLookupRepository`.
- **reports:** `REPORT_CATALOGUE`, `ReportQueryRepository`, `DashboardRead` plus `dashboard()`, `new_job_snapshot`, `dispatch_job`, `complete_snapshot`, `fail_snapshot`, `job_status_url`.
- **notifications:** `notify(...)` and `notify_role(...)`. Add members to `NotificationType` (closed vocabulary) and mirror them in `WEB/features/notifications/notification-types.ts`.
- **audit:** `AuditAction` vocabulary (`API/modules/audit/models.py:24-64`).
- **users and auth:** `UserCreate` / `UserUpdate` / `UserFilters` / `UserSummary`; `MeResponse`; `_load_principal`; `get_tenant_context`; `ROLE_PRESETS`; `_seed_roles`.
- **workers:** `API/workers/celery_app.py` beat schedule and task routes (`notifications.*` and `reports.*` queues).
- **api router:** add the module to `MODULE_ORDER` (`API/api/router.py:23-49`). Models are auto-discovered (`API/core/registry.py:29-59`); routers are not.

---

## 5. Traps and constraints

1. **Opt-in scoping leaks** (section 1.3). Do not give dealer roles any staff permission. Put the gate in the shared dependency as an allow-list, not per route or per repository.
2. **`api_client` bypasses `get_current_principal`.** The fixture overrides it (`TESTS/conftest.py:335-362`), so a gate placed there is invisible to `tests/api`. Test the path predicate as a unit test, and test the gate through the real dependency chain (as `test_route_writes_persist.py` does).
3. **Unguarded discounts** on `SalesOrderLineIn` (section 1.4). The portal must never forward client discounts or prices; offers are computed on the server. If below-cost approval should apply to offers, that changes assumption §8.
4. **Hard delete of drafts** (`delete_draft_order`, `sales/service.py:1518`) assumes "no number was reported to the customer". A portal shows that number. Block deletion for portal-sourced drafts, or always confirm or cancel instead.
5. **A DISTRIBUTOR channel with no price row raises** (`ITEM_MASTER_FALLBACK_FIELD[DISTRIBUTOR]=None`). The catalog must show "price on request" rather than fail the whole page.
6. **Price rows cannot overlap** (`products/service.py:1800-1812`). Offers need their own table.
7. **"A foreign key to a UUID is not a tenant or kind check"** (CLAUDE.md). `users.customer_id`, cart `item_id` and offer `category_id` must each be checked through tenant-scoped repositories, including `can_sell`, `is_active` and `customer_type`.
8. **Partial unique indexes are checked per statement.** Demote or close the old row before inserting the new one (one live follow-up, one active cart, a default image).
9. **A rolled-back savepoint expires the identity map.** If `confirm_order` fails after `create_order` during checkout, keep `order.id` in a variable first. Run confirm as its own outermost `transaction()` so the draft survives (`API/core/database.py:141-181`). Never call `session.rollback()`.
10. **`if xs: stmt.where(col.in_(xs))` without an else inverts the filter.** An empty set of the dealer's customers must produce an empty result. Assumption §28 is the precedent.
11. **`clause or default` raises.** Use `x if x is None else y` (`products/repository.py:552-560`).
12. **`model_validate(orm_row)` reads `lazy="raise"` relationships.** Assemble detail objects from dicts (`customers/service.py:1350`) or call `set_committed_value`.
13. **`created_at` ties within a transaction.** Order logs and notes by `(created_at, id)`. Notes are append-heavy, so page them with keyset cursors.
14. **Dependency direction** (`DOCS/architecture.md:152-170`): customers is a master and cannot import finance or sales. An overdue gate inside `evaluate_credit` must receive the overdue figure as an **input**, or read a cached column maintained by a downstream sweep. The portal and collections modules must sit downstream.
15. **Multi-currency** (§27, §38): never sum across currencies. Group dealer KPIs and follow-ups by (customer, currency). `customers.outstanding_amount` is in company currency only.
16. **Never cache availability** (§34, CLAUDE.md). A catalog stock badge must read live and stay out of Redis. Banner and offer caches must respect `settings.cache_enabled`, including when invalidating.
17. **Cached payload shape.** `/reports/dashboard` caches `DashboardRead` for 60 s and reports cache rows for 300 s. A new required field fails validation against old cached JSON. Give new fields defaults or bump the cache key.
18. **New `ReportKey` needs a migration.** The DB CHECK `ck_report_snapshots_report_key_is_known` lists keys literally, and autogenerate will not catch it. Copy `MIG/20260905_0100_report_snapshots_packing_list_key.py`, then update `test_reports_catalogue.py` and `WEB/features/reports/reports-types.ts:23-43`. The route must be the kebab case of the key.
19. **Permissions must reach roles.** `_seed_roles` refreshes only `is_system` roles. Existing deployments need a grant migration (precedent `MIG/20260906_0902_...`).
    - `test_role_presets_reach_roles.py` asserts every resource is in `PERMISSION_GROUPS` and **pins the system-role list** (L63-84). Adding DEALER_* system roles needs that test updated deliberately.
    - `VIEW_ONLY` auto-includes every `*.view`, so a `portal.*.view` would leak into AUDITOR. Prefer non-`.view` action names.
    - ADMIN and SUPER_ADMIN get every new permission automatically, so portal routes must also require a customer binding.
20. **Meta-tests that will fail on new code:**
    - `TESTS/unit/test_frontend_permission_catalogue.py`: `WEB/lib/permissions.ts` must mirror the enum.
    - `test_frontend_permission_gates.py`: each mutation's enforced right must be named in the calling feature folder or route.
    - `test_status_badge_catalogue.py`: every `*Status` / `*State` StrEnum member needs a `STATUS_VARIANTS` key in `WEB/components/shared.tsx`.
    - `test_state_machine_entity_names.py`, `test_entity_names.py`, `test_repository_entity_label.py`: human-readable entity names.
    - `test_frontend_contract_types.py` (MIRRORS at L68-77, including `MeResponse`): register hand-written types or use `SchemaOf`.
    - `test_frontend_constants.py`: no literal page sizes or debounce values.
    - `test_frontend_field_errors.py`
    - `test_query_filter_binding.py`: use `bind_query_filters` for cross-field filters.
    - `test_api_path.py`: URLs inside bodies go through `api_path`.
    - `test_schema_identifier_length.py`: CHECK names ≤ 63 bytes.
    - `test_e2e_sweep_routes.py`: every static route must appear in `E2E/scripts/browser-sweep.mjs` `STATIC_ROUTES`, `a11y-sweep.mjs` `ROUTES` and `responsive-sweep.mjs` `STATIC_ROUTES`, and `permission-sweep.mjs` `ROUTES` must name the route's gate.
    - `test_postman_and_contract_targets.py`: `DOCS/api.md:28` "494 requests in 27 folders" must match the regenerated collection.
21. **Contract regeneration.** `make contract` regenerates `openapi.json`, `WEB/types/openapi.ts` and `postman_collection.json`, and the pre-push hook checks them.
22. **A sortable column the server does not allow empties the table.** Never mark a computed price column sortable.
23. **A screen gating on a right its endpoint does not enforce** (CLAUDE.md). Portal screens must gate on the exact `portal.*` right of each route.
24. **A hand-written frontend type is compared with nothing.** `DashboardPayload` and `CustomerRow` are hand-written; new fields must be mirrored, or the types derived from `SchemaOf`.
25. **Images and auth.** The bearer header is the only credential, and downloads are `attachment` with `no-store`. A catalog grid must fetch blobs, and the number of images per page must be bounded. Serving images publicly would be a security decision for `DOCS/security.md`.
26. **Mobile and other origins** (§14): a different origin or a native app needs a cookie jar with `X-CSRF-Token`, or a deliberate redesign. SameSite and CORS are coupled.
27. **Redirect needed.** The index route is the staff dashboard, which needs `dashboard.view`. Dealer users must be redirected to `/portal`.
28. **Rate limits are per-IP flood guards** (CLAUDE.md, `DOCS/security.md` §8). Dealers logging in from many sites is fine, but do not add per-IP fairness limits.
29. **Remaining CLAUDE.md non-negotiables:** paginate every list; no `await` inside loops; `lazy="raise"`; Decimal only; an audit row in the same transaction; status changes only through `StateMachine`; Alembic only; Celery for PDFs and exports.
30. **Docs drift already present:**
    - `DOCS/security.md:183` overstates vendor scoping.
    - The handbook promises "payments due" alerts that nothing raises.
    - The handbook lists "sales invoices" under Accounts, but none exist.

---

## 6. Files a change here will touch

**Backend: binding, auth and roles**
- `C:\Project\Other\ATCRAFT\apps\api\src\core\deps.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\core\permissions.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\core\seed.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\users\{models,schemas,service,repository,router}.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\auth\{schemas,service}.py`

**Backend: portal**
- new `C:\Project\Other\ATCRAFT\apps\api\src\modules\portal\{__init__,models,schemas,repository,service,router}.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\api\router.py` (`MODULE_ORDER`)

**Backend: sales, products and documents**
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\sales\{models,schemas,service,repository}.py` (`source`, optional `offer_id`, `_scoped`, delete guard)
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\products\{models,schemas,service}.py` (`image_document_id`)
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\documents\service.py` (an image-serving helper, if needed)
- `C:\Project\Other\ATCRAFT\apps\api\src\common\codes.py` (new DocTypes)

**Backend: gap 10**
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\reports\{models,schemas,repository,service,router}.py`
- new `C:\Project\Other\ATCRAFT\apps\api\src\modules\collections\*` (or a finance extension)
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\notifications\models.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\workers\celery_app.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\workers\tasks\notifications.py` (or a new `collections.py` task)
- `C:\Project\Other\ATCRAFT\apps\api\src\core\demo_data\{masters,operations,finance}.py` (demo dealer users, carts, follow-ups)

**Migrations** (in `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\`)
- `users.customer_id` plus CHECK; `roles.is_dealer_role`
- portal tables; `sales_orders.source`; `items.image_document_id`
- report-key CHECK widening; follow-up tables
- permission grant to system roles and dealer role rows for existing companies

**Backend tests** (in `C:\Project\Other\ATCRAFT\apps\api\tests\`)
- new `unit\test_portal_*.py`, `integration\test_portal_service.py`, `api\test_dealer_portal_scope_http.py`
- update `unit\test_role_presets_reach_roles.py`, `unit\test_reports_catalogue.py`, `unit\test_frontend_contract_types.py` (if mirrors are added)
- new `unit\test_collections_*.py`, `integration\test_collections_service.py`
- `seed_support.py` builders (customers, dealer users)

**Frontend** (in `C:\Project\Other\ATCRAFT\apps\web\src\`)
- new `features\portal\*` (pages, models, tests)
- `app\route-loaders.ts`, `app\router.tsx`, `app\navigation.ts`
- `lib\permissions.ts`, `lib\query-client.ts`
- `features\auth\auth-context.tsx`
- `features\admin\user-dialogs.tsx`
- `features\customers\customer-picker.tsx` (channel prop)
- `features\reports\reports-types.ts`
- `features\dashboard\dashboard-page.tsx`
- `features\finance\finance-page.tsx` (+ a collections tab)
- `features\notifications\notification-types.ts`
- `components\shared.tsx` (`STATUS_VARIANTS`)
- `types\openapi.ts` (generated)

**Generated contract**
- `C:\Project\Other\ATCRAFT\openapi.json`
- `C:\Project\Other\ATCRAFT\postman_collection.json`

**E2E**
- `C:\Project\Other\ATCRAFT\tests\e2e\scripts\{browser-sweep,a11y-sweep,responsive-sweep,permission-sweep,theme-sweep,capture-screens}.mjs`
- a dealer-isolation counterpart to `tenant-isolation.mjs`

**Docs**
- `C:\Project\Other\ATCRAFT\docs\database.md` (module map §2, state machines §7, integration §8)
- `C:\Project\Other\ATCRAFT\docs\assumptions.md` (new sections)
- `C:\Project\Other\ATCRAFT\docs\security.md` (§4 portal isolation, image serving)
- `C:\Project\Other\ATCRAFT\docs\api.md` (endpoint map, request count)
- `C:\Project\Other\ATCRAFT\docs\architecture.md` (dependency diagram)
- `C:\Project\Other\ATCRAFT\docs\handbook\content.json`

---

## 7. Concrete design: a dealer portal inside this ERP

Keep it inside the modular monolith, the same SPA and the same API. Do not add a new stack (CLAUDE.md "Things not to do").

### 7.1 Principal and isolation

1. **Binding columns.**
   - `users.customer_id` GUID NULL, FK customers ON DELETE CASCADE (mirrors `vendor_id`).
   - Index `ix_users_customer (company_id, customer_id)`.
   - CHECK `vendor_id IS NULL OR customer_id IS NULL`, named short.
   - `roles.is_dealer_role` bool, default false.
2. **Principal and tenant context.**
   - `Principal.customer_id` and `is_dealer_user`; `_load_principal` copies them.
   - `TenantContext.customer_id`; `get_tenant_context`.
   - `MeResponse.customer_id`, and on the frontend `CurrentUser.customer_id` and `isDealerUser`.
3. **The gate.** In `get_current_principal`, next to the temporary-password gate, add:
   ```python
   if principal.customer_id is not None and not dealer_route_allowed(request.url.path):
       raise AuthorizationError("This endpoint is not available to dealer accounts.", code=ErrorCode.SCOPE_DENIED)
   ```
   `dealer_route_allowed` permits `{prefix}/portal/**`, `{prefix}/notifications**`, `{prefix}/auth/me`, `change-password`, `logout`, `logout-all`, `sessions**` and `mfa/**`. The pattern is `password_change_route_allowed` (`deps.py:54-63`).
4. **Portal-only dependency.** `require_dealer_user` raises 403 SCOPE_DENIED if `customer_id is None`. Add it to every `/portal` route, because ADMIN holds every permission.
5. **Users service.**
   - Generalise `assert_vendor_role_consistency` into `assert_portal_role_consistency(vendor_id, customer_id, roles)`.
   - `_assert_references` checks the customer through the tenant-scoped repository, that it is active, and (assumption) that `customer_type ∈ DEALER_CHANNELS`.
   - Add `customer_id` and `clear_customer` to `UserCreate` / `UserUpdate`; `customer_id` and `is_dealer_user` to `UserFilters` and `UserSummary`.
   - `UsersRepository._scoped` adds `User.customer_id == tenant.customer_id`.
6. **Optional defence in depth:** `_scoped` customer filters on `SalesOrderRepository`, `DispatchRepository` and `CustomerRepository`, mirroring `PurchaseOrderRepository._scoped`.

### 7.2 Schema (new module `portal`, downstream)

- **`portal_carts`** (`TenantEntity + OptimisticLockMixin`): `customer_id` FK RESTRICT, `user_id` FK users CASCADE. Unique `(company_id, user_id)`; one cart per dealer user is an assumption.
- **`portal_cart_lines`** (`TenantEntity`): `cart_id` FK CASCADE, `item_id` FK RESTRICT, `item_variant_id` FK RESTRICT NULL, `quantity` Quantity > 0.
  - Unique on `(cart_id, item_id, COALESCE(item_variant_id, ATTRIBUTE_NULL_SENTINEL))`.
  - At most `MAX_LINES_PER_ORDER` lines.
  - Stores no price; it is re-priced on every read.
- **`portal_wishlist_items`** (`TenantEntity`): `customer_id`, `user_id`, `item_id`, `item_variant_id`, with the same unique pattern per user.
- **`portal_banners`** (`TenantMasterEntity`):
  - `code` unique per company, `title`, `subtitle`, `image_document_id` (bare UUID)
  - `link_kind` {NONE, ITEM, CATEGORY, OFFER} and `link_id`
  - `audience` {ALL, DEALER, DISTRIBUTOR}, `sort_order`, `valid_from`/`valid_to` (CHECK ordered), `is_active`
  - Lifecycle copied from `CUSTOMER_LIFECYCLE`.
- **`portal_offers`** (`TenantMasterEntity`):
  - `code`, `title`, `description`
  - `scope_kind` {ALL, CATEGORY, ITEM} with `item_id`/`category_id` matching it (CHECK)
  - `price_type` (PriceType audience), `min_quantity`
  - `discount_percentage` Percentage in (0, 100] (assumption: percentage only, no stacking, the best offer wins)
  - `valid_from`/`valid_to`, `is_active`
  - Applied at checkout as the line's `discount_percentage`.
  - Optional `sales_order_lines.offer_id` (bare UUID) so the order explains the discount.
- **`sales_orders.source`**: `SalesOrderSource` {ERP, DEALER_PORTAL}, String(16), NOT NULL, `server_default 'ERP'`, CHECK. Set by the server only. Add it to `SalesOrderSummary` and to the filters.
- **`items.image_document_id`**: bare UUID, like `companies.logo_document_id`, to hold the primary image. A gallery could use `documents(entity_type='item')`.
- **Return requests** follow gap 4. Seams: `TransactionType.CUSTOMER_RETURN`, the QC `"customer_return"` stage, `items.can_return`.

### 7.3 Permissions and roles

- New resource `portal`, with a `PERMISSION_GROUPS` entry "Dealer Portal": `("portal",)`:
  - `portal.browse`
  - `portal.order`
  - `portal.account`
  - `portal.return` (after gap 4)
  - `portal.content_manage` (staff: banners, offers, item images)
- Presets, all with `is_system: True` and `is_dealer_role: True`; update the pinned test:

  | Role | Permissions |
  |---|---|
  | DEALER_OWNER | all `portal.*` except content_manage, plus `notification.view` |
  | DEALER_BUYER | browse, order, `notification.view` |
  | DEALER_ACCOUNTS | browse, account, `notification.view` |

- Add a grant and role-insert migration for existing companies, and mirror everything in `WEB/lib/permissions.ts`.

### 7.4 Endpoints

Prefix `/portal`, tag "Dealer Portal". Every route has `require_permission(...)` and `require_dealer_user`, and uses `tenant.customer_id`; none accepts a `customer_id`.

| Route | Permission | Reuses |
|---|---|---|
| `GET /portal/me` (account, credit position, ship-to addresses, primary contact) | portal.browse | `CustomerService.get_customer`, `evaluate_credit` |
| `GET /portal/catalog/categories` | portal.browse | products categories repository |
| `GET /portal/catalog/items?search&category_id&page&limit&sort_by=code\|name` | portal.browse | item search with `can_sell` and `is_active`; batched `PriceListRepository.candidates_for_items` + `select_price_row` + fallback (DISTRIBUTOR → "price on request"); `AvailabilityRepository.available_by_item` (live, shown as a band — assumption); offers, wishlist and cart membership, **one query each per page** |
| `GET /portal/catalog/items/{id}` (variants, today's slabs for the dealer's channel, offers) | portal.browse | same |
| `GET /portal/catalog/items/{id}/price?quantity&variant_id` | portal.browse | `products.service.resolve_price(price_type=customer.price_list_type)` plus the offer |
| `GET /portal/catalog/items/{id}/image`, `GET /portal/banners/{id}/image` | portal.browse | document lookup, then MIME in jpeg/png/webp, then `stream_file` + `DOWNLOAD_HEADERS` |
| `GET /portal/banners`, `GET /portal/offers` | portal.browse | new repositories |
| `GET /portal/cart` (lines, priced preview, tax split, credit preview, warnings) | portal.order | `price_line`, `compute_line_amounts`, `sum_order_totals`, `resolve_tax_treatment`, `evaluate_credit`; never reserves |
| `POST /portal/cart/lines`, `PATCH /portal/cart/lines/{id}`, `DELETE /portal/cart/lines/{id}`, `DELETE /portal/cart` | portal.order | audit is optional for a cart (assumption) |
| `GET/POST /portal/wishlist`, `DELETE /portal/wishlist/{id}` | portal.browse | |
| `POST /portal/checkout {cart_version, required_date, shipping_address_id?, billing_address_id?, customer_po_number?, notes?}` → 201 | portal.order | see the flow below |
| `GET /portal/orders?status&open_only`, `GET /portal/orders/{id}` | portal.order | `list_orders` with a forced `customer_id`; `get_order_detail` + `allocation_status` behind an ownership check (404 if foreign) |
| `POST /portal/orders/{id}/cancel {reason}` (assumption: DRAFT only) | portal.order | `cancel_order` |
| `GET /portal/orders/{id}/dispatches`, `GET /portal/dispatches/{id}/tracking` | portal.order | `list_dispatches(customer_id, sales_order_id)`, `list_tracking_events`, with a dealer-safe projection (drop driver mobile and freight cost: assumption) |
| `GET /portal/statement?date_from&date_to`, `GET /portal/outstanding` | portal.account | `party_statement`, `ageing_report(party_type=CUSTOMER, party_id=tenant.customer_id)` |
| `GET /portal/invoices`, `GET /portal/invoices/{id}/pdf` | portal.account | after gap 3; the PDF follows the packing-list job pattern |
| `GET/POST /portal/returns` | portal.return | after gap 4 |
| Staff `/portal-admin/banners`, `/portal-admin/offers` (CRUD, activate/deactivate/retire with reasons), `PUT /products/items/{id}/image` | portal.content_manage / item.update | customers-style master pattern |

**Checkout flow:**
1. **First outer transaction:**
   - Lock the cart and check `cart_version`.
   - Re-validate the lines.
   - Resolve the default warehouse (assumption) and the addresses.
   - Build a `SalesOrderCreate` with `priority=DEFAULT_PRIORITY`, no `unit_price`, and the offer discount computed on the server.
   - Call `sales.service.create_order` and set `source=DEALER_PORTAL`.
   - Clear the cart, write `record_audit` (CREATE, `entity_type "portal_checkout"`), and notify `assigned_sales_user_id`.
   - Keep `order_id` in a variable.
2. **Second outermost transaction (optional):** `confirm_order(reason="Placed via dealer portal")`. If credit fails, the order stays DRAFT and the response says so.
   - Whether to auto-confirm or leave orders for sales review is an assumption to record.

### 7.5 Screens

New `WEB/features/portal/`; register each in `route-loaders.ts`, `router.tsx` and `navigation.ts`.

- `/portal`: home with banners, offers, credit tiles (limit, outstanding, headroom, overdue) and recent orders. Redirect `/` here when `isDealerUser`.
- `/portal/catalog` and `/portal/catalog/:itemId`
- `/portal/cart`, with a checkout dialog for address, required date and PO reference
- `/portal/wishlist`
- `/portal/orders` and `/portal/orders/:orderId` (status timeline, allocation, dispatch tracking)
- `/portal/account` (statement, ageing, credit), `/portal/invoices`, `/portal/returns` (the last two later)
- Staff: `/portal-admin` (Banners and Offers tabs), an item image upload on the item detail, and in `user-dialogs.tsx` a dealer binding (a `CustomerPicker` with a channels prop, or `/customers/dealers`) with roles filtered by `is_dealer_role`.
- Navigation: a "Dealer portal" section. Hide it from staff, either with an audience flag or by checking `isDealerUser`.

### 7.6 Assumptions to record in `DOCS/assumptions.md`

- Which customer types may hold portal users.
- Whether a cart belongs to a user or to the customer.
- Auto-confirm, credit-failure behaviour and draft deletion for portal orders.
- The default warehouse and the default `required_date`.
- Offer stacking and below-cost handling.
- How availability is displayed.
- What to show when no channel price exists.
- What a dealer may cancel.
- Image visibility.
- Mobile refresh.
- Whether to allow anonymous browsing; it is not possible without a tenant-resolution decision.

---

## 8. Concrete design: gap 10

### A. `DEALER_PERFORMANCE` catalogue report

1. **Key and migration.** Add `ReportKey.DEALER_PERFORMANCE` and widen the CHECK with a migration copied from `MIG/20260905_0100_...`.
2. **Row model `DealerPerformanceRow`.** Field suffixes drive the frontend formatting.
   - Identity and terms: `customer_id`, `customer_code`, `customer_name`, `customer_type`, `price_list_type`, `currency`, `credit_limit`, `outstanding_amount`
   - Volumes: `order_count`, `order_value_amount`, `net_sales_amount` (pre-tax, net of discount, the same rule as product profitability), `ordered_quantity`, `dispatched_quantity`, `late_order_count`, `receipts_amount`
   - Derived: `fulfilment_percent`, `average_order_value_amount`, `credit_utilisation_percent` (None when the limit is unlimited), `collection_ratio_percent`
3. **Builder `ReportQueryRepository.dealer_performance(period, filters, *, as_of)`.**
   - Grouped subqueries: orders and lines, excluding `NON_REVENUE_SALES_STATUSES`.
   - Late orders: `OPEN_DEMAND_STATUSES` with `required_date < as_of`.
   - Receipts: `payments` where `payment_type=RECEIPT`, `party_type=CUSTOMER`, status APPROVED or allocated, and `payment_date` in the period.
   - Join `customers` where `customer_type IN DEALER_CHANNELS`, grouped by (customer, currency) per §27.
   - Filters: `customer_id`, `search`. Order by `order_value_amount DESC, customer_code, customer_id`.
4. **Flags.** `requires_period=True`. Set `cacheable=False` if live `outstanding_amount` stays in the row.
5. **Route.** `GET /reports/dealer-performance` with `report.view`.
   - Consider adding `ReportDefinition.permission`, for example `dealer.view`, and filtering `descriptors()` by caller. That would also fix the misleading "filtered" claim.
6. **Frontend.** Add the key to the `reports-types.ts` union only; the runner is generic.
7. **Optional later:** a stored dealer score on `customers`, using the vendor-rating pattern.

### B. Payment follow-up (new downstream module `collections`)

Place it after `finance` in `MODULE_ORDER`.

- **`payment_follow_ups`** (`TenantEntity + OptimisticLockMixin`):
  - Columns:
    - `code` (`DocTypes.PAYMENT_FOLLOW_UP`), `customer_id` FK RESTRICT, `currency`
    - `status` (`FollowUpStatus`: OPEN, PROMISED, ESCALATED, RESOLVED, CANCELLED)
    - `assigned_to_id` (defaults to `customers.assigned_sales_user_id`)
    - `opened_outstanding_amount`, `next_action_date`
    - `promised_amount`/`promised_date` (pair CHECK)
    - `last_reminded_at` (flood control), `resolution_note`
  - Partial unique: one live row per (company, customer, currency) WHERE status is OPEN, PROMISED or ESCALATED.
  - `FOLLOW_UP_LIFECYCLE` is a `StateMachine` with `entity="Payment follow-up"`.
- **`payment_follow_up_notes`** (`ImmutableLedgerEntity`): `follow_up_id`, `channel` (CALL, EMAIL, VISIT, MESSAGE, SYSTEM), `note`, `next_action_date`. Page with keyset, ordered by `(created_at, id)`.
- **Routes** (`/collections`, permissions `collection.view` / `collection.manage` in the "Finance" group; add them to the ACCOUNTS preset and optionally SALES_EXECUTIVE):
  - `GET /worklist`: one SQL joining receivable open items to follow-ups; filters assigned_to=me, minimum bucket, dealer channels.
  - `POST /follow-ups`, `GET /follow-ups`, `GET/PATCH /follow-ups/{id}`
  - `POST /follow-ups/{id}/notes`, `GET /follow-ups/{id}/notes` (keyset)
  - `POST /follow-ups/{id}/promise`, `/escalate` (optionally calls `CustomerService.block`), `/resolve`, `/cancel`
- **Beat task `collections.scan_overdue_receivables`** (daily), modelled on `scan_delays`:
  - Overdue means `entry_date + credit_days < as_of`. This is a new statement beside `receivable_ageing_stmt`; do not silently change the existing ageing semantics, and record it as an assumption.
  - Open or refresh the follow-up.
  - Detect broken promises (PROMISED → OPEN, audited).
  - Notify `PAYMENT_DUE` with `group_key=f"payment-due:{customer_id}:{currency}"` to the assigned salesperson or the ACCOUNTS role.
  - Run each company in its own nested transaction.
  - Optionally maintain a cached `customers.overdue_amount`, so a future gate in `evaluate_credit` can take it as an input without breaking the dependency direction.
- **Dashboard.** Add `DashboardRead` fields **with defaults**: `receivables_outstanding`, `customers_over_limit`, `follow_ups_due`. Add matching tiles and payload fields in `dashboard-page.tsx`.
- **Frontend.**
  - A "Collections" tab on `finance-page.tsx`: worklist `DataTable`, follow-up dialog, notes timeline.
  - A collections panel on the customer screen.
  - Notification deep links through `action_url`.
  - Badge colours for every `FollowUpStatus`.

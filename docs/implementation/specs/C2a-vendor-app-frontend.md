# WP C2a: Vendor app (mobile-first frontend for vendor users)

Lane C (portals), round 3, step 1. Placeholder prefix: `§C2-n`.

**Proposal:** Add-on §05 "Vendor Mobile App Features" (vendor dashboard, PO detail screen, vendor actions) and §02 vendor roles and category mapping.

**What this delivers:** a mobile-first app inside the same SPA at `/vendor/*`. WP C2b makes it installable as a PWA with push. The backend endpoints all exist from WP C1a/C1b on `main`: read `apps/api/src/modules/vendor_portal/router.py` and the generated types.

**Background:** `reports/procurement-vendor-portal.md` "Conclusion (b) / Frontend screens", and `reports/anatomy.md` D1–D6.

## Decisions (binding)

### 1. Shell and routing
- When `useAuth().isVendorUser`, the router mounts **`VendorLayout`** for `/vendor/*`.
- Every other path (including `/`) redirects to `/vendor`, except these, which stay reachable:
  - `/notifications`
  - `/profile`
  - `/change-password`
  - the auth pages
- Internal users never see `/vendor/*`. Guard the route group on `isVendorUser` and redirect staff to `/`.
- **Phones:** a top app bar plus a **bottom tab bar** with Home, Orders, Shipments, Invoices, More. "More" opens Receipts & QC, Payments, Notifications, Profile and Sign out.
- **Tablet and desktop:** a compact sidebar with the same items.
- The staff `AppLayout` and its sidebar must never render for vendor users.
- Pages are lazy (`route-loaders.ts`) and respect the bundle budget.

### 2. Pages
Use `useListQuery` + a responsive list or cards for lists, `SchemaOf` types, and keys under `queryKeys.vendorPortal.*`.

**`/vendor`: Dashboard.**
- Greeting, vendor name, and "my categories".
- Seven count tiles from `/vendor-portal/dashboard`: To accept, Accepted, Ready for dispatch, In transit, Delivered, Payment pending, QC rejected. Each links to a filtered list.

**`/vendor/orders`.**
- Status chips: To accept, Accepted, Ready, Dispatched, Delivered, All.
- Search, and an "Assigned to me" toggle.
- Card list on phones, table on desktop.

**`/vendor/orders/:orderId`: PO detail.**
- Header: number, date, status badge, category, expected and committed dates, delivery address, terms.
- Lines: item code and name, qty, UoM, rate, tax, total, received/accepted/rejected.
- Also: timeline, shipments, documents, receipts with QC, invoice/payment summary.
- **Sticky action bar.** Each action is shown only when the status allows it **and** the user holds the right:

| Action | Right | Notes |
|---|---|---|
| Accept | respond | Sheet: committed date + vendor reference |
| Decline | respond | Reason required |
| Change delivery date | respond | Date + reason |
| Mark ready for dispatch | ship | |
| Add shipment | ship | Transporter picker from `/vendor-portal/transporters/options`, or a courier name. Fields: tracking, LR, vehicle, driver name/mobile, shipped date, expected delivery, packages, weight, freight, notes |
| Upload document | ship, or invoice for kind INVOICE | Kind select; `<input type="file" accept="image/*,application/pdf" capture="environment">` |
| Download PO PDF | access | Uses the portal job-status route, then `api.download` |

**`/vendor/shipments`:** the vendor's shipments across POs, filterable by in transit or delivered.

**`/vendor/receipts`:** GRN lines with accepted/rejected quantities, rejection reasons and the QC verdict.

**`/vendor/invoices`:**
- A list with a payment-status chip, balance and due date.
- A "New invoice against PO" sheet: pick one of my POs that has receipts, then enter the invoice number, date, lines (prefilled from the PO and receipts), tax and an optional file. It posts to `/vendor-portal/invoices`.

**`/vendor/payments`:** the vendor payment dashboard. Summary tiles come from `/vendor-portal/payments/summary`: outstanding, overdue, due in the next 7 days, paid this month, and the last payment. Below them, the payments list with allocations.

**Dashboard rating card:** the vendor's own rating from `/vendor-portal/rating`: score, on-time %, rejection %, period and sample size, with a one-line explanation.

**Profile:** reuse the profile page's password and MFA sections inside the vendor shell. The vendor-specific header shows vendor and roles.

### 3. UX rules
- All copy goes through `lib/messages.ts`, with specific success messages ("PO-… accepted", "Shipment 2 recorded for PO-…").
- Constants come from `lib/constants.ts`.
- Validation mirrors the server (zod with `(v): boolean` predicates). Server field errors go through `applyFieldErrors`.
- Touch targets ≥ 44 px. The layout works at 360 px width with no horizontal scroll. Dialogs become bottom sheets on phones: reuse the dialog primitive with a sheet variant if one exists, otherwise responsive `DialogContent`.
- Status badges come from `STATUS_VARIANTS`; add `VENDOR_DECLINED` if C1a did not.
- **Every gate names the exact endpoint permission** (`P.VENDOR_PORTAL_*`), so `test_frontend_permission_gates.py` passes.

### 4. Wiring
- `route-loaders.ts`, `router.tsx` (vendor route group), the four sweep scripts, and `capture-screens.mjs`.
- In the sweeps, vendor routes need a vendor account. Mark them so the sweep can sign in as the demo vendor user, or record `null` where the existing scripts allow it. The `test_e2e_sweep_routes.py` contract must pass.
- Handbook: a new "Vendor app" chapter (screens, who uses them, flows, rules).

### 5. Tests (renderScreen harness)
- Router: a vendor user is redirected to `/vendor`; staff are kept out of `/vendor`.
- Dashboard tiles link with the right filters.
- Orders list filters reach the URL and the request.
- PO detail: each action appears only in an allowed state with the right; the request bodies are exact (accept, decline, date change, ready, shipment, upload kind).
- New-invoice sheet body.
- Mobile layout smoke test: the bottom bar renders under a narrow `matchMedia`.

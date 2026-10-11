# WP B5: Purchase and inbound-dispatch reports, operations e2e journeys, operations handbook pass

Lane B (operations), round 5. Assumption placeholder prefix: `§B5-n`.

**Proposal.** Add-on §09 "Reports":
- Purchase: Pending PO, Vendor-wise PO, Category-wise PO, Delayed PO, Duplicate PO warning, Vendor acceptance, Delivery performance.
- Dispatch (inbound): In-transit material, Expected today, Late delivery, Courier-wise tracking, Transport-wise cost, Vendor dispatch history.

**Already present:** pending PO, vendor performance (delivery), and the outbound courier/dispatch reports. The rest were never built for **inbound** purchase shipments, which have existed since C1a.

**Background:** `reports/procurement-vendor-portal.md` E11, and `reports/dealers-reports-docs.md` §1.5 (how a report is implemented).

## Part 1: Reports

Each report needs all of the following:
- a ReportKey and a catalogue entry
- a kebab-case route with `report.view`
- one CHECK-swap migration for all new keys, listing every key literally
- the frontend union
- a row model whose column suffixes drive formatting
- one SQL builder (no N+1) with indexes checked
- a period where meaningful

| Key | What it shows | Notes |
|---|---|---|
| `PURCHASE_ORDER_REGISTER` | PO-level rows: code, date, vendor, category, assignee, status, total, expected/committed dates, received % | Filters vendor, category, status; search. Vendor-wise and category-wise views are the same report with grouping filters. Also add `PURCHASE_ORDER_SUMMARY`, grouped by vendor or by category through a `group_by` filter, **or** fold the grouping into the frontend. Choose the simpler path that still gives "Vendor-wise PO" and "Category-wise PO" totals, and document it. |
| `DELAYED_PURCHASE_ORDERS` | Uses the shared "PO late" predicate from C1a: days late, vendor, category, pending quantity | |
| `DUPLICATE_PO_OVERRIDES` | POs created with `duplicate_acknowledged`: who, reason, matched candidates if recorded, date | |
| `VENDOR_ACCEPTANCE` | Per vendor: POs sent, acknowledged, declined, median and average hours to acknowledge, % acknowledged within 24 h | Use `sent_at` / `acknowledged_at`. The 24 h threshold matches the alert default. |
| `INBOUND_SHIPMENTS` | Shipment rows: vendor, PO, courier/transporter, tracking, LR, shipped date, expected date, status (in transit / received), days late | Filters `in_transit_only`, `expected_today`, `late_only`, `transporter`. This one report covers "In-transit material", "Expected today", "Late delivery" and "Courier-wise tracking". |
| `INBOUND_FREIGHT` | Transport-wise cost: freight by transporter or courier and month | |
| `VENDOR_INBOUND_HISTORY` | Per vendor, a shipment history with on-time %; the inbound counterpart of the existing outbound report | |

`ReportFilters` may gain optional fields (`category_id` already exists but some reports ignore it; `transporter_id`). Keep the shared model backward compatible.

The reports page runner is generic. Make sure the new filters appear in its `FILTERS` list where needed (category picker, transporter picker) and that columns format correctly.

## Part 2: Operations e2e journeys

Add `operations-flows.spec.ts`, API-driven. Follow the existing e2e conventions:

1. **Cost centre.** Create a cost centre, tag a vendor invoice line, approve it, and check that the cost-centre statement shows it.
2. **Shifts.** Create a shift and a roster assignment. A double-booking attempt must be refused with the conflict named. Book production with `shift_id` and check that the shift-production report shows it.
3. **Requisition to POs.** Convert a requisition into category-wise POs: three POs, each with the right category and assignee. Submit, approve and send one.
4. **Vendor journey, as the demo vendor user** (use the session helpers; add a vendor sign-in helper if needed):
   - the dashboard shows the PO;
   - accept with a committed date;
   - add a shipment with tracking;
   - upload an LR image;
   - staff see it on inbound tracking;
   - post the GRN;
   - the vendor sees the receipt and QC result;
   - a vendor invoice is submitted against the PO.
5. **Job work.** Issue → receive → record a job-work invoice from billable lines → approve. Check the expense and recovery journals.
6. **Smart alerts.** Make a PO stale through the API or a seeded fixture, run the alert sweep through whatever operational entry point exists, and check that a PO_NOT_ACKNOWLEDGED notification appears for the creator and the vendor assignee. If no entry point exists, add a guarded admin-only trigger. That would be a new route needing a permission, so it may not be worth it; prefer calling the Celery task in-process from a test helper script.

Same rule as WP A5: write the specs; run them only if a stack is already up; never start or stop Docker.

## Part 3: Operations handbook pass

`docs/handbook/content.json` and `capture-screens.mjs` must accurately cover:
- Organisation (Cost centres, Company bank details)
- Shift roster
- Work order and production dialogs (shift select)
- Requisitions (Raise POs, Close)
- PO detail (Send with assignee, shipments, vendor date, declined)
- Inbound tracking
- Job-work invoices
- Alerts admin
- The vendor app chapter (screens, roles, the install-and-push steps)
- The new reports

Also fix stale claims, for example "shift is typed as text".

## Docs

`assumptions.md` (`§B5-n`), `api.md`, handbook.

## Tests

- **Unit:** derive functions; the catalogue test updated.
- **Integration:** each report on seeded data, with sums checked by hand-computed expectations; filters work; list returns rows.
- **Frontend:** the reports page shows the new filters.

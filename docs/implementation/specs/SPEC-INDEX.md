# Planned work packages (specs live in this folder)

Round 1 is being built now. Rounds 2–5 are specified but not built yet. Read a spec file only when you need to know whether it fully covers a checklist item.

| WP | Round/lane | Scope summary |
|---|---|---|
| A1-brand-manufacturer | R1 / A | Brand and Manufacturer masters; `item.brand_id`, `manufacturer_id`, MPN; masters tabs; item form/filter |
| B1-cost-centres | R1 / B | Cost-centre master; tagging of departments, work centres, production/job-work orders, stock adjustments, vendor invoices and ledger entries; cost-centre statement report; company bank details |
| C0-requisition-split | R1 / C | Requisition → one PO per (vendor, category); vendor manager assignment; PO category; vendor visibility by category; duplicate match on requisition line; requisition close; vendor user category scope UI |
| A2-sales-invoice | R2 / A | GST sales invoice per dispatch (SI/ series), PDF, AR posting, receipt allocation to invoices, due-date ageing, GST summary from invoices, SO close; fixes the dev Celery queue |
| B2-shifts | R2 / B | Shift master; roster (no double booking); `shift_id` on work orders and bookings; operator directory; shift-production report; rostered capacity |
| C1a-vendor-portal-backend | R2 / C | Vendor isolation fix (allow-list gate); `vendor_portal` module (dashboard, POs, accept/decline/date/ready, shipments, typed documents, PO PDF, receipts+QC, invoices, payments); inbound tracking board API; vendor-facing notifications |
| C1b-web-push-backend | R2 / C | Web Push channel (RFC 8291/8292, no new dependency): subscriptions, VAPID, worker, recovery sweep, routes |
| A3-returns-credit-notes | R3 / A | Sales return document (dispatch-based, QC hold, RETURN QC, serial disposition); GST credit note (CN/ series, PDF); customer adjustment screen |
| B3a-job-work-invoice | R3 / B | Job-work invoice as a typed vendor invoice (match against receipts, JOB_WORK_EXPENSE, excess-loss recovery); fix for cancel-after-dispute reversal |
| B3b-procurement-cost-centres | R3 / B | Cost centres on requisitions, PO lines and vendor invoice lines |
| C2a-vendor-app-frontend | R3 / C | Vendor mobile-first app at `/vendor/*` (dashboard, orders, PO detail and actions, shipments, receipts, invoices, payments) |
| C2b-pwa-push-inbound-board | R3 / C | Installable PWA (manifest, service worker), push opt-in UI, admin inbound tracking page, PO detail shipments / vendor date / declined / PDF |
| A4-collections-dealer-performance | R4 / A | Collections module (follow-ups, notes, promises, escalation, daily overdue sweep, PAYMENT_DUE); dealer performance report; dashboard receivable tiles |
| B4-smart-alerts-events | R4 / B | Smart-alert engine (PO not acknowledged, delivery missed, dispatch not updated, tracking missing, GRN not created, QC pending, vendor invoice pending, vendor payment overdue, job-work overdue, dispatch delayed), thresholds UI; internal event notifications (the admin list); link fixes; retention |
| C3a-dealer-portal-core-backend | R4 / C | Dealer principal and isolation; catalogue with dealer price, offers, availability band; cart; wishlist; checkout → DRAFT SO; orders; tracking; statement |
| C3b-dealer-portal-commerce-content | R4 / C | Dealer invoices, credit notes and returns (return requests); banners and offers admin (`/promotions`); item images; optional public catalogue; dealer notification types |
| A5-dealer-notifications-commercial-e2e | R5 / A | Dealer-facing notifications (confirmed, dispatched with courier/tracking/expected date, delivered, invoice, payment reminder, return); commercial e2e; handbook |
| B5-procurement-reports-ops-e2e | R5 / B | PO register (vendor-wise / category-wise), delayed PO, duplicate-PO overrides, vendor acceptance, inbound shipments (in transit / expected today / late / courier-wise), inbound freight (transport-wise cost), vendor inbound history; operations e2e; handbook |
| C4a-dealer-app-frontend | R5 / C | Dealer storefront app at `/dealer/*` (home, banners, offers, catalogue, item, cart, checkout, wishlist, orders and tracking, invoices, returns, account) |
| C4b-promotions-admin-public-catalog | R5 / C | Staff Promotions page; item images panel; dealer user binding in the user dialog; SO source column; optional public catalogue pages |
| D5-specifications-accessories | R3 / B | Specification attribute master + category templates + item specification values; product accessories (optional / included); filters; dealer catalogue hook |
| E1a-dispatch-linkage-partial-dispatch-topup | R2 / A | Dispatch lines bound to SO lines (critical defect); partial dispatch of reserved stock; allocate/top-up endpoint and sweep (K01, K08) |
| E1b-sales-automation-trace | R4 / D | Auto-plan shortfall on confirm (DRAFT production/requisition), SO fulfilment trail, other-warehouse availability + transfer (S01.1, S03.13, K02, K10) |
| E2-allocation-reallocation-completion | R3 / D | Reallocation approval UI, recipient linking + owner sync port, priorities/customer priority, committed (allocated) qty, SOFT policy, auto-proposals, cross-warehouse, spare requests |
| E3a-mrp-engine-depth | R4 / D | Low-level-code netting, routing lead times, per-level offsets, expected availability dates |
| E3b-mrp-outputs-scheduling | R5 / D | TRANSFER, REALLOCATION, JOB_WORK, ASSEMBLY targets; scheduled MRP; reorder and packing-material demand |
| E4-assembly-completion-qc-unification | R2 / B | Assembly UI lifecycle; one QC verdict path (owner callbacks to GRN/PO, assembly, job work); stranded-row reconciler; assembly → packing hand-over |
| E5-quality-completion | R4 / B | Production QC and final QC, manual inspection, inspection detail/photos, rework tracking and report |
| E6a-procurement-flow-completion | R6 / B | PO INVOICED/PAID sync, requisition detail, quotation → PO, PO edit/close + duplicate re-check, GRN detail/cancel, vendor invoice dispute UI, PO email to vendor |
| E6b-procurement-finance-vendors | R6 / B | Purchase-return debit notes + return-aware match, QC reject → return/replacement (K09), lead-time report, transport and service vendors, RFQ PDF/email, rating refresh |
| E7a-production-completion | R4 / B | Complete/close capture, costing + variance + report, WIP, planning board + capacity, machine overlap, daily bookings, routing machine, BOM draft editor + operations, idle-machine utilisation |
| E7b-job-work-completion | R4 / A | Job-work detail/timeline, receive options, rework orders, challan register + PDF, vendor material ledger, vendor-app job work, cost capitalisation |
| E8a-inventory-operations-completion | R2 / D | Transfer/adjustment UI completion, bins + bin moves, scrap visibility + disposal, consumables internal issue, `can_scrap` |
| E8b-batch-serial-tracking | R6 / C | Batch/serial screen, quarantine/expiry/FEFO, serial capture on GRN/production/assembly/adjustment/transfer, reserved serial dispatch |
| E9-packing-dispatch-completion | R6 / A | Packing order creation, packing specs + shortage requisition (K06), carton labels/barcodes, carton tracking, dispatch planning board/edit, outbound attachments |
| E10a-item-master-completion | R3 / B | Variants UI + SO variant select, item form prices/planning fields, item-vendor link editor, discount authority, dealer address rule |
| E10b-finance-reports-dashboard | R7 / A | GL account statement, trial balance, sales/purchase registers, GSTR-1/3B export, per-report permissions + export, actual profitability, management dashboard |
| E12-phase1-documents-cleanup | R7 / A | Security issue resolution report + legacy review, deployment checklist, dead code |
| K-scenarios-e2e | R7 / D | K01–K10 integration tests + Playwright journeys + handbook chapter; fix what breaks |
| D1-duplicate-po-actions | R5 / B | Duplicate PO actions (merge, increase quantity, create with approval confirmed at approval, cancel request); category and BOM-demand duplicate rules; PO amendment flow (rev N, vendor re-accepts) |
| D2-whatsapp-sms-channels | R4 / B | WhatsApp Cloud API and SMS (MSG91/generic) channels behind configuration; consent; templates admin; delivery log |
| D3-courier-api-auto-tracking | R4 / A | Courier integration (Shiprocket provider + generic webhook); auto tracking status for outbound dispatches and inbound shipments; admin page; refresh button |
| D4-po-digital-signature | R5 / B | Certificate-backed DSC/PAdES signature on approved POs; QR + public verification page; revision on amendment. Gated on legal/counterparty and certificate procurement; Ed25519 draft superseded |

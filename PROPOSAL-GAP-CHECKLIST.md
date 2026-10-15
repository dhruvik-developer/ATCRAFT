# Atcraft ERP — Proposal vs Implementation Gap Checklist
Local-only audit. Source: `Atcraft Manufacturing ERP Development_compressed.pdf` (24 pages, Aug 2026 v1.0) vs `main` on this machine.
Trace baseline: `docs/implementation/trace/status.tsv` (177 PRESENT / 159 PLANNED_FULL / 93 PARTIAL / 43 PLANNED_PARTIAL / 2 ABSENT), `docs/implementation/trace/gaps.md`, backend `apps/api/src/modules/*`, frontend `apps/web/src/features/*`.

Status key: DONE = present on main | PARTIAL = backend and/or UI incomplete | PLANNED = not on main, owned by a work package (C0/C1a/C1b/C2a/C2b/A2/A3/A4/B1-B5/D1-D4) | MISSING = no owner.
Work-package shorthand used below is the same as in `docs/implementation/specs/SPEC-INDEX.md`.

## 01 — Existing System Stabilization & Security Hardening (Phase 01)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Review existing API & frontend code | PARTIAL | No legacy-review doc mapping old modules to new (`gaps.md P01.1`) |
| Remove unused / dead code | PARTIAL | Dead: approval inbox (`audit/models.py:113` no writer), `routing_operations.qc_required` never read, `OrderPartyRepository.customer_price_type` never called |
| Secure API authentication | DONE | `auth/service.py` login/refresh/revoke, `core/security.py`, `core/deps.py` |
| Add JWT expiry | DONE | Short access + rotating refresh |
| Remove hardcoded bypass token | DONE | |
| Move secrets to env vars | DONE | Refuses to start in prod without `JWT_SECRET` |
| Remove plain-text password usage | DONE | Argon2id+bcrypt `core/security.py` |
| Replace MD5 with secure hash | DONE | |
| Secure unauthenticated APIs | DONE | |
| Add RBAC basics | DONE | ~150 `Permission`, role presets incl. `VENDOR_*`, `core/permissions.py` |
| Standard API response format | DONE | `ApiResponse` / `PaginatedResponse` |
| Improve error handling | DONE | `core/errors.py`, no SQL leak |
| Audit logging for critical actions | DONE | `audit/service.py record_audit` in-transaction |
| Deliverables: secure login, updated auth, cleaned config, role base, deployment checklist | DONE | |
| Deliverable: security issue resolution report | PARTIAL | No issue→fix→evidence report (`gaps.md P01.D5`) |

## 02 — Foundation Masters (Phase 02)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Company / Branch / Plant / Department master | DONE | `organization/*` |
| Cost center master | PLANNED (B1) | Tree master, guards need row locks (BACKLOG #4) |
| Warehouses: RM, Component, WIP, Assembly, FG, Packing, Scrap, Return | DONE | `warehouses/models.py WarehouseType`, seed present |
| User roles / Permission matrix / Activity log | DONE | `users/*`, `audit/*` |
| Approval workflow | PARTIAL + PLANNED (B4, D1) | Central `approval_requests` queue dead (no writer, dashboard always empty); reallocation approve/reject API-only; no configurable thresholds/levels (`gaps.md P02.U3`) |
| Business partners: Customer/Dealer, Vendor, Job-work vendor, Transporter, Courier | DONE | `customers/*`, `vendors/*` (+Transporter) |
| Item roles: can_purchase / manufacture / sell / use-in-BOM / use-in-assembly / send-for-jobwork | DONE | `products/models.py` flags, default capabilities |
| Item role: use as spare | PARTIAL | Flag read by nothing; no SPARE consume path; no `/allocation/reservations/{id}/issue` |
| Item role: can_return | PLANNED (A3) | Nothing checks it; customer-return doc missing on main |
| Item role: can_scrap | PARTIAL | No scrap path checks flag (`inventory/service.py:397`, QC SCRAP, job-work scrap, adjustments ignore it) |
| Core rule: one item, many roles (lock body/handle/cylinder/screw/spring) | DONE | |

## 03 — Inventory Engine (Phase 03)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Types: RM, Components, SFG, Assembly items, FG, Packaging, Consumables, Scrap, Return, QC-hold | PARTIAL | DONE except: ASSEMBLY group missing (`ItemGroup` no ASSEMBLY); Consumables: no store type, no internal-issue doc; Scrap: maintained but shown on no screen, no disposal/sale; Return stock needs A3 |
| Stock status per item: On-hand / Available / Reserved / Allocated / QC-hold / In-transit / Jobwork-issued / Damaged / Scrap | PARTIAL | DONE except: `Allocated` (`inventory_balances.allocated_quantity`) never written — always 0, no pick/commit step; Scrap invisible + never disposable (moves from SCRAP refused `inventory/service.py:529`) |
| Multi-warehouse stock / Stock ledger / Physical verification / Audit trail | DONE | `inventory/*`, FIFO layers, `PhysicalCount`, ledger immutable |
| Stock transfer | PARTIAL | UI lines item+qty only, no batch/bin picker; no cancel/edit UI; serial refused; same-warehouse bin move impossible (`models.py:1111`) |
| Stock adjustment | PARTIAL | No Reject/Cancel UI; no batch/bin/unit-cost fields; `stock_adjustment_lines` no `serial_id` so serial items unadjustable even via API |
| Bin / location support | PARTIAL | No screen puts/moves/shows stock by bin; GRN/transfer/adjust/count/reserve dialogs have no `location_id`; same-warehouse putaway doc missing |
| Batch / serial where required | PARTIAL | `POST /inventory/batches|serials` no UI caller; GRN/production/assembly/transfer/adjust refuse serial inbound; `BatchStatus` QUARANTINED/EXPIRED never set; no FEFO; no Batches & Serials screen |
| Reservation & release | PARTIAL | Manual reserve needs pasted demand UUID; release/expiry/reallocation never updates owning doc (SO stays ALLOCATED → dispatch/cancel then fails); release-all-or-nothing; paths not idempotent (`gaps.md P03.F7`) |
| Reallocation | PLANNED (B4 notifies only) | No list/approve/reject UI; `P.ALLOCATION_APPROVE` gates nothing; recipient `demand_line_id=None` so dispatch/production never consume it; donors same-warehouse only; unallocated demand cannot request |

## 04 — Product Engineering & BOM (Phase 04)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Product master: code, name, category, size, finish, color, variant, HSN/GST, sale price, dealer price, production type | PARTIAL | DONE except: Variant — no create/edit UI (`item-detail-page.tsx:430`), SO line never offers variant, no variant stock grain; Sale price + Dealer price — API + read-only display only, `ItemFormDialog` omits both |
| BOM: single-level / multi-level / alternate / approval | DONE | `bom/*` + `engine.py BomExplosion`, versioning, effectivity, where-used |
| BOM versioning | PARTIAL | `PATCH /bom/{id}` never called by UI; no Edit-draft action; copied version cannot be changed on screen |
| BOM costing | PARTIAL | UI BOMs cost material only; dialog posts no operations; routing snapshot never written into `bom_operations`; ops shown as count only |
| Common-part calculation + consolidation rule (100 kits + 50 handles + 25 bodies → merged spring) | DONE | Engine consolidates + cycle detection |
| Routing: operation sequence / work center / standard time / setup time / process cost | DONE | `routing/*`, cost/lead-time math |
| Routing: machine | PARTIAL | Form carries `machine_id` with no control (`routing-dialogs.tsx:508`); detail has no Machine column; pick happens only per work order |

## 05 — Allocation & Reallocation Engine (Phase 05)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Reservation types: sales / production / assembly / job-work / service-spare / soft / hard | PARTIAL | DONE: sales, production, job-work, hard. PARTIAL: assembly (no create/approve/release UI, priority 100, issue doesn't touch reservation); service/spare (no SPARE doc, `DemandType.SPARE` unused, `allocation.consume` only from dispatch+production); soft (nothing creates SOFT, SOFT blocks like HARD, contradicts `assumptions.md §10`) |
| Allocation priority 1–9 (FG → assembly → SFG → components → RM → cross-warehouse → purchase → mfg → job-work) | PARTIAL | Broken at: assembly/SFG/components (intermediate stock not netted — only directly demanded items exploded, `mrp/engine.py:1071`, `bom/engine.py:589`); cross-warehouse (SO reserves only `order.warehouse_id`, donors same-warehouse, `TRANSFER` never produced); job-work (convert refused `job_work/service.py:2502`) |
| Reallocation scenario (check reservation → priority → suggest → approval → release → allocate → audit; + start-date, issue-status, notify, approval) | PARTIAL | Check reservation/priority/audit DONE. PARTIAL: material-issue status (assembly issue moves no stock, packed stock still plain reservation, `allocated_quantity` never kept); suggest (UI starts only from existing reservation row — zero-allocation urgent order cannot request); approval (no UI, approvers not notified until B4); release (API-only, donor doc not updated → dispatch/cancel fails); allocate-to-urgent (recipient `demand_line_id=None`, SO line/status not updated, dispatch refuses, stays CONFIRMED); notify (no dept/role recipients, requester not told decision, broken `/allocation/reservations/{id}` link until B4) |
| Auto drivers: customer priority / delivery date / SO priority / production priority / work-order status / material-issue status / manager rules | MISSING/PARTIAL | `P05.D1` customer priority ABSENT (no field, no scoring). `D5` work-order status ABSENT (not an input). Rest PARTIAL: no auto trigger (no beat besides stale-release, no hook on confirm), `is_urgent` ignored, date/priority edits not propagated, production default 100 vs SO 1–9 scale mismatch, fixed-code rules, no SOFT auto-approve, no thresholds/routing/escalation |

## 06 — MRP Engine (Phase 06)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Flow: SO → check FG → explode → merge common → check inventory → reserved → incoming PO → production → shortage → suggestions | DONE | Async run + lock + snapshot, `mrp/engine.py`, `repository.py` |
| Outputs: manufacturing suggestion / purchase requisition suggestion | DONE | Convertible via hooks (needs master-data fixes below) |
| Output: job-work suggestion | PARTIAL | Always refused (`create_order_from_mrp` raises); UI Convert still offered → bulk convert fails; open JW output not netted as supply |
| Output: stock-transfer suggestion | PARTIAL | Never generated (warehouses pooled per item); no `ConversionTarget.STOCK_TRANSFER`; no inventory hook |
| Output: reallocation suggestion | PARTIAL | No `REALLOCATION` type (`mrp/models.py:139`); reserved stock just excluded, never proposed from lower-priority demand |
| BOM explosion / common grouping / shortage report | DONE | |
| Multi-level demand calc | PARTIAL | No level-by-level netting; stocked sub-assembly doesn't reduce children; all levels dated from root lead time only (`engine.py:932`) |
| Auto purchase planning | PARTIAL | Vendor-link editor list-only (no lead-time/MOQ/multiple); item MOQ/safety/default-warehouse absent from form; unscoped-run convert fails without default warehouse; no scheduled/triggered run |
| Auto production planning | PARTIAL | Ignores routing lead time (`manufacture_lead_time_days` no caller); no per-level offsets; infinite capacity 24/7; no assembly-order target; open assembly orders not netted |
| Lead-time calc / expected availability date | PARTIAL | Routing lead time unused; vendor lead time uneditable on screen; no `expected_available_date` on suggestion/requirement/SO line; `promised_date` hand-typed |

## 07 — Procurement & Vendor Mgmt (Phase 07) + Add-on §03/04 PO flow
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Purchase requisition | PLANNED (C0+D1) | Approved req cannot become PO on main; no `/close`; no read-only approver view (dialog shows code+note only); MRP conversion drops `source_demands` |
| RFQ | PARTIAL | Send only stamps `sent_at`; no PDF/email/vendor view — buyer sends outside ERP |
| Vendor quotation | PARTIAL | SELECTED leads nowhere; no quotation→PO (`quotations-page.tsx:303`); only demo loader sets `converted_purchase_order_id` |
| Quotation comparison | DONE | `score_offer/rank_offers` |
| Purchase order | PLANNED (C0/C1a/C2b/D1) | No draft-edit UI; no close/short-close UI; no PO PDF/print; no req/quotation linkage in payload; no duplicate re-check on PATCH; status stuck at QC_PENDING/GRN_COMPLETED |
| GRN | PARTIAL | No detail screen; no cancel-draft UI; `qc_status` stays PENDING forever → Waiting-on-QC filter never clears |
| Incoming QC | PLANNED (C1a derives, B4 alerts) | Verdict never written back to GRN line or PO (`decide_inspection` doesn't call procurement); PO stuck QC_PENDING; vendor unretirable |
| Purchase return | PARTIAL | No financial leg: no debit note, no AP/tax reversal; match uses accepted without netting returns |
| Vendor invoice | PLANNED (B3a/C1a/C2a) | Dispute/resolve/cancel API-only; APPROVED→DISPUTED→CANCELLED never reverses journal (phantom payable); match not return-aware; approval never moves PO to INVOICED |
| Vendor payment / ledger / rating | PARTIAL→PLANNED | Payments/ledger present; rating computes only on button press, no nightly recompute; inbound freight report needs B5 |
| Lead-time tracking | PARTIAL | Never measured (no ordered→received per PO/vendor/item; no actual-vs-master report; masters never refreshed) |
| Vendor types: RM / component / packing / job-work | DONE | |
| Vendor type: transport | PARTIAL | `transporters.vendor_id` missing — freight can't be billed/paid/reconciled; no TRANSPORT demo vendor |
| Vendor type: service | PARTIAL | Service PO received into stock; POs without service acceptance; service bill fails match without override; needs non-stock/service-acceptance flow |
| Duplicate check rules (vendor, item/category, req date, SO/MRP ref, pending req, BOM demand, warehouse) + prompt + 4 options | PLANNED (C0+D1) | Detection + advisory lock present; category-level signal, demand-refs persistence, real merge/increase/create-with-approval/cancel buttons missing on main |
| PO status flow (req → vendor → category PO → manager → notify → accept/reject → delivery date → dispatch → courier → received → QC → GRN → invoice → payment) | PLANNED (C0/C1a/C2a/C2b/B4) | Only req/v lequel/received steps work on main; vendor notify/accept/dispatch/courier/QC/GRN/invoice/payment + derived `InboundTrackingStatus` are C1a/C2b |

## 08 — Production Management (Phase 08)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Manufacturing order / material issue / material return / work order | DONE | `production/*`, tolerances, backflush |
| Production planning | PARTIAL | No board/calendar/Gantt; infinite capacity; no MPS; no capacity check (`capacity_per_hour`, `working_hours_per_day` stored, unused) |
| Machine allocation | PLANNED (B2) + PARTIAL | `_assert_machine` tenant+centre only (RETIRED/BREAKDOWN assignable, `WORKABLE_MACHINE_STATUSES` unused); no overlap check; no machine schedule view |
| Operator allocation / shift planning | PLANNED (B2-shifts, in lane `c768493`, not on main) | Roster, operator directory, shift master, one-operator-per-machine EXCLUDE |
| Daily production entry | PLANNED (B2) + PARTIAL | Order-only bookings; UI never sends `work_order_id`; downtime-only refused in UI; batch/serial FG unbookable |
| WIP tracking | PARTIAL | `GET /production/wip` no screen; `late_only` ignored on board; no WIP valuation |
| Production completion | PARTIAL | Complete/close post `{}` — no qty/reject/scrap/dates/notes from UI; tracked FG cannot be received |
| Production costing | PARTIAL | Materials/operations breakdown API-only; no standard-vs-actual variance; outsourced/job charges, overhead, finance posting, cross-order report missing |
| Statuses Draft→Approved→Released→Material-issued→In-process→Completed→Closed/Cancelled | DONE | State machine present |

## 09 — Assembly Line Management (Phase 09)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Assembly BOM / tracking | PARTIAL | BOM side DONE; tracking PARTIAL: no Start UI (`POST /assembly/{id}/start` unused), free-text line, one-shot completion, no progress/WIP board, no operator output |
| Assembly order / component issue / completion / packing handover | PARTIAL | See P09.M1/M3/M6/M7: no create/edit/approve/release/start/close/cancel UI; top-up issue refused by machine; serial/batch outputs refused; handover endpoints never called, `packing` never references assembly |
| Assembly QC | PARTIAL | `POST /assembly/{id}/qc-result` no UI; `/quality` decision doesn't advance order (stranded QC_PENDING, double-post if both called) |
| Example: lock-set consumes handle/body/cylinder/keys/screw-kit/box → FG stock | PARTIAL | Flow exists in service but not runnable end-to-end from UI; serialised sets impossible |

## 10 — Job Work Management (Phase 10)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Job-work order / material issue to vendor / material receipt / vendor ledger | DONE→PLANNED (B3a ledger) | Order/challan-issue/receipt with scrap/loss tolerance present; per-vendor material+charges ledger needs B3a |
| Job-work challan | PARTIAL | No list/detail/register UI; no GST rule-55 challan PDF; no ITC-04 |
| Vendor process tracking | PLANNED (B4) | `JOB_WORK_OVERDUE` never raised; no per-order timeline; no vendor-reported progress |
| Short / excess receipt | PARTIAL | Dialog never sends `is_final`/`approve_excess_loss` — short return can't close order; tolerance/debit API-only |
| Job-work QC / rejection / rework | PARTIAL | Two disconnected verdict paths (quality vs job-work, double-post or stranded PENDING); multi-output receipt raises one inspection for first item only; receive dialog hard-codes RETURN_TO_STOCK, no action/reason/rework order/debit |
| Job-work invoice | PLANNED (B3a) | Typed JOB_WORK invoice, match, expense, recovery journal |

## 11 — Quality Control (Phase 11)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| QC types: incoming | DONE (C1a derives board status) | Raised by GRN |
| QC types: in-process / job-work / assembly / final / return | PARTIAL→PLANNED | In-process: nothing raises it, no Raise-inspection UI. Job/assembly: dual-path defect (see Phase 10/09). Final: nothing raises it, no gate before packing/dispatch. Return QC needs A3 |
| Results: Accepted / Rejected / Rework / Hold / Scrap | DONE | |
| Checklist / parameter-based / rejection reason / vendor report / product history | DONE | AQL sampling, `evaluate_reading`, summaries |
| Photo upload | PARTIAL | Template-param only; no inspection-level/defect photo; text-only "Photo attached", no thumbnail/download; decided inspections unopenable |
| Rework tracking | PARTIAL | No Rework tab/list; no who/where/cost/time record; decisions overwrite quantities (history in audit only); QC_HOLD can't be issued to bench; no rework rate report |

## 12 — Packing & Dispatch (Phase 12)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Packing material issue / transport-courier assignment / delivery confirmation | DONE | |
| Packing order | PARTIAL | No New/prefill UI ("created from allocated SOs" copy is false); no SO action; no auto-create; no assembly→packing wire-up |
| Box/label/barcode generation | PARTIAL | Check-digit barcode computed but only shown/printed as text; no 100×150 PDF/ZPL label, no print buttons, no scan/lookup |
| Carton tracking | PARTIAL | Dispatch always sends `carton_ids: []`, never `packing_order_id`; consignment records no cartons; hand counts/weights typed, not copied; no Find-carton/scan/load step |
| Dispatch planning | PARTIAL | No due-to-ship board; lines never carry `sales_order_line_id` → `allocation.consume` skipped, reservation untouched, SO qty never moves; no packing-order picker; `PATCH /dispatch/{id}` no UI; no vehicle/load planning |
| LR details | PARTIAL | LR typable at create only; `lr_number` gate unused; no `lr_date`; no LR-copy/photo/POD upload (inbound-only in C1a) |
| Packing list | PLANNED (A2 worker fix) | Queued PDF present; needs dev-worker `-Q` fix to run |
| Invoice handover | PLANNED (A2) | One GST invoice per SALES dispatch |

## 13 — Finance Integration (Phase 13)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Sales ledger / customer ledger / payment collection / outstanding / follow-up | PLANNED (A2/A4) | No sales doc/posting/register/GL view on main; A2 posts Dr AR / Cr REVENUE / Cr OUTPUT_TAX + due-date + allocations + invoice PDF; A4 follow-ups + dealer performance |
| Purchase ledger / vendor ledger | PLANNED (B3a) + PARTIAL | AP + 3-way match present; missing: return debit notes, cancel-reversal fix, GL account statement view |
| Job-work cost | PLANNED (B3a) | No invoice/expense/billed-vs-accrued screen; cost never capitalised into output value |
| Production cost | PARTIAL | Never flows to inventory/GL; no variance; no PRODUCTION_COST report |
| Inventory valuation | DONE | FIFO layers, average derivation, valuation report |
| GST report | PLANNED (A2/A3) | Not invoice-based on main; no HSN snapshot netting, no GSTR-1/3B export |

## 14 — Reports & Dashboards (Phase 14)
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Sales-order status / stock availability / reserved-vs-available / MRP shortage / purchase pending / production pending / job-work pending / QC rejection / vendor performance / fast-slow-dead stock / order delay / production efficiency / inventory valuation | DONE | `reports/*` inline+queued snapshots, dashboard aggregates |
| Dealer performance / dashboards | PLANNED (A4) | `DEALER_PERFORMANCE`, receivables tiles, trend endpoint still open; PO tile mislabelled |
| Product profitability | PARTIAL | Estimates only (booked orders × standard cost); needs dispatch FIFO COGS net of returns + invoice revenue; bypasses `item.cost_view` |
| Machine utilization | PLANNED (B2) + PARTIAL | Needs machine-driven query (idle machines vanish) + rostered capacity |
| Purchase reports: pending PO / delivery performance | DONE | |
| Purchase reports: vendor-wise / category-wise / delayed / duplicate-warning / acceptance | PLANNED (B5/C0/D1) | Register, DELAYED_POS, DUPLICATE_OVERRIDES, VENDOR_ACCEPTANCE |
| Dispatch reports: in-transit / expected-today / late / courier-wise / transport-cost / vendor history | PLANNED (C1a/C2b/B5) | Shipments board + INBOUND_SHIPMENTS + INBOUND_FREIGHT + VENDOR_INBOUND_HISTORY |

## Add-on 01 — Notification System
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Admin list (16): purchase req, vendor order, PO approved/rejected, material received, QC pending/rejected, JW sent/received, SO created, shortage, reallocation, dispatch, tracking, payment pending, vendor delay | PLANNED (B4/C1a) | Events + links + recipient fallback still open for QC-rejected link, dispatch-created/tracking-update fallback, approvals beyond PR/PO/reallocation/payment |
| Mobile list (10): PO assigned/approved, order dispatched, courier+tracking, expected date, payment reminder, QC reject, JW pending, new order req, approval req | PLANNED (B4/C0/C1a/C2b/A2/A4/A5) | JW-vendor job-work endpoints missing (vendors never see JW); approval-pending helper missing for BOM/invoice/adjustment/production/SO/vendor-master/JW |
| Channels: in-app / email | DONE | Prefs, dedup, grouping, digest |
| Channels: push | PLANNED (C1b) | `PUSH`, subscriptions, VAPID |
| Channels: WhatsApp/SMS | PLANNED (D2, future) | |

## Add-on 02/05 — Vendor Purchase Mobile App + Features
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Roles: Owner / Manager / Dispatch Mgr / Account Mgr / QC Contact; category mapping (Mgr1→Lock Body, Mgr2→Handles, Mgr3→Packing); category-wise PO split (100 bodies → PO-001, 200 handles → PO-002, 500 boxes → PO-003) | PLANNED (C0/C1a) | Assignee + visibility + conversion-preview on main via C0 lane work; portal allow-list gate, role remap, per-vendor dashboards in C1a/C2a |
| Dashboard: pending / accepted / ready / dispatched / delivered / payment-pending / QC-rejected | PLANNED (C1a/C2a) | |
| PO detail: number / items / category / qty+rate / delivery date / address / terms / PDF | PLANNED (C1a/C2a) | PDF via `ReportKey.PURCHASE_ORDER` |
| Actions: accept / reject+reason / update date / ready / courier-transport / upload invoice / dispatch photo / LR-receipt / view QC / view payment | PLANNED (C1a/C2a) | Shipments + documents (`INVOICE/DISPATCH_PHOTO/LR_COPY/DELIVERY_CHALLAN`) + receipts + payments endpoints |

## Add-on 06/07 — Dispatch Tracking / Admin Tracking
| Proposal item | Status | Evidence / gap |
|---|---|---|
| Capture: courier, transport, tracking ID, LR, vehicle, driver+mobile, dispatch/expected dates, boxes, weight, freight, photo | PLANNED (C1a/C2a) | `purchase_order_shipments` columns; outbound reuse still needs LR-copy/photo/POD panel on dispatch |
| Admin view: vendor, PO, courier, tracking, status, expected, delay, received, QC-pending, GRN-pending; statuses Not-dispatched→Ready→Dispatched→In-transit→Delivered→Received→QC-pending→GRN-done | PLANNED (C1a/C2b/B4/D3) | Derived `InboundTrackingStatus` + inbound board + overdue/expected-today filters + courier auto-track |

## Add-on 08/09/10 — Smart Alerts / Reports / Priority
| Proposal item | Status | Evidence / gap |
|---|---|---|
| 9 alerts: PO unaccepted 24h, delivery missed, dispatch not updated, tracking missing, QC-pending, QC-rejected, GRN missing, invoice pending, payment overdue | PLANNED (B4) | Rules + sweeps; per-GRN invoice detection + QC link still open |
| Priority Must-have (10: admin notify … admin tracking screen) | PLANNED (B4/C0/C1a/C1b/C2b) | See above; push + portal required |
| Priority Should-have (6: duplicate prevention … QC visible) | PLANNED (C0/D1/C1a/B4) | |
| Priority Future (6: WhatsApp … PO signature) | PLANNED (D2/D3/C1a/D4) | Rating auto-recompute + DSC-vs-Ed25519 decision open (`gaps.md A10.F4/F6`) |

## Key Business Scenarios (proposal §07) — all PARTIAL on main
| # | Scenario | Gap |
|---|---|---|
| 01 | Direct FG sale (100 handles reserved+dispatched) | Dispatch lines unlinked → reservation never consumed; refused when only reserved stock exists (`gaps.md K01`) |
| 02 | FG short (500 ordered, 120 avail → plan 380) | No auto plan on confirm; order stuck CONFIRMED, never shippable (`K02`) |
| 03 | Same part sold + in BOM | DONE |
| 04 | Common parts merged | DONE |
| 05 | Urgent reallocation with approval | API-only, zero-allocation order cannot request, recipient never linked/consumed (`K05`) |
| 06 | Packing shortage → purchase req | Packing never calls procurement; no spec/sweep (`K06`) |
| 07 | Job-work sent/received/rejected/pending | Rejected qty captured-not-shown; no detail/reconciliation screen (`K07`) |
| 08 | Partial dispatch (500 → 200 now + 300 plan) | CONFIRMED unshippable; no top-up/auto-plan (`K08`) |
| 09 | QC reject → return/replacement | No draft return, no CREDIT/REPLACEMENT path, `return_required` never cleared (`K09`) |
| 10 | Multi-warehouse (short here → transfer) | Own-warehouse only; no elsewhere-availability or TRANSFER suggestion (`K10`) |

## Conclusion — 12 most important modules
DONE: Inventory Engine (core), Multi-role Item Master (core), Multi-level BOM, Common Parts, MRP Planning (core), Production, Assembly Line (core), Job Work (core), QC (core), Packing & Dispatch (core).
Still open: Allocation & Reallocation approval UI + TRANSFER/REALLOCATION suggestions; Finance sales-invoice/credit/collections; Packing-material planning; SO fulfilment trail.

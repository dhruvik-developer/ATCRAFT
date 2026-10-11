# Proposal checklist: every item in "Atcraft Manufacturing ERP Development" (Aug 2026, v1.0)

This list transcribes the 24-page PDF in full, keeping its wording. Each line has an ID (section.number) so audit results can refer to it.

## S01: Project understanding (p.3)

- S01.1: One integrated system in which a sales order can automatically:
  - check available stock
  - reserve inventory
  - calculate shortages
  - generate production plans
  - raise purchase requirements
  - manage job work vendors
  - track the complete manufacturing lifecycle
- S01.2: Covers raw material planning, parts management, BOM, production, assembly, job work, inventory allocation and reallocation, QC, packing, dispatch and reporting.

## S02: Current system available (p.3)

The proposal lists these as modules the old system had. They must be "reviewed, cleaned, secured, and extended", so each must exist in this software.

| ID | Item |
|---|---|
| S02.1 | Admin login & user management |
| S02.2 | Dealer / customer management |
| S02.3 | Product catalog management |
| S02.4 | Category, brand, manufacturer & **specification** masters |
| S02.5 | Product variants: size, finish, **accessories** |
| S02.6 | Dealer pricing & **discount management** |
| S02.7 | Order management |
| S02.8 | Goods return management |
| S02.9 | Invoice PDF generation |
| S02.10 | Payment & ledger management |
| S02.11 | Courier & transport management |
| S02.12 | Banner & offer management |
| S02.13 | Notifications |
| S02.14 | Reports & dashboards |
| S02.15 | Mobile API: browsing, cart, wishlist, orders, tracking, returns |

## S03: Key gaps the ERP must close (p.4)

| ID | Gap |
|---|---|
| S03.1 | No manufacturing BOM |
| S03.2 | No multi-level BOM |
| S03.3 | No common parts calculation |
| S03.4 | No raw material planning |
| S03.5 | No production order management |
| S03.6 | No assembly line tracking |
| S03.7 | No job work / vendor tracking |
| S03.8 | No reservation / reallocation engine |
| S03.9 | No automatic MRP planning |
| S03.10 | No production QC workflow |
| S03.11 | No packing material planning |
| S03.12 | No production costing |
| S03.13 | No sales-order-to-dispatch traceability |
| S03.14 | Security and technical improvements required before the system is production-ready |

## P01: Phase 01, stabilization & security hardening (p.5)

| ID | Item |
|---|---|
| P01.1 | Review existing API & frontend code |
| P01.2 | Remove unused / dead code |
| P01.3 | Secure API authentication |
| P01.4 | Add JWT expiry |
| P01.5 | Remove hardcoded bypass token |
| P01.6 | Move secrets to environment variables |
| P01.7 | Remove plain-text password usage |
| P01.8 | Replace MD5 hashing with secure hashing |
| P01.9 | Secure unauthenticated APIs |
| P01.10 | Add role-based access control basics |
| P01.11 | Add standard API response format |
| P01.12 | Improve error handling |
| P01.13 | Add audit logging for critical actions |
| P01.D1 | Deliverable: secure login flow |
| P01.D2 | Deliverable: updated API authentication |
| P01.D3 | Deliverable: cleaned configuration |
| P01.D4 | Deliverable: role / permission base structure |
| P01.D5 | Deliverable: **security issue resolution report** (a document) |
| P01.D6 | Deliverable: **production deployment checklist** (a document) |

## P02: Phase 02, foundation masters (p.6)

**Organization**

| ID | Item |
|---|---|
| P02.O1 | Company master |
| P02.O2 | Branch master |
| P02.O3 | Plant master |
| P02.O4 | Department master |
| P02.O5 | Cost center master |

**Warehouses**

| ID | Warehouse type |
|---|---|
| P02.W1 | Raw material |
| P02.W2 | Component |
| P02.W3 | WIP |
| P02.W4 | Assembly |
| P02.W5 | Finished goods |
| P02.W6 | Packing |
| P02.W7 | Scrap |
| P02.W8 | Return |

**User & approval**

| ID | Item |
|---|---|
| P02.U1 | User roles |
| P02.U2 | Permission matrix |
| P02.U3 | Approval workflow |
| P02.U4 | Activity log |

**Business partners**

| ID | Item |
|---|---|
| P02.B1 | Customer / dealer |
| P02.B2 | Vendor |
| P02.B3 | Job work vendor |
| P02.B4 | Transporter |
| P02.B5 | **Courier** |

**Item master roles**

| ID | Role |
|---|---|
| P02.I1 | Can purchase |
| P02.I2 | Can manufacture |
| P02.I3 | Can sell |
| P02.I4 | Use in BOM |
| P02.I5 | Use in assembly |
| P02.I6 | Use as spare |
| P02.I7 | Send for job work |
| P02.I8 | Can return |
| P02.I9 | Can scrap |

**Core rule**

- P02.CR: A single item can be sold directly, used as a component, used in a finished product, used as a spare, used in assembly, or sent for job work.

## P03: Phase 03, inventory engine (p.7)

**Inventory types**

| ID | Type |
|---|---|
| P03.T1 | Raw materials |
| P03.T2 | Components |
| P03.T3 | Semi-finished goods |
| P03.T4 | Assembly items |
| P03.T5 | Finished goods |
| P03.T6 | Packaging materials |
| P03.T7 | Consumables |
| P03.T8 | Scrap |
| P03.T9 | Return stock |
| P03.T10 | QC hold stock |

**Stock status, maintained per item**

| ID | Status |
|---|---|
| P03.S1 | On-hand |
| P03.S2 | Available |
| P03.S3 | Reserved |
| P03.S4 | Allocated |
| P03.S5 | QC hold |
| P03.S6 | In transit |
| P03.S7 | Job work issued |
| P03.S8 | Damaged |
| P03.S9 | Scrap |

**Features**

| ID | Feature |
|---|---|
| P03.F1 | Multi-warehouse stock |
| P03.F2 | Stock ledger |
| P03.F3 | Stock transfer |
| P03.F4 | Stock adjustment |
| P03.F5 | Bin / location support |
| P03.F6 | Batch / serial where required |
| P03.F7 | Stock reservation & release |
| P03.F8 | Stock reallocation |
| P03.F9 | Physical stock verification |
| P03.F10 | Inventory audit trail |

## P04: Phase 04, product engineering & BOM (p.8–9)

**Product master**

| ID | Field |
|---|---|
| P04.M1 | Product code |
| P04.M2 | Product name |
| P04.M3 | Category |
| P04.M4 | Size |
| P04.M5 | Finish |
| P04.M6 | Color |
| P04.M7 | Variant |
| P04.M8 | HSN / GST |
| P04.M9 | Sale price |
| P04.M10 | Dealer price |
| P04.M11 | Production type |

**BOM management**

| ID | Item |
|---|---|
| P04.B1 | Single-level BOM |
| P04.B2 | Multi-level BOM |
| P04.B3 | Alternate BOM |
| P04.B4 | BOM versioning |
| P04.B5 | BOM approval |
| P04.B6 | BOM costing |
| P04.B7 | Common part calculation |

- P04.EX: Multi-level example. Premium Lock Kit = Rose handle + Lock body + Cylinder + Keys + Screw kit + Packing box. Rose handle = Handle body + Spring + Bush + Screw. Lock body = Lock case + Spring + Pin + Screw.
- P04.CP: Common parts. Spring and screw appear across sub-assemblies, and the system merges common requirements before planning.

**Routing**

| ID | Item |
|---|---|
| P04.R1 | Operation sequence |
| P04.R2 | Work center |
| P04.R3 | Machine |
| P04.R4 | Standard time |
| P04.R5 | Setup time |
| P04.R6 | Process cost |

- P04.CON: Consolidation rule. For an order of 100 Premium Lock Kits, 50 Rose Handles and 25 Lock Bodies, spring is calculated together, never as duplicate requirements.

## P05: Phase 05, allocation & reallocation engine (p.10)

**Reservation types**

| ID | Type |
|---|---|
| P05.R1 | Sales |
| P05.R2 | Production |
| P05.R3 | Assembly |
| P05.R4 | Job work |
| P05.R5 | Service / spare |
| P05.R6 | Soft |
| P05.R7 | Hard |

**Allocation priority (checked in this order)**

| ID | Source |
|---|---|
| P05.P1 | Finished goods |
| P05.P2 | Assembly stock |
| P05.P3 | Semi-finished goods |
| P05.P4 | Components |
| P05.P5 | Raw materials |
| P05.P6 | Cross-warehouse stock |
| P05.P7 | Purchase requirement |
| P05.P8 | Manufacturing requirement |
| P05.P9 | Job work requirement |

**Reallocation scenario: an urgent order needs stock reserved for production**

| ID | Step |
|---|---|
| P05.S1 | Check current reservation |
| P05.S2 | Check production start date |
| P05.S3 | Check priority |
| P05.S4 | Check material issue status |
| P05.S5 | Suggest reallocation |
| P05.S6 | Take approval |
| P05.S7 | Release old reservation |
| P05.S8 | Allocate to urgent order |
| P05.S9 | Notify affected department |
| P05.S10 | Maintain audit log |

**Auto reallocation drivers**

| ID | Driver |
|---|---|
| P05.D1 | Customer priority |
| P05.D2 | Delivery date |
| P05.D3 | Sales order priority |
| P05.D4 | Production priority |
| P05.D5 | Work order status |
| P05.D6 | Material issue status |
| P05.D7 | Manager approval rules |

## P06: Phase 06, MRP engine (p.11)

- P06.FLOW: Sales Order → Check Finished Goods → Explode BOM → Merge Common Parts → Check Inventory → Check Reserved → Check Incoming PO → Check Production → Calculate Shortage → Generate Suggestions.

**Outputs**

| ID | Output |
|---|---|
| P06.O1 | Manufacturing order suggestion |
| P06.O2 | Purchase requisition suggestion |
| P06.O3 | Job work order suggestion |
| P06.O4 | Stock transfer suggestion |
| P06.O5 | **Reallocation suggestion** |

**Features**

| ID | Feature |
|---|---|
| P06.F1 | BOM explosion |
| P06.F2 | Multi-level demand calculation |
| P06.F3 | Common component grouping |
| P06.F4 | Shortage report |
| P06.F5 | Auto purchase planning |
| P06.F6 | Auto production planning |
| P06.F7 | Lead time calculation |
| P06.F8 | Expected availability date |

## P07: Phase 07, procurement & vendor management (p.12)

**Modules**

| ID | Module |
|---|---|
| P07.M1 | Purchase requisition |
| P07.M2 | RFQ |
| P07.M3 | Vendor quotation |
| P07.M4 | Quotation comparison |
| P07.M5 | Purchase order |
| P07.M6 | GRN |
| P07.M7 | Incoming QC |
| P07.M8 | Purchase return |
| P07.M9 | Vendor invoice |
| P07.M10 | Vendor payment |
| P07.M11 | Vendor ledger |
| P07.M12 | Vendor rating |
| P07.M13 | Lead time tracking |

**Vendor types**

| ID | Type |
|---|---|
| P07.V1 | Raw material supplier |
| P07.V2 | Component supplier |
| P07.V3 | Packing material supplier |
| P07.V4 | Job work vendor |
| P07.V5 | **Transport vendor** |
| P07.V6 | **Service vendor** |

## P08: Phase 08, production management (p.13)

**Modules**

| ID | Module |
|---|---|
| P08.M1 | Manufacturing order |
| P08.M2 | Production planning |
| P08.M3 | Work order |
| P08.M4 | Material issue |
| P08.M5 | Material return |
| P08.M6 | Machine allocation |
| P08.M7 | Operator allocation |
| P08.M8 | Shift planning |
| P08.M9 | Daily production entry |
| P08.M10 | WIP tracking |
| P08.M11 | Production completion |
| P08.M12 | Production costing |

**Statuses**

| ID | Status |
|---|---|
| P08.S1 | Draft |
| P08.S2 | Approved |
| P08.S3 | Released |
| P08.S4 | Material issued |
| P08.S5 | In process |
| P08.S6 | Completed |
| P08.S7 | Closed |
| P08.S8 | Cancelled |

## P09: Phase 09, assembly line management (p.14)

| ID | Module |
|---|---|
| P09.M1 | Assembly order |
| P09.M2 | Assembly BOM |
| P09.M3 | Component issue |
| P09.M4 | Assembly tracking |
| P09.M5 | Assembly QC |
| P09.M6 | Assembly completion |
| P09.M7 | Packing handover |

- P09.EX: A lock set assembly consumes Handle, Lock body, Cylinder, Keys, Screw kit and Packing box. The system consumes the components and creates finished assembly stock.

## P10: Phase 10, job work management (p.14)

Processes in scope: polishing, coating, PVD, laser marking, machining.

| ID | Module |
|---|---|
| P10.M1 | Job work order |
| P10.M2 | Material issue to vendor |
| P10.M3 | Job work challan |
| P10.M4 | Vendor process tracking |
| P10.M5 | Material receipt from vendor |
| P10.M6 | Short / excess receipt |
| P10.M7 | Job work QC |
| P10.M8 | Rejection / rework |
| P10.M9 | Job work invoice |
| P10.M10 | Job work vendor ledger |

## P11: Phase 11, quality control (p.15)

**QC types**

| ID | Type |
|---|---|
| P11.T1 | Incoming QC |
| P11.T2 | In-process QC |
| P11.T3 | Job work QC |
| P11.T4 | Assembly QC |
| P11.T5 | Final QC |
| P11.T6 | Return QC |

**Results**

| ID | Result |
|---|---|
| P11.R1 | Accepted |
| P11.R2 | Rejected |
| P11.R3 | Rework |
| P11.R4 | Hold |
| P11.R5 | Scrap |

**Features**

| ID | Feature |
|---|---|
| P11.F1 | QC checklist |
| P11.F2 | Parameter-based inspection |
| P11.F3 | Photo upload |
| P11.F4 | Rejection reason |
| P11.F5 | Rework tracking |
| P11.F6 | Vendor quality report |
| P11.F7 | Product quality history |

## P12: Phase 12, packing & dispatch (p.15)

Objective: final packing, label printing, carton management, dispatch, and invoice handover.

| ID | Module |
|---|---|
| P12.M1 | Packing order |
| P12.M2 | Packing material issue |
| P12.M3 | Box / label / barcode generation |
| P12.M4 | Packing list |
| P12.M5 | Carton tracking |
| P12.M6 | Dispatch planning |
| P12.M7 | Transport / courier assignment |
| P12.M8 | LR details |
| P12.M9 | Delivery confirmation |
| P12.M10 | Invoice handover (from the objective) |

## P13: Phase 13, finance integration (p.16)

| ID | Module |
|---|---|
| P13.M1 | Sales ledger |
| P13.M2 | Purchase ledger |
| P13.M3 | Vendor ledger |
| P13.M4 | Customer ledger |
| P13.M5 | Job work cost |
| P13.M6 | Production cost |
| P13.M7 | Inventory valuation |
| P13.M8 | Outstanding report |
| P13.M9 | GST report |
| P13.M10 | Payment collection |
| P13.M11 | Payment follow-up |

## P14: Phase 14, reports & dashboards (p.16)

| ID | Report |
|---|---|
| P14.1 | Sales order status |
| P14.2 | Stock availability |
| P14.3 | Reserved vs available |
| P14.4 | MRP shortage |
| P14.5 | Purchase pending |
| P14.6 | Production pending |
| P14.7 | Job work pending |
| P14.8 | QC rejection |
| P14.9 | Dealer performance |
| P14.10 | Vendor performance |
| P14.11 | Product profitability |
| P14.12 | Fast moving products |
| P14.13 | Slow moving products |
| P14.14 | Dead stock |
| P14.15 | Order delay |
| P14.16 | Machine utilization |
| P14.17 | Production efficiency |
| P14.18 | Inventory valuation |
| P14.19 | Dashboards (management-level visibility) |

## A01: Add-on 01, notification system (p.17)

Admin panel notifications:

| ID | Notification |
|---|---|
| A01.A1 | New purchase request |
| A01.A2 | New vendor order |
| A01.A3 | PO approved |
| A01.A4 | PO rejected |
| A01.A5 | Material received |
| A01.A6 | QC pending |
| A01.A7 | QC rejected |
| A01.A8 | Job work material sent |
| A01.A9 | Job work material received |
| A01.A10 | Sales order created |
| A01.A11 | Production shortage |
| A01.A12 | Stock reallocation request |
| A01.A13 | Dispatch created |
| A01.A14 | Courier tracking updated |
| A01.A15 | Payment pending |
| A01.A16 | Vendor delay |

Mobile app notifications:

| ID | Notification |
|---|---|
| A01.M1 | PO assigned |
| A01.M2 | PO approved |
| A01.M3 | Order dispatched |
| A01.M4 | Courier name & tracking ID |
| A01.M5 | Delivery expected date |
| A01.M6 | Payment reminder |
| A01.M7 | Material rejected in QC |
| A01.M8 | Job work pending |
| A01.M9 | New order request |
| A01.M10 | Approval request |

Channels:

| ID | Channel |
|---|---|
| A01.C1 | In-app notification |
| A01.C2 | Push notification |
| A01.C3 | Email notification |
| A01.C4 | WhatsApp / SMS (marked "future" in the PDF) |

## A02: Add-on 02, vendor purchase mobile app (p.18)

The problem it solves: two or three managers at one vendor cause duplicate-PO confusion, so POs are separated by role and category.

**Vendor user roles**

| ID | Role |
|---|---|
| A02.R1 | Vendor Owner |
| A02.R2 | Vendor Manager |
| A02.R3 | Dispatch Manager |
| A02.R4 | Account Manager |
| A02.R5 | QC Contact Person |

- A02.CM: Category mapping. Each vendor user is mapped to categories (Manager 1 → Lock Body, Manager 2 → Handles, Manager 3 → Packaging Material).
- A02.CS: Category-wise PO separation. A requirement for 100 lock bodies, 200 handles and 500 packing boxes becomes PO-001, PO-002 and PO-003, each visible only to its assigned manager.

## A03: Add-on 03, duplicate purchase order control (p.19)

**Rules checked before a PO is created**

| ID | Rule |
|---|---|
| A03.R1 | Same vendor |
| A03.R2 | Same item / category |
| A03.R3 | Same requirement date |
| A03.R4 | Same sales order / MRP reference |
| A03.R5 | Same pending purchase request |
| A03.R6 | Same BOM demand |
| A03.R7 | Same warehouse requirement |

- A03.P: System prompt: "This purchase requirement already exists in PO-001. Merge, increase quantity, or create a separate PO with approval?"

**Options**

| ID | Option |
|---|---|
| A03.O1 | Merge with existing PO |
| A03.O2 | Increase existing PO quantity |
| A03.O3 | Create new PO with approval |
| A03.O4 | Cancel duplicate request |

## A04: Add-on 04, purchase order status flow (p.19)

| ID | Step |
|---|---|
| A04.1 | Purchase Requirement |
| A04.2 | Vendor Selection |
| A04.3 | Category-wise PO |
| A04.4 | Manager Assignment |
| A04.5 | Vendor Notification |
| A04.6 | Accept / Reject |
| A04.7 | Delivery Date |
| A04.8 | Dispatch |
| A04.9 | Courier Details |
| A04.10 | Received |
| A04.11 | Incoming QC |
| A04.12 | GRN |
| A04.13 | Vendor Invoice |
| A04.14 | Payment |

## A05: Add-on 05, vendor mobile app features (p.19)

**Vendor dashboard**

| ID | Item |
|---|---|
| A05.D1 | Pending POs |
| A05.D2 | Accepted POs |
| A05.D3 | Ready for dispatch |
| A05.D4 | Dispatched orders |
| A05.D5 | Delivered orders |
| A05.D6 | Payment pending |
| A05.D7 | QC rejected items |

**PO detail screen**

| ID | Item |
|---|---|
| A05.P1 | PO number |
| A05.P2 | Item details |
| A05.P3 | Category |
| A05.P4 | Quantity & rate |
| A05.P5 | Delivery date |
| A05.P6 | Delivery address |
| A05.P7 | Terms & conditions |
| A05.P8 | Download PO PDF |

**Vendor actions**

| ID | Action |
|---|---|
| A05.A1 | Accept PO |
| A05.A2 | Reject with reason |
| A05.A3 | Update delivery date |
| A05.A4 | Mark ready for dispatch |
| A05.A5 | Add courier / transport |
| A05.A6 | Upload invoice |
| A05.A7 | Upload dispatch photo |
| A05.A8 | Upload LR / receipt |
| A05.A9 | View QC result |
| A05.A10 | View payment status |

## A06: Add-on 06, dispatch tracking (p.20)

Fields captured at dispatch:

| ID | Field |
|---|---|
| A06.1 | Courier name |
| A06.2 | Transport name |
| A06.3 | Tracking ID |
| A06.4 | LR number |
| A06.5 | Vehicle number |
| A06.6 | Driver name |
| A06.7 | Driver mobile |
| A06.8 | Dispatch date |
| A06.9 | Expected delivery date |
| A06.10 | Boxes / cartons |
| A06.11 | Weight |
| A06.12 | Freight amount |
| A06.13 | Attachment / photo |

- A06.EX: Example: "Blue Dart · Tracking 123456789 · Dispatched 29-06-2026 · Expected 02-07-2026 · 5 boxes · 70 KG · LR copy uploaded". The **admin is notified immediately**.

## A07: Add-on 07, admin dispatch tracking (p.20)

Fields in the admin tracking view:

| ID | Field |
|---|---|
| A07.V1 | Vendor name |
| A07.V2 | PO number |
| A07.V3 | Courier / transport |
| A07.V4 | Tracking ID |
| A07.V5 | Current status |
| A07.V6 | Expected delivery |
| A07.V7 | Delay alert |
| A07.V8 | Received status |
| A07.V9 | QC pending |
| A07.V10 | GRN pending |

Statuses:

| ID | Status |
|---|---|
| A07.S1 | Not dispatched |
| A07.S2 | Ready for dispatch |
| A07.S3 | Dispatched |
| A07.S4 | In transit |
| A07.S5 | Delivered |
| A07.S6 | Received at warehouse |
| A07.S7 | QC pending |
| A07.S8 | GRN completed |

## A08: Add-on 08, smart alerts (p.21)

| ID | Alert |
|---|---|
| A08.1 | PO not accepted within 24 hours |
| A08.2 | Vendor delivery date missed |
| A08.3 | Dispatch not updated |
| A08.4 | Tracking ID missing |
| A08.5 | Material received but QC pending |
| A08.6 | QC rejected |
| A08.7 | GRN not created |
| A08.8 | Vendor invoice pending |
| A08.9 | Payment overdue |

## A09: Add-on 09, reports (p.21)

Purchase reports:

| ID | Report |
|---|---|
| A09.P1 | Pending PO |
| A09.P2 | Vendor-wise PO |
| A09.P3 | Category-wise PO |
| A09.P4 | Delayed PO |
| A09.P5 | Duplicate PO warning |
| A09.P6 | Vendor acceptance |
| A09.P7 | Delivery performance |

Dispatch reports:

| ID | Report |
|---|---|
| A09.D1 | In-transit material |
| A09.D2 | Expected today |
| A09.D3 | Late delivery |
| A09.D4 | Courier-wise tracking |
| A09.D5 | Transport-wise cost |
| A09.D6 | Vendor dispatch history |

## A10: Add-on 10, development priority (p.21)

Must have:

| ID | Item |
|---|---|
| A10.M1 | Admin notification |
| A10.M2 | Mobile push notification |
| A10.M3 | Vendor user roles |
| A10.M4 | Category-wise access |
| A10.M5 | PO assignment |
| A10.M6 | PO accept / reject |
| A10.M7 | Expected delivery date |
| A10.M8 | Courier name & tracking ID |
| A10.M9 | Dispatch status |
| A10.M10 | Admin tracking screen |

Should have:

| ID | Item |
|---|---|
| A10.S1 | Duplicate PO prevention |
| A10.S2 | Merge PO option |
| A10.S3 | Vendor invoice upload |
| A10.S4 | LR / photo upload |
| A10.S5 | Delay alerts |
| A10.S6 | QC result visible to vendor |

Future enhancement (the user now wants these present too):

| ID | Item |
|---|---|
| A10.F1 | WhatsApp notification |
| A10.F2 | Courier API integration |
| A10.F3 | Auto tracking status |
| A10.F4 | Vendor rating |
| A10.F5 | Vendor payment dashboard |
| A10.F6 | Digital signature on PO |

## K: Key business scenarios covered (p.22)

| ID | Scenario |
|---|---|
| K01 | Direct finished-goods sale. Dealer orders 100 handles; FG stock is checked, reserved and dispatched. |
| K02 | Finished goods not available. Dealer orders 500 handles with only 120 available; the system allocates 120 and **generates a production plan for the remaining 380**. |
| K03 | Same part sold and used in a BOM. One stock, multiple usage roles. |
| K04 | Common parts in a multi-level BOM. Spring and screw are merged before purchase or production planning. |
| K05 | Urgent order reallocation. Parts reserved for future production are needed by an urgent dealer order; the system suggests reallocation with approval. |
| K06 | Packing material shortage. The finished product is ready but the box is unavailable; **the system creates a purchase request for packing material**. |
| K07 | Job work vendor process. Material goes to a vendor for coating; sent, received, rejected and pending quantities are tracked. |
| K08 | Partial dispatch. A customer orders 500 with 200 available; the system dispatches 200 and **plans the balance 300**. |
| K09 | Quality rejection. Received material fails QC; the system moves it to rejected stock and **starts a vendor return / replacement process**. |
| K10 | Multi-warehouse allocation. If one warehouse is short, the system checks another and **suggests a stock transfer**. |

## N: Non-software sections (verify only that they need no software)

- N08: Payment terms. ₹5,70,000 + GST, with the module-set payment schedule.
- N09: Terms, support & warranty. 30-day bug-fix support, AMC, source handover.
- N10: Conclusion. Its 12 most important modules: Inventory Engine, Multi-role Item Master, Multi-level BOM, Common Parts Calculation, Allocation & Reallocation, MRP Planning, Production Management, Assembly Line, Job Work, Quality Control, Packing & Dispatch, Finance & Reports.

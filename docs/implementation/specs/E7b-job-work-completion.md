# WP E7b: Job work completion

Lane A, round 4, step 2 (after A4). Placeholder prefix `§E7b-n`.

**Scope:** detail and timeline, receive options, rework, challans and their PDF, vendor material ledger, vendor-app job work, cost capitalisation.

**Checklist IDs to close:** P10.M3, P10.M4, P10.M6, P10.M8, P10.M10, P13.M5 (capitalisation), A01.M8, K07. Read each in `trace/trace.json`.

**Already on main:**
- B3a (job-work invoice, recoveries)
- E4 (the QC owner callback for job work)
- C1a/C2a (vendor portal and app)

## Decisions (binding)

### 1. Order detail and timeline (P10.M4, K07)

- Add a job-work order detail page, `/job-work/:orderId`. Add it to the sweeps and route-loaders.
- The detail shows:
  - lines: issued, received output, **rejected**, scrap, still at vendor;
  - a timeline: approved → each challan → each receipt → QC → closed;
  - the reconciliation table (`GET /job-work/{id}/reconciliation`);
  - charges, billed and recovered amounts (from B3a);
  - linked QC inspections.
- Add `rejected_quantity` and `scrap_quantity` to the `JOB_WORK_PENDING` report row.

### 2. Receive options (P10.M6, P10.M8)

`JobWorkReceiveDialog` gains, per line:
- **Close line / write off shortfall** (`is_final`). It shows the computed loss against the allowance.
- **Approve excess loss (debit vendor)**, visible to `job_work.close` holders.
- **Rejection action:** RETURN_TO_STOCK, SCRAP_AT_VENDOR or REWORK_AT_VENDOR, plus a **reason**.
- The vendor challan date and the transporter.

Also:
- Fix the wrong Alert copy.
- Show the 2% over-receipt tolerance.

### 3. Rework order (P10.M8)

Add "Send for rework", available from a rejected receipt line and from DAMAGED stock of that item. It creates a job-work order with:
- `source_type = REWORK` (add it to the enum and its CHECK through a migration);
- `source_id` = the receipt;
- the same vendor, or one the user picks.

If the client wants charge-backs for rejected output, add a vendor debit through B3a's recovery mechanism behind a setting that defaults to off (`§E7b-n`).

### 4. Challans (P10.M3)

- **Register:** `GET /job-work/challans`, company-wide, paginated, with vendor, date and order filters. It appears as a "Challans" tab.
- **Delivery challan PDF** (GST Rule 55):
  - consignor and consignee name, address and GSTIN;
  - challan number and date;
  - HSN, description, quantity, UoM and taxable value per line;
  - the purpose "Sent for job work — not for sale";
  - vehicle and transporter;
  - a signature block.
- Build it with the reports PDF pipeline: a `JOB_WORK_CHALLAN` document-only key, its CHECK-swap migration and a task.
- Add a Download button.
- **ITC-04 export (optional):** an `ITC04` report key covering goods sent to and received from job workers per quarter. Build it if time allows, otherwise record it in `§E7b-n`.

### 5. Vendor material ledger (P10.M10)

- Add `GET /job-work/vendor-ledger?vendor_id&date_from&date_to`, returning per item: opening at vendor, issued (challans), received (output), returned, scrap, closing. Also return charges accrued against billed and debited amounts, from B3a.
- Show it in a "Vendor ledger" tab with a vendor picker, period and XLSX export (via the report job pipeline, or client CSV if simpler).

### 6. Vendor app job work (A01.M8)

**Portal endpoints:**
- `GET /vendor-portal/job-work` and `GET /vendor-portal/job-work/{id}`: own orders only, with a new right `vendor_portal.job_work` granted to OWNER and MANAGER, plus a grant migration.
- The response includes challans, issued/received/pending quantities, `expected_return_date` and the QC outcome.
- **Vendor-reported status:** `POST /vendor-portal/job-work/{id}/status` with `{status: IN_PROCESS | READY_TO_RETURN | DISPATCHED_BACK, note, expected_date?}`. It is stored as tracking events on the order. It **does not move stock**: receipts stay staff-side.

**Vendor app (`/vendor/job-work`):**
- list and detail screens, with the status-update sheet;
- a dashboard tile for "Job work pending".

**Notifications:**
- The job-work vendor's users are notified when a challan is issued ("Job work pending") and when it becomes overdue (B4's rule gains vendor recipients; edit it here if B4 is on main).

### 7. Cost capitalisation (P13.M5)

- On a job-work receipt, the output `JOB_WORK_RECEIPT` movement carries `unit_cost` = (FIFO value of the input consumed for that output + the receipt charge) / good output quantity.
- Read the input value from the issue legs. Record any rounding residual.
- Add the report `JOB_WORK_COST`: vendor, process, order, input cost, charges, debits, and cost per unit.
- Record `§E7b-n` (charges capitalised at receipt; the later invoice price variance is expensed).

## Tests

**Integration**
- Detail and reconciliation
- `is_final` closes a short line
- Excess-loss approval
- Each rejection action
- Rework order linkage
- Challan register, and the PDF render
- Vendor ledger sums
- Vendor portal job-work scope (another vendor gets 404)
- Vendor status update creates events and moves no stock
- Capitalised `unit_cost` on receipt
- `JOB_WORK_COST` report

**Frontend**
- The receive dialog request body
- The detail page
- The vendor app job-work screens

## Docs

- Handbook: Job work chapter, Vendor app
- `database.md`
- `assumptions.md`

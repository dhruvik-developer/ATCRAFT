# WP E10b: Finance and reports completion

Lane A, round 7, step 1. Placeholder `§E10b-n`.

**Scope:** GL account statement, trial balance, registers, GSTR-1/3B, report permissions and export, actual profitability, management dashboard.

**Checklist IDs:** P13.M1, P13.M2 (the register/statement part), P13.M9 (GSTR sections and export), P14.11, P14.19, S02.14 (export below 5,000 rows; PDF export; report authorisation), G8 notes 4 and 5. Read each one in `trace/trace.json`.

**Already on main:** all finance documents (A2, A3, B3a, E6b debit notes), collections (A4), every report key added so far.

## Decisions (binding)

### 1. GL account statement and trial balance (P13.M1, P13.M2)

**Account statement.** `GET /finance/ledger/account/{account_id}?date_from&date_to` (`ledger.view`) returns:
- an opening balance;
- entries with a running balance, keyset paginated, ordered by `(entry_date, id)`;
- closing totals;
- cost-centre and party columns.

It uses `ix_ledger_entries_account_date`. The accounts page gets a "Ledger" drill-down.

**Trial balance.** `GET /finance/trial-balance?as_of` (`ledger.view`) returns per account: opening, period debit, period credit, closing. It is grouped by account type, and the totals must balance (a test asserts it). There is also an XLSX export through the job pipeline. New page `/finance/trial-balance`, added to the sweeps.

**Registers.** These are report keys, each with a CHECK-swap:
- `SALES_REGISTER`: invoices and credit notes per period, grouped by month × customer × HSN with taxable, CGST/SGST/IGST and total. This is the "Sales ledger" register.
- `PURCHASE_REGISTER`: vendor invoices (purchase, job-work and service) and debit notes, the same shape. This is the "Purchase ledger".

### 2. GST returns (P13.M9)

`GSTR1` export, as a document-only XLSX via the report job pipeline:
- B2B invoice-wise: buyer GSTIN, POS, invoice number/date/value, rate-wise taxable and tax;
- B2C large / B2C small summaries, with thresholds as named constants that cite the rule;
- CDNR from credit notes;
- HSN summary;
- document summary (series ranges).

`GSTR3B` summary:
- 3.1 outward supplies from invoices minus credit notes;
- 4 ITC from approved vendor/job-work invoices minus debit notes.

Both come from **posted documents only**, by document date.

ASSUMPTION `§E10b-n`: these are preparation aids for the client's GST filing, not a GSP integration. Exports, SEZ and reverse charge are out of scope until specified.

### 3. Report permissions and export (S02.14, G8 #4)

**Per-report permission.**
- `ReportDefinition.permission`, default `report.view`.
- Reports exposing cost or value require `item.cost_view` and/or `inventory.valuation_view`: PRODUCT_PROFITABILITY, INVENTORY_VALUATION, the valuation columns of STOCK_AVAILABILITY and DEAD_STOCK, PRODUCTION_COST, JOB_WORK_COST.
- Finance registers and GST require `gst_report.view` or `finance.view` as appropriate.
- `GET /reports/catalogue` is **filtered per caller**.
- Routes enforce the permission. Test both: hidden in the catalogue, and 403 on direct call.

**Export everywhere.**
- Every report gets **Export XLSX** and **Export PDF** for any result size, through the job pipeline (`report.export`).
- The existing above-5,000 path remains.
- Add a generic tabular PDF renderer (landscape, repeating header, page numbers, filters printed).

### 4. Actual profitability (P14.11)

PRODUCT_PROFITABILITY becomes actual:
- revenue from issued invoice lines net of credit notes (A2/A3), falling back to dispatched lines for uninvoiced dispatches (flagged);
- COGS from DISPATCH inventory transactions at FIFO value, net of customer returns.

The cost columns are gated as in section 3.

### 5. Management dashboard (P14.19)

**Add tiles** (each permission-gated):
- payables due / overdue;
- invoiced sales and collections month-to-date;
- MRP shortage count from the last run;
- job-work overdue;
- inbound POs late (C1a's predicate);
- receivables (A4 already).

**30-day trend endpoint.** `GET /reports/dashboard/trends` covers sales invoiced, collections, dispatches and production output per day. Build it from summary tables or one aggregate per series; never a per-day query loop. Show sparklines.

**Fix** the mislabelled "POs awaiting approval" tile. It must count PENDING_APPROVAL, or be relabelled.

**Cache.** Bump the dashboard cache key, and give every new field a default (trap 17).

## Tests

**Integration:**
- statement running balance and opening;
- trial balance balances;
- registers sum;
- GSTR-1 sections from seeded invoices, credit notes and debit notes;
- GSTR-3B nets;
- catalogue filtering and 403;
- export jobs for a small report (XLSX and PDF);
- profitability uses FIFO COGS;
- trends.

**Frontend:** trial balance page; export buttons (job flow); dashboard tiles gating.

## Docs

- Handbook: Finance and Reports chapters.
- `database.md`.
- `assumptions.md`.

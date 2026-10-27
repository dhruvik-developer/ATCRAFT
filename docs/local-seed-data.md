# Local testing seed data

[`apps/api/scripts/seed_local.py`](../apps/api/scripts/seed_local.py) is the single
entry point for populating a local test installation. It reuses the existing
baseline and demonstration seed stages and the normal business services.
Inventory, financial journals, document states and audit records are produced
through those services rather than independent SQL fixtures.

## Run locally

Follow the [README setup](../README.md) to install dependencies, create `.env`
and start PostgreSQL and Redis. The configured database must already exist.
`DATABASE_URL` and `DATABASE_SYNC_URL` must identify the same local PostgreSQL
host, port and database; `APP_ENV` must be `development` or `test`.

From the repository root on Windows:

```powershell
.\make.ps1 seed-local
.\make.ps1 dev-api
```

Start the frontend in another terminal:

```powershell
.\make.ps1 dev-web
```

On macOS/Linux, use `make seed-local` and the normal `make dev` setup. Open
<http://localhost:5173>, sign in as `admin@atcraft.example` with the generated
password displayed by the seed, then change it when prompted. A repeated seed
preserves the account's current password.

To supply your own administrator details, run the same file from `apps/api`:

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_local --admin-email admin@atcraft.example --admin-password 'ChooseYourOwnStrongPassword!'
```

Use your own password in that example. The seed does not reset an existing
account's password. Other demonstration staff and portal accounts can be
managed through the administrator's user screen.

The command upgrades the configured database to the current migration head,
loads baseline data, then fills the demonstration stages in dependency order.
It refreshes the seeded company's stock-report summaries after stock changes;
an unchanged rerun preserves the existing summaries and their audit history.
It never drops or resets the database. New demo records use `DM-` codes or
deterministic notes so a repeated run reuses them. Individual services commit
their own complete business operations; a failed run may leave earlier completed
documents and can be retried after the reported problem is corrected.

During seeding, email and push delivery are disabled and queued work uses an
isolated in-memory broker. These settings apply only to the seed process, not
your `.env` or normal API/worker processes. The final phase runs the existing
report tasks for the seed's own queued invoices and credit notes, so those PDF
downloads work without an external worker. Missing credit-note jobs are queued
through the normal finance service. Failed attempts are regenerated through a
new job; an actively running job must finish before the command can report
success. Use the normal local worker for
new background jobs raised while testing:

```powershell
.\make.ps1 dev-worker
```

## Dataset coverage

| Area | Examples |
|---|---|
| Organisation and access | Company, branch, plant, current financial year, role/permission catalogue, staff, departments and cost centres |
| Product and warehouse masters | Units/conversions, categories, attributes, brands, manufacturers, items/variants, prices, warehouses and locations; typed specification masters, inherited category templates and product accessories |
| Trading partners | Customers, dealers, addresses/contacts, vendors, vendor qualifications, transporters and portal users |
| Engineering | Work centres, machines, shifts, routings, approved BOMs and QC templates |
| Procurement | Requisitions, RFQs, competing quotations, purchase orders, shipments, posted receipts and purchase returns |
| Factory operations | Production/work orders, material issues, output, assembly, job work, inspections and rework |
| Named-unit workflows | Two dedicated serial SKUs; same-item vendor-held rework and a mixed accepted/held QC lot; four SKU conversions retaining the physical label; FINAL QC and a sealed, completed serial carton |
| Inventory | Ledger/balances, batches, serial units, bin putaway, scrap/disposal, internal issues, transfers, adjustments and physical counts |
| Sales and fulfilment | Orders at several stages, reservations, MRP, packing/cartons, planned/dispatched/delivered consignments |
| Finance and returns | Account chart, journals, vendor/sales invoices, payments/allocations, sales returns and credit notes |
| Documents and reports | Uploaded sample files, generated invoice/credit PDFs and report jobs; live reports read the seeded transactions |
| Portals and notifications | Dealer offers/cart/wishlist/order request, vendor interactions, in-app notifications and audit history |

The seed includes supported workflows and representative lifecycle states. It
does not fabricate credentials, provider webhooks, MFA enrollment, every possible
stock grain or unsupported serial-component assembly. External courier, mail,
push and signature services still require their own configured test accounts.
The final report lists any deliberately unavailable scenario or operational
failure; investigate unexpected skips rather than treating them as coverage.

For serial testing, search job-work notes for `DM-JW-SERIAL-SAME` (a partial
receipt with one unit still at the vendor and one on QC hold) or
`DM-JW-SERIAL-CONVERT` (four processed units with their original physical labels).
Items `DM-FG-SERIAL-BODY` and `DM-FG-SERIAL-PVD` show the two SKU identities;
labels begin with `DM-SERIAL-SAME-` or `DM-SERIAL-CONVERT-`. Either side of a
conversion opens the joined movement history. Packing notes
`DM-PACK-SERIAL-FINAL` identify the completed carton containing two converted
units after FINAL inspection. These scenarios use the ordinary services and
resume on rerun; they never replenish units that have already been issued.

For technical descriptions, open Masters → Specifications and search `DM-SPEC`.
The seven demo attributes cover Material, Lock Type, Backset, Keys Included,
Warranty, Fire Rated and Mounting, exercising OPTION, NUMBER, BOOLEAN and TEXT.
The handle and mortise-lock categories have required templates; finished goods
`DM-FG-LEVER-AB`, `DM-FG-LEVER-SS`, `DM-FG-MORTISE` and `DM-FG-DIGILOCK` carry
representative values. Handles deliberately store zero keys and false fire
rating so blank-value handling can be tested. Each demo handle links
`DM-ACC-SCREW-KIT` as INCLUDED and `DM-ACC-ROSE-SS` as OPTIONAL; the latter has a
dealer price and can be ordered separately. Associations do not move stock.

The specifications stage runs after masters. Its first successful transaction
writes an audited initialization marker; subsequent runs preserve even deliberate
clears, removals, ordering changes and retirements instead of reinstalling the
defaults. Pre-existing attributes with changed types/options remain untouched;
incompatible defaults are skipped and missing required values remain visible as
warnings. A failed initial stage rolls back its descriptions and marker together,
so the corrected stage can be retried.

## Verification

Earlier verification, before the serial-job-work extensions, used freshly
migrated disposable PostgreSQL databases. An
interrupted-PDF scenario marked one queued job failed through the report
service, then ran the complete command to recover it. The result contained two
sales-invoice PDFs, two credit-note PDFs and 19 stock-summary rows. A further
run preserved all 136 table counts, complete stock/finance/summary contents,
all 11 stored-file hashes and every user's password hash. The historical failed
job remained recorded beside its successful replacement. All 22 seeded journals
balanced. The complete fresh-install CLI regression also passed independently.

The automated fresh-install regression runs the single command twice from an
empty database with a custom company and administrator. It checks the populated
fixture areas, completed PDF references, unchanged table/file counts and
preserved existing credentials. No external SMTP, push or Redis broker is
required for this seed. The source development database is not reset by these
checks.

The 25 October release rehearsal upgraded a separate database through
`20261031_0100` and loaded the extended dataset, including catalogue images,
published promotions and named-unit job work/QC/packing. Its final unchanged
rerun registered 342 references across 51 kinds and created zero records in
every stage. The dedicated serial-workflow integration test also verifies that
rerunning preserves serial identities, conversion history, stock ledgers,
audit rows, QC decisions and packing contents. This extends the earlier
verification; its historical table and file counts above are not current
dataset totals.

The complete local CLI regression also passed against the current source in
230.23 seconds: a fresh database, custom company and administrator, generated
PDFs, and a second full invocation preserved all table counts, stored-file
hashes and the administrator's existing password.

The D5-specific PostgreSQL regression adds four cases: a complete first seed
with all four types and both accessory relations; identical rerun snapshots;
preserved operator edits/clears/retirement/removal and pre-existing changed
masters; and atomic rollback/retry after a simulated late failure. All four
pass. Snapshot checks include product, audit, BOM and inventory tables, proving
the descriptive stage creates no stock movements or BOM rows. The combined
release's fresh CLI regression passed in 186.45 seconds, including generated
PDFs and an unchanged second invocation. The canonical local database was also
upgraded and seeded without resetting existing credentials; its unchanged rerun
registered 351 references across 52 kinds and created no replacement PDFs.

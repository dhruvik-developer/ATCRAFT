# WP B1 — Cost-centre master (+ ledger tagging) and company bank details
Lane B (operations), round 1. Placeholder prefix `§B1-n`.

Proposal: Phase 02 "Organization — … Cost center master". Audit: "A cost-centre master" absent.
Background: `reports/masters-pricing.md` §1, §2, "Conclusion B" and Traps 2–5. Department is the precedent.

## Hard boundary for this round
Lane C is reworking **procurement** this round. **Do not touch `src/modules/procurement/**` or the
procurement frontend** (requisition / PO carriers come in WP B3, round 3). Everything else below is yours.

## Decisions (binding)
1. **Master `cost_centres`** in `organization` (TenantMasterEntity): `code String(32)` (`validate_code`),
   `name String(255)` (`clean_name`), `parent_id` self-FK RESTRICT NULL indexed (a tree; max depth = a
   named constant equal to the department constant, reuse `assert_parent_allowed`), `cost_centre_type
   String(24)` reporting-only StrEnum `PRODUCTION | SERVICE | ADMINISTRATION | SELLING` with CHECK (nothing
   may branch on it — say so in database.md), `plant_id` FK plants RESTRICT NULL, `head_user_id` FK users
   SET NULL, `description Text NULL`, `is_active`. `uq_cost_centres_company_id_code`,
   `ix_cost_centres_company_active`, CHECK `parent_not_self`. Include the actor FKs in the migration
   (trap 5: migration 0900 forgot them).
2. **Lifecycle** `COST_CENTRE_LIFECYCLE = _lifecycle("Cost centre", Permission.COST_CENTRE_MANAGE)`.
   Archive (`DELETE ?reason=`) refused via `referenced_in_use` while: live children, departments,
   work centres, production orders not CLOSED/CANCELLED, job-work orders not CLOSED/CANCELLED, vendor
   invoices not CANCELLED/PAID… keep it simple: **any live row** in any carrier table except
   `ledger_entries` (posted history keeps it). Use the raw-SQL + `to_regclass` probe pattern
   (`organization/repository.py:61-125`) for tables owned by higher modules. Deactivation (is_active=false)
   only refused for live children and department/work-centre *defaults*; documents keep their reference.
3. **Public seam** for other modules: `require_cost_centre(session, tenant, cost_centre_id) -> CostCentre`
   (tenant + active; NotFoundError for foreign ids) and a batch `active_cost_centre_ids(session, tenant, ids)
   -> set[UUID]` for postings. Lower modules must not import higher ones — organization is the lowest, so
   everyone may import it.
4. **Carriers in this round** (all `cost_centre_id` nullable FK RESTRICT, indexed, validated tenant+active on
   write via the seam; precedence **explicit value → source document → NULL (unallocated)**, ASSUMPTION):
   - `departments.cost_centre_id` (a department's default cost centre — used later by requisitions) and
     `work_centers.cost_centre_id` (production cost pools). Validate in their services.
   - `production_orders.cost_centre_id` (explicit; material issues inherit through the order — document it).
   - `job_work_orders.cost_centre_id` (explicit).
   - `stock_adjustments.cost_centre_id` (explicit; write-offs).
   - `vendor_invoices.cost_centre_id` and `vendor_invoice_lines.cost_centre_id` (line default = header).
   - **Ledger:** `ledger_entries.cost_centre_id` (nullable FK, NOT VALID+VALIDATE, index
     `(company_id, cost_centre_id, entry_date) WHERE cost_centre_id IS NOT NULL`). `JournalLine` gains
     `cost_centre_id: UUID | None = None`. `post_journal` validates all cost-centre ids of a journal in ONE
     batch (tenant+active) and allows a cost centre **only on INCOME and EXPENSE accounts** (else
     ValidationError naming the line). `reverse_journal` copies `cost_centre_id` from each original leg and
     must succeed even if the cost centre was later deactivated/archived (follow-the-row: retiring must not
     make history irreversible — reversal validates tenant only, not active). `_post_invoice_journal` splits
     the PURCHASE_EXPENSE debit **per line cost centre** (group lines by effective cost centre; amounts sum to
     the subtotal exactly, Decimal). Existing callers that pass no cost centre are unaffected.
5. **Report `COST_CENTRE_EXPENSE`** ("Cost centre statement"): period required; rows per
   (cost centre, account) over posted `ledger_entries` with a cost centre, for INCOME and EXPENSE accounts:
   `cost_centre_code, cost_centre_name, account_code, account_name, account_type, debit_amount,
   credit_amount, net_amount` (+ ids). Search filter on cost-centre code/name. ReportKey + catalogue entry +
   route `/reports/cost-centre-expense` (`report.view`) + CHECK-swap migration listing ALL ReportKey values
   literally + frontend `ReportKey` union. Not cacheable.
6. **Company bank details** (for invoice PDFs in round 2): `companies.bank_name String(120)`,
   `bank_account_name String(255)`, `bank_account_number String(34)`, `bank_ifsc String(11)` (validated
   `^[A-Z]{4}0[A-Z0-9]{6}$`, upper-cased), `bank_branch String(120)`, `invoice_terms Text` — all nullable,
   editable via `PATCH /organization/company` (`company.manage`), audited. Frontend: the Company tab of
   `/admin/organisation` gets a "Bank & invoice" card (use `lib/statutory.ts` `IFSC_PATTERN`).
7. **Prerequisite fixes (trap 2):** `delete_department` must also refuse while referenced by work centres or
   purchase requisitions (raw-SQL probe, no procurement import); `delete_plant` must refuse while referenced by
   work centres or production orders. Tests for each.
8. **Permissions** `cost_centre.view`, `cost_centre.manage` (group "Organisation"). Presets: `_ACCOUNTS`
   view+manage; `_PURCHASE`, `_PRODUCTION`, PLANNER: view. Grant migration (SUPER_ADMIN, ADMIN).
   Routes `/organization/cost-centres` (GET list paginated w/ search, parent_id, plant_id, type, is_active;
   POST 201; GET/PATCH/DELETE `{id}`), `require_permission(Permission.COST_CENTRE_VIEW|MANAGE)` literally.
9. **Frontend**
   - `/admin/organisation`: new "Cost centres" tab (copy DepartmentsTab; parent picker from a **full**
     reference list via a `useCostCentreOptions` hook in `hooks/use-organisation-options.ts`, not the current
     page — trap 18), `CostCentreFormDialog`, reuse `RetireDialog`. Route + nav rights become
     `[COMPANY_VIEW, COST_CENTRE_VIEW]`-any (and keep existing). Add `organisation-page.test.tsx` (none exists):
     tab gating, create body, retire with reason.
   - Optional cost-centre select (`asId` → null) in: department dialog, work-centre dialog (routing page),
     vendor-invoice form (header), production-order create dialog, job-work order form, stock-adjustment form.
     Show the cost-centre code on the corresponding detail views where a detail exists. Keep pickers lazy and
     gated on `can(P.COST_CENTRE_VIEW)`; posting `null` when unset.
   - Company tab: bank card.
10. **Demo data**: cost centres for an architectural-hardware plant (tree): `CC-PROD` (Production) with
    children Die-casting, Machining, Buffing & Polishing, Plating/PVD (job-worked), Assembly, Packing;
    `CC-QC`, `CC-STORES`, `CC-MAINT`, `CC-ADMIN`, `CC-SALES` (+ two sales zones). Mark these as demo
    examples; assign department defaults, work-centre cost centres, and tag a couple of demo vendor invoices
    so the report has data. Idempotent by code.
11. **Docs**: database.md (§2 organization tables; a "Cost centres" subsection with the precedence rule, the
    INCOME/EXPENSE-only rule and the reversal rule; §8 if you add an event), assumptions (`§B1-1..n`),
    api.md, handbook (Organisation chapter: new tab; Company tab: bank details).

## Tests (minimum)
Unit: lifecycle validate; journal cost-centre rules (pure: only income/expense lines may carry one; split of
invoice expense per cost centre sums exactly). Integration: CRUD + list lists; duplicate code; tree cycle
refused; archive refused while referenced (department, work centre, child, document) and allowed when free;
cross-tenant id refused on every carrier; `post_journal` refuses a cost centre on a non-expense account and
accepts it on expense; `reverse_journal` copies it even after the cost centre is archived; vendor invoice
approval posts one expense leg per cost centre; report returns expected sums; company bank PATCH validates
IFSC. Frontend: organisation page test, dialog body test, company bank card body test.

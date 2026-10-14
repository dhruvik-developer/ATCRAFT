# WP B2: Shift master and shift roster

Lane B (operations), round 2. Placeholder prefix `§B2-n`.

**Proposal:** Phase 08 Production lists "Shift planning", "Operator allocation" and "Machine allocation".
**Audit:** "A shift roster. A shift can be typed on a work order. There is no shift plan."
**Background:** read `reports/production-shifts.md` in full. Follow its "Recommended design" D1–D9 unless this spec says otherwise.

## Decisions (binding)

### 1. Placement
`shifts` and `shift_assignments` live in the **routing** module. Their routes go under `/routing/shifts…` and `/routing/roster…`, declared before `/{routing_id}`. `MODULE_ORDER` is unchanged.

### 2. Shift master
Model exactly as D2:
- columns: `code String(16)` (immutable, normalised), `name`, `start_time`, `end_time`, `crosses_midnight`, `break_minutes`, `sort_order`, `is_active`
- CHECKs: midnight flag consistent with the times; break minutes bounded
- schema validator: break minutes must be shorter than the shift's duration
- lifecycle: `SHIFT_LIFECYCLE` (entity "Shift") through `MasterLifecycleState`, with `record_status_change`
- shift times are wall-clock times in `companies.timezone`
- re-timing, deactivating or retiring a shift is refused with `referenced_in_use` while PLANNED assignments exist on or after today. To change a shift that is in use, retire it and create a new one (`§B2-1`).

Pure helpers, all unit-tested: `shift_minutes`, `net_shift_minutes`, `shift_window(roster_date, shift, tz)` (returns UTC instants and handles midnight crossing), and `windows_overlap` (half-open).

### 3. Roster: `shift_assignments`
Model as D3: `PLANNED` / `CANCELLED`, using a `StatusEnum` column.

**The window is a snapshot.** `starts_at`/`ends_at` are computed in Python at write time and stored as plain columns. They must not be generated columns (CLAUDE.md `onupdate`/`MissingGreenlet` trap).

**Overlap guards:**
- Add two PostgreSQL `EXCLUDE USING gist` constraints, declared **on the model** with `sqlalchemy.dialects.postgresql.ExcludeConstraint`, explicit short names, and `WHERE status='PLANNED'`:
  - one for an operator's windows;
  - one for a machine's windows, with `machine_id IS NOT NULL` (one operator per machine at a time, `§B2-2`).
- `btree_gist` must exist. Confirm `tests/conftest.py` creates it for the test DB and that module-scoped integration schemas still build.
- `FOREIGN KEY (machine_id, work_center_id)` is a composite FK to `machines(id, work_center_id)`.

**State machine.** `SHIFT_ASSIGNMENT_FLOW` (entity "Shift assignment"): PLANNED→CANCELLED, irreversible.

**Validation is batched for single and bulk writes:**
1. Take `advisory_xact_lock(session, "shift_roster", company_id)`.
2. Batch-load shifts, work centres, machines and operators.
   - Operators must be active, internal (`vendor_id IS NULL`) and not deleted.
   - Machines must be active and not RETIRED, and belong to the stated work centre. MAINTENANCE and BREAKDOWN produce a *warning* in the response message, not a block.
3. Run one query for overlapping PLANNED rows.
4. Report **every** conflict in `details` with a `BusinessRuleError`. The EXCLUDE constraint is the race backstop.
5. Map SQLSTATE 23P01 explicitly in `core/exception_handlers.py` to a 409 "That assignment overlaps another for the same operator or machine", with a unit test.

### 4. Endpoints
Exactly as D2/D3:
- shifts CRUD and activate/deactivate/retire (`shift.view` / `shift.manage`). The read endpoints are gated `require_any_permission(SHIFT_VIEW, SHIFT_ROSTER_VIEW, WORK_ORDER_VIEW, PRODUCTION_ENTRY_CREATE)` so existing production roles can fill pickers.
- `GET /routing/roster/assignments`:
  - requires `date_from` and `date_to`, at most 31 days apart (`MAX_ROSTER_RANGE_DAYS`)
  - filters: `shift_id`, `work_center_id`, `machine_id`, `operator_user_id`, `status`
  - returns a projection with codes and names, paginated
- `POST /routing/roster/assignments` returns 201.
- `POST /routing/roster/assignments/bulk` is all-or-nothing, capped at `MAX_ROSTER_BULK`.
- `POST /routing/roster/assignments/copy` copies a day into a date range, bounded, with an optional `work_center_id`.
- `PATCH /routing/roster/assignments/{id}` re-validates.
- `POST /routing/roster/assignments/{id}/cancel` takes a reason.
- `GET /routing/roster/operators` is gated `require_any_permission(SHIFT_ROSTER_VIEW, WORK_ORDER_MANAGE, PRODUCTION_ENTRY_CREATE)`. It returns active internal users (`id`, `full_name`, `employee_code`, `plant_id`, `department_id`), paginated and searchable. This fixes the empty operator pickers for supervisors, who lack `user.view`.
- Retire/move probes: `used_by_live_roster` on work centres and machines, called from retire, move-machine, and `change_status(RETIRED)`.

### 5. Production references (D4)
- Add a `shift_id` FK RESTRICT NULL to `work_orders` and `production_entries`, with indexes. **Keep the `shift` String(16) as a code snapshot**; replace the old "a shift master would be an extra table" comment with the snapshot rationale.
- Inputs `WorkOrderUpdate`, `ProductionEntryCreate` and `CompleteRequest` take `shift_id` and **drop free-text `shift`**. Update every in-repo caller: demo data, tests, both frontend dialogs.
- Outputs keep `shift` and add `shift_id`. Filters gain `shift_id`, and the text filter stays.
- `_assert_shift` checks tenant and active-on-the-way-in **before** the blind `setattr` loop. The snapshot is written whenever `shift_id` is set; both are cleared together.
- **No backfill of legacy text** (`§B2-3`: legacy text has no times, and inventing them would be an invented rule).
- The roster is **advisory**: a mismatch adds a warning to the message and never blocks (`§B2-4`, named constant).
- **Also fix (production-shifts report §D):**
  - `_assert_operator` must refuse inactive users and vendor-portal users.
  - `_assert_machine` must refuse RETIRED or inactive machines (MAINTENANCE/BREAKDOWN stay allowed).
  - The booking UI must send `work_order_id` when one is chosen, and must allow downtime-only bookings, which the API already accepts.
  - The record-production shift field must be the select, which removes the 32-versus-16 character bug.
  - Fix the wrong Alert and empty-state texts the report names.

### 6. Reports
- `SHIFT_PRODUCTION`, "Daily production by shift":
  - groups `production_entries` by `entry_date`, shift (the snapshot), work centre (via the work order), machine and operator
  - columns: produced, rejected, downtime, bookings, yield %
  - needs a ReportKey, a catalogue entry, the route `/reports/shift-production`, a CHECK-swap migration listing ALL keys, and the frontend union
- Machine utilisation gains `rostered_minutes` and `rostered_utilisation_percent`. `available_minutes` stays unchanged.

### 7. Permissions
- New: `shift.view` and `shift.manage` (group "Engineering"); `shift_roster.view` and `shift_roster.manage` (group "Production").
- Presets: `_PRODUCTION` gets `SHIFT_VIEW`, `SHIFT_ROSTER_VIEW` and `SHIFT_ROSTER_MANAGE`; PLANT_MANAGER also gets `SHIFT_MANAGE`; PLANNER gets the two view rights.
- Add a grant migration for SUPER_ADMIN and ADMIN.
- Document that deployed non-system roles need an administrator to grant these rights.

### 8. Frontend
- **New page `/production/shift-roster`** ("Shift roster"):
  - route guard any of `[P.SHIFT_ROSTER_VIEW, P.SHIFT_VIEW]`; listed in the Manufacturing nav
  - **Day board** tab:
    - rows are work centres and their machines; columns are active shifts in `sort_order`; cells hold operator chips, with Cancel and Assign
    - date picker and work-centre filter
    - data comes from the roster endpoint with `LOOKUP_LIST_LIMIT`; narrow by work centre when `has_next`
    - on a phone, show a card list
  - **Week** tab: a read-only operator × day matrix.
  - **Shifts** tab: the master list plus `ShiftFormDialog` (time inputs, crosses-midnight shown as derived).
  - Assign, Bulk assign, Copy day and Cancel dialogs.
- Hooks `use-shift-options.ts` and `use-operator-options.ts`. The latter replaces `useUserOptions` in the production pickers.
- `WorkOrderAssignDialog`:
  - Shift becomes a select on `shift_id`, showing each shift's window
  - legacy text is shown read-only
  - hint: "rostered for this cell on <local date>" when the roster has a row
- `RecordProductionDialog`:
  - Shift select and operator directory
  - pre-fill from the roster
  - optional work-order select
  - downtime-only bookings allowed
- Work orders page: a shift filter.
- Add `PLANNED` to `STATUS_VARIANTS`.
- Register the hand-written production mirrors (`WorkOrderRow`, `ProductionEntryRow`) in `MIRRORS`, or derive them with `SchemaOf`.
- Update `work-orders-page.test.tsx` (pick a shift option instead of typing 'B').

### 9. Demo data
- Shifts: A 06:00–14:00, B 14:00–22:00, C 22:00–06:00 (crosses midnight), GEN 09:00–17:30 with a 30-minute break.
- A handful of operator users (internal, valid demo emails) if none exist.
- One week of roster rows on the demo work centres.
- Demo bookings use `shift_id`.
- Idempotent: rows are keyed on (operator, shift, date offset from an anchor stored as identity, **not** "today"). See the CLAUDE.md idempotence trap: key on identity.

### 10. Docs
- `database.md`: a "Shifts and the roster" section, §2, §7.
- `assumptions.md`, `§B2-n`: the roster date is the local date the shift starts; times are in the company timezone; windows are snapshots; one PLANNED window per operator; one operator per machine; the roster is advisory; shift codes are immutable; rostered capacity sits beside calendar capacity. Also record the code-only ASSUMPTIONs the report lists.
- Open questions for Atcraft: employees without logins, helpers, holidays, minimum rest, a publish step.
- `api.md`, and the handbook (Manufacturing: Shift roster; fix the shift-is-free-text descriptions).

## Tests
- **Unit:** windows (cross-midnight, 24 h, back-to-back, GEN overlapping A/B, the night shift on D against A on D+1, break bounds, local-versus-UTC dates in IST); schemas; lifecycle `validate()`; the 23P01 mapping.
- **Integration:**
  - tenant probes refused
  - inactive or vendor operator refused
  - a machine from another work centre, or a retired one, refused
  - operator and machine double-booking refused, with every conflict named
  - editing a row does not conflict with itself
  - cancel is audited
  - retire/move probes
  - list lists
  - first-ever tests for `update_work_order`: `shift_id` writes the snapshot, and a foreign or retired shift is refused
  - booking with `shift_id`
  - `SHIFT_PRODUCTION` sums
- **Concurrency:** two sessions racing for one operator window; exactly one wins (409 or business-rule error).
- **Frontend:** roster page gates with and without each right; exact POST body; conflict surfaced; shift dialog; the updated work-order and production dialogs.

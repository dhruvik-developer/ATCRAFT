# Carry-over backlog (for the round-7 buffer lanes)

These are known gaps reported by lane agents that no work package owns. Round 7's buffer lanes (B and C) take them, or E12 records why one is left. Each line names the round and WP that reported it.

## Defects

1. **Departments tab parent picker offers only the current page** (trap 18 class). The cost-centre form uses the full tree; the department form still uses the page rows. Use the full-list option hook. (R1 B1)
2. **Reversal of a journal whose ledger account was later deactivated is refused** (masters report trap 4). `post_journal` requires accounts active on reversal legs; cost centres are already exempt. A reversal of an existing entry must be allowed to name an inactive account. (R1 B1)
3. **Vendor master IFSC validator runs after `max_length=11`**, so an IFSC typed with a space is refused for length rather than normalised. Validate (strip + upper) before the length check, as the company schema does. (R1 B1)
4. **Cost-centre deactivate/archive guards are check-then-act** with no row lock, like every other master. Take `FOR NO KEY UPDATE` on the centre in deactivate/archive and `FOR SHARE` in `require_cost_centre` when assigning, as A1 did for brands. (R1 B1)

## UX and wording

5. "Not in the active list" also appears for a recorded brand or maker when the viewer lacks `brand.view` / `manufacturer.view`; for a permission gap the sentence should say the value is hidden from them. Update `lib/messages.ts`, tests and the handbook together. (R1 A1)
6. The PO list's category select lists only active categories; an inactive category in the URL filters but shows the placeholder. Keep the URL's category as a labelled option. (R1 C0)
7. The requisition conversion preview does not flag an `expected_delivery_date` before `po_date`, or a group over `MAX_LINES_PER_DOCUMENT`; convert refuses both. Surface both in the preview. (R1 C0)
8. The batch requisition picker's "select all on this page" stops silently at 50; the footer says so, but the checkbox should show an indeterminate state. (R1 C0)

## Business confirmations (record in the final report, not code)

9. A second partial conversion of the same requisition line, to the same vendor and warehouse, is an EXACT duplicate (409) and needs an approver's acknowledgement (C0 decision 9). (R1 C0)
10. Demo vendor managers have generated must-change passwords; a demonstrator uses Reset password. (R1 C0)

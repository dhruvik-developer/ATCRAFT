/**
 * Wire shapes and vocabulary for the BOM module.
 *
 * Declared here rather than inferred at each call site so the list, the detail
 * tabs and the two dialogs cannot drift apart.
 *
 * Three rules from the engine (docs/database.md section 5) shape everything in
 * this folder and are worth stating once:
 *
 * 1. An explosion is **consolidated by item**. `SCREW-M4` used in two different
 *    sub-assemblies comes back as ONE row carrying the merged quantity, with
 *    `source_paths` recording every branch that contributed. The UI must never
 *    re-split those rows, and must make the merge visible.
 * 2. Quantities, percentages and money are decimal STRINGS. They stay strings
 *    all the way to the wire; `toDecimal` is the only way they are computed on.
 * 3. **A bill carries ids, not names.** `BomSummary` names `item_id` and
 *    `base_uom_id`; `BomLineRead` names `component_item_id` and `uom_id`. The
 *    only endpoint in the module that returns part numbers is the explosion,
 *    which is why the structure tab reads names from there rather than
 *    inventing a lookup per line.
 */

import type { DecimalString } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

export const BOM_STATUSES = ['DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'OBSOLETE'] as const
export type BomStatus = (typeof BOM_STATUSES)[number]

export const BOM_TYPES = ['PRODUCTION', 'ASSEMBLY', 'PHANTOM', 'ALTERNATE'] as const
export type BomType = (typeof BOM_TYPES)[number]

/** A draft is submitted for approval; a submitted bill is approved or rejected. */
export const isSubmittable = (status: BomStatus): boolean => status === 'DRAFT'
export const isApprovable = (status: BomStatus): boolean => status === 'PENDING_APPROVAL'
export const isRejectable = (status: BomStatus): boolean => status === 'PENDING_APPROVAL'
export const isObsoletable = (status: BomStatus): boolean => status === 'APPROVED'
/**
 * Deletion is a DRAFT-only affair, and deliberately NOT the same rule as
 * `isObsoletable` above.
 *
 * The two retire a bill for opposite reasons. Obsoleting an APPROVED bill
 * retires a live engineering record that planning, production orders and
 * costing already point at, so the row survives and only stops being selected.
 * Deleting removes a draft that never took effect: `delete_draft_bom` in the
 * API hard-deletes it precisely because nothing outside engineering has ever
 * been able to reference it, and refuses every other status by name.
 *
 * PENDING_APPROVAL is excluded on the API's word rather than on a guess - a
 * bill waiting on an approver is rejected back to draft first, and only then
 * deleted.
 *
 * One condition this predicate cannot see: the API also refuses a draft that
 * alternate structures point at. That is a row-level fact, not a status, so the
 * screen offers the action and lets the API have the last word.
 */
export const isDeletable = (status: BomStatus): boolean => status === 'DRAFT'
/** Only a draft still accepts line edits. */
export const isEditable = (status: BomStatus): boolean => status === 'DRAFT'

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /bom` (`BomSummary`). */
export interface BomRow {
  id: string
  code: string
  item_id: string
  version: number
  revision: string | null
  status: BomStatus
  bom_type: BomType
  is_default: boolean
  output_quantity: DecimalString
  base_uom_id: string
  effective_from: string
  effective_to: string | null
  parent_bom_id: string | null
  approved_by_id: string | null
  approved_at: string | null
  created_at: string
  updated_at: string
}

/** `GET /bom/{id}` (`BomDetail`). The header plus its own single level of lines. */
export interface BomDetail extends BomRow {
  notes: string | null
  lines?: BomLine[]
  operations?: BomOperation[]
}

/** One row of `bom_lines` (`BomLineRead`). */
export interface BomLine {
  id: string
  bom_id: string
  line_number: number
  component_item_id: string
  quantity_per: DecimalString
  uom_id: string
  scrap_percentage: DecimalString
  is_optional: boolean
  substitute_for_line_id: string | null
  operation_sequence: number | null
  notes: string | null
}

/** One operation on the bill itself (`BomOperationRead`), distinct from a routing. */
export interface BomOperation {
  id: string
  bom_id: string
  operation_sequence: number
  name: string
  description: string | null
  work_center_id: string | null
  routing_operation_id: string | null
  setup_time_minutes: DecimalString
  run_time_minutes_per_unit: DecimalString
  cost_per_hour: DecimalString
  fixed_cost: DecimalString
  is_outsourced: boolean
  notes: string | null
}

/**
 * One consolidated component from `GET /bom/{id}/explode`
 * (`ExplodedComponentRead`).
 *
 * `total_quantity` is already merged across every branch that reaches this item;
 * `source_paths` lists those branches as paths of item codes, and
 * `source_path_count` is exact even when the listing is capped.
 */
export interface ExplodedComponent {
  item_id: string
  item_code: string
  item_name: string
  uom_id: string
  /** Shallowest level at which the item appears. */
  level: number
  total_quantity: DecimalString
  scrap_adjusted_quantity: DecimalString
  is_purchased: boolean
  is_manufactured: boolean
  source_paths: string[][]
  source_path_count: number
}

/** `GET /bom/{id}/explode` (`BomExplosionRead`). */
export interface BomExplosion {
  bom_id: string
  bom_code: string
  root_item_id: string
  root_item_code: string
  root_bom_status: BomStatus
  quantity: DecimalString
  as_of: string
  max_level_reached: number
  /**
   * Phantom sub-assemblies the explosion passed straight through: they never
   * appear as a component of their own, their children do (assumptions 12).
   */
  phantom_item_codes: string[]
  components: ExplodedComponent[]
}

/** `GET /bom/{id}/cost` (`BomCostRead`). */
export interface BomCost {
  bom_id: string
  bom_code: string
  item_id: string
  item_code: string
  /** Quantity the roll-up was computed for - the BOM's output quantity. */
  output_quantity: DecimalString
  material_cost: DecimalString
  operation_cost: DecimalString
  total_cost: DecimalString
  unit_cost: DecimalString
  components: BomCostComponent[]
  /**
   * Items with no standard cost behind them. The totals above are therefore an
   * understatement, not a complete answer, and the screen has to say so.
   */
  items_without_cost: string[]
}

export interface BomCostComponent {
  item_id: string
  item_code: string
  quantity_per_unit: DecimalString
  unit_cost: DecimalString
  extended_cost: DecimalString
  /** Where the unit cost came from - standard, last purchase, and so on. */
  cost_source: string
}

/** One parent assembly from `GET /bom/where-used/{itemId}` (`WhereUsedRead`). */
export interface BomWhereUsedRow {
  bom_id: string
  bom_code: string
  bom_version: number
  bom_status: BomStatus
  is_default: boolean
  parent_item_id: string
  parent_item_code: string
  parent_item_name: string
  line_number: number
  quantity_per: DecimalString
  level: number
}

/**
 * The item master as the UI consumes it: wire shapes, the capability
 * vocabulary, and the two reference lookups every item screen needs.
 *
 * Declared once rather than inferred at each call site so the list, the detail
 * page and the form cannot drift apart - and so the rule that governs this
 * domain is stated in exactly one place: the capability flags are NINE
 * INDEPENDENT BOOLEANS. One item may be purchased and manufactured and sold at
 * the same time (master rules 1). Nothing here may collapse them into a single
 * discriminator, and no screen may render them as a single-choice control.
 *
 * `item_group` sits alongside them as a reporting classification only. It never
 * decides what an item is allowed to do.
 *
 * The lifecycle predicates at the foot of the wire shapes belong here for the
 * same reason: what `ITEM_LIFECYCLE` permits is one statement, read by the
 * header action and by the dialog behind it, rather than a status comparison
 * copied into JSX.
 */

import { useQuery } from '@tanstack/react-query'
import {
  Factory,
  Hammer,
  Layers,
  Puzzle,
  Recycle,
  RotateCcw,
  ShoppingCart,
  Tag,
  Wrench,
  type LucideIcon,
} from 'lucide-react'

import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

/**
 * Declared as const tuples rather than union types so the same value drives the
 * TypeScript type, the `<Select>` options and the zod enum. One list, no drift.
 */
export const ITEM_GROUPS = [
  'RAW_MATERIAL',
  'COMPONENT',
  'SEMI_FINISHED',
  'FINISHED_GOOD',
  'CONSUMABLE',
  'PACKING_MATERIAL',
  'SPARE',
  'SCRAP',
  'SERVICE',
] as const
export type ItemGroup = (typeof ITEM_GROUPS)[number]

export const PRODUCTION_TYPES = ['MAKE', 'BUY', 'MAKE_OR_BUY', 'JOB_WORK'] as const
export type ProductionType = (typeof PRODUCTION_TYPES)[number]

export type PriceType = 'SALE' | 'DEALER' | 'DISTRIBUTOR'

// ---------------------------------------------------------- capabilities ----

export type CapabilityKey =
  | 'can_purchase'
  | 'can_manufacture'
  | 'can_sell'
  | 'can_be_bom_component'
  | 'can_be_assembly_component'
  | 'can_be_spare'
  | 'can_job_work'
  | 'can_return'
  | 'can_scrap'

/** The seven flags the list projection carries and the search endpoint filters on. */
export type SummaryCapabilityKey = Exclude<CapabilityKey, 'can_return' | 'can_scrap'>

export const CAPABILITY_META: Record<CapabilityKey, { label: string; icon: LucideIcon }> = {
  can_purchase: { label: 'Purchasable', icon: ShoppingCart },
  can_manufacture: { label: 'Manufacturable', icon: Factory },
  can_sell: { label: 'Sellable', icon: Tag },
  can_be_bom_component: { label: 'BOM component', icon: Layers },
  can_be_assembly_component: { label: 'Assembly component', icon: Puzzle },
  can_be_spare: { label: 'Spare part', icon: Wrench },
  can_job_work: { label: 'Job work', icon: Hammer },
  can_return: { label: 'Returnable', icon: RotateCcw },
  can_scrap: { label: 'Scrappable', icon: Recycle },
}

export const ALL_CAPABILITIES: readonly CapabilityKey[] = [
  'can_purchase',
  'can_manufacture',
  'can_sell',
  'can_be_bom_component',
  'can_be_assembly_component',
  'can_be_spare',
  'can_job_work',
  'can_return',
  'can_scrap',
]

/** Shown as the compact icon row in the list grid. */
export const GRID_CAPABILITIES: readonly SummaryCapabilityKey[] = [
  'can_purchase',
  'can_manufacture',
  'can_sell',
  'can_be_bom_component',
  'can_be_spare',
  'can_job_work',
]

/** Exactly the flags `GET /products/items` accepts as query parameters. */
export const FILTERABLE_CAPABILITIES: readonly SummaryCapabilityKey[] = [
  'can_purchase',
  'can_manufacture',
  'can_sell',
  'can_be_bom_component',
  'can_be_assembly_component',
  'can_be_spare',
  'can_job_work',
]

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /products/items` (`ItemSummary`).
 *
 * Deliberately narrow: a page of 200 items must not ship the whole master
 * record. In particular it carries no stock and no reorder level - those live on
 * the inventory endpoints, and a list column bound to them would be blank on
 * every row forever. The item detail Stock tab is where stock is answered.
 */
export interface ItemRow extends Record<SummaryCapabilityKey, boolean> {
  id: string
  code: string
  name: string
  item_group: ItemGroup
  category_id: string | null
  /** Named through `useBrandOptions`; the list filters on it server-side. */
  brand_id: string | null
  base_uom_id: string
  production_type: ProductionType
  sale_price: DecimalString
  is_active: boolean
  /** Sent only to a principal holding `item.cost_view`. */
  standard_cost?: DecimalString
}

/** `GET /products/items/{id}`. */
/**
 * The item as the detail screen reads it.
 *
 * Hand-written rather than `SchemaOf<'ItemDetail'>`, which is a real cost worth
 * naming: `tsc` compares this against nothing, so a field the API stops sending
 * stays declared here and stays rendered. That happened - `valuation_method`
 * was dropped from the API when every item became FIFO, and for months this
 * interface still declared it and the screen still drew a "Valuation" row for
 * it, which `humanise(undefined)` rendered as a dash on every item in the ERP.
 *
 * `tests/unit/test_frontend_contract_types.py` now compares the two, so the next
 * one fails a build instead of shipping.
 */
export interface ItemDetail extends Record<CapabilityKey, boolean> {
  id: string
  code: string
  name: string
  description: string | null
  item_group: ItemGroup
  category_id: string | null

  base_uom_id: string
  purchase_uom_id: string | null
  sale_uom_id: string | null

  hsn_code: string | null
  gst_rate: DecimalString
  sale_price: DecimalString
  dealer_price: DecimalString
  /** Sent only to a principal holding `item.cost_view`. */
  standard_cost?: DecimalString

  /**
   * References into `item_attributes`, not text. The name is resolved from
   * the cached attribute list - see `useItemAttributeOptions` - the same way
   * `category_id` and `base_uom_id` above are.
   */
  finish_id: string | null
  size_id: string | null
  colour_id: string | null

  /**
   * Classification and traceability only - nothing branches on either. Named
   * through `useBrandOptions` / `useManufacturerOptions`; the part number is
   * the maker's own, as free text.
   */
  brand_id: string | null
  manufacturer_id: string | null
  manufacturer_part_number: string | null

  production_type: ProductionType
  lead_time_days: number
  reorder_level: DecimalString
  reorder_quantity: DecimalString
  min_order_quantity: DecimalString
  safety_stock: DecimalString
  shelf_life_days: number | null

  is_batch_tracked: boolean
  is_serial_tracked: boolean
  default_warehouse_id: string | null
  default_location_id: string | null
  is_active: boolean
  lifecycle_state: 'ACTIVE' | 'INACTIVE' | 'ARCHIVED'

  created_at: string
  updated_at: string
}

/**
 * One row of `GET /products/items/{id}/variants` (`VariantOut`).
 *
 * Derived from the contract rather than restated: a variant is a
 * finish/size/colour of the *same* item - it shares that item's BOM, planning
 * parameters and all nine capability flags, and differs only in identity, in
 * the two deltas and in its barcode. Anything that needs its own BOM is a new
 * item, not a variant.
 *
 * `price_delta` and `cost_delta` are deltas, never absolute prices, so that a
 * price revision on the parent item applies to every variant without touching
 * two hundred variant rows. The delta is added to an item-wide price list row;
 * a variant-specific row already *is* the variant's price and no delta is
 * applied on top (products `resolve_price`). That is why nothing here prints an
 * "effective price" - it would be right in one of those two cases and wrong in
 * the other.
 */
export type ItemVariant = SchemaOf<'VariantOut'>

// ------------------------------------------------------------- lifecycle ----

/** The three states `ITEM_LIFECYCLE` knows, as the detail payload reports them. */
export type ItemLifecycleState = ItemDetail['lifecycle_state']

/**
 * The two edges of `ITEM_LIFECYCLE` labelled "Retire": ACTIVE -> ARCHIVED and
 * INACTIVE -> ARCHIVED. ARCHIVED is terminal and has no self-edge, so retiring
 * an already-retired item is not a quiet no-op - the machine refuses it.
 */
const RETIREABLE_STATES: readonly ItemLifecycleState[] = ['ACTIVE', 'INACTIVE']

/**
 * Does this item accept retirement?
 *
 * A predicate rather than an `is_active` test at the call site, because
 * `is_active` answers the wrong question in both directions: it is false for a
 * merely INACTIVE item, which retires perfectly well, and it says nothing at
 * all about ARCHIVED, which is the one state the API refuses.
 *
 * In practice the detail page never holds an ARCHIVED item - a retired item is
 * filtered out of every read this application makes, so `GET
 * /products/items/{id}` answers 404 rather than returning it - but the gate is
 * written from the state machine, not from that side effect.
 */
export const acceptsRetirement = (item: Pick<ItemDetail, 'lifecycle_state'>): boolean =>
  RETIREABLE_STATES.includes(item.lifecycle_state)

/**
 * Does this variant accept retirement? Every one a list returns does.
 *
 * A variant has no lifecycle machine of its own: `delete_variant` soft-deletes
 * whatever `get_or_404` hands it, and that lookup already excludes retired
 * rows. So `is_active` is an availability flag here, not a state, and an
 * inactive variant is exactly as retireable as an active one - worth saying out
 * loud, because the item beside it does have a machine, and one state it
 * refuses.
 *
 * Stated as a predicate anyway so the panel asks the same question of a variant
 * that the header asks of an item, and so the day the API grows a rule there is
 * one place to put it.
 */
export const acceptsVariantRetirement = (_variant: Pick<ItemVariant, 'is_active'>): boolean => true

/**
 * One warehouse row of `GET /inventory/items/{id}/summary` (`WarehouseStockRow`).
 *
 * Aggregated over locations and batches by the API: a heavily binned item can
 * have hundreds of balance rows in one warehouse and none of them is
 * interesting on its own here. That is why there is no location or batch on
 * this row - the balances screen is where the individual positions live.
 */
export interface ItemWarehouseStockRow {
  warehouse_id: string
  warehouse_code: string | null
  warehouse_name: string | null
  on_hand_quantity: DecimalString
  reserved_quantity: DecimalString
  available_quantity: DecimalString
  in_transit_quantity: DecimalString
  qc_hold_quantity: DecimalString
  damaged_quantity: DecimalString
  job_work_quantity: DecimalString
  scrap_quantity: DecimalString
  stock_value: DecimalString
}

/**
 * `GET /inventory/items/{id}/summary` (`ItemStockSummary`), returned as a
 * single-element page whose pagination block describes `warehouses`.
 *
 * The totals cover the whole item, not the current page. Folding the page in
 * the browser instead would give a figure that changes as the user pages, which
 * is worse than no total at all.
 */
export interface ItemStockSummary {
  item_id: string
  item_code: string | null
  item_name: string | null
  reorder_level: DecimalString | null
  total_on_hand: DecimalString
  total_reserved: DecimalString
  total_available: DecimalString
  total_in_transit: DecimalString
  total_job_work: DecimalString
  total_scrap: DecimalString
  /** On hand plus everything owned but elsewhere - in transit, at a job worker. */
  total_owned_quantity: DecimalString
  total_value: DecimalString
  warehouses: ItemWarehouseStockRow[]
}

/** One parent assembly from `GET /bom/where-used/{itemId}` (`WhereUsedRead`). */
export interface WhereUsedRow {
  bom_id: string
  bom_code: string
  bom_version: number
  bom_status: 'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'OBSOLETE'
  is_default: boolean
  level: number
  line_number: number
  parent_item_id: string
  parent_item_code: string
  parent_item_name: string
  quantity_per: DecimalString
}

/** `GET /products/items/{id}/prices`. */
export interface ItemPrice {
  id: string
  item_id: string
  variant_id: string | null
  price_type: PriceType
  currency: string
  price: DecimalString
  min_quantity: DecimalString
  valid_from: string
  valid_to: string | null
  is_active: boolean
  notes: string | null
}

/**
 * One row of `GET /products/items/{id}/vendors` (`VendorLinkOut`).
 *
 * Derived from the contract rather than restated: the endpoint declares its
 * response model, so a hand-written copy would only be a second opinion - and
 * the maker fields this row gained are exactly the kind a copy forgets.
 */
export type ItemVendorLink = SchemaOf<'VendorLinkOut'>

export interface CategoryOption {
  id: string
  code: string
  name: string
  path: string
  depth: number
  is_active: boolean
}

export interface UomOption {
  id: string
  code: string
  name: string
  decimal_places: number
  is_active: boolean
}

// ------------------------------------------------------------- reference ----

/**
 * Categories and units are reference data - a company has tens of them - so one
 * page at the API's maximum size (`LOOKUP_LIST_LIMIT`) is the whole set in
 * practice, and a picker that paged would be harder to use than one that does not.
 */

/**
 * Category names for the grid and the pickers.
 *
 * `enabled` exists because this is a separate permission: a storekeeper may
 * hold `item.view` without `category.view`, and firing the request anyway would
 * earn a 403 the user can do nothing about.
 */
export function useCategoryOptions(enabled: boolean) {
  return useQuery({
    queryKey: [...queryKeys.categories.all, 'options'],
    queryFn: async () =>
      (
        await api.getPage<CategoryOption>('/products/categories', {
          params: { limit: LOOKUP_LIST_LIMIT, is_active: true },
        })
      ).data,
    staleTime: staleTimes.reference,
    enabled,
  })
}

export function useUomOptions(enabled: boolean) {
  return useQuery({
    queryKey: [...queryKeys.uoms.all, 'options'],
    queryFn: async () =>
      (
        await api.getPage<UomOption>('/products/uoms', {
          params: { limit: LOOKUP_LIST_LIMIT, is_active: true },
        })
      ).data,
    staleTime: staleTimes.reference,
    enabled,
  })
}

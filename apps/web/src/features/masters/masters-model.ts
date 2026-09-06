/**
 * Item-master reference data as the UI consumes it: categories, units of
 * measure, the conversions between them, and the dated price list.
 *
 * Every wire shape here is an alias into the generated contract rather than a
 * hand-written interface, so a field the API drops becomes a compile error
 * instead of a column that renders blank forever.
 *
 * Three properties of this corner of the contract shape the screens above it,
 * and all three are easy to get wrong:
 *
 * 1. **A conversion factor is directional.** `factor` answers "how many
 *    `to_uom` make ONE `from_uom`" - the service writes the audit line
 *    `1 {from} = {factor} {to}` (apps/api/src/modules/products/service.py,
 *    `create_conversion`). Reading it the other way round silently corrupts
 *    every quantity derived from it, so nothing in this feature prints a bare
 *    factor without the sentence that fixes its direction.
 * 2. **None of these four list endpoints accepts `sort_by`.** Categories come
 *    back ordered by the materialised `path` (depth-first tree order), units
 *    and conversions by the repository's own order, prices by validity. Every
 *    column in this feature therefore sets `enableSorting: false`: a header
 *    that reorders nothing is worse than no header affordance at all.
 * 3. **A PATCH here ignores nulls.** `update_category`, `update_uom` and
 *    `update_price` all loop `if value is not None: setattr(...)`, so a null
 *    field is dropped rather than clearing the column. A text field is cleared
 *    by sending `""`; a parent is cleared by the explicit `reparent_to_root`
 *    flag; an end date cannot be cleared at all.
 */

import { useQuery } from '@tanstack/react-query'
import Decimal from 'decimal.js'

import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no branch for this screen, so
 * the keys live here in the same hierarchical shape: invalidating
 * `mastersKeys.all` clears all four lists at once, which is what a write to any
 * of them warrants - a new unit changes what the conversion form may offer, and
 * a new category changes what the parent picker holds.
 *
 * The price key carries the item id because those rows are addressed as
 * `/products/items/{id}/prices`. Without it two items would share one cache
 * entry and the second item opened would show the first item's price list.
 */
export const mastersKeys = {
  all: ['masters'] as const,
  categories: (params: unknown) => ['masters', 'categories', params] as const,
  category: (id: string) => ['masters', 'categories', 'detail', id] as const,
  categoryReference: () => ['masters', 'categories', 'reference'] as const,
  uoms: (params: unknown) => ['masters', 'uoms', params] as const,
  attributes: (params: unknown) => ['masters', 'attributes', params] as const,
  uomReference: () => ['masters', 'uoms', 'reference'] as const,
  conversions: (params: unknown) => ['masters', 'conversions', params] as const,
  conversionCheck: (params: unknown) => ['masters', 'conversions', 'check', params] as const,
  prices: (itemId: string, params: unknown) => ['masters', 'prices', itemId, params] as const,
  variants: (itemId: string) => ['masters', 'variants', itemId] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /products/categories` (`CategoryOut`). */
export type CategoryRow = SchemaOf<'CategoryOut'>

/** One row of `GET /products/attributes` (`ItemAttributeOut`). */
export type ItemAttributeRow = SchemaOf<'ItemAttributeOut'>

/** One row of `GET /products/uoms` (`UomOut`). */
export type UomRow = SchemaOf<'UomOut'>

/** One row of `GET /products/uom-conversions` (`UomConversionOut`). */
export type ConversionRow = SchemaOf<'UomConversionOut'>

/** One row of `GET /products/items/{id}/prices` (`PriceOut`). */
export type PriceRow = SchemaOf<'PriceOut'>

/** One row of `GET /products/items/{id}/variants` (`VariantOut`). */
export type VariantRow = SchemaOf<'VariantOut'>

/** `GET /products/uom-conversions/convert` (`ConversionResult`). */
export type ConversionResult = SchemaOf<'ConversionResult'>

/** Measurement family. A conversion may only relate two units of one family. */
export type UomCategory = SchemaOf<'UomCategory'>

/** Which commercial channel a price list row applies to. */
export type PriceType = SchemaOf<'PriceType'>

// ------------------------------------------------------------ vocabulary ----

/**
 * Listed as const tuples so one declaration drives the TypeScript type, the
 * `<Select>` options and the zod enum. `satisfies` keeps them honest: a value
 * the contract does not define fails to compile here rather than 422-ing at the
 * server.
 */
export const UOM_CATEGORIES = [
  'COUNT',
  'WEIGHT',
  'LENGTH',
  'AREA',
  'VOLUME',
  'TIME',
] as const satisfies readonly UomCategory[]

export const PRICE_TYPES = ['SALE', 'DEALER', 'DISTRIBUTOR'] as const satisfies readonly PriceType[]

/** `UomCreate.decimal_places` is bounded 0..6 by the contract. */
export const MIN_UOM_DECIMALS = 0
export const MAX_UOM_DECIMALS = 6

/**
 * `UomConversionCreate.factor` accepts at most 10 integer digits and 8
 * decimals and must be strictly positive. Transcribed from the contract's own
 * pattern so the form refuses what the API would refuse, before the round trip.
 */
export const FACTOR_PATTERN = /^\d{1,10}(\.\d{1,8})?$/

/** `PriceCreate.price`: 12 integer digits, 6 decimals, zero allowed. */
export const PRICE_PATTERN = /^\d{1,12}(\.\d{1,6})?$/

/** `PriceCreate.min_quantity`: 14 integer digits, 4 decimals, zero allowed. */
export const QUANTITY_PATTERN = /^\d{1,14}(\.\d{1,4})?$/

// ------------------------------------------------------------- reference ----

/**
 * Units and categories are reference data - a company has tens of them - so one
 * page at the API's maximum size (`LOOKUP_LIST_LIMIT`) is the whole set in
 * practice, and a picker that paged would be harder to use than one that does
 * not. `has_next` is carried back so a company that has outgrown that
 * assumption is told, rather than quietly shown a truncated parent list.
 */

export interface ReferenceSet<T> {
  rows: T[]
  /** True when the API held more rows than one page could carry. */
  truncated: boolean
}

/**
 * Every unit the tenant has, **including retired ones**.
 *
 * Deliberately unlike `useUomOptions` in `features/items/item-model.ts`, which
 * asks for active units only. That is right for a picker that assigns a unit to
 * a new item; it is wrong here, because a conversion row may point at a unit
 * that was later deactivated and the grid still has to name it. An unresolved
 * id prints as a UUID or, worse, as a blank cell that reads like "no unit".
 */
export function useUomReference(enabled: boolean) {
  return useQuery({
    queryKey: mastersKeys.uomReference(),
    queryFn: async (): Promise<ReferenceSet<UomRow>> => {
      const page = await api.getPage<UomRow>('/products/uoms', {
        params: { limit: LOOKUP_LIST_LIMIT, with_total: false },
      })
      return { rows: page.data, truncated: page.pagination.has_next }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

/**
 * Every category, active or not, ordered by `path` - depth-first tree order, so
 * a select built from this reads as the tree.
 *
 * Inactive categories are included because one may still be the current parent
 * of the row being edited, and dropping it from the list would re-parent the
 * subtree the moment the form was saved.
 */
export function useCategoryReference(enabled: boolean) {
  return useQuery({
    queryKey: mastersKeys.categoryReference(),
    queryFn: async (): Promise<ReferenceSet<CategoryRow>> => {
      const page = await api.getPage<CategoryRow>('/products/categories', {
        params: { limit: LOOKUP_LIST_LIMIT, with_total: false },
      })
      return { rows: page.data, truncated: page.pagination.has_next }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

/**
 * Variants of one item, for the price list's variant column and filter.
 *
 * `GET /products/items/{id}/variants` is gated on `item.view`, not
 * `price.view`, so this is asked for separately: a principal holding only the
 * price rights sees "Variant-specific" without a code rather than earning a 403
 * they can do nothing about.
 */
export function useVariantOptions(itemId: string, enabled: boolean) {
  return useQuery({
    queryKey: mastersKeys.variants(itemId),
    queryFn: async () =>
      (
        await api.getPage<VariantRow>(`/products/items/${itemId}/variants`, {
          params: { limit: LOOKUP_LIST_LIMIT, with_total: false },
        })
      ).data,
    staleTime: staleTimes.master,
    enabled: enabled && Boolean(itemId),
  })
}

// -------------------------------------------------------------- derived -----

/** `id -> row`, for resolving the bare ids a conversion row carries. */
export function indexById<T extends { id: string }>(rows: readonly T[] | undefined) {
  return new Map((rows ?? []).map((row) => [row.id, row]))
}

/**
 * A factor with its trailing zeros trimmed.
 *
 * Not `formatQuantity`: that caps at four decimals, and a conversion read the
 * other way round is routinely smaller than that - the inverse of 1 000 000 is
 * 0.000001, which four places renders as "0".
 */
export function formatFactor(value: Decimal): string {
  return value
    .toSignificantDigits(10)
    .toFixed()
    .replace(/(\.\d*?)0+$/, '$1')
    .replace(/\.$/, '')
}

/**
 * The one sentence that fixes the direction of a conversion.
 *
 * `factor` is how many `to` units make ONE `from` unit, so the only safe way to
 * show it is as the equation the server itself recorded in the audit trail.
 */
export function conversionStatement(
  fromCode: string,
  toCode: string,
  factor: DecimalString,
): string {
  return `1 ${fromCode} = ${formatFactor(toDecimal(factor))} ${toCode}`
}

/**
 * The same conversion read backwards, or null when the factor is unusable.
 *
 * Worth showing because the resolver uses it: `UomGraph.factor` falls back to
 * `1 / factor` when only the opposite direction is stored, so a pair entered
 * once is live in both directions and the operator should see both.
 */
export function inverseFactor(factor: DecimalString): Decimal | null {
  const value = toDecimal(factor)
  if (!value.isFinite() || value.lessThanOrEqualTo(0)) return null
  return new Decimal(1).dividedBy(value)
}

/**
 * Categories that may legally become the parent of `category`.
 *
 * The server refuses a move beneath the node's own descendant, and the
 * materialised path makes that exactly a prefix test - the same one
 * `_reparent_category` performs. Narrowing an already-loaded reference set is
 * not a list query; it withholds an option whose only possible answer is a
 * rejection the user cannot act on.
 */
export function eligibleParents(
  all: readonly CategoryRow[],
  category: CategoryRow | null,
): CategoryRow[] {
  if (!category) return [...all]
  return all.filter((row) => !row.path.startsWith(category.path))
}

/** Level as an operator counts it; the API's `depth` is 0-based. */
export function levelLabel(depth: number): string {
  return depth === 0 ? 'Top level' : `Level ${depth + 1}`
}

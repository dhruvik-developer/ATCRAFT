/**
 * The client's brands, for the pickers and for naming a stored `brand_id`.
 *
 * Built exactly like `use-item-attribute-options.ts`: one bounded page of a
 * structural table (`LOOKUP_LIST_LIMIT`), active rows only, cached for the
 * reference-data window. A company has a handful of brands, not thousands.
 *
 * A permission refusal returns an empty list rather than propagating: an
 * account may hold `item.view` without `brand.view`, and a brand label is
 * decoration on the item screen rather than the point of it. Only that refusal
 * is swallowed - a 500 still surfaces, because an empty picker after an outage
 * reads as "this company has no brands" to the person filling in the form.
 */

import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { empty } from '@/lib/messages'
import { staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type BrandOption = SchemaOf<'BrandOut'>

/** Under the shared `products` branch, beside the attribute options. */
export const brandOptionKeys = {
  all: ['products', 'brands', 'options'] as const,
} as const

const OPTION_PARAMS = { limit: LOOKUP_LIST_LIMIT, is_active: true, with_total: false } as const

/** Active brands, in the order the client put them in. */
export function useBrandOptions(enabled = true) {
  return useQuery({
    queryKey: brandOptionKeys.all,
    queryFn: async (): Promise<BrandOption[]> => {
      try {
        const page = await api.getPage<BrandOption>('/products/brands', { params: OPTION_PARAMS })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<BrandOption>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

/**
 * A lookup from id to display name, for a screen that shows a stored value.
 *
 * Falls back to a sentence rather than to the raw id: an id missing from the
 * active list is a brand retired or deactivated since it was assigned, or one
 * this account may not read - and a UUID on screen says neither.
 */
export function brandLabeller(options: BrandOption[] | undefined) {
  const byId = new Map((options ?? []).map((option) => [option.id, option.name]))
  return (id: string | null | undefined): string =>
    id ? (byId.get(id) ?? empty.unlistedReference) : '-'
}

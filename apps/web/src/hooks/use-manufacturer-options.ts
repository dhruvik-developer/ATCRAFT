/**
 * The client's manufacturers, for the pickers and for naming a stored
 * `manufacturer_id` - on an item, on a brand (its owner) and on an
 * item-vendor link.
 *
 * Built exactly like `use-item-attribute-options.ts` and `use-brand-options.ts`:
 * one bounded page (`LOOKUP_LIST_LIMIT`), active rows only, cached for the
 * reference-data window, and a permission refusal answered with an empty list
 * rather than an error - an account may read items without holding
 * `manufacturer.view`, and the maker's name is a label, not the point of the
 * screen.
 */

import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { empty } from '@/lib/messages'
import { staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type ManufacturerOption = SchemaOf<'ManufacturerOut'>

export const manufacturerOptionKeys = {
  all: ['products', 'manufacturers', 'options'] as const,
} as const

const OPTION_PARAMS = { limit: LOOKUP_LIST_LIMIT, is_active: true, with_total: false } as const

/** Active manufacturers, alphabetically - the server's default order. */
export function useManufacturerOptions(enabled = true) {
  return useQuery({
    queryKey: manufacturerOptionKeys.all,
    queryFn: async (): Promise<ManufacturerOption[]> => {
      try {
        const page = await api.getPage<ManufacturerOption>('/products/manufacturers', {
          params: OPTION_PARAMS,
        })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<ManufacturerOption>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

/**
 * A lookup from id to display name. Like `brandLabeller`, a value missing from
 * the active list reads as a sentence rather than as a raw id.
 */
export function manufacturerLabeller(options: ManufacturerOption[] | undefined) {
  const byId = new Map((options ?? []).map((option) => [option.id, option.name]))
  return (id: string | null | undefined): string =>
    id ? (byId.get(id) ?? empty.unlistedReference) : '-'
}

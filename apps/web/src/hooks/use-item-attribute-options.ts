/**
 * The client's own finish, size and colour vocabulary, for the pickers.
 *
 * These were free text on the item until recently, which is why "Brushed",
 * "brushed" and "Brushed Steel" could be three different finishes and no report
 * could group by one. They are rows now, and this is how a screen offers them.
 *
 * One query per kind rather than one query filtered three ways: the three lists
 * are used independently - an item form wants all three, a filter wants one -
 * and a shared cache entry would make the filter's fetch decide what the form
 * sees. Each is a bounded page of a structural table, tens of rows.
 *
 * A permission refusal returns an empty list rather than propagating, the same
 * bargain the organisation options make: an account may hold `item.view`
 * without `item_attribute.view`, and an unlabelled finish is decoration on the
 * item screen rather than the point of it. Only that refusal is swallowed - a
 * 500 still surfaces, because an empty picker after an outage reads as "this
 * company has no finishes" to the person filling in the form.
 */

import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type ItemAttributeType = SchemaOf<'ItemAttributeType'>
export type ItemAttributeOption = SchemaOf<'ItemAttributeOut'>

export const itemAttributeOptionKeys = {
  all: ['products', 'attributes', 'options'] as const,
  ofType: (attribute: ItemAttributeType) =>
    ['products', 'attributes', 'options', attribute] as const,
} as const

const OPTION_PARAMS = { limit: LOOKUP_LIST_LIMIT, is_active: true, with_total: false } as const

/** The permitted values of one attribute, in the order the client put them in. */
export function useItemAttributeOptions(attribute: ItemAttributeType, enabled = true) {
  return useQuery({
    queryKey: itemAttributeOptionKeys.ofType(attribute),
    queryFn: async (): Promise<ItemAttributeOption[]> => {
      try {
        const page = await api.getPage<ItemAttributeOption>('/products/attributes', {
          params: { ...OPTION_PARAMS, attribute },
        })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<ItemAttributeOption>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

/**
 * A lookup from id to display name, for a screen that shows a stored value.
 *
 * Returns the id itself when the value is not in the list, which happens for a
 * retired attribute an item still carries - retiring removes it from the picker
 * and deliberately does not rewrite the items that already reference it.
 */
export function attributeLabeller(options: ItemAttributeOption[] | undefined) {
  const byId = new Map((options ?? []).map((option) => [option.id, option.name]))
  return (id: string | null | undefined): string => (id ? (byId.get(id) ?? id) : '-')
}

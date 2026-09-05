/**
 * Organisation placement lookups - branches, plants and departments.
 *
 * One implementation, one query key per list. Three areas needed these lists
 * independently (admin, warehouses, production) and each grew its own copy.
 * Because every copy keyed on `['organization', <kind>, 'options']` while
 * sending different query parameters, they collided in the query cache:
 * whichever screen mounted first decided what the others saw for the next
 * thirty minutes, so a picker could be offered a branch that had been retired.
 *
 * These are structural tables - tens of rows, not thousands - so one bounded
 * page is the whole list. Only active rows are offered, because the point of
 * these lists is choosing where something is filed next.
 *
 * A permission refusal returns an empty list rather than propagating. The
 * caller may hold `warehouse.view` or `production_order.view` without
 * `company.view`, and a missing branch label is decoration on those screens,
 * not the point of them - an otherwise working page should not be replaced by
 * an error state. Only that refusal is swallowed: an outage or a server fault
 * still surfaces, because an empty picker after a 500 reads as "no branches
 * exist" to the person filling in the form.
 */

import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { staleTimes } from '@/lib/query-client'

/** The fields every caller reads. Rows carry more; nothing here depends on it. */
export interface OrganisationOption {
  id: string
  code: string
  name: string
}

export const organisationOptionKeys = {
  branches: ['organization', 'branches', 'options'] as const,
  plants: ['organization', 'plants', 'options'] as const,
  departments: ['organization', 'departments', 'options'] as const,
} as const

const OPTION_PARAMS = { limit: LOOKUP_LIST_LIMIT, is_active: true, with_total: false } as const

function useOrganisationOptions<T>(path: string, key: readonly unknown[], enabled: boolean) {
  return useQuery({
    queryKey: key,
    queryFn: async (): Promise<T[]> => {
      try {
        const page = await api.getPage<T>(path, { params: { ...OPTION_PARAMS } })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<T>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

export function useBranchOptions<T = OrganisationOption>(enabled = true) {
  return useOrganisationOptions<T>(
    '/organization/branches',
    organisationOptionKeys.branches,
    enabled,
  )
}

export function usePlantOptions<T = OrganisationOption>(enabled = true) {
  return useOrganisationOptions<T>('/organization/plants', organisationOptionKeys.plants, enabled)
}

export function useDepartmentOptions<T = OrganisationOption>(enabled = true) {
  return useOrganisationOptions<T>(
    '/organization/departments',
    organisationOptionKeys.departments,
    enabled,
  )
}

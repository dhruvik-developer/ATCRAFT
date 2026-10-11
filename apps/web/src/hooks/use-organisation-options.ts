/**
 * Organisation lookups - branches, plants, departments and cost centres.
 *
 * One implementation, one query key per list. Three areas needed these lists
 * independently (admin, warehouses, production) and each grew its own copy.
 * Because every copy keyed on `['organization', <kind>, 'options']` while
 * sending different query parameters, they collided in the query cache:
 * whichever screen mounted first decided what the others saw for the next
 * thirty minutes, so a picker could be offered a branch that had been retired.
 *
 * These are structural tables - tens of rows, not thousands - so one bounded
 * page is the whole list. Only active branches, plants and departments are
 * offered, because the point of those lists is choosing where something is
 * filed next. Cost centres are the exception, and `useCostCentreOptions` says
 * why.
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
import type { SchemaOf } from '@/types/api'

/** The fields every caller reads. Rows carry more; nothing here depends on it. */
export interface OrganisationOption {
  id: string
  code: string
  name: string
}

/**
 * What a cost-centre picker or label reads.
 *
 * Derived from the contract rather than restated, so a field the API drops
 * stops compiling here instead of rendering as a blank.
 */
export type CostCentreOption = Pick<
  SchemaOf<'CostCentreRead'>,
  'id' | 'code' | 'name' | 'parent_id' | 'is_active'
>

export const organisationOptionKeys = {
  branches: ['organization', 'branches', 'options'] as const,
  plants: ['organization', 'plants', 'options'] as const,
  departments: ['organization', 'departments', 'options'] as const,
  costCentres: ['organization', 'cost-centres', 'options'] as const,
} as const

const OPTION_PARAMS = { limit: LOOKUP_LIST_LIMIT, is_active: true, with_total: false } as const

/** Every live row, active or not. Retired (archived) rows are never listed. */
const REFERENCE_PARAMS = { limit: LOOKUP_LIST_LIMIT, with_total: false } as const

function useOrganisationOptions<T>(
  path: string,
  key: readonly unknown[],
  enabled: boolean,
  params: Readonly<Record<string, unknown>> = OPTION_PARAMS,
) {
  return useQuery({
    queryKey: key,
    queryFn: async (): Promise<T[]> => {
      try {
        const page = await api.getPage<T>(path, { params: { ...params } })
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

/**
 * Every live cost centre - the inactive ones too - for pickers and labels.
 *
 * The one list here that does not ask for active rows only, for the reason the
 * category reference list gives: a document keeps its cost centre after
 * somebody deactivates it (docs/assumptions.md §B1-3). Dropping that centre
 * from the list would leave the document's own edit form unable to name what
 * it already holds - so the next save would quietly look like "unallocated" -
 * and the label on its detail view blank. Pickers still offer only the active
 * rows, plus whichever one the record already carries; `CostCentreSelect` is
 * where that is decided.
 *
 * Also the full reference list the cost-centre tab draws its tree from, rather
 * than the page of the register that happens to be on screen.
 *
 * Callers gate it on `cost_centre.view`, which `GET /organization/cost-centres`
 * enforces; the permission swallow below is the backstop, not the gate.
 */
export function useCostCentreOptions<T = CostCentreOption>(enabled = true) {
  return useOrganisationOptions<T>(
    '/organization/cost-centres',
    organisationOptionKeys.costCentres,
    enabled,
    REFERENCE_PARAMS,
  )
}

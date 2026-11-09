/**
 * `GET /warehouses` read whole: the list every warehouse select offers.
 *
 * One spelling of the read, so a screen that has to name the warehouse a select
 * chose reads the cache entry the select filled rather than fetching it again.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { WAREHOUSE_LOOKUP_PARAMS, type WarehouseOption } from './types'

export function useWarehouseLookup() {
  const params = WAREHOUSE_LOOKUP_PARAMS
  return useQuery({
    queryKey: queryKeys.warehouses.list(params),
    queryFn: ({ signal }) => api.getPage<WarehouseOption>('/warehouses', { params, signal }),
    staleTime: staleTimes.master,
  })
}

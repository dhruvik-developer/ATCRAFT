import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type LocationOption = SchemaOf<'StorageLocationRead'>

export function useLocationOptions(warehouseId: string, search: string, enabled: boolean) {
  const params = {
    warehouse_id: warehouseId,
    is_active: true,
    ...(search ? { search } : {}),
    limit: PICKER_PAGE_SIZE,
    with_total: false,
  }
  return useQuery({
    queryKey: [...queryKeys.warehouses.all, 'location-options', params],
    queryFn: () => api.getPage<LocationOption>('/warehouses/locations', { params }),
    enabled: enabled && Boolean(warehouseId),
    staleTime: staleTimes.master,
  })
}

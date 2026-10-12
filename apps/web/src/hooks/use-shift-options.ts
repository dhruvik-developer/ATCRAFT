import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type ShiftOption = SchemaOf<'ShiftRead'>
export function shiftLabel(shift: ShiftOption) {
  return `${shift.code} · ${shift.name} (${shift.start_time.slice(0, 5)}–${shift.end_time.slice(0, 5)}${shift.crosses_midnight ? ' next day' : ''})`
}
export function useShiftOptions(enabled = true) {
  return useQuery({
    queryKey: [...queryKeys.routing.all, 'shifts', 'options'],
    queryFn: async () => {
      try {
        return (
          await api.getPage<ShiftOption>('/routing/shifts', {
            params: {
              limit: LOOKUP_LIST_LIMIT,
              is_active: true,
              sort_by: 'sort_order',
              with_total: false,
            },
          })
        ).data
      } catch (error) {
        return emptyOnPermissionError<ShiftOption>(error)
      }
    },
    enabled,
    staleTime: staleTimes.reference,
  })
}

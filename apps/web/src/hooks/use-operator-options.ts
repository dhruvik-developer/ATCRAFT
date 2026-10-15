import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type ShiftOperator = SchemaOf<'RosterOperatorRead'>
export function useOperatorOptions({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: [...queryKeys.routing.all, 'roster', 'operators'],
    queryFn: async () => {
      try {
        return (
          await api.getPage<ShiftOperator>('/routing/roster/operators', {
            params: { limit: LOOKUP_LIST_LIMIT, with_total: false },
          })
        ).data
      } catch (error) {
        return emptyOnPermissionError<ShiftOperator>(error)
      }
    },
    enabled,
    staleTime: staleTimes.reference,
  })
}

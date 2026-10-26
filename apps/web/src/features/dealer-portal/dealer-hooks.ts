import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

export const DEALER_API = '/dealer-portal'

export function dealerError(error: unknown): string {
  return fieldErrorSummary(error) || describeError(error)
}

export function useDealerRead<T>(resource: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.dealerPortal.list(resource, {}),
    queryFn: ({ signal }) => api.get<T>(`${DEALER_API}/${resource}`, { signal }),
    enabled,
    staleTime: staleTimes.transactional,
  })
}

export function useDealerAction<TVariables, TResult>(
  action: (variables: TVariables) => Promise<TResult>,
  message: string,
  onSuccess?: (result: TResult) => void,
  onError?: (error: unknown) => void,
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: action,
    onError,
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
      toast.success(message)
      onSuccess?.(result)
    },
  })
}

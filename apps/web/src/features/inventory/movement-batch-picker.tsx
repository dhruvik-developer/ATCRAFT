import { useQuery } from '@tanstack/react-query'

import { BatchSelect } from '@/features/dispatch/stock-pickers'
import type { ItemDetail } from '@/features/items/item-model'
import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'

export default function MovementBatchPicker({
  id,
  itemId,
  value,
  onChange,
}: {
  id: string
  itemId: string
  value: string
  onChange: (value: string) => void
}) {
  const item = useQuery({
    queryKey: queryKeys.items.detail(itemId),
    queryFn: () => api.get<ItemDetail>('/products/items/' + itemId),
    enabled: Boolean(itemId),
    staleTime: staleTimes.master,
  })
  if (!item.data?.is_batch_tracked) return null
  return <BatchSelect id={id} itemId={itemId} value={value} onChange={onChange} />
}

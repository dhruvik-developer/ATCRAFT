import { useQuery } from '@tanstack/react-query'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import { CreateAdjustmentDialog } from './movement-dialogs'

export default function EditAdjustmentDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const detail = useQuery({
    queryKey: [...queryKeys.inventory.all, 'adjustment', id],
    queryFn: () => api.get<SchemaOf<'StockAdjustmentRead'>>('/inventory/adjustments/' + id),
    staleTime: staleTimes.transactional,
  })
  if (!detail.data)
    return (
      <div role="status" className="rounded border p-4">
        {detail.isLoading ? (
          'Loading adjustment...'
        ) : (
          <FieldError message={detail.error?.message} />
        )}
        <Button variant="outline" onClick={onClose}>
          Cancel edit
        </Button>
      </div>
    )
  return (
    <CreateAdjustmentDialog
      open
      adjustment={detail.data}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    />
  )
}

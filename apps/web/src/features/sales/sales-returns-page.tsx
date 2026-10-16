import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { type ColumnDef } from '@tanstack/react-table'

import { DataTable } from '@/components/data-table'
import { FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { useListQuery } from '@/hooks/use-list-query'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { useAuth } from '@/features/auth/auth-context'
import { formatDate } from '@/lib/format'
import { api } from '@/lib/api-client'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import type { DispatchDetail, DispatchRow } from '@/features/dispatch/dispatch-types'

type SalesReturnRow = {
  id: string
  code: string
  dispatch_id: string
  customer_id: string
  status: 'DRAFT' | 'RECEIVED' | 'CLOSED' | 'CANCELLED'
  source: 'ERP' | 'DEALER_PORTAL'
  created_at?: string
  received_at: string | null
}

function NewSalesReturnDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: () => void
}) {
  const queryClient = useQueryClient()
  const [dispatchId, setDispatchId] = useState('')
  const [warehouseId, setWarehouseId] = useState('')
  const [quantities, setQuantities] = useState<Record<string, string>>({})
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [notes, setNotes] = useState('')
  const dispatches = useQuery({
    queryKey: ['dispatch', 'returnable'],
    queryFn: () =>
      api.getPage<DispatchRow>('/dispatch', {
        params: {
          limit: 100,
          dispatch_type: 'SALES',
          status: 'DELIVERED',
          sort_by: 'dispatch_date',
          sort_dir: 'desc',
        },
      }),
    enabled: open,
    staleTime: staleTimes.transactional,
  })
  const detail = useQuery({
    queryKey: ['dispatch', 'detail', dispatchId],
    queryFn: () => api.get<DispatchDetail>(`/dispatch/${dispatchId}`),
    enabled: Boolean(dispatchId),
  })
  const warehouses = useWarehouseOptions(open)
  const create = useMutation({
    mutationFn: () =>
      api.post('/sales-returns', {
        dispatch_id: dispatchId,
        return_warehouse_id: warehouseId || null,
        notes: notes.trim() || null,
        lines: (detail.data?.lines ?? [])
          .filter((line) => quantities[line.id])
          .map((line) => ({
            dispatch_line_id: line.id,
            quantity: quantities[line.id],
            reason_code: reasons[line.id]?.trim() || 'CUSTOMER_RETURN',
            requires_qc: true,
          })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['sales-returns'] })
      onCreated()
      onClose()
      setDispatchId('')
      setQuantities({})
      setReasons({})
      setNotes('')
    },
  })
  const selected = detail.data
  const canSubmit = Boolean(dispatchId) && Object.values(quantities).some(Boolean)

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>New sales return</DialogTitle>
          <DialogDescription>
            Select a delivered sales dispatch and the quantities being returned.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="return-dispatch">Sales dispatch</Label>
            <Select
              value={dispatchId}
              onValueChange={(value) => {
                setDispatchId(value)
                setQuantities({})
                setReasons({})
              }}
            >
              <SelectTrigger id="return-dispatch">
                <SelectValue placeholder="Choose a dispatch" />
              </SelectTrigger>
              <SelectContent>
                {(dispatches.data?.data ?? []).map((dispatch) => (
                  <SelectItem key={dispatch.id} value={dispatch.id}>
                    {dispatch.code} · {dispatch.dispatch_date}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {dispatches.error && <FieldError message={dispatches.error.message} />}
          </div>
          <div className="space-y-2">
            <Label htmlFor="return-warehouse">Return warehouse (optional)</Label>
            <Select value={warehouseId} onValueChange={setWarehouseId}>
              <SelectTrigger id="return-warehouse">
                <SelectValue placeholder="Use dispatch warehouse" />
              </SelectTrigger>
              <SelectContent>
                {(warehouses.data ?? []).map((warehouse) => (
                  <SelectItem key={warehouse.id} value={warehouse.id}>
                    {warehouse.code} · {warehouse.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {selected && (
            <div className="space-y-3">
              {selected.lines.map((line) => (
                <div
                  key={line.id}
                  className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_8rem_1fr]"
                >
                  <div>
                    <p className="font-mono text-xs">{line.item_id}</p>
                    <p className="text-sm text-muted-foreground">Dispatched: {line.quantity}</p>
                  </div>
                  <Input
                    aria-label={`Return quantity for ${line.item_id}`}
                    type="number"
                    min="0"
                    max={line.quantity}
                    step="any"
                    placeholder="Quantity"
                    value={quantities[line.id] ?? ''}
                    onChange={(event) =>
                      setQuantities((current) => ({ ...current, [line.id]: event.target.value }))
                    }
                  />
                  <Input
                    aria-label={`Return reason for ${line.item_id}`}
                    placeholder="Reason"
                    value={reasons[line.id] ?? ''}
                    onChange={(event) =>
                      setReasons((current) => ({ ...current, [line.id]: event.target.value }))
                    }
                  />
                </div>
              ))}
            </div>
          )}
          <div className="space-y-2">
            <Label htmlFor="return-notes">Notes</Label>
            <Textarea
              id="return-notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </div>
          {create.error && <FieldError message={create.error.message} />}
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!canSubmit || create.isPending}
            loading={create.isPending}
            onClick={() => create.mutate()}
          >
            Create return
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default function SalesReturnsPage() {
  const { can } = useAuth()
  const [selected, setSelected] = useState<string | null>(null)
  const [newReturnOpen, setNewReturnOpen] = useState(false)
  const list = useListQuery<SalesReturnRow>({
    path: '/sales-returns',
    queryKey: (params) => ['sales-returns', 'list', params],
    defaultSortBy: 'created_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })

  const columns = useMemo<ColumnDef<SalesReturnRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Return',
        cell: ({ row }) => (
          <button
            type="button"
            className="font-medium underline-offset-4 hover:underline"
            onClick={() => setSelected(row.original.id)}
          >
            {row.original.code}
          </button>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      { accessorKey: 'source', header: 'Source' },
      { accessorKey: 'dispatch_id', header: 'Dispatch' },
      { accessorKey: 'customer_id', header: 'Customer' },
      {
        accessorKey: 'received_at',
        header: 'Received',
        cell: ({ row }) => (row.original.received_at ? formatDate(row.original.received_at) : '-'),
      },
    ],
    [],
  )

  async function transition(id: string, action: 'receive' | 'close') {
    await api.post(`/sales-returns/${id}/${action}`)
    await list.refetch()
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Sales returns"
        description="Track customer returns, receipt and QC follow-up."
        actions={
          can(P.SALES_RETURN_CREATE) ? (
            <Button onClick={() => setNewReturnOpen(true)}>New return</Button>
          ) : undefined
        }
      />
      {selected && (
        <div className="rounded-lg border border-border bg-card p-4 text-sm">
          <div className="flex items-center justify-between">
            <strong>Return {selected}</strong>
            <Button variant="ghost" onClick={() => setSelected(null)}>
              Close
            </Button>
          </div>
          <div className="mt-3 flex gap-2">
            {can(P.SALES_RETURN_CREATE) && (
              <>
                <Button onClick={() => void transition(selected, 'receive')}>Receive</Button>
                <Button variant="outline" onClick={() => void transition(selected, 'close')}>
                  Close
                </Button>
              </>
            )}
          </div>
        </div>
      )}
      <DataTable
        columns={columns}
        data={list.data}
        isLoading={list.isLoading}
        error={list.error}
        pagination={list.pagination}
        onPageChange={list.setPage}
        onSortChange={list.setSort}
      />
      <NewSalesReturnDialog
        open={newReturnOpen}
        onClose={() => setNewReturnOpen(false)}
        onCreated={() => void list.refetch()}
      />
    </div>
  )
}

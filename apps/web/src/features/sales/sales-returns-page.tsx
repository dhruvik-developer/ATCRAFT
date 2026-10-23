import { describeError } from '@/lib/messages'
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
import { queryKeys, staleTimes } from '@/lib/query-client'
import { useAuth } from '@/features/auth/auth-context'
import { formatDate } from '@/lib/format'
import { api } from '@/lib/api-client'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import type { DispatchDetail, DispatchRow } from '@/features/dispatch/dispatch-types'
import type { SchemaOf } from '@/types/api'

export type SalesReturnRow = SchemaOf<'SalesReturnSummary'>
type SalesReturnDetail = SchemaOf<'SalesReturnDetail'>

type SerialDisposition = 'accepted' | 'damaged' | 'scrapped'

function isSerialDisposition(value: string): value is SerialDisposition {
  return value === 'accepted' || value === 'damaged' || value === 'scrapped'
}

function undisposedSerialIds(detail: SalesReturnDetail | undefined): string[] {
  return (detail?.lines ?? []).flatMap((line) => {
    const disposed = new Set([
      ...line.accepted_serial_ids,
      ...line.damaged_serial_ids,
      ...line.scrapped_serial_ids,
    ])
    return line.serial_ids.filter((serialId) => !disposed.has(serialId))
  })
}

type SalesReturnAction = 'receive' | 'close' | 'cancel'

function availableReturnActions(status: SalesReturnRow['status']): SalesReturnAction[] {
  switch (status) {
    case 'DRAFT':
      return ['receive', 'cancel']
    case 'RECEIVED':
      return ['close', 'cancel']
    default:
      return []
  }
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
  const [serials, setSerials] = useState<Record<string, string>>({})
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
            serial_ids: (serials[line.id] ?? '')
              .split(',')
              .map((serial) => serial.trim())
              .filter(Boolean),
            reason_code: reasons[line.id]?.trim() || 'CUSTOMER_RETURN',
            requires_qc: true,
          })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.salesReturns.all })
      onCreated()
      onClose()
      setDispatchId('')
      setQuantities({})
      setSerials({})
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
                setSerials({})
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
            {dispatches.error && <FieldError message={describeError(dispatches.error)} />}
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
                    {line.serial_ids.length > 0 && (
                      <p className="break-all text-xs text-muted-foreground">
                        Available serial IDs: {line.serial_ids.join(', ')}
                      </p>
                    )}
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
                  {line.serial_ids.length > 0 && (
                    <Input
                      aria-label={`Returned serial IDs for ${line.item_id}`}
                      placeholder="Serial IDs, comma separated"
                      value={serials[line.id] ?? ''}
                      onChange={(event) =>
                        setSerials((current) => ({ ...current, [line.id]: event.target.value }))
                      }
                      className="sm:col-span-3"
                    />
                  )}
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
          {create.error && <FieldError message={describeError(create.error)} />}
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
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [newReturnOpen, setNewReturnOpen] = useState(false)
  const [transitionError, setTransitionError] = useState<string | null>(null)
  const [transitioning, setTransitioning] = useState<{
    id: string
    action: SalesReturnAction
  } | null>(null)
  const [unitDispositionOpen, setUnitDispositionOpen] = useState(false)
  const [unitDispositions, setUnitDispositions] = useState<Record<string, SerialDisposition>>({})
  const [unitDispositionError, setUnitDispositionError] = useState<string | null>(null)
  const list = useListQuery<SalesReturnRow>({
    path: '/sales-returns',
    queryKey: (params) => queryKeys.salesReturns.list(params),
    defaultSortBy: 'created_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })
  const selectedDetail = useQuery({
    queryKey: queryKeys.salesReturns.detail(selected ?? ''),
    queryFn: () => api.get<SalesReturnDetail>(`/sales-returns/${selected}`),
    enabled: Boolean(selected),
    staleTime: staleTimes.transactional,
  })
  const pendingSerials = undisposedSerialIds(selectedDetail.data)
  const disposeUnits = useMutation({
    mutationFn: (payload: { accepted: string[]; damaged: string[]; scrapped: string[] }) =>
      api.post(`/sales-returns/${selected}/units`, payload),
    onSuccess: async () => {
      setUnitDispositionOpen(false)
      setUnitDispositions({})
      setUnitDispositionError(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.salesReturns.all }),
        queryClient.invalidateQueries({
          queryKey: queryKeys.salesReturns.detail(selected ?? ''),
        }),
      ])
    },
    onError: (error) => {
      setUnitDispositionError(describeError(error, 'Serial dispositions could not be saved.'))
    },
  })

  const selectedReturn = list.data?.find((row) => row.id === selected) ?? null
  const availableActions = selectedReturn ? availableReturnActions(selectedReturn.status) : []

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

  async function transition(id: string, action: SalesReturnAction) {
    if (action === 'cancel') {
      const promptReason = window.prompt(
        'Provide a reason for cancelling this return.',
        'Customer requested cancellation',
      )
      if (promptReason === null) {
        return
      }
      const reason = promptReason.trim()
      if (!reason) {
        return
      }

      setTransitionError(null)
      setTransitioning({ id, action })
      try {
        await api.post(`/sales-returns/${id}/cancel`, { reason })
        setSelected(null)
        await list.refetch()
      } catch (error) {
        const message = describeError(error, 'The return action failed.')
        setTransitionError(message)
      } finally {
        setTransitioning(null)
      }
      return
    }

    setTransitionError(null)
    setTransitioning({ id, action })
    try {
      await api.post(`/sales-returns/${id}/${action}`)
      setSelected(null)
      await list.refetch()
    } catch (error) {
      const message = describeError(error, 'The return action failed.')
      setTransitionError(message)
    } finally {
      setTransitioning(null)
    }
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
      {selectedReturn && (
        <div className="rounded-lg border border-border bg-card p-4 text-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <strong>{selectedReturn.code}</strong>
              <div className="mt-1 text-xs text-muted-foreground">
                Status: <StatusBadge status={selectedReturn.status} />
              </div>
            </div>
            <Button variant="ghost" onClick={() => setSelected(null)}>
              Hide details
            </Button>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {can(P.SALES_RETURN_CREATE) && availableActions.length > 0 ? (
              availableActions.map((action) => (
                <Button
                  key={action}
                  variant={action === 'cancel' ? 'outline' : 'default'}
                  disabled={transitioning?.id === selectedReturn.id}
                  loading={
                    transitioning?.id === selectedReturn.id && transitioning.action === action
                  }
                  onClick={() => void transition(selectedReturn.id, action)}
                >
                  {action === 'receive' ? 'Receive' : action === 'close' ? 'Close' : 'Cancel'}
                </Button>
              ))
            ) : (
              <span className="text-xs text-muted-foreground">
                No actions available for this status.
              </span>
            )}
          </div>
          {transitionError && <FieldError message={transitionError} />}
          {selectedReturn.status === 'RECEIVED' &&
            can(P.SALES_RETURN_CREATE) &&
            pendingSerials.length > 0 && (
              <Button
                variant="outline"
                onClick={() => {
                  setUnitDispositions({})
                  setUnitDispositionError(null)
                  setUnitDispositionOpen(true)
                }}
              >
                Dispose serial units
              </Button>
            )}
          {selectedDetail.error && <FieldError message={describeError(selectedDetail.error)} />}
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
      <Dialog
        open={unitDispositionOpen}
        onOpenChange={(open) => !open && setUnitDispositionOpen(false)}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>Disposition returned serial units</DialogTitle>
            <DialogDescription>
              Accepted units return to stock. Damaged and scrapped units remain unavailable.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-3">
            {pendingSerials.map((serialId) => (
              <div
                key={serialId}
                className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_12rem]"
              >
                <code className="break-all text-xs">{serialId}</code>
                <Select
                  value={unitDispositions[serialId] ?? ''}
                  onValueChange={(value) => {
                    if (isSerialDisposition(value)) {
                      setUnitDispositions((current) => ({
                        ...current,
                        [serialId]: value,
                      }))
                    }
                  }}
                >
                  <SelectTrigger aria-label={`Disposition for ${serialId}`}>
                    <SelectValue placeholder="Choose disposition" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="accepted">Accepted</SelectItem>
                    <SelectItem value="damaged">Damaged</SelectItem>
                    <SelectItem value="scrapped">Scrapped</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            ))}
            {unitDispositionError && <FieldError message={unitDispositionError} />}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUnitDispositionOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={disposeUnits.isPending || !Object.keys(unitDispositions).length}
              loading={disposeUnits.isPending}
              onClick={() =>
                disposeUnits.mutate({
                  accepted: Object.entries(unitDispositions)
                    .filter(([, disposition]) => disposition === 'accepted')
                    .map(([serialId]) => serialId),
                  damaged: Object.entries(unitDispositions)
                    .filter(([, disposition]) => disposition === 'damaged')
                    .map(([serialId]) => serialId),
                  scrapped: Object.entries(unitDispositions)
                    .filter(([, disposition]) => disposition === 'scrapped')
                    .map(([serialId]) => serialId),
                })
              }
            >
              Save dispositions
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

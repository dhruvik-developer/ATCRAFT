import { useMutation, useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { type ColumnDef } from '@tanstack/react-table'

import { CellButton, DataTable } from '@/components/data-table'
import {
  DescriptionList,
  FieldError,
  LoadError,
  PageHeader,
  ReasonDialog,
  StatusBadge,
} from '@/components/shared'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroller,
} from '@/components/ui/table'
import { useListQuery } from '@/hooks/use-list-query'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { useAuth } from '@/features/auth/auth-context'
import { EMPTY_VALUE, formatDate, formatQuantity, humanise } from '@/lib/format'
import { api } from '@/lib/api-client'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { toast } from '@/lib/toast'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { DispatchPicker } from '@/features/dispatch/dispatch-picker'
import { dispatchKeys, type DispatchDetail } from '@/features/dispatch/dispatch-types'
import {
  labelSerials,
  resolveSerialLabels,
  type NamedSerials,
  type SerialUnit,
} from '@/features/inventory/serial-numbers'
import type { SchemaOf } from '@/types/api'

export type SalesReturnRow = SchemaOf<'SalesReturnSummary'>
type SalesReturnDetail = SchemaOf<'SalesReturnDetail'>
type SalesReturnLine = SchemaOf<'SalesReturnLineRead'>

type SerialDisposition = 'accepted' | 'damaged' | 'scrapped'

function isSerialDisposition(value: string): value is SerialDisposition {
  return value === 'accepted' || value === 'damaged' || value === 'scrapped'
}

/** Where a return came from: raised here, or requested by the dealer. */
const RETURN_SOURCES: Record<SalesReturnRow['source'], string> = {
  ERP: 'ERP',
  DEALER_PORTAL: 'Dealer portal',
}

/** The customer as a person reads them: code, then name. */
function customerLabel(row: SalesReturnRow): string {
  if (!row.customer_code) return EMPTY_VALUE
  return row.customer_name ? `${row.customer_code} - ${row.customer_name}` : row.customer_code
}

/** The verdict a returned unit was given, once it has one. */
function dispositionOf(line: SalesReturnLine, serialId: string): SerialDisposition | null {
  if (line.accepted_serial_ids.includes(serialId)) return 'accepted'
  if (line.damaged_serial_ids.includes(serialId)) return 'damaged'
  if (line.scrapped_serial_ids.includes(serialId)) return 'scrapped'
  return null
}

/** The returned units still waiting for a verdict, each named by its label. */
function undisposedSerials(detail: SalesReturnDetail | undefined): SerialUnit[] {
  return (detail?.lines ?? []).flatMap((line) =>
    labelSerials(line.serial_ids, line.serial_numbers).filter(
      (unit) => dispositionOf(line, unit.id) === null,
    ),
  )
}

/** A reason picked from the API's vocabulary reads as words; one typed by hand, as typed. */
function reasonLabel(reason: string): string {
  return /^[A-Z0-9_]+$/.test(reason) ? humanise(reason) : reason
}

/** How the form names a dispatched line: its number, and its item when known. */
function lineName(line: { line_number: number; item_code?: string | null }): string {
  return line.item_code
    ? `line ${line.line_number} (${line.item_code})`
    : `line ${line.line_number}`
}

/** What is wrong with the labels typed against a line, said beside the field. */
function serialProblem(named: NamedSerials): string | null {
  const said: string[] = []
  if (named.unknown.length > 0)
    said.push(`Not dispatched on this line: ${named.unknown.join(', ')}.`)
  if (named.repeated.length > 0) said.push(`Named more than once: ${named.repeated.join(', ')}.`)
  return said.length > 0 ? said.join(' ') : null
}

type SalesReturnAction = 'receive' | 'close' | 'cancel'

/** Named for the return: the dialog's own X is already "Close". */
const ACTION_LABELS: Record<SalesReturnAction, string> = {
  receive: 'Receive return',
  close: 'Close return',
  cancel: 'Cancel return',
}

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
  // A refusal is said once the person has left the field or tried to create,
  // not while a label is still being typed.
  const [serialsLeft, setSerialsLeft] = useState<Record<string, boolean>>({})
  const [attempted, setAttempted] = useState(false)
  const detail = useQuery({
    queryKey: dispatchKeys.detail(dispatchId),
    queryFn: ({ signal }) => api.get<DispatchDetail>(`/dispatch/${dispatchId}`, { signal }),
    enabled: Boolean(dispatchId),
  })
  const warehouses = useWarehouseOptions(open)
  // The API takes serial ids, which nobody can read off a unit. The operator
  // types or scans the labels instead, and each line's are matched against the
  // units that line shipped - so a unit it did not ship, or one named twice,
  // is refused beside the field rather than by the API after the whole form.
  const lines = (detail.data?.lines ?? []).map((line) => {
    const units = labelSerials(line.serial_ids, line.serial_numbers)
    const named = resolveSerialLabels(serials[line.id] ?? '', units)
    return { line, units, named, problem: serialProblem(named) }
  })
  const create = useMutation({
    mutationFn: () =>
      api.post<SalesReturnDetail>('/sales-returns', {
        dispatch_id: dispatchId,
        return_warehouse_id: warehouseId || null,
        notes: notes.trim() || null,
        lines: lines
          .filter(({ line }) => quantities[line.id])
          .map(({ line, named }) => ({
            dispatch_line_id: line.id,
            quantity: quantities[line.id],
            serial_ids: named.ids,
            reason_code: reasons[line.id]?.trim() || 'CUSTOMER_RETURN',
            requires_qc: true,
          })),
      }),
    onSuccess: (created) => {
      toast.success(success.created('Sales return'), created.code)
      void queryClient.invalidateQueries({ queryKey: queryKeys.salesReturns.all })
      onCreated()
      onClose()
      setDispatchId('')
      clearLines()
      setNotes('')
    },
    // Said in the dialog, above its footer; a toast would repeat it.
    meta: { suppressErrorToast: true },
  })
  const selected = detail.data
  const canSubmit = Boolean(dispatchId) && Object.values(quantities).some(Boolean)

  function clearLines() {
    setQuantities({})
    setSerials({})
    setReasons({})
    setSerialsLeft({})
    setAttempted(false)
  }

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
          {/* Searched on the server: a fixed first page made any older delivery
              impossible to return against. */}
          <DispatchPicker
            id="return-dispatch"
            label="Sales dispatch"
            value={dispatchId || null}
            onChange={(next) => {
              setDispatchId(next ?? '')
              clearLines()
            }}
            filters={{ dispatch_type: 'SALES', status: 'DELIVERED' }}
            placeholder="Search delivered sales dispatches"
          />
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
              {lines.map(({ line, units, problem }) => {
                const name = lineName(line)
                const errorId = `return-serials-${line.id}-error`
                const refused = problem && (attempted || serialsLeft[line.id]) ? problem : null
                return (
                  <div
                    key={line.id}
                    className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_8rem_1fr]"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium">
                        Line {line.line_number}
                        {line.item_code && <span className="ml-2">{line.item_code}</span>}
                      </p>
                      {line.item_name && (
                        <p className="truncate text-xs text-muted-foreground">{line.item_name}</p>
                      )}
                      <p className="text-sm text-muted-foreground">
                        Dispatched: {formatQuantity(line.quantity)}
                      </p>
                    </div>
                    <Input
                      aria-label={`Return quantity for ${name}`}
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
                      aria-label={`Return reason for ${name}`}
                      placeholder="Reason"
                      value={reasons[line.id] ?? ''}
                      onChange={(event) =>
                        setReasons((current) => ({ ...current, [line.id]: event.target.value }))
                      }
                    />
                    {units.length > 0 && (
                      <div className="sm:col-span-3">
                        <Textarea
                          aria-label={`Returned serial numbers for ${name}`}
                          rows={2}
                          placeholder="Serial numbers, one per line or comma separated"
                          className="font-mono text-xs"
                          value={serials[line.id] ?? ''}
                          onChange={(event) =>
                            setSerials((current) => ({ ...current, [line.id]: event.target.value }))
                          }
                          onBlur={() =>
                            setSerialsLeft((current) => ({ ...current, [line.id]: true }))
                          }
                          aria-invalid={Boolean(refused)}
                          aria-describedby={refused ? errorId : undefined}
                        />
                        <p className="mt-1 break-all text-xs text-muted-foreground">
                          Dispatched serials: {units.map((unit) => unit.label).join(', ')}
                        </p>
                        <FieldError id={errorId} message={refused ?? undefined} />
                      </div>
                    )}
                  </div>
                )
              })}
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
          {create.error && (
            <FieldError message={fieldErrorSummary(create.error) || describeError(create.error)} />
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!canSubmit || create.isPending}
            loading={create.isPending}
            onClick={() => {
              setAttempted(true)
              if (lines.every(({ problem }) => problem === null)) create.mutate()
            }}
          >
            Create return
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** What came back: each line's item, quantity, units with their verdicts, and its credit. */
function ReturnLines({ lines }: { lines: SalesReturnLine[] }) {
  return (
    <TableScroller label="Returned lines">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="whitespace-nowrap">Item</TableHead>
            <TableHead align="right" className="whitespace-nowrap">
              Quantity
            </TableHead>
            <TableHead className="whitespace-nowrap">Serials</TableHead>
            <TableHead className="whitespace-nowrap">Reason</TableHead>
            <TableHead align="right" className="whitespace-nowrap">
              Credited
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines.map((line) => (
            <TableRow key={line.id} className="[&>td]:align-top">
              <TableCell>
                <span className="block font-medium">{line.item_code ?? EMPTY_VALUE}</span>
                {line.item_name && (
                  <span className="block max-w-[16rem] truncate text-xs text-muted-foreground">
                    {line.item_name}
                  </span>
                )}
              </TableCell>
              <TableCell align="right">{formatQuantity(line.quantity)}</TableCell>
              <TableCell>
                {line.serial_ids.length === 0 ? (
                  EMPTY_VALUE
                ) : (
                  <ul className="space-y-0.5">
                    {labelSerials(line.serial_ids, line.serial_numbers).map((unit) => {
                      const verdict = dispositionOf(line, unit.id)
                      return (
                        <li key={unit.id} className="whitespace-nowrap">
                          <span className="font-mono text-xs">{unit.label}</span>
                          {verdict && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              {humanise(verdict)}
                            </span>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </TableCell>
              <TableCell>{reasonLabel(line.reason_code)}</TableCell>
              <TableCell align="right">{formatQuantity(line.credited_quantity)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroller>
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
  const [cancelling, setCancelling] = useState<SalesReturnRow | null>(null)
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
    queryFn: ({ signal }) => api.get<SalesReturnDetail>(`/sales-returns/${selected}`, { signal }),
    enabled: Boolean(selected),
    staleTime: staleTimes.transactional,
  })
  const pendingSerials = undisposedSerials(selectedDetail.data)

  /**
   * Drop what a return's write changed beyond the return itself.
   *
   * Receiving posts the units into stock, holds the inspected ones in QC with an
   * inspection raised for each, and counts them back off the dispatch; settling
   * held units moves them to free stock, damaged or scrap. Each of the three,
   * closing too, may credit the accepted part - a credit note against the
   * customer's balance (`_issue_return_credit_note`). Only this screen's own
   * keys were dropped, so the stock, the inspection queue and the credit read
   * as before the write until each expired.
   */
  function invalidateReturnEffects(write: 'receive' | 'dispose' | 'close') {
    const roots: QueryKey[] = [
      queryKeys.salesReturns.all,
      queryKeys.finance.all,
      queryKeys.customers.all,
      queryKeys.dealerPortal.all,
    ]
    if (write !== 'close') roots.push(queryKeys.inventory.all, queryKeys.allocation.all)
    if (write === 'receive') roots.push(queryKeys.quality.all, queryKeys.dispatch.all)
    return Promise.all(roots.map((queryKey) => queryClient.invalidateQueries({ queryKey })))
  }
  // The reason is the person's own: it is collected in a dialog that opens
  // empty, never pre-filled, because whatever a hurried OK sends is what the
  // audit trail keeps.
  const cancelReturn = useMutation({
    mutationFn: ({ target, reason }: { target: SalesReturnRow; reason: string }) =>
      api.post(`/sales-returns/${target.id}/cancel`, { reason }),
    onSuccess: async (_result, { target }) => {
      toast.success(success.cancelled('Sales return'), target.code)
      setCancelling(null)
      closeDetail()
      await queryClient.invalidateQueries({ queryKey: queryKeys.salesReturns.all })
    },
    // The reason dialog shows the refusal beside what was typed.
    meta: { suppressErrorToast: true },
  })
  const disposeUnits = useMutation({
    mutationFn: (payload: { accepted: string[]; damaged: string[]; scrapped: string[] }) =>
      api.post(`/sales-returns/${selected}/units`, payload),
    onSuccess: async () => {
      toast.success(success.recorded('Serial dispositions'))
      setUnitDispositionOpen(false)
      setUnitDispositions({})
      setUnitDispositionError(null)
      // The return's detail is under its root, which this drops with the rest.
      await invalidateReturnEffects('dispose')
    },
    onError: (error) => {
      // The dispositions are plain choices, so a 422 is named here in words.
      setUnitDispositionError(
        fieldErrorSummary(error) || describeError(error, 'Serial dispositions could not be saved.'),
      )
    },
    meta: { suppressErrorToast: true },
  })

  // The detail once it is read, so the actions follow the return's own status
  // rather than a list row that may be older.
  const selectedReturn: SalesReturnRow | null =
    selectedDetail.data ?? list.data?.find((row) => row.id === selected) ?? null
  const availableActions = selectedReturn ? availableReturnActions(selectedReturn.status) : []
  const mayDispose =
    selectedReturn?.status === 'RECEIVED' && can(P.SALES_RETURN_CREATE) && pendingSerials.length > 0
  const mayAct = can(P.SALES_RETURN_CREATE) && availableActions.length > 0

  const columns = useMemo<ColumnDef<SalesReturnRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Return',
        cell: ({ row }) => (
          <CellButton onClick={() => setSelected(row.original.id)}>{row.original.code}</CellButton>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        accessorKey: 'source',
        header: 'Source',
        cell: ({ row }) => RETURN_SOURCES[row.original.source],
      },
      {
        id: 'dispatch',
        header: 'Dispatch',
        cell: ({ row }) => row.original.dispatch_code ?? EMPTY_VALUE,
      },
      {
        id: 'customer',
        header: 'Customer',
        cell: ({ row }) =>
          row.original.customer_code ? (
            <div className="min-w-0">
              <span className="block font-medium">{row.original.customer_code}</span>
              {row.original.customer_name && (
                <span className="block max-w-[16rem] truncate text-xs text-muted-foreground">
                  {row.original.customer_name}
                </span>
              )}
            </div>
          ) : (
            EMPTY_VALUE
          ),
      },
      {
        accessorKey: 'received_at',
        header: 'Received',
        cell: ({ row }) =>
          row.original.received_at ? formatDate(row.original.received_at) : EMPTY_VALUE,
      },
    ],
    [],
  )

  function closeDetail() {
    setSelected(null)
    setTransitionError(null)
  }

  async function transition(row: SalesReturnRow, action: Exclude<SalesReturnAction, 'cancel'>) {
    const { id } = row
    setTransitionError(null)
    setTransitioning({ id, action })
    try {
      await api.post(`/sales-returns/${id}/${action}`)
      toast.success(
        action === 'receive'
          ? success.statusChanged('Sales return', 'received')
          : success.closed('Sales return'),
        row.code,
      )
      closeDetail()
      // The detail as well as the list: the dialog reads the return's status
      // from its own detail, which would otherwise reopen at the old one.
      await invalidateReturnEffects(action)
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
      {/* No sort is offered: the endpoint lists returns newest first and takes
          no sort, so a heading that offered one would change nothing. */}
      <DataTable
        columns={columns}
        data={list.data}
        isLoading={list.isLoading}
        error={list.error}
        pagination={list.pagination}
        onPageChange={list.setPage}
      />
      {/* A dialog, like the credit note's: opened from a row anywhere in a long
          list, a panel above the table would open out of sight. */}
      <Dialog
        open={selectedReturn !== null}
        onOpenChange={(open) => {
          if (!open && !transitioning) closeDetail()
        }}
      >
        {selectedReturn && (
          <DialogContent size="lg">
            <DialogHeader>
              <DialogTitle>{selectedReturn.code}</DialogTitle>
              <DialogDescription>{customerLabel(selectedReturn)}</DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-4 text-sm">
              <DescriptionList
                columns={3}
                items={[
                  { label: 'Status', value: <StatusBadge status={selectedReturn.status} /> },
                  { label: 'Dispatch', value: selectedReturn.dispatch_code ?? EMPTY_VALUE },
                  { label: 'Source', value: RETURN_SOURCES[selectedReturn.source] },
                  {
                    label: 'Received',
                    value: selectedReturn.received_at
                      ? formatDate(selectedReturn.received_at)
                      : EMPTY_VALUE,
                  },
                  ...(selectedReturn.cancellation_reason
                    ? [{ label: 'Cancellation reason', value: selectedReturn.cancellation_reason }]
                    : []),
                ]}
              />
              {selectedReturn.notes && (
                <p className="text-muted-foreground">{selectedReturn.notes}</p>
              )}
              {selectedDetail.isLoading && (
                <p className="text-muted-foreground">Loading line details…</p>
              )}
              {selectedDetail.error && (
                <LoadError
                  error={selectedDetail.error}
                  onRetry={() => void selectedDetail.refetch()}
                />
              )}
              {selectedDetail.data && <ReturnLines lines={selectedDetail.data.lines} />}
              {transitionError && <FieldError message={transitionError} />}
            </DialogBody>
            {(mayAct || mayDispose) && (
              <DialogFooter>
                {mayDispose && (
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
                {mayAct &&
                  availableActions.map((action) => (
                    <Button
                      key={action}
                      variant={action === 'cancel' ? 'destructive-outline' : 'default'}
                      disabled={transitioning?.id === selectedReturn.id}
                      loading={
                        transitioning?.id === selectedReturn.id && transitioning.action === action
                      }
                      onClick={() =>
                        action === 'cancel'
                          ? setCancelling(selectedReturn)
                          : void transition(selectedReturn, action)
                      }
                    >
                      {ACTION_LABELS[action]}
                    </Button>
                  ))}
              </DialogFooter>
            )}
          </DialogContent>
        )}
      </Dialog>
      <NewSalesReturnDialog
        open={newReturnOpen}
        onClose={() => setNewReturnOpen(false)}
        onCreated={() => void list.refetch()}
      />
      <ReasonDialog
        open={cancelling !== null}
        onOpenChange={(open) => {
          if (!open) {
            setCancelling(null)
            cancelReturn.reset()
          }
        }}
        title={`Cancel ${cancelling?.code ?? 'this return'}?`}
        description="A cancelled return cannot be reopened. The reason is kept on the return and in its audit trail."
        confirmLabel="Cancel return"
        destructive
        loading={cancelReturn.isPending}
        error={cancelReturn.error}
        inputId="sales-return-cancel-reason"
        onConfirm={(reason) => {
          if (cancelling) cancelReturn.mutate({ target: cancelling, reason })
        }}
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
            {pendingSerials.map((unit) => (
              <div
                key={unit.id}
                className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_12rem]"
              >
                <code className="break-all text-xs">{unit.label}</code>
                <Select
                  value={unitDispositions[unit.id] ?? ''}
                  onValueChange={(value) => {
                    if (isSerialDisposition(value)) {
                      setUnitDispositions((current) => ({
                        ...current,
                        [unit.id]: value,
                      }))
                    }
                  }}
                >
                  <SelectTrigger aria-label={`Disposition for ${unit.label}`}>
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

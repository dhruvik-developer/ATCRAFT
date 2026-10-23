/**
 * The tracker's write paths and its one read-through panel.
 *
 * Everything here is deliberately a dialog rather than a route: a lot or a
 * serial is a fact you look up while doing something else, and losing the
 * filtered register behind a navigation is the fastest way to make an operator
 * stop using it. The serial trace is read-only; every control that changes
 * stock or a lifecycle state is behind INVENTORY_BATCH_MANAGE.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { FieldError, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Input, Label, Skeleton, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { REASON_MIN_LENGTH } from '@/lib/constants'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatQuantity,
  humanise,
  toDateInput,
  toDecimal,
} from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import MovementBatchPicker from './movement-batch-picker'
import { ItemPicker, WarehouseSelect } from './pickers'
import type { StockBatchRow, StockSerialDetail, StockSerialRow } from './types'

const SERIAL_BULK_MAX = 500

function errorMessage(error: unknown): string | undefined {
  return error ? describeError(error) : undefined
}

function splitSerials(value: string): string[] {
  return value
    .split(/[\s,;]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)
}

// ------------------------------------------------------------- register batch

export function RegisterBatchDialog({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const [itemId, setItemId] = useState('')
  const [batchNumber, setBatchNumber] = useState('')
  const [supplierBatch, setSupplierBatch] = useState('')
  const [manufacturedOn, setManufacturedOn] = useState('')
  const [expiresOn, setExpiresOn] = useState('')
  const [receivedOn, setReceivedOn] = useState(toDateInput(new Date()))
  const [initialQuantity, setInitialQuantity] = useState('0')
  const [unitCost, setUnitCost] = useState('0')
  const [notes, setNotes] = useState('')

  const create = useMutation({
    mutationFn: () =>
      api.post<StockBatchRow>('/inventory/batches', {
        item_id: itemId,
        batch_number: batchNumber.trim(),
        supplier_batch_number: supplierBatch.trim() || null,
        manufactured_on: manufacturedOn || null,
        expires_on: expiresOn || null,
        received_on: receivedOn || null,
        initial_quantity: initialQuantity.trim() || '0',
        unit_cost: unitCost.trim() || '0',
        notes: notes.trim() || null,
      }),
    onSuccess: (batch) => {
      toast.success(success.created('Batch'), batch.batch_number)
      void client.invalidateQueries({ queryKey: queryKeys.inventory.all })
      onClose()
    },
  })

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Register a stock batch</DialogTitle>
          <DialogDescription>
            A batch-tracked item cannot receive stock until its lot exists. Register the label
            goods-in or production will use.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <ItemPicker
            id="batch-item"
            label="Item"
            value={itemId || null}
            onChange={(value) => setItemId(value ?? '')}
            filters={{ is_batch_tracked: true }}
            placeholder="Select a batch-tracked item"
            emptyMessage="No batch-tracked item matches that search."
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="batch-number" required>
                Batch number
              </Label>
              <Input
                id="batch-number"
                value={batchNumber}
                maxLength={64}
                onChange={(event) => setBatchNumber(event.target.value)}
                placeholder="e.g. L2026-07"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-supplier">Supplier batch number</Label>
              <Input
                id="batch-supplier"
                value={supplierBatch}
                maxLength={64}
                onChange={(event) => setSupplierBatch(event.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-manufactured">Manufactured on</Label>
              <Input
                id="batch-manufactured"
                type="date"
                value={manufacturedOn}
                onChange={(event) => setManufacturedOn(event.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-expires">Expires on</Label>
              <Input
                id="batch-expires"
                type="date"
                value={expiresOn}
                onChange={(event) => setExpiresOn(event.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-received">Received on</Label>
              <Input
                id="batch-received"
                type="date"
                value={receivedOn}
                onChange={(event) => setReceivedOn(event.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-initial">Initial quantity</Label>
              <Input
                id="batch-initial"
                inputMode="decimal"
                value={initialQuantity}
                onChange={(event) => setInitialQuantity(event.target.value)}
                title="Informational only; stock arrives through a receipt"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-cost">Unit cost</Label>
              <Input
                id="batch-cost"
                inputMode="decimal"
                value={unitCost}
                onChange={(event) => setUnitCost(event.target.value)}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="batch-notes">Notes</Label>
            <Textarea
              id="batch-notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </div>
          <FieldError message={errorMessage(create.error)} />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            loading={create.isPending}
            disabled={!itemId || !batchNumber.trim()}
            onClick={() => create.mutate()}
          >
            Register batch
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------- lifecycle action

const ACTION_COPY: Record<
  'quarantine' | 'activate' | 'expire',
  {
    title: string
    description: string
    confirm: string
    done: string
    destructive: boolean
  }
> = {
  quarantine: {
    title: 'Quarantine this lot?',
    description:
      'Available stock of this lot in the warehouses you can see moves to QC hold. It stays owned and countable, but nothing can be promised or issued from it.',
    confirm: 'Quarantine lot',
    done: 'Batch quarantined',
    destructive: true,
  },
  activate: {
    title: 'Release this lot from quarantine?',
    description:
      'The lot becomes active again. Stock already in QC hold stays there deliberately: releasing stock from a hold is a separate warehouse movement with its own record.',
    confirm: 'Activate lot',
    done: 'Batch activated',
    destructive: false,
  },
  expire: {
    title: 'Expire this lot?',
    description:
      'An expired lot cannot be issued to production or dispatch. The stock remains on the books so it can be written off, transferred or disposed of with a record.',
    confirm: 'Expire lot',
    done: 'Batch expired',
    destructive: true,
  },
}

export function BatchActionDialog({
  batch,
  action,
  onClose,
}: {
  batch: StockBatchRow
  action: 'quarantine' | 'activate' | 'expire'
  onClose: () => void
}) {
  const client = useQueryClient()
  const [reason, setReason] = useState('')
  const copy = ACTION_COPY[action]
  const run = useMutation({
    mutationFn: () =>
      api.post<StockBatchRow>(`/inventory/batches/${batch.id}/${action}`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(copy.done, batch.batch_number)
      void client.invalidateQueries({ queryKey: queryKeys.inventory.all })
      onClose()
    },
  })

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>
            <span className="block font-mono text-xs text-foreground">
              {batch.item_code ?? batch.item_id} / {batch.batch_number}
            </span>
            {copy.description}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-1.5">
          <Label htmlFor="batch-action-reason" required>
            Reason
          </Label>
          <Textarea
            id="batch-action-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="What was found, and who decided?"
          />
          <p className="text-xs text-muted-foreground">
            Recorded against the lot&apos;s status history for recall and audit.
          </p>
          <FieldError message={errorMessage(run.error)} />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={run.isPending}>
            Cancel
          </Button>
          <Button
            variant={copy.destructive ? 'destructive' : 'default'}
            loading={run.isPending}
            disabled={reason.trim().length < REASON_MIN_LENGTH}
            onClick={() => run.mutate()}
          >
            {copy.confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------- serial bulk

export function RegisterSerialsDialog({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const [itemId, setItemId] = useState('')
  const [warehouseId, setWarehouseId] = useState('')
  const [batchId, setBatchId] = useState('')
  const [text, setText] = useState('')
  const [prefix, setPrefix] = useState('')
  const [start, setStart] = useState('1')
  const [end, setEnd] = useState('10')
  const [padding, setPadding] = useState('4')
  const [receivedOn, setReceivedOn] = useState(toDateInput(new Date()))
  const [notes, setNotes] = useState('')

  const numbers = splitSerials(text)
  const unique = new Set(numbers)
  const overLimit = unique.size > SERIAL_BULK_MAX
  const rangeError =
    !Number.isInteger(Number(start)) ||
    !Number.isInteger(Number(end)) ||
    Number(end) < Number(start) ||
    Number(end) - Number(start) + 1 > SERIAL_BULK_MAX

  const addRange = () => {
    const from = Number(start)
    const to = Number(end)
    const width = Math.max(1, Math.min(12, Number(padding) || 1))
    if (rangeError || from < 1 || to < 1) return
    const generated = Array.from(
      { length: to - from + 1 },
      (_, offset) => `${prefix}${String(from + offset).padStart(width, '0')}`,
    )
    setText((current) => [...splitSerials(current), ...generated].join('\n'))
  }

  const create = useMutation({
    mutationFn: () =>
      api.post<StockSerialRow[]>('/inventory/serials/bulk', {
        item_id: itemId,
        serial_numbers: [...unique],
        batch_id: batchId || null,
        warehouse_id: warehouseId || null,
        location_id: null,
        received_on: receivedOn || null,
        unit_cost: '0',
        notes: notes.trim() || null,
      }),
    onSuccess: (created) => {
      toast.success(success.created('Serials'), `${created.length} unit(s)`)
      void client.invalidateQueries({ queryKey: queryKeys.inventory.all })
      onClose()
    },
  })

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Register serial numbers</DialogTitle>
          <DialogDescription>
            Register labels ahead of the goods, or adopt a pasted run from a supplier. A later
            receipt ties each unit to its line and lot; registering here does not move stock.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <ItemPicker
            id="serials-item"
            label="Item"
            value={itemId || null}
            onChange={(value) => {
              setItemId(value ?? '')
              setBatchId('')
            }}
            filters={{ is_serial_tracked: true }}
            placeholder="Select a serial-tracked item"
            emptyMessage="No serial-tracked item matches that search."
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <WarehouseSelect
              id="serials-warehouse"
              label="Warehouse"
              value={warehouseId}
              onChange={setWarehouseId}
              includeAll={false}
              placeholder="Expected warehouse"
            />
            <MovementBatchPicker
              id="serials-batch"
              itemId={itemId}
              value={batchId}
              onChange={setBatchId}
            />
            <div className="space-y-1.5">
              <Label htmlFor="serials-received">Received on</Label>
              <Input
                id="serials-received"
                type="date"
                value={receivedOn}
                onChange={(event) => setReceivedOn(event.target.value)}
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="serials-text" required>
              Serial numbers
            </Label>
            <Textarea
              id="serials-text"
              rows={7}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={'One per line, or separated by commas\nSN-0001\nSN-0002'}
              className="font-mono text-xs"
              aria-describedby="serials-count"
            />
            <p
              id="serials-count"
              className={overLimit ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}
            >
              {unique.size} unique serial(s); the register accepts up to {SERIAL_BULK_MAX} at a
              time.
            </p>
          </div>

          <div className="rounded-md border p-3">
            <p className="mb-2 text-xs font-medium text-muted-foreground">
              Or generate a label range
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-[8rem] flex-1 space-y-1">
                <Label htmlFor="serials-prefix" className="text-xs">
                  Prefix
                </Label>
                <Input
                  id="serials-prefix"
                  value={prefix}
                  maxLength={32}
                  onChange={(event) => setPrefix(event.target.value)}
                />
              </div>
              <div className="w-20 space-y-1">
                <Label htmlFor="serials-start" className="text-xs">
                  From
                </Label>
                <Input
                  id="serials-start"
                  inputMode="numeric"
                  value={start}
                  onChange={(event) => setStart(event.target.value)}
                />
              </div>
              <div className="w-20 space-y-1">
                <Label htmlFor="serials-end" className="text-xs">
                  To
                </Label>
                <Input
                  id="serials-end"
                  inputMode="numeric"
                  value={end}
                  onChange={(event) => setEnd(event.target.value)}
                />
              </div>
              <div className="w-20 space-y-1">
                <Label htmlFor="serials-padding" className="text-xs">
                  Digits
                </Label>
                <Input
                  id="serials-padding"
                  inputMode="numeric"
                  value={padding}
                  onChange={(event) => setPadding(event.target.value)}
                />
              </div>
              <Button
                type="button"
                variant="outline"
                onClick={addRange}
                disabled={rangeError || !prefix.trim()}
              >
                Add range
              </Button>
            </div>
            {rangeError && (
              <p className="mt-2 text-xs text-destructive">
                Choose whole numbers with To not before From, and at most {SERIAL_BULK_MAX} at a
                time.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="serials-notes">Notes</Label>
            <Textarea
              id="serials-notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </div>
          <FieldError message={errorMessage(create.error)} />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            loading={create.isPending}
            disabled={!itemId || unique.size === 0 || overLimit}
            onClick={() => create.mutate()}
          >
            Register {unique.size || ''} serial(s)
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------- serial trace

export function SerialDetailDialog({
  serialId,
  onClose,
}: {
  serialId: string
  onClose: () => void
}) {
  const { can } = useAuth()
  const canSeeValue = can(P.INVENTORY_VALUATION_VIEW)
  const detail = useQuery({
    queryKey: queryKeys.inventory.serial(serialId),
    queryFn: () => api.get<StockSerialDetail>('/inventory/serials/' + serialId),
    staleTime: staleTimes.live,
  })

  const serial = detail.data?.serial
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{serial?.serial_number ?? 'Serial trace'}</DialogTitle>
          <DialogDescription>
            {serial
              ? `${serial.item_code ?? serial.item_id} - ${serial.item_name ?? 'Retired item'}`
              : 'Loading the unit and every movement that touched it.'}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          {detail.isLoading && <Skeleton className="h-24 w-full" />}
          <FieldError message={errorMessage(detail.error)} />
          {serial && (
            <div className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <p className="text-xs text-muted-foreground">Status</p>
                <StatusBadge status={serial.status} />
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Batch</p>
                <p className="font-mono text-xs">{serial.batch_number ?? 'No batch'}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Warehouse</p>
                <p>
                  {serial.warehouse_code ??
                    (serial.warehouse_id ? serial.warehouse_id : 'No longer in a warehouse')}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Received</p>
                <p>{serial.received_on ? formatDate(serial.received_on) : '-'}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Recorded</p>
                <p>{formatDateTime(serial.created_at)}</p>
              </div>
              {canSeeValue && (
                <div>
                  <p className="text-xs text-muted-foreground">Unit cost</p>
                  <p>{formatMoney(serial.unit_cost)}</p>
                </div>
              )}
            </div>
          )}

          {detail.data && (
            <div className="overflow-x-auto rounded border">
              <table className="w-full text-sm">
                <thead className="bg-muted text-left">
                  <tr>
                    <th className="p-2 font-medium">Occurred</th>
                    <th className="p-2 font-medium">Movement</th>
                    <th className="p-2 font-medium">Warehouse</th>
                    <th className="p-2 text-right font-medium">Quantity</th>
                    <th className="p-2 font-medium">Reference</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.data.movements ?? []).map((movement) => {
                    const quantity = toDecimal(movement.quantity)
                    return (
                      <tr key={movement.id} className="border-t align-top">
                        <td className="whitespace-nowrap p-2 tabular">
                          {formatDateTime(movement.occurred_at)}
                        </td>
                        <td className="p-2">
                          {humanise(movement.transaction_type)}
                          <span className="block text-xs text-muted-foreground">
                            {humanise(movement.stock_state)}
                          </span>
                        </td>
                        <td className="p-2">{movement.warehouse_code ?? '-'}</td>
                        <td
                          className={
                            quantity.greaterThan(0)
                              ? 'p-2 text-right font-medium text-success tabular'
                              : 'p-2 text-right font-medium text-destructive tabular'
                          }
                        >
                          {formatQuantity(movement.quantity)}
                        </td>
                        <td className="p-2">
                          {movement.reference_code ?? '-'}
                          <span className="block max-w-[16rem] truncate text-xs text-muted-foreground">
                            {movement.reason}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                  {(detail.data.movements ?? []).length === 0 && (
                    <tr>
                      <td colSpan={5} className="p-4 text-center text-sm text-muted-foreground">
                        This unit has been registered but no movement has touched it yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

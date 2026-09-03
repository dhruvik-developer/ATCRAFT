/**
 * Choosing the *identified* stock a document is about: a batch, or the exact
 * units by serial.
 *
 * Both the dispatch bay and the packing bench need these, and they need them to
 * behave identically - a serial named in a carton is the same unit the
 * consignment then ships - so they are written once here rather than twice.
 *
 * docs/assumptions.md section 23 is the rule they exist for. `is_serial_tracked`
 * gates every movement that changes whose hands hold a unit, packing into a
 * sealed carton and dispatch included, so the operator has to be able to name
 * the units. A picker that cannot offer any says so plainly instead of letting
 * the operator discover it from a 422 at the end of a long form.
 *
 * Neither picker filters in the browser. The item and warehouse narrow the
 * search on the server; a unit that is not `IN_STOCK` is shown with its status
 * and cannot be chosen, because hiding it would make "where is that serial?"
 * unanswerable while quietly dropping matches past the first page.
 */

import { useQuery } from '@tanstack/react-query'
import { ScanBarcode } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Label, Badge, Input, Skeleton } from '@/components/ui/primitives'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { api } from '@/lib/api-client'
import { formatQuantity, humanise, toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import type { StockBatch, StockSerial } from './dispatch-types'

/** Radix Select forbids an empty option value, so "not chosen" needs a sentinel. */
const NONE = '__none'

/** One page of candidates. Both endpoints page on the server. */
const STOCK_PAGE = 50

// ---------------------------------------------------------- batch select ----

export function BatchSelect({
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
  const params = { item_id: itemId, limit: STOCK_PAGE, with_total: false }
  const batches = useQuery({
    queryKey: ['inventory', 'batches', params],
    queryFn: () => api.getPage<StockBatch>('/inventory/batches', { params }),
    enabled: Boolean(itemId),
    staleTime: staleTimes.transactional,
  })

  const rows = batches.data?.data ?? []

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} required>
        Batch
      </Label>
      <Select value={value || NONE} onValueChange={(next) => onChange(next === NONE ? '' : next)}>
        <SelectTrigger id={id}>
          <SelectValue placeholder="Choose the batch" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>Not chosen</SelectItem>
          {rows.map((batch) => (
            <SelectItem key={batch.id} value={batch.id}>
              {batch.batch_number}
              {batch.status === 'ACTIVE' ? '' : ` (${humanise(batch.status)})`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {!batches.isLoading && rows.length === 0 && (
        <p className="text-xs text-warning">
          No batch of this item is on record. A batch-tracked item cannot move until one exists.
        </p>
      )}
    </div>
  )
}

// --------------------------------------------------------- serial picker ----

export function SerialPicker({
  idPrefix,
  itemId,
  warehouseId,
  quantity,
  selected,
  onChange,
}: {
  /** Keeps the search box addressable when several pickers share a screen. */
  idPrefix: string
  itemId: string
  warehouseId: string
  quantity: string
  selected: string[]
  onChange: (serialIds: string[]) => void
}) {
  const [term, setTerm] = useState('')
  const [debounced, setDebounced] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const params = {
    item_id: itemId,
    ...(warehouseId ? { warehouse_id: warehouseId } : {}),
    ...(debounced ? { search: debounced } : {}),
    limit: STOCK_PAGE,
    with_total: false,
  }

  const serials = useQuery({
    queryKey: ['inventory', 'serials', params],
    queryFn: () => api.getPage<StockSerial>('/inventory/serials', { params }),
    enabled: Boolean(itemId) && Boolean(warehouseId),
    // Whose hands hold a unit changes all day; a stale register would offer a
    // serial that left this morning.
    staleTime: staleTimes.live,
  })

  const rows = serials.data?.data ?? []
  const wanted = toDecimal(quantity)
  const chosen = selected.length

  const toggle = (serialId: string) =>
    onChange(
      selected.includes(serialId)
        ? selected.filter((id) => id !== serialId)
        : [...selected, serialId],
    )

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Label htmlFor={`${idPrefix}-serial-search`} className="flex items-center gap-1.5">
          <ScanBarcode className="h-3.5 w-3.5" aria-hidden />
          Serial numbers
        </Label>
        <span
          className={cn(
            'text-xs',
            wanted.equals(chosen) ? 'text-success' : 'font-medium text-warning',
          )}
        >
          {chosen} of {formatQuantity(quantity)} identified
        </span>
      </div>

      {!warehouseId ? (
        <p className="text-xs text-warning">
          A serial is held at a warehouse, so the warehouse has to be settled before its units can
          be named.
        </p>
      ) : (
        <>
          <Input
            id={`${idPrefix}-serial-search`}
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Search the serial register"
            autoComplete="off"
          />

          <div className="max-h-48 space-y-1 overflow-y-auto">
            {serials.isLoading && <Skeleton className="h-16 w-full" />}

            {!serials.isLoading && rows.length === 0 && (
              <p className="py-3 text-center text-xs text-warning">
                No unit of this item is registered at this warehouse. A serial-tracked item cannot
                move until its units are registered in stock - this will be refused without them.
              </p>
            )}

            {rows.map((serial) => {
              const inStock = serial.status === 'IN_STOCK'
              const isChosen = selected.includes(serial.id)
              return (
                <button
                  key={serial.id}
                  type="button"
                  disabled={!inStock}
                  aria-pressed={isChosen}
                  onClick={() => toggle(serial.id)}
                  className={cn(
                    'flex w-full items-center justify-between gap-2 rounded-sm border px-2 py-1.5 text-left text-sm',
                    isChosen ? 'border-primary bg-primary/5' : 'border-transparent',
                    inStock ? 'hover:bg-accent' : 'cursor-not-allowed opacity-60',
                  )}
                >
                  <span className="truncate font-mono text-xs">{serial.serial_number}</span>
                  {inStock ? (
                    isChosen && <Badge variant="success">Chosen</Badge>
                  ) : (
                    <Badge variant="muted">{humanise(serial.status)}</Badge>
                  )}
                </button>
              )
            })}
          </div>

          <p className="text-xs text-muted-foreground">
            One serial per unit - the API refuses a serialised line that does not name exactly as
            many as it moves (docs/assumptions.md section 23).
          </p>
        </>
      )}
    </div>
  )
}

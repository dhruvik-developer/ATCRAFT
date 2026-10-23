import { describeError } from '@/lib/messages'
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
import { Check, ChevronsUpDown, ScanBarcode, Search, X } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Label, Badge, Input, Skeleton } from '@/components/ui/primitives'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { formatQuantity, humanise, toDecimal } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import type { StockBatch, StockSerial } from './dispatch-types'

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
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [page, setPage] = useState(1)
  const [picked, setPicked] = useState<StockBatch | null>(null)
  const debounced = useDebouncedValue(term)

  useEffect(() => {
    setOpen(false)
    setTerm('')
    setPage(1)
    setPicked(null)
  }, [itemId])

  const params = {
    item_id: itemId,
    ...(debounced ? { search: debounced } : {}),
    page,
    limit: PICKER_PAGE_SIZE,
    with_total: false,
  }
  const batches = useQuery({
    queryKey: queryKeys.inventory.batches(params),
    queryFn: () => api.getPage<StockBatch>('/inventory/batches', { params }),
    enabled: open && Boolean(itemId),
    staleTime: staleTimes.transactional,
  })

  const rows = batches.data?.data ?? []
  const fromPage = rows.find((batch) => batch.id === value)
  const remembered = picked?.item_id === itemId && picked.id === value ? picked : null
  const detail = useQuery({
    queryKey: queryKeys.inventory.batch(itemId, value),
    queryFn: () =>
      api.getPage<StockBatch>('/inventory/batches', {
        params: { item_id: itemId, batch_id: value, limit: 1, with_total: false },
      }),
    enabled: Boolean(itemId) && Boolean(value) && !fromPage && !remembered,
    staleTime: staleTimes.transactional,
  })
  const selected = fromPage ?? remembered ?? detail.data?.data[0] ?? null

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} required>
        Batch
      </Label>
      <div className="flex items-center gap-1">
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              id={id}
              type="button"
              variant="outline"
              role="combobox"
              aria-expanded={open}
              aria-label="Batch"
              disabled={!itemId}
              className="flex-1 justify-between font-normal"
            >
              <span className="truncate">
                {selected?.batch_number ??
                  (value ? 'Selected batch (unavailable)' : 'Choose the batch')}
              </span>
              <ChevronsUpDown className="opacity-50" aria-hidden />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-[min(22rem,calc(100vw-2rem))] p-0">
            <div className="relative border-b p-2">
              <Search
                className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                autoFocus
                value={term}
                maxLength={64}
                aria-label="Search batches"
                placeholder="Search batch or supplier batch number"
                className="pl-8"
                onChange={(event) => {
                  setTerm(event.target.value)
                  setPage(1)
                }}
              />
            </div>
            <div className="max-h-64 overflow-y-auto p-1" role="listbox" aria-label="Batches">
              {batches.isLoading && <Skeleton className="h-16 w-full" />}
              {batches.error && (
                <p role="alert" className="p-2 text-sm text-destructive">
                  {describeError(batches.error)}
                </p>
              )}
              {!batches.isLoading && !batches.error && rows.length === 0 && (
                <p className="p-3 text-center text-sm text-muted-foreground">
                  {debounced
                    ? 'No batch matches that search.'
                    : 'No batch of this item is on record.'}
                </p>
              )}
              {rows.map((batch) => (
                <button
                  key={batch.id}
                  type="button"
                  role="option"
                  aria-selected={batch.id === value}
                  onClick={() => {
                    setPicked(batch)
                    onChange(batch.id)
                    setOpen(false)
                  }}
                  className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                >
                  <Check
                    className={cn('h-4 w-4 shrink-0', batch.id !== value && 'invisible')}
                    aria-hidden
                  />
                  <span className="truncate">
                    {batch.batch_number}
                    {batch.status === 'ACTIVE' ? '' : ` (${humanise(batch.status)})`}
                  </span>
                </button>
              ))}
            </div>
            <div className="flex items-center justify-between gap-2 border-t p-2">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label="Previous batch page"
                disabled={page === 1 || batches.isFetching}
                onClick={() => setPage((current) => current - 1)}
              >
                Previous
              </Button>
              <span className="text-xs text-muted-foreground">Page {page}</span>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label="Next batch page"
                disabled={!batches.data?.pagination.has_next || batches.isFetching}
                onClick={() => setPage((current) => current + 1)}
              >
                Next
              </Button>
            </div>
          </PopoverContent>
        </Popover>
        {value && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Clear batch"
            onClick={() => onChange('')}
          >
            <X />
          </Button>
        )}
      </div>
      {detail.error && (
        <p role="alert" className="text-xs text-destructive">
          {describeError(detail.error)}
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

  const debounced = useDebouncedValue(term)

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

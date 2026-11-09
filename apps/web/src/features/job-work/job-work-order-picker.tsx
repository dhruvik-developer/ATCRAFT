/**
 * One vendor's job-work order, searched on the server.
 *
 * Recording a job-work bill used to offer that vendor's 200 newest orders in a
 * plain select, so a bill for an older order could not be recorded from the
 * screen and nothing said the list had been cut. This asks `GET /job-work` for
 * the vendor's orders matching the term (code or process description), newest
 * first, and says when there are more than one page holds.
 *
 * Every status is offered, as before: whether an order still has anything to
 * bill is answered by its billable lines, which the dialog reads next.
 */

import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Search } from 'lucide-react'
import { useState } from 'react'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { Input, Label } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { humanise } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { jobWorkKeys, useJobWorkOrder, type JobWorkOrderRow } from './job-work-model'

function orderLabel(order: Pick<JobWorkOrderRow, 'code' | 'status' | 'receipt_count'>): string {
  return `${order.code} - ${humanise(order.status)}${order.receipt_count === 0 ? ' (no receipts)' : ''}`
}

export function JobWorkOrderPicker({
  id,
  label,
  vendorId,
  value,
  onChange,
  required = false,
  disabled = false,
  placeholder = 'Search this vendor’s orders',
}: {
  id: string
  label: string
  /** The orders are this vendor's; without one there is nothing to search. */
  vendorId: string | null
  value: string | null
  /** The chosen row comes with the id: the caller needs its code and SAC. */
  onChange: (orderId: string | null, order: JobWorkOrderRow | null) => void
  required?: boolean
  disabled?: boolean
  placeholder?: string
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [picked, setPicked] = useState<JobWorkOrderRow | null>(null)
  const debouncedTerm = useDebouncedValue(term)
  const inactive = disabled || !vendorId

  const searchParams = {
    vendor_id: vendorId,
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    sort_by: 'created_at',
    sort_dir: 'desc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: jobWorkKeys.list(searchParams),
    queryFn: ({ signal }) =>
      api.getPage<JobWorkOrderRow>('/job-work', { params: searchParams, signal }),
    enabled: open && !inactive,
    staleTime: staleTimes.transactional,
  })

  // Only when the value did not come from a pick here - a bookmarked or
  // restored selection still shows its code rather than a UUID.
  const detail = useJobWorkOrder(value ?? '', Boolean(value) && picked?.id !== value)

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null
  const rows = results.data?.data ?? []
  const more = results.data?.pagination.has_next === true

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} required={required}>
        {label}
      </Label>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-label={label}
            disabled={inactive}
            className={cn(
              'h-9 w-full justify-between font-normal',
              !selected && 'text-muted-foreground',
            )}
          >
            <span className="truncate">{selected ? orderLabel(selected) : placeholder}</span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>

        <PopoverContent align="start" className="w-[min(24rem,calc(100vw-2rem))] p-0">
          <div className="relative border-b p-2">
            <Search
              className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              autoFocus
              value={term}
              onChange={(event) => setTerm(event.target.value)}
              placeholder="Search by order code or process"
              aria-label="Search job-work orders"
              className="pl-8"
            />
          </div>

          <div className="max-h-64 overflow-y-auto p-1" role="listbox" aria-label={label}>
            {results.isLoading && (
              <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Searching...
              </p>
            )}

            {!results.isLoading && results.error && (
              <FieldError
                className="mt-0 px-2 py-6 text-sm"
                message={describeError(results.error)}
              />
            )}

            {!results.isLoading && !results.error && rows.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                {debouncedTerm
                  ? 'No order of this vendor matches that search.'
                  : 'This vendor has no job-work orders.'}
              </p>
            )}

            {rows.map((order) => (
              <button
                key={order.id}
                type="button"
                role="option"
                aria-selected={order.id === value}
                onClick={() => {
                  setPicked(order)
                  onChange(order.id, order)
                  setOpen(false)
                }}
                className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              >
                <Check
                  className={cn('mt-0.5 h-4 w-4 shrink-0', order.id !== value && 'invisible')}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{orderLabel(order)}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {order.process_description || humanise(order.process_type)}
                  </span>
                </span>
              </button>
            ))}

            {more && (
              <p className="px-2 py-2 text-xs text-muted-foreground">
                Showing the newest {rows.length}. Type more of the code to find an older order.
              </p>
            )}
          </div>
        </PopoverContent>
      </Popover>
    </div>
  )
}

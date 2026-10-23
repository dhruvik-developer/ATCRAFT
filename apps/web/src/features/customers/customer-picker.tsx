import { describeError } from '@/lib/messages'
/**
 * Customer picker, searched on the server.
 *
 * A dealer network runs to thousands of accounts, so this never downloads the
 * list and filters it in the browser (master rules 31). The chosen customer is
 * resolved by id when the value arrives from a bookmarked URL, so a filter
 * restored from a link shows a name rather than a UUID.
 */

import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Search, X } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { Badge, Input, Label } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import type { CustomerRow } from './customer-types'

export function CustomerPicker({
  id,
  label,
  value,
  onChange,
  hideLabel = false,
  placeholder = 'Any customer',
  invalid = false,
  disabled = false,
  className,
}: {
  id: string
  label: string
  value: string | null
  onChange: (customerId: string | null) => void
  hideLabel?: boolean
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [picked, setPicked] = useState<CustomerRow | null>(null)

  // One request per keystroke would put a trigram scan behind every character.
  const debouncedTerm = useDebouncedValue(term)

  const searchParams = {
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    is_active: true,
    sort_by: 'code',
    sort_dir: 'asc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.customers.list(searchParams),
    queryFn: () => api.getPage<CustomerRow>('/customers', { params: searchParams }),
    enabled: open && !disabled,
    staleTime: staleTimes.master,
  })

  const detail = useQuery({
    queryKey: queryKeys.customers.detail(value ?? ''),
    queryFn: () => api.get<CustomerRow>(`/customers/${value}`),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.master,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null
  const rows = results.data?.data ?? []

  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id} className={cn(hideLabel && 'sr-only')}>
        {label}
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
              aria-label={label}
              aria-invalid={invalid}
              disabled={disabled}
              className={cn(
                'h-9 min-w-[200px] flex-1 justify-between font-normal',
                !selected && 'text-muted-foreground',
                invalid && 'border-destructive',
              )}
            >
              <span className="truncate">
                {selected ? `${selected.code} - ${selected.name}` : placeholder}
              </span>
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
                placeholder="Search by code, name or GSTIN"
                aria-label="Search customers"
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
                <p className="px-2 py-6 text-sm text-destructive">{describeError(results.error)}</p>
              )}

              {!results.isLoading && !results.error && rows.length === 0 && (
                <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                  {debouncedTerm
                    ? 'No customer matches that search.'
                    : 'Type to search the customer master.'}
                </p>
              )}

              {rows.map((customer) => (
                <button
                  key={customer.id}
                  type="button"
                  role="option"
                  aria-selected={customer.id === value}
                  onClick={() => {
                    setPicked(customer)
                    onChange(customer.id)
                    setOpen(false)
                  }}
                  className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                >
                  <Check
                    className={cn('mt-0.5 h-4 w-4 shrink-0', customer.id !== value && 'invisible')}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate font-medium">{customer.code}</span>
                      {/* A blocked account is shown in the picker itself: finding
                          out at confirm time wastes the whole order entry. */}
                      {customer.is_blocked && <Badge variant="destructive">Blocked</Badge>}
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {customer.name}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </PopoverContent>
        </Popover>

        {value && !disabled && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Clear ${label.toLowerCase()}`}
            onClick={() => {
              setPicked(null)
              onChange(null)
            }}
          >
            <X />
          </Button>
        )}
      </div>
    </div>
  )
}

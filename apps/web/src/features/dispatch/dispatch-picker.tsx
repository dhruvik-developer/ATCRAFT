/**
 * A dispatch, searched on the server.
 *
 * Raising a sales return used to offer the 100 latest delivered dispatches in a
 * plain select, so a return against the 101st could not be raised from the
 * screen at all and nothing said the list had been cut. This asks the server
 * instead: the caller's filters (`dispatch_type`, `status`) narrow the search,
 * the term matches the code, tracking id, LR, vehicle or courier, and a page
 * that holds more matches says so rather than passing for the whole answer.
 *
 * The chosen dispatch is resolved by id when the value arrives without a pick in
 * this session, so the trigger shows a code rather than a UUID.
 */

import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Search, X } from 'lucide-react'
import { useState } from 'react'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { Input, Label } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { formatDate } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { dispatchKeys, type DispatchDetail, type DispatchRow } from './dispatch-types'

/** The `GET /dispatch` filters a picker may fix; tenancy is never one of them. */
export interface DispatchPickerFilters {
  dispatch_type?: DispatchRow['dispatch_type']
  status?: DispatchRow['status']
  customer_id?: string
}

export function DispatchPicker({
  id,
  label,
  value,
  onChange,
  filters = {},
  placeholder = 'Search dispatches',
  invalid = false,
  disabled = false,
  className,
}: {
  id: string
  label: string
  value: string | null
  onChange: (dispatchId: string | null) => void
  filters?: DispatchPickerFilters
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [picked, setPicked] = useState<DispatchRow | null>(null)

  // One request per keystroke would put a search scan behind every character.
  const debouncedTerm = useDebouncedValue(term)

  const searchParams = {
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    sort_by: 'dispatch_date',
    sort_dir: 'desc',
    ...filters,
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.dispatch.list(searchParams),
    queryFn: ({ signal }) =>
      api.getPage<DispatchRow>('/dispatch', { params: searchParams, signal }),
    enabled: open && !disabled,
    staleTime: staleTimes.transactional,
  })

  const detail = useQuery({
    queryKey: dispatchKeys.detail(value ?? ''),
    queryFn: ({ signal }) => api.get<DispatchDetail>(`/dispatch/${value}`, { signal }),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.transactional,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null
  const rows = results.data?.data ?? []
  const more = results.data?.pagination.has_next === true

  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id}>{label}</Label>
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
                'h-9 min-w-0 flex-1 justify-between font-normal',
                !selected && 'text-muted-foreground',
                invalid && 'border-destructive',
              )}
            >
              <span className="truncate">
                {selected
                  ? `${selected.code} · ${formatDate(selected.dispatch_date)}`
                  : placeholder}
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
                placeholder="Search by code, tracking id, LR or vehicle"
                aria-label="Search dispatches"
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
                    ? 'No dispatch matches that search.'
                    : 'No dispatch is available here.'}
                </p>
              )}

              {rows.map((dispatch) => (
                <button
                  key={dispatch.id}
                  type="button"
                  role="option"
                  aria-selected={dispatch.id === value}
                  onClick={() => {
                    setPicked(dispatch)
                    onChange(dispatch.id)
                    setOpen(false)
                  }}
                  className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                >
                  <Check
                    className={cn('mt-0.5 h-4 w-4 shrink-0', dispatch.id !== value && 'invisible')}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{dispatch.code}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {formatDate(dispatch.dispatch_date)}
                      {dispatch.tracking_id ? ` · ${dispatch.tracking_id}` : ''}
                    </span>
                  </span>
                </button>
              ))}

              {more && (
                <p className="px-2 py-2 text-xs text-muted-foreground">
                  Showing the latest {rows.length}. Type more of the code to find an older one.
                </p>
              )}
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

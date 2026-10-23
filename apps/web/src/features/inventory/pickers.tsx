import { describeError } from '@/lib/messages'
/**
 * Item and warehouse pickers shared by the four inventory screens.
 *
 * Both search on the server. An item picker that downloaded the whole item
 * master and filtered it in the browser would be the exact anti-pattern master
 * rules 31 forbids - and this catalogue runs to tens of thousands of rows.
 */

import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Search, X } from 'lucide-react'
import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Input, Label } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { WAREHOUSE_LOOKUP_PARAMS, type ItemOption, type WarehouseOption } from './types'

/** Radix Select forbids an empty item value, so "no filter" needs a sentinel. */
const ALL = '__all__'

// ------------------------------------------------------- WarehouseSelect ----

export function WarehouseSelect({
  id,
  label,
  value,
  onChange,
  hideLabel = false,
  includeAll = false,
  allLabel = 'All warehouses',
  placeholder = 'Select a warehouse',
  invalid = false,
  disabled = false,
  className,
}: {
  id: string
  label: string
  /** Empty string means "no warehouse chosen". */
  value: string
  onChange: (warehouseId: string) => void
  hideLabel?: boolean
  includeAll?: boolean
  allLabel?: string
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  className?: string
}) {
  const params = WAREHOUSE_LOOKUP_PARAMS
  const warehouses = useQuery({
    queryKey: queryKeys.warehouses.list(params),
    queryFn: () => api.getPage<WarehouseOption>('/warehouses', { params }),
    staleTime: staleTimes.master,
  })

  const rows = warehouses.data?.data ?? []

  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id} className={cn(hideLabel && 'sr-only')}>
        {label}
      </Label>
      <Select
        // Radix treats an empty string as "controlled, nothing chosen" and shows
        // the placeholder; `undefined` would silently make the select
        // uncontrolled.
        value={value || (includeAll ? ALL : '')}
        onValueChange={(next) => onChange(next === ALL ? '' : next)}
        disabled={disabled}
      >
        <SelectTrigger
          id={id}
          aria-label={label}
          aria-invalid={invalid}
          className={cn('min-w-[180px]', invalid && 'border-destructive')}
        >
          <SelectValue placeholder={warehouses.isLoading ? 'Loading...' : placeholder} />
        </SelectTrigger>
        <SelectContent>
          {includeAll && <SelectItem value={ALL}>{allLabel}</SelectItem>}
          {rows.map((warehouse) => (
            <SelectItem key={warehouse.id} value={warehouse.id}>
              {warehouse.code} - {warehouse.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

// ------------------------------------------------------------ ItemPicker ----

export function ItemPicker({
  id,
  label,
  value,
  onChange,
  hideLabel = false,
  placeholder = 'Any item',
  invalid = false,
  disabled = false,
  className,
  filters,
  emptyMessage,
}: {
  id: string
  label: string
  value: string | null
  onChange: (itemId: string | null, item?: ItemOption | null) => void
  hideLabel?: boolean
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  className?: string
  /**
   * Extra query parameters for `GET /products/items`, for a caller that must
   * not offer items the endpoint it posts to would refuse - a stock transfer
   * and a serial-tracked item, say. The narrowing is done by the API, because
   * downloading the master and filtering it here is the thing master rules 31
   * forbids and would silently drop matches past the first page.
   */
  filters?: Record<string, unknown>
  /** Shown when a search returns nothing, in place of the generic line. */
  emptyMessage?: string
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [picked, setPicked] = useState<ItemOption | null>(null)

  // One request per keystroke would put a trigram scan of the item master
  // behind every character typed.
  const debouncedTerm = useDebouncedValue(term)

  const searchParams = useMemo(
    () => ({
      limit: PICKER_PAGE_SIZE,
      with_total: false,
      is_active: true,
      ...filters,
      ...(debouncedTerm ? { search: debouncedTerm } : {}),
    }),
    // `filters` is typically an inline object; serialising it keeps the query
    // key stable instead of changing identity on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [debouncedTerm, JSON.stringify(filters)],
  )
  const results = useQuery({
    queryKey: queryKeys.items.list(searchParams),
    queryFn: () => api.getPage<ItemOption>('/products/items', { params: searchParams }),
    enabled: open && !disabled,
    staleTime: staleTimes.master,
  })

  // A filter restored from a bookmarked URL arrives as a bare id with no label,
  // so the chosen item is resolved from the API rather than shown as a UUID.
  const detail = useQuery({
    queryKey: queryKeys.items.detail(value ?? ''),
    queryFn: () => api.get<ItemOption>('/products/items/' + value),
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
                {selected ? selected.code + ' - ' + selected.name : placeholder}
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
                onChange={(event) => setTerm(event.target.value)}
                placeholder="Search by code or name"
                aria-label="Search items"
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
                    ? (emptyMessage ?? 'No item matches that search.')
                    : 'Type to search the item master.'}
                </p>
              )}

              {rows.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="option"
                  aria-selected={item.id === value}
                  onClick={() => {
                    setPicked(item)
                    onChange(item.id, item)
                    setOpen(false)
                  }}
                  className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                >
                  <Check
                    className={cn('mt-0.5 h-4 w-4 shrink-0', item.id !== value && 'invisible')}
                    aria-hidden
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{item.code}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {item.name}
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
            aria-label={'Clear ' + label.toLowerCase()}
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

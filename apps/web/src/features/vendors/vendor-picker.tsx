/**
 * Vendor picker, searched on the server.
 *
 * The vendor master runs to thousands of rows in a company with a long supply
 * base, so this never downloads the list and filters it in the browser (master
 * rules 31). A vendor arriving as a bare id from a bookmarked URL is resolved
 * by id, so a restored filter reads as a name rather than a UUID.
 *
 * `approvedOnly` matters on a purchase order: the API refuses a PO raised on an
 * unapproved vendor, and finding that out at save time wastes the whole entry.
 */

import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Search, X } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { Badge, Input, Label } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import type { VendorRow } from './vendor-types'

export function VendorPicker({
  id,
  label,
  value,
  onChange,
  hideLabel = false,
  placeholder = 'Any vendor',
  approvedOnly = false,
  invalid = false,
  disabled = false,
  className,
}: {
  id: string
  label: string
  value: string | null
  onChange: (vendorId: string | null) => void
  hideLabel?: boolean
  placeholder?: string
  /** Restrict the search to vendors a purchase order may legally be raised on. */
  approvedOnly?: boolean
  invalid?: boolean
  disabled?: boolean
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [debouncedTerm, setDebouncedTerm] = useState('')
  const [picked, setPicked] = useState<VendorRow | null>(null)

  // One request per keystroke would put a trigram scan behind every character.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedTerm(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const searchParams = {
    limit: 20,
    with_total: false,
    is_active: true,
    sort_by: 'code',
    sort_dir: 'asc',
    ...(approvedOnly ? { is_approved: true } : {}),
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.vendors.list(searchParams),
    queryFn: () => api.getPage<VendorRow>('/vendors', { params: searchParams }),
    enabled: open && !disabled,
    staleTime: staleTimes.master,
  })

  const detail = useQuery({
    queryKey: queryKeys.vendors.detail(value ?? ''),
    queryFn: () => api.get<VendorRow>(`/vendors/${value}`),
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

          <PopoverContent align="start" className="w-[24rem] p-0">
            <div className="relative border-b p-2">
              <Search
                className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                autoFocus
                value={term}
                onChange={(event) => setTerm(event.target.value)}
                placeholder="Search by code, name, GSTIN or city"
                aria-label="Search vendors"
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
                <p className="px-2 py-6 text-sm text-destructive">{results.error.message}</p>
              )}

              {!results.isLoading && !results.error && rows.length === 0 && (
                <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                  {debouncedTerm
                    ? 'No vendor matches that search.'
                    : approvedOnly
                      ? 'Type to search approved vendors.'
                      : 'Type to search the vendor master.'}
                </p>
              )}

              {rows.map((vendor) => (
                <button
                  key={vendor.id}
                  type="button"
                  role="option"
                  aria-selected={vendor.id === value}
                  onClick={() => {
                    setPicked(vendor)
                    onChange(vendor.id)
                    setOpen(false)
                  }}
                  className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                >
                  <Check
                    className={cn('mt-0.5 h-4 w-4 shrink-0', vendor.id !== value && 'invisible')}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate font-medium">{vendor.code}</span>
                      {!vendor.is_approved && <Badge variant="warning">Unapproved</Badge>}
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {vendor.name}
                      {vendor.lead_time_days > 0 && ` - ${vendor.lead_time_days} day lead time`}
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

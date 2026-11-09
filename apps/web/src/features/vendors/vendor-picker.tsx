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
import { useState } from 'react'

import { Badge } from '@/components/ui/primitives'
import { SearchPicker } from '@/components/ui/search-picker'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'

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
  const [picked, setPicked] = useState<VendorRow | null>(null)

  const detail = useQuery({
    queryKey: queryKeys.vendors.detail(value ?? ''),
    queryFn: ({ signal }) => api.get<VendorRow>(`/vendors/${value}`, { signal }),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.master,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null

  return (
    <SearchPicker<VendorRow>
      id={id}
      label={label}
      labelDisplay={hideLabel ? 'sr-only' : 'visible'}
      className={className}
      triggerClassName="min-w-[200px]"
      display={selected ? `${selected.code} - ${selected.name}` : null}
      placeholder={placeholder}
      invalid={invalid}
      disabled={disabled}
      searchLabel="Search vendors"
      searchPlaceholder="Search by code, name, GSTIN or city"
      query={(term) => {
        const params = {
          limit: PICKER_PAGE_SIZE,
          with_total: false,
          is_active: true,
          sort_by: 'code',
          sort_dir: 'asc',
          ...(approvedOnly ? { is_approved: true } : {}),
          ...(term ? { search: term } : {}),
        }
        return {
          queryKey: queryKeys.vendors.list(params),
          queryFn: ({ signal }) => api.getPage<VendorRow>('/vendors', { params, signal }),
          staleTime: staleTimes.master,
        }
      }}
      getKey={(vendor) => vendor.id}
      selectedKey={value}
      onSelect={(vendor) => {
        setPicked(vendor)
        onChange(vendor.id)
      }}
      renderOption={(vendor) => (
        <>
          <span className="flex items-center gap-2">
            <span className="truncate font-medium">{vendor.code}</span>
            {!vendor.is_approved && <Badge variant="warning">Unapproved</Badge>}
          </span>
          <span className="block truncate text-xs text-muted-foreground">
            {vendor.name}
            {vendor.lead_time_days > 0 && ` - ${vendor.lead_time_days} day lead time`}
          </span>
        </>
      )}
      emptyText={(term) =>
        term
          ? 'No vendor matches that search.'
          : approvedOnly
            ? 'Type to search approved vendors.'
            : 'Type to search the vendor master.'
      }
      clear={
        value && !disabled
          ? {
              label: `Clear ${label.toLowerCase()}`,
              onClear: () => {
                setPicked(null)
                onChange(null)
              },
            }
          : undefined
      }
    />
  )
}

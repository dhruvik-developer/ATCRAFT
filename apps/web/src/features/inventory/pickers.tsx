/**
 * Item and warehouse pickers shared by the four inventory screens.
 *
 * Both search on the server. An item picker that downloaded the whole item
 * master and filtered it in the browser would be the exact anti-pattern master
 * rules 31 forbids - and this catalogue runs to tens of thousands of rows.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Label } from '@/components/ui/primitives'
import { SearchPicker } from '@/components/ui/search-picker'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import type { ItemOption } from './types'
import { useWarehouseLookup } from './use-warehouse-lookup'

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
  const warehouses = useWarehouseLookup()

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
  const [picked, setPicked] = useState<ItemOption | null>(null)

  // A filter restored from a bookmarked URL arrives as a bare id with no label,
  // so the chosen item is resolved from the API rather than shown as a UUID.
  const detail = useQuery({
    queryKey: queryKeys.items.detail(value ?? ''),
    queryFn: ({ signal }) => api.get<ItemOption>('/products/items/' + value, { signal }),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.master,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null

  return (
    <SearchPicker<ItemOption>
      id={id}
      label={label}
      labelDisplay={hideLabel ? 'sr-only' : 'visible'}
      className={className}
      triggerClassName="min-w-[200px]"
      display={selected ? selected.code + ' - ' + selected.name : null}
      placeholder={placeholder}
      invalid={invalid}
      disabled={disabled}
      searchLabel="Search items"
      searchPlaceholder="Search by code or name"
      query={(term) => {
        // The key is hashed by value, so an inline `filters` object from the
        // caller does not make a new query on every render.
        const params = {
          limit: PICKER_PAGE_SIZE,
          with_total: false,
          is_active: true,
          ...filters,
          ...(term ? { search: term } : {}),
        }
        return {
          queryKey: queryKeys.items.list(params),
          queryFn: ({ signal }) => api.getPage<ItemOption>('/products/items', { params, signal }),
          staleTime: staleTimes.master,
        }
      }}
      getKey={(item) => item.id}
      selectedKey={value}
      onSelect={(item) => {
        setPicked(item)
        onChange(item.id, item)
      }}
      renderOption={(item) => (
        <>
          <span className="block truncate font-medium">{item.code}</span>
          <span className="block truncate text-xs text-muted-foreground">{item.name}</span>
        </>
      )}
      emptyText={(term) =>
        term ? (emptyMessage ?? 'No item matches that search.') : 'Type to search the item master.'
      }
      clear={
        value && !disabled
          ? {
              label: 'Clear ' + label.toLowerCase(),
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

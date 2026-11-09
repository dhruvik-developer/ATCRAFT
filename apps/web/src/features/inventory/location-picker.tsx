/**
 * A bin in one warehouse, searched on the server.
 *
 * A warehouse runs to hundreds of bins, so this searches `GET
 * /warehouses/locations` within the chosen warehouse rather than downloading
 * them. Changing the warehouse clears a bin that belonged to the old one, and an
 * item whose default bin is in this warehouse has it filled in once, when the
 * field is still empty. A role that may not read bins sees no field at all.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { SearchPicker } from '@/components/ui/search-picker'
import { useAuth } from '@/features/auth/auth-context'
import type { ItemDetail } from '@/features/items/item-model'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type LocationOption = SchemaOf<'StorageLocationRead'>

export function LocationPicker({
  id,
  label = 'Bin',
  warehouseId,
  itemId = '',
  value,
  onChange,
  disabled = false,
}: {
  id: string
  label?: string
  warehouseId: string
  itemId?: string
  value: string | null
  onChange: (value: string | null) => void
  disabled?: boolean
}) {
  const { can } = useAuth()
  const mayRead = can(P.LOCATION_VIEW)
  const [picked, setPicked] = useState<LocationOption | null>(null)
  const defaults = useQuery({
    queryKey: queryKeys.items.detail(itemId),
    queryFn: ({ signal }) => api.get<ItemDetail>('/products/items/' + itemId, { signal }),
    enabled: Boolean(itemId) && mayRead && can(P.ITEM_VIEW),
    staleTime: staleTimes.master,
  })
  const prefilled = useRef('')
  const previousWarehouse = useRef(warehouseId)
  useEffect(() => {
    if (previousWarehouse.current !== warehouseId && value) onChange(null)
    previousWarehouse.current = warehouseId
  }, [warehouseId, value, onChange])
  useEffect(() => {
    const key = itemId + ':' + warehouseId
    if (!defaults.data || prefilled.current === key) return
    prefilled.current = key
    if (
      !value &&
      defaults.data.default_warehouse_id === warehouseId &&
      defaults.data.default_location_id
    ) {
      onChange(defaults.data.default_location_id)
    }
  }, [defaults.data, itemId, warehouseId, value, onChange])
  // A bin that did not come from a pick here - an item's default, a saved
  // record - is resolved by id so the field reads as a code, not a UUID.
  const saved = useQuery({
    queryKey: [...queryKeys.warehouses.all, 'location', value],
    queryFn: ({ signal }) => api.get<LocationOption>('/warehouses/locations/' + value, { signal }),
    enabled: Boolean(value) && mayRead && picked?.id !== value,
    staleTime: staleTimes.master,
  })
  const selected = value ? (picked?.id === value ? picked : (saved.data ?? null)) : null
  if (!mayRead) return null

  return (
    <SearchPicker<LocationOption>
      id={id}
      label={label}
      className="min-w-0"
      display={selected ? selected.code + ' - ' + selected.name : null}
      placeholder="No bin"
      disabled={disabled || !warehouseId}
      searchLabel={'Search ' + label.toLowerCase()}
      searchPlaceholder="Search bins"
      query={(term) => {
        const params = {
          warehouse_id: warehouseId,
          is_active: true,
          ...(term ? { search: term } : {}),
          limit: PICKER_PAGE_SIZE,
          with_total: false,
        }
        return {
          queryKey: [...queryKeys.warehouses.all, 'location-options', params],
          queryFn: ({ signal }) =>
            api.getPage<LocationOption>('/warehouses/locations', { params, signal }),
          staleTime: staleTimes.master,
          enabled: Boolean(warehouseId),
        }
      }}
      getKey={(row) => row.id}
      selectedKey={value}
      onSelect={(row) => {
        setPicked(row)
        onChange(row.id)
      }}
      renderOption={(row) => (
        <span className="block truncate">
          {row.code} - {row.name}
        </span>
      )}
      emptyText="No active bin matches."
      clear={
        value
          ? {
              label: 'Clear ' + label.toLowerCase(),
              onClear: () => {
                setPicked(null)
                onChange(null)
              },
              disabled,
            }
          : undefined
      }
    />
  )
}

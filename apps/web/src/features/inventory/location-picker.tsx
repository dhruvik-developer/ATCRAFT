/**
 * A bin in one warehouse, searched on the server.
 *
 * A warehouse runs to hundreds of bins, so this searches `GET
 * /warehouses/locations` within the chosen warehouse rather than downloading
 * them. Changing the warehouse clears a bin that belonged to the old one, and an
 * item whose default bin is in this warehouse has it filled in once, when the
 * field is still empty. A role that may not read bins sees no field at all.
 *
 * The default comes from the item, read by id - or from the caller, when the
 * document it is filling already names it (`itemDefault`), which costs no read
 * of the item or of the bin.
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

/** An item's default put-away position, as a document line already names it. */
export interface ItemDefaultBin {
  warehouseId: string | null
  locationId: string | null
  /** The bin's code and name, so a proposed bin is named without reading it. */
  locationCode?: string | null
  locationName?: string | null
}

export function LocationPicker({
  id,
  label = 'Bin',
  warehouseId,
  itemId = '',
  itemDefault,
  value,
  onChange,
  disabled = false,
}: {
  id: string
  label?: string
  warehouseId: string
  itemId?: string
  /** The item's default bin when the caller holds it; the item is then not read. */
  itemDefault?: ItemDefaultBin
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
    enabled: Boolean(itemId) && !itemDefault && mayRead && can(P.ITEM_VIEW),
    staleTime: staleTimes.master,
  })
  // Where the item is put away: as the caller names it, or as the item says.
  const defaultKnown = Boolean(itemDefault) || Boolean(defaults.data)
  const defaultWarehouseId = itemDefault
    ? itemDefault.warehouseId
    : (defaults.data?.default_warehouse_id ?? null)
  const defaultLocationId = itemDefault
    ? itemDefault.locationId
    : (defaults.data?.default_location_id ?? null)
  const prefilled = useRef('')
  const previousWarehouse = useRef(warehouseId)
  useEffect(() => {
    if (previousWarehouse.current !== warehouseId && value) onChange(null)
    previousWarehouse.current = warehouseId
  }, [warehouseId, value, onChange])
  useEffect(() => {
    const key = itemId + ':' + warehouseId
    // Proposed only to a role that may see the field: a bin nobody can see or
    // clear would be sent all the same.
    if (!defaultKnown || !mayRead || prefilled.current === key) return
    prefilled.current = key
    if (!value && defaultWarehouseId === warehouseId && defaultLocationId) {
      onChange(defaultLocationId)
    }
  }, [
    defaultKnown,
    mayRead,
    defaultWarehouseId,
    defaultLocationId,
    itemId,
    warehouseId,
    value,
    onChange,
  ])
  const named =
    itemDefault?.locationId &&
    itemDefault.locationId === value &&
    itemDefault.locationCode &&
    itemDefault.locationName
      ? { code: itemDefault.locationCode, name: itemDefault.locationName }
      : null
  // A bin that did not come from a pick here - an item's default, a saved
  // record - is resolved by id so the field reads as a code, not a UUID,
  // unless the caller already named it.
  const saved = useQuery({
    queryKey: [...queryKeys.warehouses.all, 'location', value],
    queryFn: ({ signal }) => api.get<LocationOption>('/warehouses/locations/' + value, { signal }),
    enabled: Boolean(value) && mayRead && picked?.id !== value && !named,
    staleTime: staleTimes.master,
  })
  const selected = value ? (picked?.id === value ? picked : (named ?? saved.data ?? null)) : null
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

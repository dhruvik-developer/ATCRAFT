import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { Input, Label } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { useLocationOptions, type LocationOption } from '@/hooks/use-location-options'
import { useAuth } from '@/features/auth/auth-context'
import type { ItemDetail } from '@/features/items/item-model'
import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { P } from '@/lib/permissions'

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
  const [open, setOpen] = useState(false)
  const { can } = useAuth()
  const mayRead = can(P.LOCATION_VIEW)
  const [search, setSearch] = useState('')
  const debounced = useDebouncedValue(search)
  const options = useLocationOptions(warehouseId, debounced, open && !disabled && mayRead)
  const defaults = useQuery({
    queryKey: queryKeys.items.detail(itemId),
    queryFn: () => api.get<ItemDetail>('/products/items/' + itemId),
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
  const saved = useQuery({
    queryKey: [...queryKeys.warehouses.all, 'location', value],
    queryFn: () => api.get<LocationOption>('/warehouses/locations/' + value),
    enabled: Boolean(value) && mayRead,
    staleTime: staleTimes.master,
  })
  const selected = value ? (options.data?.data.find((row) => row.id === value) ?? saved.data) : null
  if (!mayRead) return null

  return (
    <div className="min-w-0 space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex min-w-0 gap-1">
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              id={id}
              type="button"
              variant="outline"
              role="combobox"
              aria-label={label}
              aria-expanded={open}
              disabled={disabled || !warehouseId}
              className="min-w-0 flex-1 justify-between font-normal"
            >
              <span className="truncate">
                {selected ? selected.code + ' - ' + selected.name : 'No bin'}
              </span>
              <ChevronsUpDown aria-hidden />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-[min(22rem,calc(100vw-2rem))] p-2">
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search bins"
              aria-label={'Search ' + label.toLowerCase()}
            />
            <div className="mt-2 max-h-60 overflow-y-auto" role="listbox" aria-label={label}>
              {options.isLoading && <p className="p-2 text-sm">Loading bins...</p>}
              {options.error && (
                <p className="p-2 text-sm text-destructive">{options.error.message}</p>
              )}
              {!options.isLoading && !options.error && options.data?.data.length === 0 && (
                <p className="p-2 text-sm text-muted-foreground">No active bin matches.</p>
              )}
              {options.data?.data.map((row) => (
                <button
                  key={row.id}
                  type="button"
                  role="option"
                  aria-selected={row.id === value}
                  className="flex w-full gap-2 rounded p-2 text-left text-sm hover:bg-accent"
                  onClick={() => {
                    onChange(row.id)
                    setOpen(false)
                  }}
                >
                  {row.id === value && <Check aria-hidden className="h-4 w-4" />}
                  {row.code} - {row.name}
                </button>
              ))}
            </div>
          </PopoverContent>
        </Popover>
        {value && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={disabled}
            aria-label={'Clear ' + label.toLowerCase()}
            onClick={() => onChange(null)}
          >
            <X />
          </Button>
        )}
      </div>
    </div>
  )
}

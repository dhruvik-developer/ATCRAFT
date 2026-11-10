import { ChevronsUpDown, X } from 'lucide-react'
import { useMemo, useState } from 'react'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { MultiSelectList } from '@/components/ui/multi-select-list'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { useAuth } from '@/features/auth/auth-context'
import { useWarehouseLookup } from '@/features/inventory/use-warehouse-lookup'
import { plural } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { cn } from '@/lib/utils'

/**
 * An empty selection means all active warehouses. The lookup is optional:
 * `warehouse.view` is not implied by the MRP permission, so callers without it
 * see a clear explanation instead of a forbidden request.
 */
export function WarehouseMultiSelect({
  id,
  value,
  onChange,
  disabled = false,
  unavailableMessage = 'Your role cannot read the warehouse list; the run plans across all warehouses.',
  maxSelections = 50,
}: {
  id: string
  value: string[]
  onChange: (next: string[]) => void
  disabled?: boolean
  unavailableMessage?: string
  maxSelections?: number
}) {
  const { can } = useAuth()
  const canReadWarehouses = can(P.WAREHOUSE_VIEW)
  const [open, setOpen] = useState(false)
  const warehouses = useWarehouseLookup(canReadWarehouses)

  const rows = useMemo(() => warehouses.data?.data ?? [], [warehouses.data])
  const chosen = useMemo(() => new Set(value), [value])
  const label = useMemo(() => {
    if (value.length === 0) return 'All warehouses'
    if (value.length === 1) {
      const match = rows.find((warehouse) => warehouse.id === value[0])
      return match ? `${match.code} - ${match.name}` : '1 warehouse'
    }
    return `${value.length} warehouses`
  }, [rows, value])

  const toggle = (warehouseId: string) => {
    onChange(
      chosen.has(warehouseId)
        ? value.filter((existing) => existing !== warehouseId)
        : [...value, warehouseId],
    )
  }

  if (!canReadWarehouses) {
    return (
      <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
        {unavailableMessage}
      </p>
    )
  }

  // Said in the list's live region, never inside the list: "Loading..." and the
  // empty sentence were plain text a screen reader was never told about.
  let status: string | null = null
  let statusClassName = 'sr-only'
  if (warehouses.isLoading) {
    status = 'Loading warehouses...'
    statusClassName = 'px-2 py-6 text-center text-sm'
  } else if (warehouses.error) {
    status = null
  } else if (rows.length === 0) {
    status = 'No warehouses are configured yet.'
    statusClassName = 'px-2 py-6 text-center text-sm'
  } else {
    status = plural(rows.length, 'warehouse')
  }

  return (
    <div className="flex items-center gap-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            disabled={disabled}
            onKeyDown={(event) => {
              // A closed combobox opens on the arrow keys as well as Enter, as
              // the shared search picker's does; the list then takes focus.
              if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
                event.preventDefault()
                setOpen(true)
              }
            }}
            className={cn(
              'h-9 w-full justify-between font-normal',
              value.length === 0 && 'text-muted-foreground',
            )}
          >
            <span className="truncate">{warehouses.isLoading ? 'Loading...' : label}</span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          aria-label="Choose warehouses"
          className="w-80 max-w-[calc(100vw-2rem)] p-0"
        >
          <p className="border-b px-3 py-2 text-xs text-muted-foreground">
            Leave everything unticked to include all warehouses. Select up to {maxSelections}.
          </p>
          <div className="p-1">
            <MultiSelectList
              label="Warehouses"
              rows={rows}
              getKey={(warehouse) => warehouse.id}
              isSelected={(warehouse) => chosen.has(warehouse.id)}
              // Reachable and read at the limit, but not ticked past it.
              isDisabled={(warehouse) => !chosen.has(warehouse.id) && value.length >= maxSelections}
              onToggle={(warehouse) => toggle(warehouse.id)}
              renderOption={(warehouse) => (
                <span className="block min-w-0">
                  <span className="block truncate font-medium">{warehouse.code}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {warehouse.name}
                  </span>
                </span>
              )}
              status={status}
              statusClassName={statusClassName}
              className="max-h-64"
            />
            {!warehouses.isLoading && warehouses.error && (
              <FieldError
                className="mt-0 px-2 py-6 text-center text-sm"
                message={describeError(warehouses.error)}
              />
            )}
          </div>
        </PopoverContent>
      </Popover>

      {value.length > 0 && !disabled && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Clear warehouse selection"
          onClick={() => onChange([])}
        >
          <X />
        </Button>
      )}
    </div>
  )
}

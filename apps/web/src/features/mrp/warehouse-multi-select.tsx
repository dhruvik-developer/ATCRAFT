import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, X } from 'lucide-react'
import { useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/overlays'
import { useAuth } from '@/features/auth/auth-context'
import { WAREHOUSE_LOOKUP_PARAMS } from '@/features/inventory/types'
import { api } from '@/lib/api-client'
import { describeError } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import type { WarehouseOption } from './mrp-types'

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
  const params = WAREHOUSE_LOOKUP_PARAMS
  const warehouses = useQuery({
    queryKey: queryKeys.warehouses.list(params),
    queryFn: () => api.getPage<WarehouseOption>('/warehouses', { params }),
    enabled: canReadWarehouses,
    staleTime: staleTimes.master,
  })

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

  return (
    <div className="flex items-center gap-1">
      <Popover>
        <PopoverTrigger asChild>
          <Button
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            disabled={disabled}
            className={cn(
              'h-9 w-full justify-between font-normal',
              value.length === 0 && 'text-muted-foreground',
            )}
          >
            <span className="truncate">{warehouses.isLoading ? 'Loading...' : label}</span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] p-0">
          <p className="border-b px-3 py-2 text-xs text-muted-foreground">
            Leave everything unticked to include all warehouses. Select up to {maxSelections}.
          </p>
          <div className="max-h-64 overflow-y-auto p-1">
            {warehouses.isLoading && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                Loading warehouses...
              </p>
            )}
            {!warehouses.isLoading && warehouses.error && (
              <p className="px-2 py-6 text-center text-sm text-destructive">
                {describeError(warehouses.error)}
              </p>
            )}
            {!warehouses.isLoading && !warehouses.error && rows.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                No warehouses are configured yet.
              </p>
            )}
            {rows.map((warehouse) => {
              const isSelected = chosen.has(warehouse.id)
              return (
                <button
                  key={warehouse.id}
                  type="button"
                  role="checkbox"
                  aria-checked={isSelected}
                  disabled={!isSelected && value.length >= maxSelections}
                  onClick={() => toggle(warehouse.id)}
                  className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Check
                    className={cn('mt-0.5 h-4 w-4 shrink-0', !isSelected && 'invisible')}
                    aria-hidden
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{warehouse.code}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {warehouse.name}
                    </span>
                  </span>
                </button>
              )
            })}
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

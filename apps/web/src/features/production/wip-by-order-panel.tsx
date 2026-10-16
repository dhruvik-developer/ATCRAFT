import { keepPreviousData, useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Factory, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { EmptyState, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { useAuth } from '@/features/auth/auth-context'
import { usePlantOptions } from '@/hooks/use-organisation-options'
import { api } from '@/lib/api-client'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney, formatNumber, formatQuantity } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import type { PaginatedEnvelope } from '@/types/api'

import { workOrderKeys, type WipOrderRow } from './work-order-model'

const DEFAULT_LIMIT = 25

type Filters = {
  warehouseId: string
  plantId: string
  itemId: string | null
  lateOnly: boolean
}

const EMPTY_FILTERS: Filters = {
  warehouseId: '',
  plantId: '',
  itemId: null,
  lateOnly: false,
}

export function WipByOrderPanel() {
  const { can } = useAuth()
  const canView = can(P.PRODUCTION_ORDER_VIEW) && can(P.PRODUCTION_COST_VIEW)
  const canViewPlant = can(P.PLANT_VIEW)
  const canViewWarehouse = can(P.WAREHOUSE_VIEW)
  const canViewItem = can(P.ITEM_VIEW)

  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(DEFAULT_LIMIT)

  const updateFilters = (patch: Partial<Filters>) => {
    setFilters((current) => ({ ...current, ...patch }))
    setPage(1)
  }

  const params = useMemo(
    () => ({
      page,
      limit,
      with_total: true,
      ...(filters.warehouseId ? { warehouse_id: filters.warehouseId } : {}),
      ...(filters.plantId ? { plant_id: filters.plantId } : {}),
      ...(filters.itemId ? { item_id: filters.itemId } : {}),
      ...(filters.lateOnly ? { late_only: true } : {}),
    }),
    [filters, limit, page],
  )

  const orders = useQuery({
    queryKey: workOrderKeys.wipOrders(params),
    queryFn: (): Promise<PaginatedEnvelope<WipOrderRow>> =>
      api.getPage<WipOrderRow>('/production/wip', { params }),
    placeholderData: keepPreviousData,
    staleTime: staleTimes.transactional,
    refetchOnWindowFocus: true,
    enabled: canView,
  })

  const plants = usePlantOptions(canView && canViewPlant)
  const plantRows = plants.data ?? []

  const columns = useMemo<ColumnDef<WipOrderRow, unknown>[]>(
    () => [
      {
        id: 'job',
        header: 'Production order',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/production/${row.original.production_order_id}`}
            aria-label={`Open production order ${row.original.code}`}
            className="font-medium hover:underline"
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        accessorKey: 'planned_end_date',
        header: 'Planned finish',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.planned_end_date),
      },
      {
        accessorKey: 'days_late',
        header: 'Lateness',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.days_late > 0 ? (
            <Badge variant="warning">
              {formatNumber(row.original.days_late)} {row.original.days_late === 1 ? 'day' : 'days'}{' '}
              late
            </Badge>
          ) : (
            <span className="text-muted-foreground">On time</span>
          ),
      },
      {
        id: 'operations',
        header: 'Open operations',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">
            {formatNumber(row.original.open_work_orders)} /{' '}
            {formatNumber(row.original.total_work_orders)}
          </span>
        ),
      },
      {
        accessorKey: 'balance_quantity',
        header: 'Balance to make',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.balance_quantity),
      },
      {
        accessorKey: 'wip_value',
        header: 'WIP value',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatMoney(row.original.wip_value)}</span>
        ),
      },
    ],
    [],
  )

  if (!canView) {
    return (
      <EmptyState
        icon={Factory}
        title="You cannot see WIP value"
        description="Work in progress value requires both production-order view and production-cost view permissions."
      />
    )
  }

  const hasFilters = Boolean(
    filters.warehouseId || filters.plantId || filters.itemId || filters.lateOnly,
  )

  return (
    <div>
      <div className="mb-3">
        <h2 className="text-sm font-semibold">WIP by order</h2>
        <p className="text-xs text-muted-foreground">
          Net posted material cost plus operation cost to date. Running operations accrue elapsed
          time.
        </p>
      </div>

      <DataTable
        columns={columns}
        data={orders.data?.data ?? []}
        pagination={orders.data?.pagination}
        isLoading={orders.isLoading}
        isFetching={orders.isFetching}
        error={orders.error}
        onPageChange={setPage}
        onLimitChange={(next) => {
          setLimit(next)
          setPage(1)
        }}
        onRefresh={() => void orders.refetch()}
        getRowId={(row) => row.production_order_id}
        emptyTitle="No work in progress"
        emptyDescription="No live production orders match these filters."
        toolbar={
          <>
            {canViewPlant && plantRows.length > 0 && (
              <Select
                value={filters.plantId || SELECT_ALL}
                onValueChange={(next) =>
                  updateFilters({ plantId: next === SELECT_ALL ? '' : next })
                }
              >
                <SelectTrigger className="w-full sm:w-[12rem]" aria-label="Filter by plant">
                  <SelectValue placeholder="All plants" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_ALL}>All plants</SelectItem>
                  {plantRows.map((plant) => (
                    <SelectItem key={plant.id} value={plant.id}>
                      {plant.code} - {plant.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {canViewWarehouse && (
              <WarehouseSelect
                id="wip-warehouse-filter"
                label="Filter by warehouse"
                hideLabel
                value={filters.warehouseId}
                onChange={(warehouseId) => updateFilters({ warehouseId })}
                includeAll
                allLabel="All warehouses"
              />
            )}

            {canViewItem && (
              <ItemPicker
                id="wip-item-filter"
                label="Filter by item"
                hideLabel
                value={filters.itemId}
                onChange={(itemId) => updateFilters({ itemId })}
                placeholder="All items"
              />
            )}

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="wip-late-only"
                checked={filters.lateOnly}
                onCheckedChange={(checked) => updateFilters({ lateOnly: checked === true })}
              />
              <Label htmlFor="wip-late-only" className="cursor-pointer text-xs font-normal">
                Late only
              </Label>
            </div>

            {hasFilters && (
              <Button variant="ghost" size="sm" onClick={() => updateFilters(EMPTY_FILTERS)}>
                <X />
                Clear filters
              </Button>
            )}
          </>
        }
      />
    </div>
  )
}

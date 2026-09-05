/**
 * Production order list.
 *
 * Status, item, plant and the planned-date window are query parameters the API
 * answers. A plant that runs a few hundred orders a month accumulates tens of
 * thousands of them, and the browser never holds more than one page (master
 * rules 31/36/78).
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Plus, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { useListQuery } from '@/hooks/use-list-query'
import { usePlantOptions } from '@/hooks/use-organisation-options'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatNumber, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { ProductionOrderFormDialog } from './production-dialogs'
import {
  PRODUCTION_SOURCE_TYPES,
  PRODUCTION_STATUSES,
  priorityBand,
  progressPercent,
  type ProductionOrderRow,
} from './production-model'

/** Every URL key the toolbar owns, so "Clear" can drop them in one write. */
const FILTER_KEYS = [
  'status',
  'item_id',
  'plant_id',
  'planned_from',
  'planned_to',
  'source_type',
] as const

/**
 * Produced against planned.
 *
 * The bar carries a text value too: colour alone is not a readable answer to
 * "how far along is this order", and the number is what a planner writes down.
 */
function ProgressCell({ row }: { row: ProductionOrderRow }) {
  const percent = progressPercent(row.produced_quantity, row.planned_quantity)
  const complete = percent >= 100

  return (
    <div className="min-w-[7rem]">
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${row.code} progress`}
      >
        <div
          className={complete ? 'h-full rounded-full bg-success' : 'h-full rounded-full bg-primary'}
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="mt-1 block text-2xs tabular text-muted-foreground">{percent}%</span>
    </div>
  )
}

export default function ProductionPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  const status = searchParams.get('status') ?? ''
  const itemId = searchParams.get('item_id') ?? ''
  const plantId = searchParams.get('plant_id') ?? ''
  const dateFrom = searchParams.get('planned_from') ?? ''
  const dateTo = searchParams.get('planned_to') ?? ''
  const sourceType = searchParams.get('source_type') ?? ''

  const plants = usePlantOptions(true)
  const plantRows = plants.data ?? []

  const list = useListQuery<ProductionOrderRow>({
    path: '/production',
    queryKey: queryKeys.production.list,
    defaultSortBy: 'planned_start_date',
    defaultSortDir: 'desc',
    // Orders move through their state machine all day; a stale list sends
    // someone to a machine for an order that was completed an hour ago.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(itemId ? { item_id: itemId } : {}),
      ...(plantId ? { plant_id: plantId } : {}),
      // `planned_from` / `planned_to`, not `date_from` / `date_to`. An unknown
      // query parameter is dropped without complaint, so under the old names the
      // date window a planner typed had no effect at all on the rows returned.
      ...(dateFrom ? { planned_from: dateFrom } : {}),
      ...(dateTo ? { planned_to: dateTo } : {}),
      ...(sourceType ? { source_type: sourceType } : {}),
    },
  })

  const { setParam } = list

  /**
   * Clearing is one write, not six.
   *
   * `setSearchParams` resolves its updater against the `searchParams` of the
   * render it was called from and navigates immediately, so six calls in one
   * handler all start from the same location and the last navigation is the only
   * one that survives - clearing the source filter and silently leaving the
   * other five in place.
   */
  const clearFilters = () =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        next.delete('page')
        return next
      },
      { replace: true },
    )

  const filterCount =
    (status ? 1 : 0) +
    (itemId ? 1 : 0) +
    (plantId ? 1 : 0) +
    (dateFrom ? 1 : 0) +
    (dateTo ? 1 : 0) +
    (sourceType ? 1 : 0)

  const columns = useMemo<ColumnDef<ProductionOrderRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Order',
        // A real link as well as a clickable row: this screen is driven from
        // the keyboard all day.
        cell: ({ row }) => (
          <Link
            to={`/production/${row.original.id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        // The list row carries `item_id` and nothing else about the item: no
        // code, no name, no unit of measure. A link into the item master is the
        // only honest thing to offer until the API denormalises the code.
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.item_id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            Open item
          </Link>
        ),
      },
      {
        id: 'planned_quantity',
        header: 'Planned',
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.planned_quantity),
      },
      {
        // `produced_quantity` is not one of the API's sortable fields, so the
        // header must not offer to sort on it: an unknown `sort_by` is a hard
        // validation error, not a silently ignored parameter.
        id: 'produced_quantity',
        header: 'Produced',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.produced_quantity),
      },
      {
        id: 'progress',
        header: 'Progress',
        enableSorting: false,
        cell: ({ row }) => <ProgressCell row={row.original} />,
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'planned_start_date',
        header: 'Planned start',
        cell: ({ row }) => formatDate(row.original.planned_start_date),
      },
      {
        id: 'planned_end_date',
        header: 'Planned end',
        cell: ({ row }) => formatDate(row.original.planned_end_date),
      },
      {
        id: 'source',
        header: 'Raised by',
        enableSorting: false,
        // Whether the order came from a planner, from MRP or straight off a
        // sales order is what tells a supervisor how freely it can be moved.
        cell: ({ row }) => (
          <span className="text-muted-foreground">{humanise(row.original.source_type)}</span>
        ),
      },
      {
        id: 'priority',
        header: 'Priority',
        // A plain integer on the wire, lower being more urgent - not an enum.
        // The band is a reading aid; the number is what was actually set, so
        // both are shown rather than one standing in for the other.
        cell: ({ row }) => {
          const band = priorityBand(row.original.priority)
          return (
            <Badge variant={band.variant}>
              {band.label} ({formatNumber(row.original.priority)})
            </Badge>
          )
        },
      },
    ],
    [],
  )

  // Gating an affordance only - the API authorises the write itself.
  const newOrderButton = can(P.PRODUCTION_ORDER_CREATE) ? (
    <Button onClick={() => setCreateOpen(true)}>
      <Plus />
      New order
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Production orders"
        description="What the plant is making. An order explodes its BOM into components, draws them from stock and books output against the plan."
        actions={newOrderButton}
      />

      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search order code or item..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/production/${row.id}`)}
        emptyTitle="No production orders found"
        emptyDescription="Nothing matches the current filters. Widen the date window or clear a filter."
        emptyAction={newOrderButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {PRODUCTION_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <ItemPicker
              id="production-item-filter"
              label="Item"
              hideLabel
              value={itemId || null}
              onChange={(value) => setParam('item_id', value ?? undefined)}
              placeholder="All items"
            />

            {plantRows.length > 0 && (
              <Select
                value={plantId || SELECT_ALL}
                onValueChange={(value) =>
                  setParam('plant_id', value === SELECT_ALL ? undefined : value)
                }
              >
                <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by plant">
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

            <Select
              value={sourceType || SELECT_ALL}
              onValueChange={(value) =>
                setParam('source_type', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger
                className="w-full sm:w-[170px]"
                aria-label="Filter by how the order was raised"
              >
                <SelectValue placeholder="Any source" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any source</SelectItem>
                {PRODUCTION_SOURCE_TYPES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <DateRangeFilter
              id="production-date"
              from={dateFrom}
              to={dateTo}
              onFromChange={(value) => setParam('planned_from', value || undefined)}
              onToChange={(value) => setParam('planned_to', value || undefined)}
            />

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {can(P.PRODUCTION_ORDER_CREATE) && (
        <ProductionOrderFormDialog open={createOpen} onOpenChange={setCreateOpen} />
      )}
    </>
  )
}

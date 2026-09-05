/**
 * Stock balances - what is physically here, what is promised, what is free.
 *
 * This is the screen the business opens most, and the one where a stale number
 * causes real damage: a planner who promises 100 pieces against a cached
 * balance creates a shortage nobody discovers until picking. Availability is
 * therefore never served from cache (master rules 34) - see the query below.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { PackageSearch } from 'lucide-react'
import { useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader } from '@/components/shared'
import { Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { formatMoney, formatQuantity, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { WarehouseSelect } from './pickers'
import { BALANCE_SORT_FIELDS, type InventoryBalanceRow } from './types'

type RowTone = 'none' | 'warning' | 'destructive'

/**
 * Rows worth an operator's eye: nothing free to promise, or free stock that has
 * fallen through the item's reorder point. The comparison runs on Decimal
 * because a 4-decimal quantity does not survive a float round-trip.
 */
function toneFor(row: InventoryBalanceRow): RowTone {
  const available = toDecimal(row.available_quantity)
  if (available.lessThanOrEqualTo(0)) return 'destructive'

  if (row.reorder_level !== null) {
    const reorder = toDecimal(row.reorder_level)
    if (reorder.greaterThan(0) && available.lessThan(reorder)) return 'warning'
  }
  return 'none'
}

const TONE_LABEL: Record<RowTone, string> = {
  none: '',
  warning: 'Below reorder level',
  destructive: 'Nothing available to promise',
}

export default function InventoryBalancesPage() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()

  // Valuation is a separate permission from seeing quantities: a store keeper
  // counts stock, a controller values it. Hiding the column is UX - the API
  // omits the figure regardless of what the browser renders.
  const canSeeValuation = can(P.INVENTORY_VALUATION_VIEW)

  const warehouseId = searchParams.get('warehouse_id') ?? ''
  const belowReorder = searchParams.get('below_reorder') === 'true'
  const onlyInStock = searchParams.get('only_in_stock') === 'true'

  const list = useListQuery<InventoryBalanceRow>({
    path: '/inventory/balances',
    queryKey: queryKeys.inventory.balances,
    filters: {
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(belowReorder ? { below_reorder: true } : {}),
      ...(onlyInStock ? { only_in_stock: true } : {}),
    },
    // No default sort column. The API sorts balances by `updated_at` when none
    // is named, and it accepts only the five quantitative columns listed in
    // BALANCE_SORT_FIELDS - naming `item_code` here made the very first request
    // of the screen a 422 and the grid never painted at all.
    defaultSortDir: 'desc',
    // Master rules 34: availability is never cached. `staleTimes.live` is zero,
    // so every mount refetches, and refocusing a tab left open all morning
    // re-reads the balance instead of showing what it held at 09:00.
    staleTime: staleTimes.live,
    refetchOnWindowFocus: true,
  })

  const columns = useMemo<ColumnDef<InventoryBalanceRow, unknown>[]>(() => {
    const base: ColumnDef<InventoryBalanceRow, unknown>[] = [
      {
        accessorKey: 'item_code',
        header: 'Item code',
        cell: ({ row }) => {
          const tone = toneFor(row.original)
          return (
            // The accent is drawn inside the first cell rather than on the row,
            // so a highlighted row reads as marked, not as selected.
            <div
              className={cn(
                '-my-2 -ml-3 border-l-2 border-transparent py-2 pl-[10px]',
                tone === 'destructive' && 'border-destructive',
                tone === 'warning' && 'border-warning',
              )}
            >
              <Link
                to={'/items/' + row.original.item_id}
                className="font-mono text-xs hover:underline"
              >
                {row.original.item_code ?? row.original.item_id}
              </Link>
              {tone !== 'none' && <span className="sr-only"> - {TONE_LABEL[tone]}</span>}
            </div>
          )
        },
      },
      {
        accessorKey: 'item_name',
        header: 'Item',
        cell: ({ row }) => (
          <span className="block max-w-[22rem] truncate">{row.original.item_name ?? '-'}</span>
        ),
      },
      {
        accessorKey: 'warehouse_code',
        header: 'Warehouse',
        cell: ({ row }) => row.original.warehouse_code ?? '-',
      },
      {
        id: 'batch',
        header: 'Batch',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="font-mono text-xs text-muted-foreground">
            {row.original.batch_number ?? '-'}
          </span>
        ),
      },
      {
        accessorKey: 'on_hand_quantity',
        header: 'On hand',
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.on_hand_quantity),
      },
      {
        id: 'reserved_quantity',
        header: 'Reserved',
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.reserved_quantity),
      },
      {
        id: 'allocated_quantity',
        header: 'Allocated',
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.allocated_quantity),
      },
      {
        id: 'qc_hold_quantity',
        header: 'QC hold',
        meta: { align: 'right' },
        cell: ({ row }) => {
          const held = toDecimal(row.original.qc_hold_quantity)
          return (
            <span className={cn(held.greaterThan(0) && 'text-warning')}>
              {formatQuantity(row.original.qc_hold_quantity)}
            </span>
          )
        },
      },
      {
        // Stock that has left its source warehouse and not yet arrived. It is
        // owned but unpickable, and it is the quantity that makes a position
        // look short when nothing is actually missing.
        id: 'in_transit_quantity',
        header: 'In transit',
        meta: { align: 'right' },
        cell: ({ row }) => {
          const moving = toDecimal(row.original.in_transit_quantity)
          return (
            <span className={cn(moving.greaterThan(0) && 'text-muted-foreground')}>
              {formatQuantity(row.original.in_transit_quantity)}
            </span>
          )
        },
      },
      {
        accessorKey: 'available_quantity',
        header: 'Available',
        meta: { align: 'right' },
        cell: ({ row }) => {
          const tone = toneFor(row.original)
          return (
            <span
              className={cn(
                'font-medium',
                tone === 'destructive' && 'text-destructive',
                tone === 'warning' && 'text-warning',
              )}
            >
              {formatQuantity(row.original.available_quantity)}
            </span>
          )
        },
      },
    ]

    if (canSeeValuation) {
      base.push({
        accessorKey: 'stock_value',
        header: 'Stock value',
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.stock_value),
      })
    }

    // Sortability is decided here, from one list, rather than column by column.
    // The API accepts exactly BALANCE_SORT_FIELDS and rejects the whole request
    // for anything else - which is how the screen came to open on a 422 - so a
    // column added later has to opt in rather than inherit the table's default.
    return base.map((column) => {
      const id = column.id ?? ('accessorKey' in column ? String(column.accessorKey) : undefined)
      return id !== undefined && BALANCE_SORT_FIELDS.includes(id)
        ? column
        : { ...column, enableSorting: false }
    })
  }, [canSeeValuation])

  return (
    <div>
      <PageHeader
        title="Stock balances"
        description="On-hand, promised, in-transit and free stock by item, warehouse and batch. Read live on every visit - never from cache."
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
        searchPlaceholder="Search item code or name"
        getRowId={(row) => row.id}
        // 200-row pages of a 12-column grid are where the DOM node count starts
        // to hurt scrolling on a warehouse terminal.
        virtualise={list.data.length > 150}
        maxHeight="65svh"
        emptyTitle="No stock matches these filters"
        emptyDescription="Change the warehouse or clear the toggles to widen the search."
        toolbar={
          <>
            <WarehouseSelect
              id="balances-warehouse"
              label="Warehouse"
              hideLabel
              includeAll
              className="w-full sm:w-auto"
              value={warehouseId}
              onChange={(next) => list.setParam('warehouse_id', next || undefined)}
            />

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="balances-below-reorder"
                checked={belowReorder}
                onCheckedChange={(checked) =>
                  list.setParam('below_reorder', checked === true ? 'true' : undefined)
                }
              />
              <Label
                htmlFor="balances-below-reorder"
                className="cursor-pointer text-xs font-normal"
              >
                Below reorder level
              </Label>
            </div>

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="balances-only-in-stock"
                checked={onlyInStock}
                onCheckedChange={(checked) =>
                  list.setParam('only_in_stock', checked === true ? 'true' : undefined)
                }
              />
              <Label
                htmlFor="balances-only-in-stock"
                className="cursor-pointer text-xs font-normal"
              >
                Has stock
              </Label>
            </div>
          </>
        }
        emptyAction={
          <p className="inline-flex items-center gap-2 text-xs text-muted-foreground">
            <PackageSearch className="h-4 w-4" aria-hidden />
            Stock appears here as soon as a receipt is posted.
          </p>
        }
      />

      <p className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-0.5 rounded bg-destructive" aria-hidden />
          Nothing available to promise
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-0.5 rounded bg-warning" aria-hidden />
          Below the item&apos;s reorder level
        </span>
      </p>
    </div>
  )
}

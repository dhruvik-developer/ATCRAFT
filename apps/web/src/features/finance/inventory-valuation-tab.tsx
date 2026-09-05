/**
 * Inventory valuation - what the stock on hand is worth.
 *
 * `GET /finance/inventory-valuation` answers an
 * `ApiResponse<InventoryValuationPage>`: the pagination block and the totals
 * for the whole filtered set live *inside* `data`, beside the rows. That is not
 * the `PaginatedResponse` envelope `useListQuery` reads, so this screen holds
 * its own page state in the URL and unwraps the page itself.
 *
 * The position is valued at what its open FIFO cost layers actually cost, as it
 * stands now; there is no "as on" parameter, because a historical valuation is a
 * replay of the ledger rather than a filter on the current balances. The
 * full-company run is a different animal again - every item in every warehouse
 * is a report, not a page - so it is queued as a background job and arrives as a
 * file (docs/api.md, "Long-running operations").
 *
 * The two value columns are a reconciliation, and which is which matters:
 *
 * * `value` is `sum(stock_value)` - what the inventory engine last wrote on each
 *   balance row of the position.
 * * `recorded_value` is that same total recomputed **from the cost layer rows
 *   themselves** (`remaining_quantity x unit_cost`), reaching past the balance
 *   so the check is independent of it.
 *
 * So a difference means a balance has drifted from the layers behind it. The API
 * is explicit that a *small* one is expected and is not a defect - `value` is
 * rounded per position, the layer product is not, and the two are added in
 * different orders - while a material one is a reconciliation item. The screen
 * therefore shows the difference itself rather than colouring every rounding
 * remainder as a fault, and says so on screen instead of hiding the reason in a
 * tooltip.
 *
 * `average_cost` is a unit price (six places on the wire), not money at two,
 * so it is displayed with `formatUnitPrice` rather than `formatMoney`.
 */

import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { FileSpreadsheet } from 'lucide-react'
import { useCallback, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/auth-context'
import { WarehouseSelect } from '@/features/inventory/pickers'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import {
  formatDateTime,
  formatMoney,
  formatNumber,
  formatQuantity,
  formatUnitPrice,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  financeKeys,
  type InventoryValuationPage,
  type InventoryValuationRow,
} from './finance-types'

/**
 * `average_cost` is a UNIT PRICE, not money.
 *
 * The API derives it as value divided by on-hand quantity and quantises it to
 * `UNIT_PRICE_QUANTUM` - six decimal places, against money's two. `formatMoney`
 * fixes two, so a washer that genuinely costs 0.004250 each renders as a cost
 * per unit of nothing at all. `formatUnitPrice` in `lib/format.ts` is the
 * shared helper for that case; the purchase order, sales order and
 * invoice-match grids still format a unit price as money and should move to it.
 */
export function InventoryValuationTab() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  // UX only - the API refuses the export regardless of what this button does.
  const canExport = can(P.REPORT_EXPORT)

  const warehouseId = searchParams.get('warehouse_id') ?? ''
  const page = Number(searchParams.get('page') ?? 1)
  const limit = Number(searchParams.get('limit') ?? DEFAULT_PAGE_SIZE)

  /**
   * Write every key a handler changes in ONE update.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` captured when the callback was created, then
   * navigates. Two calls in one handler both start from that same stale base and
   * the second navigation overwrites the first, so narrowing to a warehouse
   * while sitting on page 6 used to reset the page and never apply the
   * warehouse.
   */
  const updateParams = useCallback(
    (changes: Record<string, string | number | undefined>) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          for (const [key, value] of Object.entries(changes)) {
            if (value === undefined || value === '') next.delete(key)
            else next.set(key, String(value))
          }
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const params = useMemo(
    () => ({
      page,
      limit,
      with_total: true,
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
    }),
    [page, limit, warehouseId],
  )

  const valuation = useQuery({
    queryKey: financeKeys.valuation(params),
    queryFn: () => api.get<InventoryValuationPage>('/finance/inventory-valuation', { params }),
    staleTime: staleTimes.transactional,
    // Hold the current page on screen while the next one loads. Without it
    // the table collapses to a skeleton on every page click - `useListQuery`
    // sets this for every list that goes through it, and these four are the
    // hand-rolled ones that did not.
    placeholderData: keepPreviousData,
  })

  const data = valuation.data ?? null
  const rows = data?.rows ?? []
  const job = useReportJob()

  const queueExport = useMutation({
    // A POST with its filters in the query string, which is what the endpoint
    // declares - it takes no request body.
    mutationFn: () =>
      api.post<AcceptedJobData>('/finance/inventory-valuation/export', undefined, {
        params: warehouseId ? { warehouse_id: warehouseId } : undefined,
      }),
    // Nothing is invalidated: an export reads, it does not change any record.
    onSuccess: (accepted) => job.start(accepted),
  })

  const columns = useMemo<ColumnDef<InventoryValuationRow, unknown>[]>(
    () => [
      {
        id: 'item_code',
        header: 'Item code',
        enableSorting: false,
        cell: ({ row }) => (
          <Link to={`/items/${row.original.item_id}`} className="font-mono text-xs hover:underline">
            {row.original.item_code}
          </Link>
        ),
      },
      {
        id: 'item_name',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[22rem] truncate">{row.original.item_name}</span>
        ),
      },
      {
        id: 'warehouse_code',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[12rem]">
            <span className="block truncate font-mono text-xs">{row.original.warehouse_code}</span>
            <span className="block truncate text-2xs text-muted-foreground">
              {row.original.warehouse_name}
            </span>
          </div>
        ),
      },
      {
        id: 'on_hand_quantity',
        header: 'On hand',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.on_hand_quantity),
      },
      {
        id: 'average_cost',
        header: 'Cost per unit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatUnitPrice(row.original.average_cost),
      },
      {
        id: 'value',
        header: 'Value on balance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="font-medium">{formatMoney(row.original.value)}</span>,
      },
      {
        id: 'recorded_value',
        header: 'Value from cost layers',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          // Both figures are money at two places, and the subtraction runs on
          // the decimal strings - never on a float.
          const difference = toDecimal(row.original.recorded_value).minus(
            toDecimal(row.original.value),
          )
          return (
            <div>
              <span className="block text-muted-foreground">
                {formatMoney(row.original.recorded_value)}
              </span>
              {!difference.isZero() && (
                <span className="block text-2xs text-warning">
                  {difference.isNegative() ? '' : '+'}
                  {formatMoney(difference.toFixed())} against the balance
                </span>
              )}
            </div>
          )
        },
      },
    ],
    [],
  )

  return (
    <div className="space-y-4">
      <ReportJobPanel controller={job} label="Full inventory valuation" />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {/* A dash when nothing loaded, never a formatted zero: a failed read
            must not look like a company whose stock is worth nothing. */}
        <StatTile
          label="Total value"
          value={data ? formatMoney(data.summary.total_value) : '-'}
          hint="Across every position matching these filters"
          isLoading={valuation.isLoading}
        />
        <StatTile
          label="Total quantity"
          value={data ? formatQuantity(data.summary.total_quantity) : '-'}
          hint={
            data
              ? `${formatNumber(data.summary.row_count)} item and warehouse positions`
              : undefined
          }
          isLoading={valuation.isLoading}
        />
        <StatTile
          label="Valued at"
          value={formatDateTime(data?.summary.as_of)}
          hint="FIFO cost layers as the API read them"
          isLoading={valuation.isLoading}
        />
      </div>

      {/* The reconciliation, said out loud. A tooltip on one cell is not an
          explanation: the reader has to know what the two value columns are
          before a difference between them means anything. */}
      <p className="text-xs text-muted-foreground">
        Stock is valued at what its open FIFO cost layers cost.{' '}
        <span className="font-medium text-foreground">Value on balance</span> is what the inventory
        engine last wrote on the position;{' '}
        <span className="font-medium text-foreground">value from cost layers</span> is the same
        total recomputed from the layer rows themselves, so the two are an independent check on each
        other. A difference is shown against the second: a few paise is expected rounding, anything
        material is a position whose balance has drifted from its layers and is a reconciliation
        item.
      </p>

      <DataTable
        columns={columns}
        data={rows}
        pagination={data?.pagination}
        isLoading={valuation.isLoading}
        isFetching={valuation.isFetching}
        error={valuation.error as Error | null}
        onPageChange={(next) => updateParams({ page: next })}
        onLimitChange={(next) => updateParams({ limit: next, page: undefined })}
        onRefresh={() => void valuation.refetch()}
        getRowId={(row) => `${row.item_id}:${row.warehouse_id}`}
        virtualise={rows.length > 150}
        maxHeight="60svh"
        emptyTitle="Nothing to value"
        emptyDescription="No stock is held for these filters."
        toolbar={
          <>
            <WarehouseSelect
              id="valuation-warehouse"
              label="Warehouse"
              hideLabel
              includeAll
              value={warehouseId}
              onChange={(next) =>
                updateParams({ warehouse_id: next || undefined, page: undefined })
              }
            />

            {canExport && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => queueExport.mutate()}
                loading={queueExport.isPending}
              >
                <FileSpreadsheet />
                Export full valuation
              </Button>
            )}
          </>
        }
      />
    </div>
  )
}

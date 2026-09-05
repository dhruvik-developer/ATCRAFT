/**
 * Inventory ledger - the append-only record of why every balance is what it is.
 *
 * Paged by keyset cursor rather than page number. `inventory_transactions` grows
 * without bound, and `OFFSET 40000` makes PostgreSQL walk and discard forty
 * thousand rows before returning fifty; a cursor reads the index straight to the
 * next slice, whatever the table's size. That is also why this screen does not
 * use `useListQuery`, which is built for offset pagination.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { useInfiniteQuery } from '@tanstack/react-query'
import { subDays } from 'date-fns'
import { ScrollText } from 'lucide-react'
import { useCallback, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Label } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDateTime, formatQuantity, humanise, toDateInput, toDecimal } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { CursorEnvelope } from '@/types/api'

import { ItemPicker, WarehouseSelect } from './pickers'
import { TRANSACTION_TYPES, type InventoryLedgerRow, type StockState } from './types'

const ALL = '__all__'

/** Makes a date-only upper bound inclusive of the whole day. */
const END_OF_DAY = 'T23:59:59.999'

/**
 * How each pool reads at a glance. `AVAILABLE` is the ordinary case and gets no
 * colour; everything else is stock that is owned but cannot be picked.
 */
const STOCK_STATE_TONE: Partial<Record<StockState, 'secondary' | 'warning' | 'destructive'>> = {
  AVAILABLE: 'secondary',
  QC_HOLD: 'warning',
  DAMAGED: 'destructive',
  IN_TRANSIT: 'warning',
  JOB_WORK: 'warning',
  SCRAP: 'destructive',
  RETURN: 'warning',
}

/**
 * `api.getPage` returns the response envelope untouched; only its TypeScript
 * shape assumes offset pagination. The ledger's envelope is cursor-shaped, so it
 * is re-typed here rather than widening the shared client for one endpoint.
 */
async function fetchLedgerPage(
  params: Record<string, unknown>,
): Promise<CursorEnvelope<InventoryLedgerRow>> {
  const envelope = await api.getPage<InventoryLedgerRow>('/inventory/ledger', { params })
  return envelope as unknown as CursorEnvelope<InventoryLedgerRow>
}

/**
 * Where a ledger entry's source document lives.
 *
 * `reference_id` is deliberately not a foreign key - the ledger outlives its
 * source - so a link is only offered where the id genuinely identifies the
 * document that screen shows. Everything else falls back to the audit trail,
 * which searches by document code: a slower route to the same story, but never
 * a dead link.
 */
const REFERENCE_ROUTES: Record<string, (id: string) => string> = {
  purchase_order: (id) => '/procurement/purchase-orders/' + id,
  sales_order: (id) => '/sales-orders/' + id,
  production_order: (id) => '/production/' + id,
  goods_receipt: () => '/procurement/goods-receipts',
  material_issue: () => '/production',
  stock_transfer: () => '/inventory/movements',
  stock_adjustment: () => '/inventory/movements',
  physical_count: () => '/inventory/movements',
  dispatch: () => '/dispatch',
  qc_inspection: () => '/quality',
  assembly_order: () => '/assembly',
  job_work_order: () => '/job-work',
  job_work_challan: () => '/job-work',
  job_work_receipt: () => '/job-work',
}

function referenceLink(
  referenceType: string,
  referenceId: string | null,
  referenceCode: string | null,
): string | null {
  const build = REFERENCE_ROUTES[referenceType]
  if (build && referenceId) return build(referenceId)
  return referenceCode ? '/audit?q=' + encodeURIComponent(referenceCode) : null
}

export default function InventoryLedgerPage() {
  const [searchParams, setSearchParams] = useSearchParams()

  const setFilter = useCallback(
    (key: string, value: string | undefined) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          if (!value) next.delete(key)
          else next.set(key, value)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // A ledger with no date bound is an invitation to scan the whole table. Thirty
  // days is the window an operator almost always wants, and it is widened
  // explicitly rather than by default.
  const defaultFrom = useMemo(() => toDateInput(subDays(new Date(), 30)), [])
  const defaultTo = useMemo(() => toDateInput(new Date()), [])

  const itemId = searchParams.get('item_id')
  const warehouseId = searchParams.get('warehouse_id') ?? ''
  const transactionType = searchParams.get('transaction_type') ?? ''
  const dateFrom = searchParams.get('from') || defaultFrom
  const dateTo = searchParams.get('to') || defaultTo

  const filters = useMemo(
    () => ({
      ...(itemId ? { item_id: itemId } : {}),
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(transactionType ? { transaction_type: transactionType } : {}),
      // `occurred_from` / `occurred_to`, not `date_from` / `date_to`. The ledger
      // filters on when the movement happened, and the two transfer-style names
      // are not parameters this endpoint declares - so they were dropped and
      // every visit scanned the whole table the 30-day default exists to avoid.
      //
      // The upper bound is widened to the end of the chosen day. These are
      // datetime bounds compared with `<=`, so a bare date resolves to midnight
      // and "to today" would hide everything posted today - the rows an
      // operator opens this screen to see.
      occurred_from: dateFrom,
      occurred_to: dateTo + END_OF_DAY,
      limit: DEFAULT_PAGE_SIZE,
    }),
    [itemId, warehouseId, transactionType, dateFrom, dateTo],
  )

  const ledger = useInfiniteQuery({
    queryKey: queryKeys.inventory.ledger(filters),
    queryFn: ({ pageParam }) =>
      fetchLedgerPage({ ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    // Posted movements are immutable, so a page already fetched cannot change;
    // only the head of the ledger moves.
    staleTime: staleTimes.transactional,
  })

  const rows = useMemo(() => ledger.data?.pages.flatMap((page) => page.data) ?? [], [ledger.data])

  const columns = useMemo<ColumnDef<InventoryLedgerRow, unknown>[]>(
    () => [
      {
        id: 'occurred_at',
        header: 'Occurred',
        enableSorting: false,
        cell: ({ row }) => (
          <time dateTime={row.original.occurred_at} className="whitespace-nowrap tabular">
            {formatDateTime(row.original.occurred_at)}
          </time>
        ),
      },
      {
        id: 'transaction_type',
        header: 'Type',
        enableSorting: false,
        cell: ({ row }) => {
          // The row's own signed quantity is the authority on direction - the
          // type vocabulary agrees with it, but the ledger is what posted.
          const inflow = toDecimal(row.original.quantity).greaterThan(0)
          return (
            <Badge variant={inflow ? 'success' : 'destructive'}>
              {inflow ? '+' : '-'} {humanise(row.original.transaction_type)}
            </Badge>
          )
        },
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="min-w-0">
            <Link
              to={'/items/' + row.original.item_id}
              className="font-mono text-xs hover:underline"
            >
              {row.original.item_code ?? row.original.item_id}
            </Link>
            <span className="block max-w-[18rem] truncate text-xs text-muted-foreground">
              {row.original.item_name ?? ''}
            </span>
          </div>
        ),
      },
      {
        id: 'warehouse',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => row.original.warehouse_code ?? '-',
      },
      {
        id: 'quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const quantity = toDecimal(row.original.quantity)
          const inflow = quantity.greaterThan(0)
          return (
            <span className={inflow ? 'font-medium text-success' : 'font-medium text-destructive'}>
              {inflow ? '+' : ''}
              {formatQuantity(row.original.quantity)}
            </span>
          )
        },
      },
      {
        id: 'balance_after',
        header: 'Balance after',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {formatQuantity(row.original.balance_after)}
          </span>
        ),
      },
      {
        id: 'reference',
        header: 'Reference',
        enableSorting: false,
        cell: ({ row }) => {
          const { reference_type, reference_id, reference_code } = row.original
          if (!reference_code) return <span className="text-muted-foreground">-</span>
          const href = referenceLink(reference_type, reference_id, reference_code)
          return href ? (
            <Link to={href} className="font-mono text-xs hover:underline">
              {reference_code}
            </Link>
          ) : (
            <span className="font-mono text-xs">{reference_code}</span>
          )
        },
      },
      {
        id: 'reason',
        header: 'Reason',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[20rem] truncate" title={row.original.reason}>
            {row.original.reason || '-'}
          </span>
        ),
      },
      {
        // The pool the movement landed in. A QC hold and a goods issue both
        // reduce what can be promised, but only one of them has left the
        // building, and the transaction type alone does not say which.
        id: 'stock_state',
        header: 'Stock state',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={STOCK_STATE_TONE[row.original.stock_state] ?? 'secondary'}>
            {humanise(row.original.stock_state)}
          </Badge>
        ),
      },
    ],
    [],
  )

  return (
    <div>
      <PageHeader
        title="Inventory ledger"
        description="Every stock movement, in the order it happened, with the reason it was posted."
      />

      <DataTable
        columns={columns}
        data={rows}
        isLoading={ledger.isLoading}
        isFetching={ledger.isFetching && !ledger.isFetchingNextPage}
        error={ledger.error as Error | null}
        onRefresh={() => void ledger.refetch()}
        getRowId={(row) => row.id}
        virtualise={rows.length > 150}
        maxHeight="65svh"
        emptyTitle="No movements in this window"
        emptyDescription="Widen the date range or clear the item and warehouse filters."
        emptyAction={
          <p className="inline-flex items-center gap-2 text-xs text-muted-foreground">
            <ScrollText className="h-4 w-4" aria-hidden />
            Receipts, issues, transfers and adjustments all land here.
          </p>
        }
        toolbar={
          <div className="flex flex-1 flex-wrap items-end gap-2">
            <ItemPicker
              id="ledger-item"
              label="Item"
              hideLabel
              className="w-full sm:w-auto"
              value={itemId}
              onChange={(next) => setFilter('item_id', next ?? undefined)}
            />

            <WarehouseSelect
              id="ledger-warehouse"
              label="Warehouse"
              hideLabel
              includeAll
              className="w-full sm:w-auto"
              value={warehouseId}
              onChange={(next) => setFilter('warehouse_id', next || undefined)}
            />

            <div className="w-full space-y-1.5 sm:w-auto">
              <Label htmlFor="ledger-type" className="sr-only">
                Transaction type
              </Label>
              <Select
                value={transactionType || ALL}
                onValueChange={(next) =>
                  setFilter('transaction_type', next === ALL ? undefined : next)
                }
              >
                <SelectTrigger
                  id="ledger-type"
                  aria-label="Transaction type"
                  className="sm:min-w-[190px]"
                >
                  <SelectValue placeholder="All movement types" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All movement types</SelectItem>
                  {TRANSACTION_TYPES.map((type) => (
                    <SelectItem key={type} value={type}>
                      {humanise(type)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <DateRangeFilter
              id="ledger"
              from={dateFrom}
              to={dateTo}
              onFromChange={(value) => setFilter('from', value || undefined)}
              onToChange={(value) => setFilter('to', value || undefined)}
              fromInputProps={{ max: dateTo }}
              toInputProps={{ min: dateFrom }}
            />
          </div>
        }
      />

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
        <p className="text-muted-foreground" role="status">
          {rows.length === 0
            ? 'No movements'
            : rows.length + (rows.length === 1 ? ' movement loaded' : ' movements loaded')}
        </p>

        {ledger.hasNextPage ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => void ledger.fetchNextPage()}
            loading={ledger.isFetchingNextPage}
          >
            Load more
          </Button>
        ) : (
          rows.length > 0 && (
            <p className="text-xs text-muted-foreground">End of the ledger for these filters.</p>
          )
        )}
      </div>
    </div>
  )
}

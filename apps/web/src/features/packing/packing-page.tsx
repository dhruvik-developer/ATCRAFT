import { describeError } from '@/lib/messages'
/**
 * Packing orders.
 *
 * The list answers one question for the despatch supervisor: what is still on
 * the bench and what is packed and waiting to be handed over. The list
 * projection is a header - cartons, weights, dates and the dispatch it was
 * handed to - so the packed-against-ordered progress is shown inside the order,
 * where the lines actually live.
 *
 * Opening a row goes into the carton builder, which is where the work happens.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { subDays } from 'date-fns'
import { Boxes, PackageOpen, PackagePlus, ScanBarcode, X } from 'lucide-react'
import { useMemo, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { fetchAuditPage, type AuditLogRow } from '@/features/audit/audit-types'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { CARTON_SCAN_HISTORY_DAYS, DEFAULT_PAGE_SIZE, SELECT_ALL } from '@/lib/constants'
import {
  formatDate,
  formatDateTime,
  formatNumber,
  formatQuantity,
  humanise,
  toDateInput,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { PackingOrderDialog } from './packing-order-dialog'
import { PackingOrderCreateDialog } from './packing-order-create-dialog'
import {
  PACKING_STATUSES,
  packingKeys,
  type CartonScanRequest,
  type CartonScanResult,
  type PackingOrderRow,
} from './packing-types'
import { ReadyFromAssemblyPanel } from './ready-from-assembly'

/** The URL keys this screen filters on, in the order the toolbar shows them. */
const FILTER_KEYS = [
  'status',
  'customer_id',
  'sales_order_id',
  'open_only',
  'awaiting_dispatch',
  'planned_from',
  'planned_to',
] as const

function CartonScanHistory() {
  const filters = useMemo(() => {
    const today = new Date()
    return {
      date_from: toDateInput(subDays(today, CARTON_SCAN_HISTORY_DAYS)),
      date_to: toDateInput(today),
      action: 'SCAN',
      entity_type: 'carton_scan',
      limit: DEFAULT_PAGE_SIZE,
    }
  }, [])
  const history = useInfiniteQuery({
    queryKey: queryKeys.audit.logs(filters),
    queryFn: ({ pageParam }) =>
      fetchAuditPage('/audit/logs', { ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    staleTime: staleTimes.transactional,
  })
  const rows = useMemo(() => history.data?.pages.flatMap((page) => page.data) ?? [], [history.data])

  return (
    <div className="mt-4 border-t pt-3" aria-labelledby="carton-scan-history-heading">
      <h3 id="carton-scan-history-heading" className="text-sm font-semibold">
        Recent scan history
      </h3>
      {history.isLoading && <p className="mt-2 text-sm text-muted-foreground">Loading scans...</p>}
      {history.error && (
        <div className="mt-2">
          <FieldError message={describeError(history.error)} />
        </div>
      )}
      {!history.isLoading && !history.error && rows.length === 0 && (
        <p className="mt-2 text-sm text-muted-foreground">
          No carton scans in the last {CARTON_SCAN_HISTORY_DAYS} days.
        </p>
      )}
      {rows.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm">
          {rows.map((row: AuditLogRow) => (
            <li key={row.id} className="flex flex-wrap gap-x-2">
              <time dateTime={row.created_at} className="text-muted-foreground">
                {formatDateTime(row.created_at)}
              </time>
              <span>{row.actor_label ?? 'System'}</span>
              <span>{row.summary}</span>
            </li>
          ))}
        </ul>
      )}
      {history.hasNextPage && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="mt-2"
          disabled={history.isFetchingNextPage}
          onClick={() => void history.fetchNextPage()}
        >
          {history.isFetchingNextPage ? 'Loading...' : 'Load older scans'}
        </Button>
      )}
    </div>
  )
}

function CartonScanPanel({
  onOpenOrder,
  showHistory,
}: {
  onOpenOrder: (order: PackingOrderRow) => void
  showHistory: boolean
}) {
  const [barcode, setBarcode] = useState('')
  const queryClient = useQueryClient()
  const scan = useMutation({
    mutationFn: (value: string) =>
      api.post<CartonScanResult>('/packing/scan', { barcode: value } satisfies CartonScanRequest),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.audit.logsAll })
    },
  })

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const normalized = barcode.trim().toUpperCase()
    if (normalized.length < 8) return
    setBarcode(normalized)
    scan.mutate(normalized)
  }

  return (
    <section className="mb-5 rounded-lg border bg-card p-4" aria-labelledby="carton-scan-heading">
      <div className="mb-3">
        <h2 id="carton-scan-heading" className="font-semibold">
          Find a carton
        </h2>
        <p className="text-sm text-muted-foreground">
          Scan or enter a carton label to view its packing record.
        </p>
      </div>
      <form className="flex flex-col gap-2 sm:flex-row" onSubmit={submit}>
        <Label htmlFor="packing-carton-barcode" className="sr-only">
          Scan a carton barcode
        </Label>
        <Input
          id="packing-carton-barcode"
          value={barcode}
          onChange={(event) => {
            setBarcode(event.target.value)
            if (scan.data || scan.error) scan.reset()
          }}
          placeholder="Scan carton barcode..."
          autoComplete="off"
          maxLength={64}
        />
        <Button type="submit" disabled={scan.isPending || barcode.trim().length < 8}>
          <ScanBarcode />
          {scan.isPending ? 'Searching...' : 'Find carton'}
        </Button>
      </form>
      {scan.error && (
        <div className="mt-2">
          <FieldError message={describeError(scan.error)} />
        </div>
      )}
      {scan.data && (
        <div className="mt-3 rounded-md border p-3" role="status" aria-live="polite">
          <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
            <div>
              <p className="font-medium">
                {scan.data.carton.barcode} · Carton {scan.data.carton.carton_number}
              </p>
              <p className="text-sm text-muted-foreground">
                {scan.data.packing_order.code} ·{' '}
                {scan.data.carton.is_sealed
                  ? `Sealed ${formatDateTime(scan.data.carton.sealed_at)}`
                  : 'Not sealed'}
                {' · '}
                {formatQuantity(scan.data.carton.gross_weight_kg)} kg gross
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenOrder(scan.data.packing_order)}
            >
              <PackageOpen />
              Open packing record
            </Button>
          </div>
        </div>
      )}
      {showHistory && <CartonScanHistory />}
    </section>
  )
}

export default function PackingPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [openOrder, setOpenOrder] = useState<PackingOrderRow | null>(null)
  const [createStandaloneOpen, setCreateStandaloneOpen] = useState(false)
  const createFromSalesOrderId = searchParams.get('create_from_sales_order_id')

  const status = searchParams.get('status') ?? ''
  const customerId = searchParams.get('customer_id') ?? ''
  const salesOrderId = searchParams.get('sales_order_id') ?? ''
  const openOnly = searchParams.get('open_only') === 'true'
  const awaitingDispatch = searchParams.get('awaiting_dispatch') === 'true'
  const plannedFrom = searchParams.get('planned_from') ?? ''
  const plannedTo = searchParams.get('planned_to') ?? ''

  const list = useListQuery<PackingOrderRow>({
    path: '/packing',
    queryKey: packingKeys.list,
    defaultSortBy: 'planned_date',
    defaultSortDir: 'asc',
    // Packing progresses through a shift; a cached view sends a picker to a
    // carton somebody else already sealed.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(customerId ? { customer_id: customerId } : {}),
      ...(salesOrderId ? { sales_order_id: salesOrderId } : {}),
      ...(openOnly ? { open_only: true } : {}),
      ...(awaitingDispatch ? { awaiting_dispatch: true } : {}),
      ...(plannedFrom ? { planned_from: plannedFrom } : {}),
      ...(plannedTo ? { planned_to: plannedTo } : {}),
    },
  })

  const { setParam } = list

  /**
   * Clear every filter in one write.
   *
   * Six `setParam` calls looked like six clears but were one: react-router's
   * `setSearchParams` closes over the search params of the render it was
   * created in, so each call recomputed from the *same* starting point and only
   * the last survived - the screen kept five of the six filters and looked
   * broken. The next params are computed once and written once.
   */
  const clearFilters = () => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        // Page 4 of the filtered list does not exist in the unfiltered one.
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }

  const filterCount =
    (status ? 1 : 0) +
    (customerId ? 1 : 0) +
    (salesOrderId ? 1 : 0) +
    (openOnly ? 1 : 0) +
    (awaitingDispatch ? 1 : 0) +
    (plannedFrom ? 1 : 0) +
    (plannedTo ? 1 : 0)

  const columns = useMemo<ColumnDef<PackingOrderRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Packing order',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'sales_order',
        header: 'Against',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.sales_order_id ? (
            <Link
              to={`/sales-orders/${row.original.sales_order_id}`}
              className="hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              Sales order
            </Link>
          ) : (
            <span className="text-muted-foreground">Standalone</span>
          ),
      },
      {
        accessorKey: 'planned_date',
        header: 'Planned for',
        cell: ({ row }) => formatDate(row.original.planned_date),
      },
      {
        accessorKey: 'packed_date',
        header: 'Packed',
        cell: ({ row }) => formatDateTime(row.original.packed_date),
      },
      {
        accessorKey: 'total_cartons',
        header: 'Cartons',
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.total_cartons === 0 ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatNumber(row.original.total_cartons)
          ),
      },
      {
        id: 'total_gross_weight',
        header: 'Gross kg',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.total_gross_weight),
      },
      {
        id: 'total_net_weight',
        header: 'Net kg',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.total_net_weight),
      },
      {
        id: 'handover',
        header: 'Handed over',
        enableSorting: false,
        // A packed order that has not been handed to a consignment is the queue
        // the despatch bay works from, so it is called out rather than blank.
        cell: ({ row }) =>
          row.original.handed_over_at ? (
            <Badge variant="success">{formatDate(row.original.handed_over_at)}</Badge>
          ) : row.original.status === 'PACKED' ? (
            <Badge variant="warning">Awaiting dispatch</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <Button
            variant="outline"
            size="sm"
            onClick={(event) => {
              event.stopPropagation()
              setOpenOrder(row.original)
            }}
          >
            <PackageOpen />
            {can(P.PACKING_MANAGE) ? 'Pack' : 'View'}
          </Button>
        ),
      },
    ],
    [can],
  )

  return (
    <div>
      <PageHeader
        title="Packing"
        description="Orders being made ready for despatch. Raise standalone or sales-order packing work, then build cartons and generate the packing list."
        actions={
          can(P.PACKING_MANAGE) && (
            <Button onClick={() => setCreateStandaloneOpen(true)}>
              <PackagePlus />
              New packing order
            </Button>
          )
        }
      />

      <CartonScanPanel onOpenOrder={setOpenOrder} showHistory={can(P.AUDIT_LOG_VIEW)} />

      <ReadyFromAssemblyPanel />

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
        onRowClick={setOpenOrder}
        searchValue={list.search}
        searchPlaceholder="Search packing order code..."
        getRowId={(row) => row.id}
        emptyTitle="No packing orders"
        emptyDescription="Nothing matches the current filters. Raise a standalone or sales-order packing order, or pack a completed assembly above."
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[190px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {PACKING_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <CustomerPicker
              id="packing-customer-filter"
              label="Customer"
              hideLabel
              value={customerId || null}
              onChange={(value) => setParam('customer_id', value ?? undefined)}
              placeholder="All customers"
            />

            <DateRangeFilter
              id="packing-planned"
              fromLabel="Planned"
              from={plannedFrom}
              to={plannedTo}
              onFromChange={(value) => setParam('planned_from', value || undefined)}
              onToChange={(value) => setParam('planned_to', value || undefined)}
            />

            <div className="flex items-center gap-2">
              <Checkbox
                id="packing-open-only"
                checked={openOnly}
                onCheckedChange={(checked) =>
                  setParam('open_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="packing-open-only" className="font-normal">
                Still on the bench
              </Label>
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="packing-awaiting-dispatch"
                checked={awaitingDispatch}
                onCheckedChange={(checked) =>
                  setParam('awaiting_dispatch', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="packing-awaiting-dispatch" className="font-normal">
                Awaiting dispatch
              </Label>
            </div>

            {salesOrderId && (
              <Badge variant="muted" className="gap-1">
                One sales order's packing
                <button
                  type="button"
                  className="rounded-sm hover:text-foreground"
                  aria-label="Show every sales order"
                  onClick={() => setParam('sales_order_id', undefined)}
                >
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            )}

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear
              </Button>
            )}
          </>
        }
        emptyAction={
          <span className="inline-flex items-center gap-1.5 text-sm text-muted-foreground">
            <Boxes className="h-4 w-4" aria-hidden />
            Pack a completed assembly above to raise its packing order
          </span>
        }
      />

      <PackingOrderDialog
        order={openOrder}
        open={Boolean(openOrder)}
        onOpenChange={(open) => {
          if (!open) setOpenOrder(null)
        }}
      />

      {(createStandaloneOpen || Boolean(createFromSalesOrderId)) && (
        <PackingOrderCreateDialog
          key={createFromSalesOrderId ?? 'standalone'}
          open
          salesOrderId={createFromSalesOrderId}
          onOpenChange={(open) => {
            if (open) return
            setCreateStandaloneOpen(false)
            setSearchParams(
              (current) => {
                const next = new URLSearchParams(current)
                next.delete('create_from_sales_order_id')
                return next
              },
              { replace: true },
            )
          }}
          onCreated={(created) => {
            setCreateStandaloneOpen(false)
            setSearchParams(
              (current) => {
                const next = new URLSearchParams(current)
                next.delete('create_from_sales_order_id')
                if (created.sales_order_id) {
                  next.set('sales_order_id', created.sales_order_id)
                }
                next.delete('page')
                return next
              },
              { replace: true },
            )
          }}
        />
      )}
    </div>
  )
}

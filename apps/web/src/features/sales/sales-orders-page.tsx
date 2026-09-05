/**
 * Sales order list.
 *
 * Status, customer, the required-date window and the urgent-only switch are all
 * query parameters the API answers: the order book runs to tens of thousands of
 * rows and the browser never sees more than one page of it (master rules
 * 31/36/78).
 *
 * The list projection carries ids, not names. There is no customer name on the
 * row, so the customer is a filter rather than a column - resolving fifty names
 * with fifty reads per page is the N+1 this codebase refuses, and inventing a
 * name from a cache would be worse.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Plus, ShieldQuestion, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
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
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { daysUntil, formatDate, formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { SalesOrderFormDialog } from './sales-order-form-dialog'
import { SALES_ORDER_STATUSES, type SalesOrderRow } from './sales-types'

/** Statuses where a late required date still matters to somebody. */
const OPEN_STATUSES = new Set(['DRAFT', 'CONFIRMED', 'ALLOCATED', 'PARTIALLY_DISPATCHED'])

export default function SalesOrdersPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  const status = searchParams.get('status') ?? ''
  const customerId = searchParams.get('customer_id') ?? ''
  const requiredFrom = searchParams.get('required_from') ?? ''
  const requiredTo = searchParams.get('required_to') ?? ''
  const urgentOnly = searchParams.get('is_urgent') === 'true'
  const openOnly = searchParams.get('open_only') === 'true'

  const list = useListQuery<SalesOrderRow>({
    path: '/sales',
    queryKey: queryKeys.sales.list,
    defaultSortBy: 'order_date',
    defaultSortDir: 'desc',
    // Orders move all day - a stale order book sends someone to a dispatch bay
    // for a line that was cancelled an hour ago.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(customerId ? { customer_id: customerId } : {}),
      // The API windows on the required date, which is the date MRP plans
      // against - not on when the order was keyed in.
      ...(requiredFrom ? { required_from: requiredFrom } : {}),
      ...(requiredTo ? { required_to: requiredTo } : {}),
      ...(urgentOnly ? { is_urgent: true } : {}),
      ...(openOnly ? { open_only: true } : {}),
    },
  })

  /**
   * Every filter key that changes, written once.
   *
   * `list.setParam` takes a single key, and react-router resolves even the
   * functional form of `setSearchParams` against the `searchParams` captured
   * for *this* render rather than against an accumulator. Two calls in one
   * handler therefore both start from the same snapshot and the last
   * navigation wins, silently discarding the first - clearing six filters that
   * way cleared exactly one. Compute the next params once, write them once.
   */
  const setParams = useCallback(
    (next: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (value === undefined || value === '') updated.delete(key)
            else updated.set(key, value)
          }
          // A filter change invalidates the page cursor, exactly as setParam does.
          updated.set('page', '1')
          return updated
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const filterCount =
    (status ? 1 : 0) +
    (customerId ? 1 : 0) +
    (requiredFrom ? 1 : 0) +
    (requiredTo ? 1 : 0) +
    (urgentOnly ? 1 : 0) +
    (openOnly ? 1 : 0)

  const columns = useMemo<ColumnDef<SalesOrderRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Order',
        // A real link, not only a clickable row: this screen is driven from the
        // keyboard all day.
        cell: ({ row }) => (
          <Link
            to={`/sales-orders/${row.original.id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        id: 'customer_po_number',
        header: 'Customer PO',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[12rem] truncate text-muted-foreground">
            {row.original.customer_po_number ?? '-'}
          </span>
        ),
      },
      {
        accessorKey: 'order_date',
        header: 'Ordered',
        cell: ({ row }) => formatDate(row.original.order_date),
      },
      {
        accessorKey: 'required_date',
        header: 'Required',
        cell: ({ row }) => {
          const days = daysUntil(row.original.required_date)
          const late = days !== null && days < 0 && OPEN_STATUSES.has(row.original.status)
          return (
            <span className={late ? 'font-medium text-destructive' : undefined}>
              {formatDate(row.original.required_date)}
              {late && <span className="ml-1 text-xs">({Math.abs(days)}d late)</span>}
            </span>
          )
        },
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex flex-wrap items-center gap-1">
            <StatusBadge status={row.original.status} />
            {/* A below-cost line holds the order at DRAFT until somebody signs
                for it; that is worth seeing without opening the order. */}
            {row.original.requires_approval && !row.original.approved_at && (
              <Badge variant="warning">
                <ShieldQuestion className="h-3 w-3" aria-hidden />
                Approval
              </Badge>
            )}
          </span>
        ),
      },
      {
        accessorKey: 'priority',
        header: 'Priority',
        meta: { align: 'right' },
        // The API's priority is a 1-9 rank, not a named band, so it is shown as
        // the rank it is. Urgency is its own flag and carries the colour.
        cell: ({ row }) => (
          <span className="flex items-center justify-end gap-1.5">
            <span className="tabular text-muted-foreground">{row.original.priority}</span>
            {row.original.is_urgent && <Badge variant="destructive">Urgent</Badge>}
          </span>
        ),
      },
      {
        accessorKey: 'total_amount',
        header: 'Total',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.total_amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'supply',
        header: 'Supply',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {row.original.is_interstate ? 'Inter-state' : 'Intra-state'}
            {row.original.place_of_supply_state_code
              ? ` (${row.original.place_of_supply_state_code})`
              : ''}
          </span>
        ),
      },
    ],
    [],
  )

  const newOrderButton = can(P.SALES_ORDER_CREATE) ? (
    <Button onClick={() => setCreateOpen(true)}>
      <Plus />
      New order
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Sales orders"
        description="Customer demand as MRP sees it. Confirming an order reserves stock against its lines."
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
        searchPlaceholder="Search order code or customer PO..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/sales-orders/${row.id}`)}
        emptyTitle="No sales orders found"
        emptyDescription="Nothing matches the current filters. Widen the date window or clear a filter."
        emptyAction={newOrderButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParams({ status: value === SELECT_ALL ? undefined : value })
              }
            >
              <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {SALES_ORDER_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <CustomerPicker
              id="orders-customer-filter"
              label="Customer"
              hideLabel
              className="w-full sm:w-auto"
              value={customerId || null}
              onChange={(value) => setParams({ customer_id: value ?? undefined })}
              placeholder="All customers"
            />

            <DateRangeFilter
              id="orders-required"
              fromLabel="Required from"
              from={requiredFrom}
              to={requiredTo}
              onFromChange={(value) => setParams({ required_from: value || undefined })}
              onToChange={(value) => setParams({ required_to: value || undefined })}
            />

            <div className="flex items-center gap-2">
              <Checkbox
                id="orders-urgent-only"
                checked={urgentOnly}
                onCheckedChange={(checked) =>
                  setParams({ is_urgent: checked === true ? 'true' : undefined })
                }
              />
              <Label htmlFor="orders-urgent-only" className="font-normal">
                Urgent only
              </Label>
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="orders-open-only"
                checked={openOnly}
                onCheckedChange={(checked) =>
                  setParams({ open_only: checked === true ? 'true' : undefined })
                }
              />
              <Label htmlFor="orders-open-only" className="font-normal">
                Open only
              </Label>
            </div>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  setParams({
                    status: undefined,
                    customer_id: undefined,
                    required_from: undefined,
                    required_to: undefined,
                    is_urgent: undefined,
                    open_only: undefined,
                  })
                }
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {can(P.SALES_ORDER_CREATE) && (
        <SalesOrderFormDialog open={createOpen} onOpenChange={setCreateOpen} />
      )}
    </>
  )
}

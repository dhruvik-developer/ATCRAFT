/**
 * The daily bookings tab: date x shift across all open orders.
 *
 * The supervisor's end-of-shift view - what each live job booked today, and an
 * inline Record on every row that may still take output. Two server-side reads
 * feed it: the open orders (paginated, like the Orders tab) and one page of
 * the company's bookings for the chosen date and shift, grouped in memory by
 * order. Grouping is not filtering: the day's bookings arrive complete from
 * one query, and the grouping only decides which row each one is totalled
 * under.
 *
 * A day with more bookings than one page holds says so rather than totalling
 * silently short: the totals cover the bookings the API returned.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ShiftSelect } from '@/features/shifts/shift-select'
import { useListQuery } from '@/hooks/use-list-query'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api-client'
import { formatDate, formatQuantity, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { RecordProductionDialog } from './production-dialogs'
import {
  acceptsProductionEntry,
  progressPercent,
  type ProductionEntryRow,
  type ProductionOrderRow,
} from './production-model'
import { localTodayISO } from './planning-model'

/** One page of `GET /production/entries` is 200 rows; past that the tab says so. */
const DAY_BOOKING_LIMIT = 200

interface DayTotal {
  produced: string
  rejected: string
  bookings: number
}

function DayCell({ total }: { total: DayTotal | undefined }) {
  if (!total) return <span className="text-muted-foreground">-</span>
  const rejected = toDecimal(total.rejected)
  return (
    <span className="tabular">
      {formatQuantity(total.produced)}
      <span className="block text-2xs text-muted-foreground">
        {total.bookings} booking{total.bookings === 1 ? '' : 's'}
        {!rejected.isZero() ? ` · ${formatQuantity(total.rejected)} rejected` : ''}
      </span>
    </span>
  )
}

export function DailyBookingsPanel() {
  const { can } = useAuth()
  const canRecord = can(P.PRODUCTION_ENTRY_CREATE)
  const [date, setDate] = useState(localTodayISO)
  const [shiftId, setShiftId] = useState<string | null>(null)
  const [recording, setRecording] = useState<ProductionOrderRow | null>(null)

  const orders = useListQuery<ProductionOrderRow>({
    path: '/production',
    queryKey: queryKeys.production.list,
    defaultSortBy: 'planned_start_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: { open_only: true },
  })

  const entryParams = useMemo(
    () => ({
      entry_from: date,
      entry_to: date,
      ...(shiftId ? { shift_id: shiftId } : {}),
      limit: DAY_BOOKING_LIMIT,
      with_total: false,
    }),
    [date, shiftId],
  )
  const dayBookings = useQuery({
    queryKey: [...queryKeys.production.all, 'entries', 'day', entryParams],
    queryFn: () => api.getPage<ProductionEntryRow>('/production/entries', { params: entryParams }),
    staleTime: staleTimes.transactional,
    refetchOnWindowFocus: true,
  })

  const totals = useMemo(() => {
    // Decimal-string arithmetic throughout: a day's bookings add up through
    // decimal.js, never through doubles that cannot hold 0.1 and 0.2.
    const map = new Map<string, { produced: string; rejected: string; bookings: number }>()
    for (const entry of dayBookings.data?.data ?? []) {
      const total = map.get(entry.production_order_id) ?? {
        produced: '0',
        rejected: '0',
        bookings: 0,
      }
      total.produced = toDecimal(total.produced).plus(toDecimal(entry.produced_quantity)).toString()
      total.rejected = toDecimal(total.rejected).plus(toDecimal(entry.rejected_quantity)).toString()
      total.bookings += 1
      map.set(entry.production_order_id, total)
    }
    return map
  }, [dayBookings.data])

  const truncated = dayBookings.data?.pagination.has_next ?? false

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div>
          <Label htmlFor="daily-bookings-date">Date</Label>
          <input
            id="daily-bookings-date"
            type="date"
            value={date}
            max={localTodayISO()}
            onChange={(event) => event.target.value && setDate(event.target.value)}
            className="mt-1 flex h-9 rounded-md border border-input bg-background px-3 py-1 text-sm"
          />
        </div>
        <div>
          <Label htmlFor="daily-bookings-shift">Shift</Label>
          <ShiftSelect id="daily-bookings-shift" value={shiftId} onChange={setShiftId} />
        </div>
        {truncated && (
          <Alert variant="warning" className="basis-full">
            <AlertTitle>More bookings than shown</AlertTitle>
            <AlertDescription>
              This date holds more than {DAY_BOOKING_LIMIT} bookings; the totals below cover the
              first page only.
            </AlertDescription>
          </Alert>
        )}
      </div>

      <DataTable
        columns={[
          {
            accessorKey: 'code',
            header: 'Order',
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
            id: 'status',
            header: 'Status',
            enableSorting: false,
            cell: ({ row }) => <StatusBadge status={row.original.status} />,
          },
          {
            id: 'progress',
            header: 'Made / planned',
            enableSorting: false,
            meta: { align: 'right' },
            cell: ({ row }) => (
              <span className="tabular">
                {formatQuantity(row.original.produced_quantity)} /{' '}
                {formatQuantity(row.original.planned_quantity)}
                <span className="block text-2xs text-muted-foreground">
                  {progressPercent(row.original.produced_quantity, row.original.planned_quantity)}%
                </span>
              </span>
            ),
          },
          {
            id: 'today',
            header: `Booked ${formatDate(date)}`,
            enableSorting: false,
            meta: { align: 'right' },
            cell: ({ row }) => {
              const raw = totals.get(row.original.id)
              return (
                <DayCell
                  total={
                    raw
                      ? {
                          produced: raw.produced,
                          rejected: raw.rejected,
                          bookings: raw.bookings,
                        }
                      : undefined
                  }
                />
              )
            },
          },
          {
            id: 'record',
            header: '',
            enableSorting: false,
            enableHiding: false,
            meta: { align: 'right' },
            cell: ({ row }) => {
              if (!canRecord) return null
              if (!acceptsProductionEntry(row.original.status)) {
                return (
                  <span
                    className="text-2xs text-muted-foreground"
                    title="Issue material and start the job before booking output"
                  >
                    Not started
                  </span>
                )
              }
              return (
                <Button variant="outline" size="sm" onClick={() => setRecording(row.original)}>
                  Record
                </Button>
              )
            },
          },
        ]}
        data={orders.data}
        pagination={orders.pagination}
        isLoading={orders.isLoading || dayBookings.isLoading}
        isFetching={orders.isFetching || dayBookings.isFetching}
        error={orders.error ?? dayBookings.error}
        onPageChange={orders.setPage}
        onLimitChange={orders.setLimit}
        onSortChange={orders.setSort}
        onRefresh={() => {
          void orders.refetch()
          void dayBookings.refetch()
        }}
        getRowId={(row) => row.id}
        emptyTitle="No open orders"
        emptyDescription="Released, issued and in-process jobs appear here for their daily booking."
      />

      {recording && (
        <RecordProductionDialog
          open={Boolean(recording)}
          onOpenChange={(open) => {
            if (!open) setRecording(null)
          }}
          orderId={recording.id}
          orderCode={recording.code}
          itemId={recording.item_id}
        />
      )}
    </div>
  )
}

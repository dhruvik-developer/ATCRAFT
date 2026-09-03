/**
 * The requirements behind a run's suggestions, and what supply is late.
 *
 * Until now the late-supply information existed only as a run-level count and a
 * sentence in the warnings array, so a planner could see that *something* was
 * late but not what, how much, or how late. That is the situation the whole
 * time-phasing change exists to fix: a shortage with a purchase order behind it
 * that lands two days after it is needed is a phone call to a supplier, while
 * the same shortage with nothing coming is a new order. They look identical
 * until the late supply is on the row.
 *
 * Every figure here is rendered from a typed field on `MrpRequirementRead`,
 * never parsed out of the warning text. The type is derived from the generated
 * contract, so a backend rename becomes a compile error rather than a column of
 * blanks.
 *
 * The late quantity is a SUM over that feed's late tranches and its opening
 * date is the MINIMUM over the same tranches, so this renders the INTERVAL -
 * "first 10 on 30 Jul, all 5,010 by 15 Dec". Showing the sum against the
 * opening date alone would say five thousand arrives in July.
 */

import { AlertTriangle, Clock, PackageCheck } from 'lucide-react'
import { useMemo } from 'react'

import { DataTable } from '@/components/data-table'
import { Card, CardContent } from '@/components/ui/primitives'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatQuantity } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'

import type { MrpRequirementRow } from './mrp-types'
import type { ColumnDef } from '@tanstack/react-table'

/** A feed's late supply, rendered as the interval it actually spans. */
function LateInterval({
  quantity,
  from,
  to,
  label,
}: {
  quantity: string | null | undefined
  from: string | null | undefined
  to: string | null | undefined
  label: string
}) {
  const amount = Number(quantity ?? 0)
  if (!Number.isFinite(amount) || amount <= 0) return null

  // Older rows were planned before the closing date was computed and carry only
  // the opening one. Saying "from 30 Jul" is honest there; inventing a closing
  // date, or implying the quantity all lands on the opening one, is not.
  const sameDay = from && to && from === to
  const when = !from
    ? 'date not recorded'
    : sameDay
      ? formatDate(from)
      : to
        ? `${formatDate(from)} → ${formatDate(to)}`
        : `from ${formatDate(from)}`

  return (
    <div className="flex items-baseline gap-1.5 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">{formatQuantity(quantity)}</span>
      <span className="text-muted-foreground">{when}</span>
    </div>
  )
}

export function ShortageReportTab({ runId }: { runId: string }) {
  const requirements = useListQuery<MrpRequirementRow>({
    path: '/mrp/shortage-report',
    queryKey: (params) => queryKeys.mrp.shortageReport(runId, params),
    filters: useMemo(() => ({ run_id: runId, shortage_only: true }), [runId]),
    defaultSortBy: 'shortage_quantity',
    defaultSortDir: 'desc',
  })

  const columns = useMemo<ColumnDef<MrpRequirementRow, unknown>[]>(
    () => [
      {
        id: 'item_code',
        header: 'Item',
        enableSorting: true,
        cell: ({ row }) => (
          <div>
            <div className="font-medium">{row.original.item_code}</div>
            <div className="text-xs text-muted-foreground">
              {row.original.level === 0 ? 'Demanded directly' : `BOM level ${row.original.level}`}
            </div>
          </div>
        ),
      },
      {
        id: 'earliest_required_date',
        header: 'Needed by',
        enableSorting: true,
        cell: ({ row }) => formatDate(row.original.earliest_required_date),
      },
      {
        id: 'gross_requirement',
        header: 'Demand',
        enableSorting: true,
        cell: ({ row }) => (
          <span className="tabular-nums">{formatQuantity(row.original.gross_requirement)}</span>
        ),
      },
      {
        id: 'available_quantity',
        header: 'Available',
        enableSorting: true,
        cell: ({ row }) => (
          <span className="tabular-nums">{formatQuantity(row.original.available_quantity)}</span>
        ),
      },
      {
        id: 'incoming_quantity',
        header: 'Counted supply',
        enableSorting: true,
        cell: ({ row }) => (
          <div className="space-y-0.5">
            <div className="flex items-baseline gap-1.5 text-xs">
              <PackageCheck className="size-3 shrink-0 text-muted-foreground" aria-hidden />
              <span className="tabular-nums">{formatQuantity(row.original.incoming_quantity)}</span>
              <span className="text-muted-foreground">on order</span>
            </div>
            {Number(row.original.in_production_quantity ?? 0) > 0 && (
              <div className="flex items-baseline gap-1.5 text-xs">
                <PackageCheck className="size-3 shrink-0 text-muted-foreground" aria-hidden />
                <span className="tabular-nums">
                  {formatQuantity(row.original.in_production_quantity)}
                </span>
                <span className="text-muted-foreground">being made</span>
              </div>
            )}
          </div>
        ),
      },
      {
        // The column this whole tab exists for.
        id: 'late_supply',
        header: 'Arrives too late',
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original
          const total = Number(r.late_supply_quantity ?? 0)
          if (!Number.isFinite(total) || total <= 0) {
            return <span className="text-xs text-muted-foreground">&mdash;</span>
          }
          return (
            <div className="space-y-0.5">
              <LateInterval
                label="on order"
                quantity={r.late_incoming_quantity}
                from={r.first_late_incoming_date}
                to={r.last_late_incoming_date}
              />
              <LateInterval
                label="being made"
                quantity={r.late_in_production_quantity}
                from={r.first_late_in_production_date}
                to={r.last_late_in_production_date}
              />
            </div>
          )
        },
      },
      {
        id: 'shortage_quantity',
        header: 'Short',
        enableSorting: true,
        cell: ({ row }) => (
          <span className="font-medium tabular-nums text-destructive">
            {formatQuantity(row.original.shortage_quantity)}
          </span>
        ),
      },
    ],
    [],
  )

  const lateRows = requirements.data.filter((r) => Number(r.late_supply_quantity ?? 0) > 0).length

  return (
    <div className="space-y-4">
      {lateRows > 0 && (
        <Card>
          <CardContent className="flex items-start gap-3 py-4">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600" aria-hidden />
            <div className="space-y-1 text-sm">
              <div className="font-medium">
                {lateRows} shortage{lateRows === 1 ? '' : 's'} on this page have supply that arrives
                too late
              </div>
              <p className="text-muted-foreground">
                The stock exists and is committed, but it lands after the date it is needed, so the
                planner did not net it off and the shortage stands. Pulling one of those deliveries
                forward may remove the shortage without a new order.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      <DataTable
        columns={columns}
        data={requirements.data}
        pagination={requirements.pagination}
        isLoading={requirements.isLoading}
        isFetching={requirements.isFetching}
        error={requirements.error}
        onPageChange={requirements.setPage}
        onLimitChange={requirements.setLimit}
        onSortChange={requirements.setSort}
        onSearchChange={requirements.setSearch}
        onRefresh={() => void requirements.refetch()}
        searchValue={requirements.search}
        searchPlaceholder="Search item code..."
        getRowId={(row) => row.id}
        emptyTitle="No shortages"
        emptyDescription="Every requirement in this run is covered by stock or by supply that arrives in time."
      />

      <p className="text-xs text-muted-foreground">
        <Clock className="mr-1 inline size-3" aria-hidden />
        Supply is netted only when it arrives on or before the date the requirement needs it
        (docs/assumptions.md &sect;21). A quantity shown against two dates spans them: the first
        tranche lands on the earlier, the whole amount is in by the later.
      </p>
    </div>
  )
}

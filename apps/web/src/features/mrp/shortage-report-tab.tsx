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

import { AlertTriangle, ArrowLeftRight, Clock, PackageCheck } from 'lucide-react'
import { useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/overlays'
import { Card, CardContent } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ReallocationRequestDialog } from '@/features/inventory/reallocation-request-dialog'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatQuantity, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'

import type { MrpRequirementRow } from './mrp-types'
import type { ColumnDef } from '@tanstack/react-table'

/** A sales-order demand behind an MRP requirement, recovered from provenance. */
interface SalesDemand {
  id: string
  lineId: string
  code: string | null
  requiredDate: string | null
}

function provenanceString(entry: Record<string, unknown>, key: string): string | null {
  const value = entry[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

/**
 * The independent sales-order demand(s) this requirement was raised for.
 *
 * A component row reached through a BOM has no direct demand of its own - its
 * provenance names the parent item, not a document - so it offers no
 * reallocation target. A directly demanded row names the order and line, which
 * is exactly what a reallocation request needs. Manual demand has no document to
 * reallocate towards, so it is left out too.
 */
function salesDemands(row: MrpRequirementRow): SalesDemand[] {
  const seen = new Set<string>()
  const found: SalesDemand[] = []
  for (const entry of row.source_demands ?? []) {
    if (entry.source !== 'SALES_ORDER') continue
    const id = provenanceString(entry, 'source_id')
    const lineId = provenanceString(entry, 'source_line_id')
    // The API refuses a sales-order reallocation without its demand line.
    if (!id || !lineId || seen.has(id)) continue
    seen.add(id)
    found.push({
      id,
      lineId,
      code: provenanceString(entry, 'code'),
      requiredDate: provenanceString(entry, 'required_date'),
    })
  }
  return found
}

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
  // Decimal strings are compared as decimals; `Number()` is for the final
  // formatting step only (lib/format).
  if (!toDecimal(quantity).greaterThan(0)) return null

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

export function ShortageReportTab({
  runId,
  warehouseIds = [],
}: {
  runId: string
  /** The run's warehouse scope; a single warehouse pre-fills the request. */
  warehouseIds?: string[]
}) {
  const { can } = useAuth()
  const [reallocation, setReallocation] = useState<{
    row: MrpRequirementRow
    demand: SalesDemand
  } | null>(null)
  const canReallocate = can(P.ALLOCATION_REALLOCATE)

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
            {toDecimal(row.original.in_production_quantity).greaterThan(0) && (
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
          if (!toDecimal(r.late_supply_quantity).greaterThan(0)) {
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
      {
        // Offered only where the row is short and names a sales-order demand -
        // a reallocation gives stock to a real demand, and a BOM component row
        // has no document to give it to.
        id: 'reallocation',
        header: '',
        enableSorting: false,
        cell: ({ row }) => {
          const demands = salesDemands(row.original)
          if (
            !canReallocate ||
            demands.length === 0 ||
            !toDecimal(row.original.shortage_quantity).greaterThan(0)
          ) {
            return null
          }
          if (demands.length === 1) {
            const only = demands[0]
            if (!only) return null
            return (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setReallocation({ row: row.original, demand: only })}
              >
                <ArrowLeftRight />
                Request reallocation
              </Button>
            )
          }
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  <ArrowLeftRight />
                  Reallocate to...
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {demands.map((demand) => (
                  <DropdownMenuItem
                    key={`${demand.id}:${demand.lineId ?? ''}`}
                    onSelect={() => setReallocation({ row: row.original, demand })}
                  >
                    {demand.code ?? demand.id}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )
        },
      },
    ],
    [canReallocate],
  )

  const lateRows = requirements.data.filter((r) =>
    toDecimal(r.late_supply_quantity).greaterThan(0),
  ).length

  return (
    <div className="space-y-4">
      {lateRows > 0 && (
        <Card>
          <CardContent className="flex items-start gap-3 py-4">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
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

      {reallocation && (
        <ReallocationRequestDialog
          demand={{
            itemId: reallocation.row.item_id,
            itemCode: reallocation.row.item_code,
            // A run scoped to one warehouse names the position; a run scoped to
            // all of them leaves the choice in the dialog rather than guessing.
            warehouseId: warehouseIds.length === 1 ? warehouseIds[0] : undefined,
            demandType: 'SALES_ORDER',
            demandId: reallocation.demand.id,
            demandLineId: reallocation.demand.lineId,
            demandCode: reallocation.demand.code,
            requiredDate:
              reallocation.demand.requiredDate ?? reallocation.row.earliest_required_date,
            shortfallQuantity: reallocation.row.shortage_quantity,
          }}
          onClose={() => setReallocation(null)}
        />
      )}
    </div>
  )
}

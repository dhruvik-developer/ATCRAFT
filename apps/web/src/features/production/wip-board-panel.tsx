/**
 * Work in progress, by work centre - the load board.
 *
 * `GET /production/wip/work-centres` - `work_order.view`, which is *not* the
 * `production_order.view` its sibling `GET /production/wip` is gated on. The
 * panel checks the right it actually needs, because it is written to be mounted
 * anywhere a shop-floor board is wanted, including on screens whose own route
 * guard asks for something else.
 *
 * What the endpoint answers is one row per cell that currently holds open
 * operations of open jobs: how many are queued, how many are running, and the
 * chargeable minutes still to run at the planned rate - setup counted only
 * where the operation has not started, run time pro-rated by what is left to
 * make. Rows arrive busiest first, because the board is read to find the
 * bottleneck; the API has no `sort_by` on this endpoint and no work centre with
 * nothing on it, so a cell that has gone quiet disappears from the board rather
 * than showing zeroes.
 *
 * Three consequences worth knowing before reading a number here:
 *
 * 1. **A row carries no name.** `WipWorkCentreRow` is an aggregate keyed by
 *    `work_center_id` alone, so the label comes from the bounded work-centre
 *    lookup and falls back to saying it is unavailable rather than printing a
 *    uuid at a machine operator.
 * 2. **There is no total.** The repository deliberately does not count distinct
 *    centres, so paging is by has-next rather than by a page count.
 * 3. **`late_only` does nothing here.** `WipFilters` carries it and
 *    `load_by_work_centre` never applies it, so this panel does not offer it -
 *    the by-order board is where lateness is answered.
 *
 * Paging state is local rather than in the URL: the panel is designed to sit
 * beside another list, and two lists sharing the URL's `page` would move each
 * other. Everything is still decided by the server - one page of rows at a
 * time, never a whole table filtered in the browser.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Factory } from 'lucide-react'
import { useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { EmptyState } from '@/components/shared'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { formatNumber, formatQuantity } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import type { PaginatedEnvelope } from '@/types/api'

import {
  formatMinutes,
  indexById,
  loadShare,
  optionLabel,
  peakRemainingMinutes,
  useWorkCentreOptions,
  workOrderKeys,
  type WipWorkCentreRow,
} from './work-order-model'

const DEFAULT_LIMIT = 25

export function WipBoardPanel({
  warehouseId,
  plantId,
  itemId,
  workCentreId,
  onSelectWorkCentre,
  className,
}: {
  /** Narrow to jobs booking their output into one warehouse. */
  warehouseId?: string | null
  plantId?: string | null
  itemId?: string | null
  workCentreId?: string | null
  /** When given, each cell becomes a control that scopes the caller's queue. */
  onSelectWorkCentre?: (workCentreId: string) => void
  className?: string
}) {
  const { can } = useAuth()
  const canView = can(P.WORK_ORDER_VIEW)

  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(DEFAULT_LIMIT)

  const params = useMemo(
    () => ({
      page,
      limit,
      // The endpoint never counts distinct centres - `total` comes back null by
      // design - so asking for one would only cost a scan.
      with_total: false,
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(plantId ? { plant_id: plantId } : {}),
      ...(itemId ? { item_id: itemId } : {}),
      ...(workCentreId ? { work_center_id: workCentreId } : {}),
    }),
    [page, limit, warehouseId, plantId, itemId, workCentreId],
  )

  const board = useQuery({
    queryKey: workOrderKeys.wipWorkCentres(params),
    queryFn: (): Promise<PaginatedEnvelope<WipWorkCentreRow>> =>
      api.getPage<WipWorkCentreRow>('/production/wip/work-centres', { params }),
    placeholderData: keepPreviousData,
    staleTime: staleTimes.transactional,
    // A board left open on a wall display must not still be showing the queue
    // as it stood an hour ago when someone comes back to the tab.
    refetchOnWindowFocus: true,
    enabled: canView,
  })

  // Memoised so the empty-list fallback is not a new array on every render,
  // which would rebuild the load bars' scale each time the panel re-rendered.
  const rows = useMemo(() => board.data?.data ?? [], [board.data])

  const workCentres = useWorkCentreOptions(canView)
  const workCentreById = useMemo(() => indexById(workCentres.data ?? []), [workCentres.data])
  const peak = useMemo(() => peakRemainingMinutes(rows), [rows])

  const columns = useMemo<ColumnDef<WipWorkCentreRow, unknown>[]>(
    () => [
      {
        id: 'work_centre',
        header: 'Work centre',
        // Nothing on this endpoint sorts: the server returns the busiest cell
        // first and a header that appeared clickable would send a parameter it
        // does not accept.
        enableSorting: false,
        cell: ({ row }) => {
          const centre = workCentreById.get(row.original.work_center_id)
          const label = centre ? optionLabel(centre) : 'Work centre (name unavailable)'
          if (!onSelectWorkCentre) return <span className="font-medium">{label}</span>
          return (
            <button
              type="button"
              onClick={() => onSelectWorkCentre(row.original.work_center_id)}
              aria-label={`Show the operations queued at ${label}`}
              className="rounded-sm text-left font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {label}
            </button>
          )
        },
      },
      {
        id: 'pending_work_orders',
        header: 'Queued',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.pending_work_orders > 0 ? (
            <Badge variant="warning">{formatNumber(row.original.pending_work_orders)}</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'in_progress_work_orders',
        header: 'Running',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.in_progress_work_orders > 0 ? (
            <Badge>{formatNumber(row.original.in_progress_work_orders)}</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'planned_quantity',
        header: 'Planned',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.planned_quantity),
      },
      {
        id: 'completed_quantity',
        header: 'Booked',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.completed_quantity),
      },
      {
        id: 'balance_quantity',
        header: 'Balance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.balance_quantity)}</span>
        ),
      },
      {
        id: 'remaining_minutes',
        header: 'Time to run',
        enableSorting: false,
        meta: { align: 'right' },
        // The bar is a comparison against the busiest cell on this page, which
        // is why it is decoration: the figure beside it is the claim.
        cell: ({ row }) => (
          <div className="flex flex-col items-end gap-1">
            <span className="font-medium">{formatMinutes(row.original.remaining_minutes)}</span>
            <span className="h-1 w-24 overflow-hidden rounded-full bg-muted" aria-hidden>
              <span
                className="block h-full bg-primary"
                style={{ width: `${loadShare(row.original, peak)}%` }}
              />
            </span>
          </div>
        ),
      },
    ],
    [workCentreById, onSelectWorkCentre, peak],
  )

  if (!canView) {
    return (
      <EmptyState
        icon={Factory}
        title="You cannot see the shop-floor board"
        description="Viewing work orders requires the work order view permission. Ask an administrator to grant it."
      />
    )
  }

  return (
    <div className={className}>
      <div className="mb-3">
        <h2 className="text-sm font-semibold">Load by work centre</h2>
        <p className="text-xs text-muted-foreground">
          Open operations of open jobs, busiest cell first. Time to run counts setup only where the
          operation has not started yet.
        </p>
      </div>

      <DataTable
        columns={columns}
        data={rows}
        pagination={board.data?.pagination}
        isLoading={board.isLoading}
        isFetching={board.isFetching}
        error={board.error}
        onPageChange={setPage}
        onLimitChange={(next) => {
          setLimit(next)
          // Page 4 of 25-row pages does not exist once the page holds 200.
          setPage(1)
        }}
        onRefresh={() => void board.refetch()}
        getRowId={(row) => row.work_center_id}
        emptyTitle="Nothing on the shop floor"
        emptyDescription="No operation is queued or running on any work centre. Releasing a production order creates its operations and puts them on this board."
      />
    </div>
  )
}

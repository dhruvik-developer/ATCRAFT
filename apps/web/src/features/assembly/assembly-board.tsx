/**
 * The assembly board: open work per line, planned against actual.
 *
 * Two reads, both aggregated or ordered server-side: `GET
 * /assembly/board/lines` counts the open orders on each line in one grouped
 * statement (a board drawn from one page of orders would undercount a busy
 * line the moment its orders spill onto page two), and `GET
 * /assembly/board/orders` lists every open order in schedule order. The
 * grouping by line below is presentation only - the counts never come from it.
 *
 * `is_late` means the planned end has passed while the order still owes
 * finished stock; `start_overdue` means the planned start has passed and the
 * bench has not begun. Both are worked out on the server at read time.
 */

import { CalendarClock, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'

import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge, Skeleton } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatQuantity } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import type { OffsetPagination } from '@/types/api'

import { AssemblyDetailDialog } from './assembly-detail-dialog'
import { assemblyKeys, type AssemblyBoardOrder, type AssemblyLineLoad } from './assembly-model'

function LatenessBadge({ order }: { order: AssemblyBoardOrder }) {
  if (order.is_late) {
    return (
      <Badge variant="destructive" className="gap-1">
        <TriangleAlert className="h-3 w-3" aria-hidden />
        {order.days_late === 0 ? 'Due today' : `${order.days_late}d late`}
      </Badge>
    )
  }
  if (order.start_overdue) {
    return (
      <Badge variant="warning" className="gap-1">
        <CalendarClock className="h-3 w-3" aria-hidden />
        Start overdue
      </Badge>
    )
  }
  return <span className="text-muted-foreground">On plan</span>
}

function LineCard({ line }: { line: AssemblyLineLoad }) {
  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-semibold">{line.assembly_line ?? 'No line assigned'}</p>
        <span className="text-sm text-muted-foreground">
          {line.open_orders} open · {formatQuantity(line.planned_quantity)} planned
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>{line.not_started_orders} not started</span>
        <span>{line.in_progress_orders} on the bench</span>
        <span>{line.qc_pending_orders} awaiting inspection</span>
        {line.late_orders > 0 && (
          <span className="font-medium text-destructive">{line.late_orders} late</span>
        )}
      </div>
    </div>
  )
}

function BoardPagination({
  label,
  pagination,
  isFetching,
  onPageChange,
}: {
  label: string
  pagination?: OffsetPagination
  isFetching: boolean
  onPageChange: (page: number) => void
}) {
  if (!pagination || (!pagination.has_previous && !pagination.has_next)) return null
  return (
    <nav aria-label={`${label} pagination`} className="mt-3 flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        disabled={isFetching || !pagination.has_previous}
        onClick={() => onPageChange(pagination.page - 1)}
      >
        Previous
      </Button>
      <span className="text-sm text-muted-foreground" role="status">
        Page {pagination.page}
        {pagination.total_pages ? ` of ${pagination.total_pages}` : ''}
      </span>
      <Button
        variant="outline"
        size="sm"
        disabled={isFetching || !pagination.has_next}
        onClick={() => onPageChange(pagination.page + 1)}
      >
        Next
      </Button>
    </nav>
  )
}

export function AssemblyBoard({ onOpenOrder }: { onOpenOrder: (id: string) => void }) {
  // The board endpoints fix their own order (lines by name, orders by
  // schedule), so no sort is offered: a sortable heading would claim the
  // server honours it.
  const lines = useListQuery<AssemblyLineLoad>({
    path: '/assembly/board/lines',
    queryKey: assemblyKeys.boardLines,
    namespace: 'board_lines',
    staleTime: staleTimes.transactional,
  })
  const orders = useListQuery<AssemblyBoardOrder>({
    path: '/assembly/board/orders',
    queryKey: assemblyKeys.boardOrders,
    namespace: 'board_orders',
    staleTime: staleTimes.transactional,
    filters: { late_only: true },
  })

  // Presentation grouping only: the per-line counts above come from the
  // grouped statement, never from this page of rows.
  const groups = useMemo(() => {
    const byLine = new Map<string | null, AssemblyBoardOrder[]>()
    for (const order of orders.data) {
      const list = byLine.get(order.assembly_line) ?? []
      list.push(order)
      byLine.set(order.assembly_line, list)
    }
    return [...byLine.entries()].sort(([a], [b]) => (a ?? 'zzzz').localeCompare(b ?? 'zzzz'))
  }, [orders.data])

  return (
    <div className="space-y-6">
      <section aria-label="Load per assembly line">
        <h2 className="mb-2 text-sm font-semibold">Load per line</h2>
        {lines.isLoading ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} className="h-20 w-full" />
            ))}
          </div>
        ) : lines.error ? (
          <p className="text-sm text-destructive">{lines.error.message}</p>
        ) : lines.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No open assembly orders. Raise one to start planning the benches.
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {lines.data.map((line) => (
              <LineCard key={line.assembly_line ?? 'unassigned'} line={line} />
            ))}
          </div>
        )}
        <BoardPagination
          label="Assembly lines"
          pagination={lines.pagination}
          isFetching={lines.isFetching}
          onPageChange={lines.setPage}
        />
      </section>

      <section aria-label="Late open orders">
        <h2 className="mb-2 text-sm font-semibold">Late orders</h2>
        {orders.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : orders.error ? (
          <p className="text-sm text-destructive">{orders.error.message}</p>
        ) : orders.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing is past its planned end. The full open list, line by line, is on the orders tab.
          </p>
        ) : (
          <div className="space-y-4">
            {groups.map(([line, rows]) => (
              <div key={line ?? 'unassigned'}>
                <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {line ?? 'No line assigned'} · {rows.length}
                </p>
                <ul className="divide-y rounded-lg border">
                  {rows.map((order) => (
                    <li key={order.id}>
                      <button
                        type="button"
                        onClick={() => onOpenOrder(order.id)}
                        className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2.5 text-left text-sm hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <span className="font-medium">{order.code}</span>
                        <StatusBadge status={order.status} />
                        <LatenessBadge order={order} />
                        <span className="text-muted-foreground">
                          {formatQuantity(order.assembled_quantity)} of{' '}
                          {formatQuantity(order.planned_quantity)}
                        </span>
                        <span className="ml-auto text-xs text-muted-foreground">
                          Planned {formatDate(order.planned_start)} →{' '}
                          {formatDate(order.planned_end)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
        <BoardPagination
          label="Late orders"
          pagination={orders.pagination}
          isFetching={orders.isFetching}
          onPageChange={orders.setPage}
        />
      </section>
    </div>
  )
}

// ------------------------------------------------- board tab on the page ----

/**
 * The board as a tab of the assembly page, with its own detail panel.
 *
 * Split from the register so the register's filters stay the register's: the
 * board answers "what is late and where", not "find order ASM-0042".
 */
export function AssemblyBoardTab() {
  const { can } = useAuth()
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)
  const canManage = can(P.ASSEMBLY_ORDER_MANAGE)
  const canComplete = can(P.ASSEMBLY_COMPLETE)

  return (
    <>
      <PageHeader
        title="Assembly board"
        description="Open orders grouped by assembly line, planned against actual, with a lateness badge where the plan has slipped."
      />
      <AssemblyBoard onOpenOrder={setOpenOrderId} />
      {openOrderId && (
        <AssemblyDetailDialog
          open
          onOpenChange={(open) => !open && setOpenOrderId(null)}
          orderId={openOrderId}
          canManage={canManage}
          canComplete={canComplete}
        />
      )}
    </>
  )
}

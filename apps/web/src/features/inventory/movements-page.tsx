/**
 * Stock movements - transfers, adjustments and physical counts.
 *
 * Three documents that all end in the same place: a signed row in the inventory
 * ledger. They share a screen because they are one job - "stock moved and here
 * is why" - and because an operator comparing a count variance against the
 * adjustment it produced should not have to navigate between screens.
 *
 * Every state transition here moves real stock, so each one is confirmed before
 * it is sent and each is gated on the permission that authorises it. The gate is
 * UX: the API authorises the request again regardless.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeftRight,
  BookCheck,
  ClipboardCheck,
  ClipboardList,
  PackageCheck,
  Play,
  Plus,
  Scale,
  Send,
  ThumbsUp,
} from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/overlays'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import {
  formatDate,
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  ConfirmPhysicalCountDialog,
  CreateAdjustmentDialog,
  CreatePhysicalCountDialog,
  CreateTransferDialog,
} from './movement-dialogs'
import {
  acceptsCountConfirmation,
  type PhysicalCountRow,
  type StockAdjustmentRow,
  type StockTransferRow,
  type TransferStatus,
} from './types'

// The shared registry has no key for these three lists yet; nesting them under
// `inventory` means an `queryKeys.inventory.all` invalidation still sweeps them.
const transfersKey = (params: unknown) => ['inventory', 'transfers', params] as const
const adjustmentsKey = (params: unknown) => ['inventory', 'adjustments', params] as const
const physicalCountsKey = (params: unknown) => ['inventory', 'physical-counts', params] as const

/**
 * The transitions each document actually has, taken from the state machines in
 * `inventory/service.py`. These are not guesses: a button offered on a status
 * the machine has no edge out of is a button that always comes back an invalid
 * transition, and the operator has no way to tell that from a real failure.
 *
 * TRANSFER_FLOW  DRAFT -> APPROVED -> IN_TRANSIT -> RECEIVED
 * ADJUSTMENT_FLOW DRAFT -> POSTED, or DRAFT -> PENDING_APPROVAL -> APPROVED -> POSTED
 * COUNT_FLOW     DRAFT -> IN_PROGRESS -> COUNTED -> CONFIRMED
 */
const TRANSFER_APPROVABLE: TransferStatus = 'DRAFT'
const TRANSFER_DISPATCHABLE: TransferStatus = 'APPROVED'
const TRANSFER_RECEIVABLE: TransferStatus = 'IN_TRANSIT'

interface PendingAction {
  path: string
  title: string
  description: string
  confirmLabel: string
  successTitle: string
  successBody?: string
  destructive?: boolean
}

/** A transition offered on a row: the button, plus the confirmation behind it. */
interface RowAction extends PendingAction {
  label: string
  icon: ReactNode
}

/**
 * The one transition an adjustment can make next, or nothing.
 *
 * A draft below the approval threshold posts straight to the ledger; above it
 * the same edge is refused and the document has to go round the approval loop.
 * `requires_approval` is decided by the service when the document is raised and
 * carried on the row, so the screen never has to guess which of the two DRAFT
 * edges is the live one.
 */
function nextAdjustmentAction(
  adjustment: StockAdjustmentRow,
  { canAdjust, canApprove }: { canAdjust: boolean; canApprove: boolean },
): RowAction | null {
  const at = adjustment.warehouse_code ?? 'this warehouse'
  const value = formatMoney(adjustment.total_value)

  if (adjustment.status === 'DRAFT' && adjustment.requires_approval && canAdjust) {
    return {
      path: '/inventory/adjustments/' + adjustment.id + '/submit',
      label: 'Submit',
      icon: <Send />,
      title: 'Submit ' + adjustment.code + ' for approval?',
      description:
        'This adjustment is worth ' +
        value +
        ', which is over the threshold that can be posted without a second pair of eyes. ' +
        'Nothing moves until an approver signs it.',
      confirmLabel: 'Submit for approval',
      successTitle: 'Adjustment submitted',
      successBody: adjustment.code + ' is waiting for an approver.',
    }
  }

  if (adjustment.status === 'PENDING_APPROVAL' && canApprove) {
    return {
      path: '/inventory/adjustments/' + adjustment.id + '/approve',
      label: 'Approve',
      icon: <ThumbsUp />,
      title: 'Approve ' + adjustment.code + '?',
      description:
        'Approving clears ' +
        adjustment.code +
        ' to be posted at ' +
        at +
        '. Stock does not move until it is posted.',
      confirmLabel: 'Approve adjustment',
      successTitle: 'Adjustment approved',
      successBody: adjustment.code + ' is ready to post.',
    }
  }

  if ((adjustment.status === 'DRAFT' || adjustment.status === 'APPROVED') && canAdjust) {
    return {
      path: '/inventory/adjustments/' + adjustment.id + '/post',
      label: 'Post',
      icon: <BookCheck />,
      title: 'Post ' + adjustment.code + ' to the ledger?',
      description:
        'Stock at ' +
        at +
        ' changes now, by ' +
        value +
        ' of value across ' +
        formatNumber(adjustment.line_count) +
        ' line(s). The ledger entry cannot be deleted afterwards.',
      confirmLabel: 'Post adjustment',
      successTitle: 'Adjustment posted',
      successBody: adjustment.code + ' is on the ledger.',
      destructive: true,
    }
  }

  return null
}

/**
 * One confirmation and one mutation for every state transition on this screen.
 *
 * Transitions are POSTs with no body, and they all invalidate the same things -
 * the document list they came from and the balances they moved - so a single
 * component is clearer than three near-identical copies.
 */
function ActionConfirm({ pending, onDone }: { pending: PendingAction | null; onDone: () => void }) {
  const queryClient = useQueryClient()

  const run = useMutation({
    mutationFn: (action: PendingAction) => api.post(action.path, {}),
    onSuccess: (_result, action) => {
      toast.success(action.successTitle, action.successBody)
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      // Stock arriving or leaving changes what can be promised, so reservations
      // and availability are stale too.
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      onDone()
    },
  })

  if (!pending) return null

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) onDone()
      }}
      title={pending.title}
      description={pending.description}
      confirmLabel={pending.confirmLabel}
      destructive={pending.destructive ?? false}
      loading={run.isPending}
      onConfirm={() => run.mutate(pending)}
    />
  )
}

// ----------------------------------------------------------- transfers -----

function TransfersTab() {
  const { can } = useAuth()
  const canTransfer = can(P.INVENTORY_TRANSFER)

  const [createOpen, setCreateOpen] = useState(false)
  const [pending, setPending] = useState<PendingAction | null>(null)

  const list = useListQuery<StockTransferRow>({
    path: '/inventory/transfers',
    queryKey: transfersKey,
    defaultSortBy: 'transfer_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })

  const columns = useMemo<ColumnDef<StockTransferRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Transfer',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.code}</span>,
      },
      {
        id: 'from_warehouse_code',
        header: 'From',
        enableSorting: false,
        cell: ({ row }) => row.original.from_warehouse_code ?? '-',
      },
      {
        id: 'to_warehouse_code',
        header: 'To',
        enableSorting: false,
        cell: ({ row }) => row.original.to_warehouse_code ?? '-',
      },
      {
        id: 'line_count',
        header: 'Lines',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.line_count),
      },
      {
        id: 'total_quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.total_quantity),
      },
      {
        accessorKey: 'transfer_date',
        header: 'Transfer date',
        cell: ({ row }) => formatDate(row.original.transfer_date),
      },
      {
        id: 'expected_date',
        header: 'Expected',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.expected_date),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          if (!canTransfer) return null
          const transfer = row.original

          if (transfer.status === TRANSFER_APPROVABLE) {
            return (
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPending({
                    path: '/inventory/transfers/' + transfer.id + '/approve',
                    title: 'Approve ' + transfer.code + '?',
                    description:
                      'The transfer becomes ready to dispatch from ' +
                      (transfer.from_warehouse_code ?? 'the source warehouse') +
                      '. Stock does not move yet.',
                    confirmLabel: 'Approve transfer',
                    successTitle: 'Transfer approved',
                    successBody: transfer.code + ' can now be dispatched.',
                  })
                }
              >
                <ThumbsUp />
                Approve
              </Button>
            )
          }

          if (transfer.status === TRANSFER_DISPATCHABLE) {
            return (
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPending({
                    path: '/inventory/transfers/' + transfer.id + '/dispatch',
                    title: 'Dispatch ' + transfer.code + '?',
                    description:
                      formatQuantity(transfer.total_quantity) +
                      ' leaves ' +
                      (transfer.from_warehouse_code ?? 'the source warehouse') +
                      ' now and counts as in transit until it is received.',
                    confirmLabel: 'Dispatch transfer',
                    successTitle: 'Transfer dispatched',
                    successBody:
                      'Stock is in transit to ' +
                      (transfer.to_warehouse_code ?? 'the destination warehouse') +
                      '.',
                  })
                }
              >
                <Send />
                Dispatch
              </Button>
            )
          }

          if (transfer.status === TRANSFER_RECEIVABLE) {
            return (
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPending({
                    path: '/inventory/transfers/' + transfer.id + '/receive',
                    title: 'Receive ' + transfer.code + '?',
                    description:
                      'Stock is added to ' +
                      (transfer.to_warehouse_code ?? 'the destination warehouse') +
                      ' and becomes available to promise.',
                    confirmLabel: 'Receive transfer',
                    successTitle: 'Transfer received',
                    successBody:
                      'Stock is on hand at ' +
                      (transfer.to_warehouse_code ?? 'the destination warehouse') +
                      '.',
                  })
                }
              >
                <PackageCheck />
                Receive
              </Button>
            )
          }

          return null
        },
      },
    ],
    [canTransfer],
  )

  return (
    <>
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
        searchPlaceholder="Search transfer code"
        getRowId={(row) => row.id}
        emptyTitle="No transfers yet"
        emptyDescription="Move stock between warehouses and the paperwork lands here."
        toolbar={
          canTransfer && (
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus />
              New transfer
            </Button>
          )
        }
      />

      {canTransfer && <CreateTransferDialog open={createOpen} onOpenChange={setCreateOpen} />}
      <ActionConfirm pending={pending} onDone={() => setPending(null)} />
    </>
  )
}

// --------------------------------------------------------- adjustments -----

function AdjustmentsTab() {
  const { can } = useAuth()
  const canAdjust = can(P.INVENTORY_ADJUST)
  const canApprove = can(P.INVENTORY_ADJUST_APPROVE)

  const [createOpen, setCreateOpen] = useState(false)
  const [pending, setPending] = useState<PendingAction | null>(null)

  const list = useListQuery<StockAdjustmentRow>({
    path: '/inventory/adjustments',
    queryKey: adjustmentsKey,
    defaultSortBy: 'adjustment_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })

  const columns = useMemo<ColumnDef<StockAdjustmentRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Adjustment',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.code}</span>,
      },
      {
        id: 'warehouse_code',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => row.original.warehouse_code ?? '-',
      },
      {
        accessorKey: 'adjustment_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.adjustment_date),
      },
      {
        id: 'reason_code',
        header: 'Reason code',
        enableSorting: false,
        cell: ({ row }) => humanise(row.original.reason_code),
      },
      {
        id: 'reason',
        header: 'Reason',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[24rem] truncate" title={row.original.reason}>
            {row.original.reason}
          </span>
        ),
      },
      {
        id: 'line_count',
        header: 'Lines',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.line_count),
      },
      {
        // Money, not quantity. An adjustment can carry lines in different units,
        // so there is no net quantity that means anything across the document -
        // `total_value` is the only figure that adds up, and it is what the
        // approval threshold is measured against.
        //
        // It is a *magnitude*, not a signed net: every line is costed as
        // `abs(quantity) * unit_cost` and `stock_adjustments` carries a
        // `total_value >= 0` CHECK. Rendering it with a sign and a red/green
        // tint made a write-off and a write-on read identically - both came out
        // green with a leading `+` - which is worse than showing neither, so
        // this column states the size of the movement and leaves the direction
        // to the reason code and the lines.
        accessorKey: 'total_value',
        header: 'Value',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatMoney(row.original.total_value)}</span>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const adjustment = row.original
          const action = nextAdjustmentAction(adjustment, { canAdjust, canApprove })
          if (!action) return null
          return (
            <Button variant="outline" size="sm" onClick={() => setPending(action)}>
              {action.icon}
              {action.label}
            </Button>
          )
        },
      },
    ],
    [canAdjust, canApprove],
  )

  return (
    <>
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
        searchPlaceholder="Search adjustment code or reason"
        getRowId={(row) => row.id}
        emptyTitle="No adjustments raised"
        emptyDescription="Write-offs, shrinkage and count corrections are recorded here."
        toolbar={
          canAdjust && (
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus />
              New adjustment
            </Button>
          )
        }
      />

      {canAdjust && <CreateAdjustmentDialog open={createOpen} onOpenChange={setCreateOpen} />}
      <ActionConfirm pending={pending} onDone={() => setPending(null)} />
    </>
  )
}

// ------------------------------------------------------ physical counts ----

function PhysicalCountsTab() {
  const { can } = useAuth()
  const canCount = can(P.INVENTORY_PHYSICAL_COUNT)
  // Confirming is a different right from counting, and deliberately so: the
  // count endpoints (open, start, key entries, recount) enforce
  // `inventory.physical_count`, while `/confirm` - the step that posts the
  // variance to the ledger - enforces `inventory.reconcile`. Whoever walks the
  // aisles is not automatically the person who accepts what they wrote down.
  const canReconcile = can(P.INVENTORY_RECONCILE)

  const [createOpen, setCreateOpen] = useState(false)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [confirmTarget, setConfirmTarget] = useState<PhysicalCountRow | null>(null)

  const list = useListQuery<PhysicalCountRow>({
    path: '/inventory/physical-counts',
    queryKey: physicalCountsKey,
    defaultSortBy: 'count_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })

  const columns = useMemo<ColumnDef<PhysicalCountRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Count',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.code}</span>,
      },
      {
        id: 'warehouse_code',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => row.original.warehouse_code ?? '-',
      },
      {
        accessorKey: 'count_date',
        header: 'Count date',
        cell: ({ row }) => formatDate(row.original.count_date),
      },
      {
        id: 'line_count',
        header: 'Lines',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.line_count),
      },
      {
        id: 'variance_line_count',
        header: 'Variances',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className={cn(row.original.variance_line_count > 0 && 'text-warning')}>
            {formatNumber(row.original.variance_line_count)}
          </span>
        ),
      },
      {
        // Money, like the adjustment this sheet posts. A count covers lines in
        // different units, so the variance only totals as a value.
        id: 'variance_value',
        header: 'Variance value',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const variance = toDecimal(row.original.variance_value)
          return (
            <span
              className={cn(
                'font-medium',
                variance.greaterThan(0) && 'text-success',
                variance.lessThan(0) && 'text-destructive',
              )}
            >
              {variance.greaterThan(0) ? '+' : ''}
              {formatMoney(row.original.variance_value)}
            </span>
          )
        },
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const count = row.original

          // COUNT_FLOW: DRAFT -> IN_PROGRESS -> COUNTED -> CONFIRMED. Confirm was
          // previously offered on every unsettled status, so it appeared on
          // sheets nobody had counted yet and came back an invalid transition.
          //
          // The two halves are gated separately - a reconciler with no
          // `inventory.physical_count` still has to see Confirm, so this cannot
          // return early on `canCount` the way it used to.
          if (acceptsCountConfirmation(count)) {
            if (!canReconcile) return null
            return (
              <Button variant="outline" size="sm" onClick={() => setConfirmTarget(count)}>
                <ClipboardCheck />
                Confirm
              </Button>
            )
          }

          if (!canCount) return null

          if (count.status === 'DRAFT') {
            return (
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPending({
                    path: '/inventory/physical-counts/' + count.id + '/start',
                    title: 'Start counting ' + count.code + '?',
                    description:
                      'The sheet snapshots what the system believes is at ' +
                      (count.warehouse_code ?? 'this warehouse') +
                      ' right now. Counted figures are keyed against that snapshot.',
                    confirmLabel: 'Start counting',
                    successTitle: 'Count started',
                    successBody: count.code + ' is open for counting.',
                  })
                }
              >
                <Play />
                Start
              </Button>
            )
          }

          // IN_PROGRESS offers nothing here: the sheet moves to COUNTED by
          // keying entries, which this screen does not do, and CONFIRMED and
          // CANCELLED are terminal.
          return null
        },
      },
    ],
    [canCount, canReconcile],
  )

  return (
    <>
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
        searchPlaceholder="Search count code"
        getRowId={(row) => row.id}
        emptyTitle="No counts opened"
        emptyDescription="Open a count sheet to reconcile the shelves against the system."
        toolbar={
          canCount && (
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus />
              New count
            </Button>
          )
        }
      />

      {canCount && <CreatePhysicalCountDialog open={createOpen} onOpenChange={setCreateOpen} />}
      <ActionConfirm pending={pending} onDone={() => setPending(null)} />

      {/* Not the shared ActionConfirm: this one reads the sheet's lines first,
          because the variance has to be on screen before an irreversible
          posting is agreed to. */}
      {confirmTarget && (
        <ConfirmPhysicalCountDialog
          count={confirmTarget}
          onClose={() => setConfirmTarget(null)}
        />
      )}
    </>
  )
}

// --------------------------------------------------------------- page ------

const TABS = [
  { value: 'transfers', label: 'Transfers', icon: ArrowLeftRight },
  { value: 'adjustments', label: 'Adjustments', icon: Scale },
  { value: 'counts', label: 'Physical counts', icon: ClipboardList },
] as const

export default function MovementsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') ?? 'transfers'

  const changeTab = (next: string) => {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        params.set('tab', next)
        // Page 4 of transfers is meaningless once the table below is
        // adjustments, and so is a sort column the next list does not have.
        for (const key of ['page', 'q', 'sort', 'dir']) params.delete(key)
        return params
      },
      { replace: true },
    )
  }

  return (
    <div>
      <PageHeader
        title="Stock movements"
        description="Transfers between warehouses, adjustments to what the system believes, and the counts that reconcile the two."
      />

      <Tabs value={tab} onValueChange={changeTab}>
        <TabsList>
          {TABS.map(({ value, label, icon: Icon }) => (
            <TabsTrigger key={value} value={value}>
              <Icon className="h-4 w-4" aria-hidden />
              {label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="transfers">
          <TransfersTab />
        </TabsContent>
        <TabsContent value="adjustments">
          <AdjustmentsTab />
        </TabsContent>
        <TabsContent value="counts">
          <PhysicalCountsTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

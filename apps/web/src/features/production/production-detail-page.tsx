/**
 * Production order detail.
 *
 * The header offers only the transitions the state machine actually allows from
 * the current status (docs/database.md section 7). A released order cannot be
 * approved again and a completed one cannot be cancelled, so those buttons are
 * absent rather than present and disabled - a disabled button that never
 * explains itself is the slowest way to learn a workflow.
 *
 * The four tabs are the four questions asked about an order in progress: what
 * does it need, who is making it, what came off the machine, and what did it
 * cost. Only the visible tab is mounted, so exactly one sub-query is in flight.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  Ban,
  Check,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Coins,
  Factory,
  Lock,
  PackageMinus,
  PackagePlus,
  PlayCircle,
  Plus,
  RefreshCw,
  Wrench,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  PageHeader,
  StatTile,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { CostCentreName } from '@/features/admin/cost-centre-select'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { ApiError, api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  CloseProductionDialog,
  CompleteProductionDialog,
  IssueMaterialDialog,
  RecordProductionDialog,
  ReturnMaterialDialog,
} from './production-dialogs'
import {
  PRODUCTION_STEPS,
  acceptsMaterialIssue,
  acceptsMaterialReturn,
  acceptsProductionEntry,
  actualMinutes,
  componentShortage,
  isApprovable,
  isCancellable,
  isClosable,
  isCompletable,
  isReleasable,
  isStartable,
  outstandingQuantity,
  plannedMinutes,
  priorityBand,
  progressPercent,
  returnableQuantity,
  type ProductionComponent,
  type ProductionCost,
  type ProductionCostMaterialLine,
  type ProductionCostOperationLine,
  type ProductionEntryRow,
  type ProductionOrderDetail,
  type ComponentShortage,
  type ProductionOrderRow,
  type ProductionStatus,
  type WorkOrderRow,
} from './production-model'

// ------------------------------------------------------------- stepper ------

function StatusStepper({ status }: { status: ProductionStatus }) {
  if (status === 'CANCELLED') {
    return (
      <Alert variant="destructive">
        <AlertTitle>This order was cancelled</AlertTitle>
        <AlertDescription>
          Nothing further can be issued or booked against it. A job still holding material cannot be
          cancelled at all, so whatever had been issued was returned to the store first.
        </AlertDescription>
      </Alert>
    )
  }

  const currentIndex = PRODUCTION_STEPS.indexOf(status)

  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2" aria-label="Order progress">
      {PRODUCTION_STEPS.map((step, index) => {
        const done = index < currentIndex
        const current = index === currentIndex
        return (
          <li key={step} className="flex items-center gap-1">
            <span
              aria-current={current ? 'step' : undefined}
              className={cn(
                'flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs',
                done && 'border-success/40 bg-success/10 text-success',
                current && 'border-primary bg-primary/10 font-medium text-primary',
                !done && !current && 'border-border text-muted-foreground',
              )}
            >
              {done ? (
                <Check className="h-3 w-3" aria-hidden />
              ) : (
                <span className="tabular text-2xs">{index + 1}</span>
              )}
              {humanise(step)}
            </span>
            {index < PRODUCTION_STEPS.length - 1 && (
              <ChevronRight className="h-3 w-3 text-muted-foreground/50" aria-hidden />
            )}
          </li>
        )
      })}
    </ol>
  )
}

// ---------------------------------------------------------- components ------

function ComponentsPanel({
  components,
  isLoading,
  isFetching,
  error,
  onRefresh,
  onIssue,
  canIssue,
}: {
  components: ProductionComponent[]
  isLoading: boolean
  isFetching: boolean
  error: Error | null
  onRefresh: () => void
  onIssue: () => void
  canIssue: boolean
}) {
  const columns = useMemo<ColumnDef<ProductionComponent, unknown>[]>(
    () => [
      {
        id: 'item',
        header: 'Component',
        enableSorting: false,
        // The kit list snapshots the part number at release, so `item_code` is
        // the one denormalised name in this module and is safe to show.
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <Link
              to={`/items/${row.original.item_id}`}
              className="block truncate font-medium hover:underline"
            >
              {row.original.item_code}
            </Link>
            <span className="block text-2xs text-muted-foreground">
              line {row.original.line_number} - BOM level {row.original.bom_level}
              {row.original.is_backflushed && ' - backflushed'}
            </span>
          </div>
        ),
      },
      {
        id: 'required',
        header: 'Required',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.required_quantity),
      },
      {
        id: 'issued',
        header: 'Issued (net)',
        enableSorting: false,
        meta: { align: 'right' },
        // Net of returns: material handed back is not on the floor, so the gross
        // issue on its own would overstate what the job is holding.
        cell: ({ row }) => (
          <span>
            {formatQuantity(row.original.net_issued_quantity)}
            {toDecimal(row.original.returned_quantity).greaterThan(0) && (
              <span className="block text-2xs text-muted-foreground">
                {formatQuantity(row.original.returned_quantity)} returned
              </span>
            )}
          </span>
        ),
      },
      {
        id: 'consumed',
        header: 'Consumed',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.consumed_quantity),
      },
      {
        id: 'outstanding',
        header: 'Still to issue',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(outstandingQuantity(row.original).toFixed()),
      },
      {
        id: 'reserved',
        header: 'Reserved',
        enableSorting: false,
        meta: { align: 'right' },
        // Held for this order by the release, and already excluded from the free
        // stock beside it - which is the usual answer to "why is it short when
        // the balance says we have some?". Live coverage is optional on the
        // contract, and a figure the API did not report is a dash, never a zero.
        cell: ({ row }) =>
          row.original.reserved_quantity === undefined ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatQuantity(row.original.reserved_quantity)
          ),
      },
      {
        id: 'available',
        header: 'Free stock',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.available_quantity === undefined ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatQuantity(row.original.available_quantity)
          ),
      },
      {
        id: 'shortage',
        header: 'Shortage',
        enableSorting: false,
        meta: { align: 'right' },
        // The API's own figure. This is the column a supervisor scans before
        // starting a shift.
        cell: ({ row }) => {
          const shortage = componentShortage(row.original)
          return shortage.isZero() ? (
            <span className="text-success">Covered</span>
          ) : (
            <span className="font-medium text-destructive">
              {formatQuantity(shortage.toFixed())}
            </span>
          )
        },
      },
    ],
    [],
  )

  const shortCount = components.filter((component) =>
    componentShortage(component).greaterThan(0),
  ).length

  return (
    <div className="space-y-4">
      {shortCount > 0 && (
        <Alert variant="warning">
          <AlertTitle>
            {shortCount} component{shortCount === 1 ? '' : 's'} short of free stock
          </AlertTitle>
          <AlertDescription className="space-y-3">
            <p>
              The balance to issue is larger than what is available. Issuing what there is now is
              legitimate; the rest needs planning before the order can finish.
            </p>
            <Button variant="outline" size="sm" asChild>
              <Link to="/mrp">Plan the shortfall in MRP</Link>
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <DataTable
        columns={columns}
        data={components}
        isLoading={isLoading}
        isFetching={isFetching}
        error={error}
        onRefresh={onRefresh}
        getRowId={(row) => row.id}
        emptyTitle="No components"
        emptyDescription="This order carries no bill of materials, so nothing has to be drawn from stock."
        emptyAction={
          canIssue ? (
            <Button size="sm" onClick={onIssue}>
              <PackageMinus />
              Issue material
            </Button>
          ) : undefined
        }
      />
    </div>
  )
}

// --------------------------------------------------------- work orders ------

function WorkOrdersPanel({ orderId }: { orderId: string }) {
  // Paged by the server like every other list: a routing with a long operation
  // list would otherwise be silently cut off at the API's default page size.
  const list = useListQuery<WorkOrderRow>({
    path: `/production/${orderId}/work-orders`,
    queryKey: (params) => [...queryKeys.production.detail(orderId), 'work-orders', params] as const,
    defaultSortBy: 'sequence',
    defaultSortDir: 'asc',
    staleTime: staleTimes.transactional,
  })

  const columns = useMemo<ColumnDef<WorkOrderRow, unknown>[]>(
    () => [
      {
        id: 'sequence',
        header: 'Seq',
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.sequence}</span>,
      },
      {
        id: 'operation',
        header: 'Operation',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[16rem]">
            <span className="block truncate font-medium">{row.original.operation_name}</span>
            <span className="block truncate text-2xs text-muted-foreground">
              {row.original.operation_code} - {row.original.code}
            </span>
          </span>
        ),
      },
      {
        id: 'planned_quantity',
        header: 'Planned qty',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.planned_quantity),
      },
      {
        id: 'planned_minutes',
        header: 'Planned (min)',
        enableSorting: false,
        meta: { align: 'right' },
        // Setup is paid once for the batch and run time per unit, so the two are
        // shown apart as well as summed: a long setup and a long run are very
        // different problems.
        cell: ({ row }) => (
          <span>
            {formatQuantity(plannedMinutes(row.original).toFixed())}
            <span className="block text-2xs text-muted-foreground">
              {formatQuantity(row.original.planned_setup_minutes)} setup +{' '}
              {formatQuantity(row.original.planned_run_minutes)} run
            </span>
          </span>
        ),
      },
      {
        id: 'actual_minutes',
        header: 'Actual (min)',
        enableSorting: false,
        meta: { align: 'right' },
        // Derived from the clock the API records on the work order. Null while
        // the operation is still open - an over-run cannot be judged against a
        // number nobody has written down yet.
        cell: ({ row }) => {
          const actual = actualMinutes(row.original)
          if (actual === null) return <span className="text-muted-foreground">-</span>
          const over = actual.greaterThan(plannedMinutes(row.original))
          return (
            <span className={over ? 'font-medium text-warning' : undefined}>
              {formatQuantity(actual.toFixed())}
            </span>
          )
        },
      },
      {
        id: 'completed_quantity',
        header: 'Completed',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.completed_quantity),
      },
      {
        id: 'rejected_quantity',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const rejected = toDecimal(row.original.rejected_quantity)
          return rejected.isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="text-destructive">
              {formatQuantity(row.original.rejected_quantity)}
            </span>
          )
        },
      },
      {
        id: 'rate',
        header: 'Rate / hour',
        enableSorting: false,
        meta: { align: 'right' },
        // Snapshotted onto the work order at release, so a later change to the
        // work centre does not silently restate what this job was costed at.
        cell: ({ row }) => formatMoney(row.original.work_center_cost_per_hour),
      },
      {
        id: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [],
  )

  return (
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
      onRefresh={() => void list.refetch()}
      getRowId={(row) => row.id}
      emptyTitle="No work orders"
      emptyDescription="This order has no routing, so its operations were never expanded into work orders."
    />
  )
}

// ------------------------------------------------------------- entries ------

function EntriesPanel({
  orderId,
  canRecord,
  onRecord,
}: {
  orderId: string
  canRecord: boolean
  onRecord: () => void
}) {
  // Bookings accumulate for the life of the order, so this one is paged by the
  // server rather than fetched whole like the components and work orders.
  const list = useListQuery<ProductionEntryRow>({
    path: `/production/${orderId}/entries`,
    queryKey: (params) => [...queryKeys.production.detail(orderId), 'entries', params] as const,
    defaultSortBy: 'entry_date',
    defaultSortDir: 'desc',
    defaultLimit: 25,
    staleTime: staleTimes.transactional,
  })

  const columns = useMemo<ColumnDef<ProductionEntryRow, unknown>[]>(
    () => [
      {
        accessorKey: 'entry_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.entry_date),
      },
      {
        id: 'shift',
        header: 'Shift',
        enableSorting: false,
        cell: ({ row }) => row.original.shift ?? '-',
      },
      {
        id: 'produced_quantity',
        header: 'Produced',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.produced_quantity)}</span>
        ),
      },
      {
        id: 'rejected_quantity',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const rejected = toDecimal(row.original.rejected_quantity)
          return rejected.isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="text-destructive">
              {formatQuantity(row.original.rejected_quantity)}
            </span>
          )
        },
      },
      {
        id: 'rejection_reason',
        header: 'Rejection reason',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[16rem] truncate text-muted-foreground">
            {row.original.rejection_reason ?? '-'}
          </span>
        ),
      },
      {
        id: 'downtime_minutes',
        header: 'Downtime (min)',
        enableSorting: false,
        meta: { align: 'right' },
        // A decimal string on the wire like every other measured value, so it is
        // formatted as a quantity rather than parsed into a JS number.
        cell: ({ row }) => formatQuantity(row.original.downtime_minutes),
      },
      {
        id: 'output',
        header: 'Received into stock',
        enableSorting: false,
        // A booking with good output posts an inventory receipt. Saying which
        // ones did is how a store keeper reconciles the shift against the ledger.
        cell: ({ row }) =>
          row.original.output_transaction_id ? (
            <span className="text-success">Posted</span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ],
    [],
  )

  const recordButton = canRecord ? (
    <Button size="sm" onClick={onRecord}>
      <Plus />
      Record production
    </Button>
  ) : null

  return (
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
      onRefresh={() => void list.refetch()}
      getRowId={(row) => row.id}
      toolbar={recordButton}
      emptyTitle="Nothing booked yet"
      emptyDescription="Output and downtime are booked against a chosen operation or the whole job. Start the job before booking."
      emptyAction={recordButton ?? undefined}
    />
  )
}

// ------------------------------------------------------------- costing ------

const materialCostColumns: ColumnDef<ProductionCostMaterialLine, unknown>[] = [
  {
    id: 'component',
    header: 'Component',
    enableSorting: false,
    cell: ({ row }) => <span className="font-medium">{row.original.item_code}</span>,
  },
  {
    id: 'required',
    header: 'Required',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatQuantity(row.original.required_quantity),
  },
  {
    id: 'issued',
    header: 'Issued',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatQuantity(row.original.issued_quantity),
  },
  {
    id: 'returned',
    header: 'Returned',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatQuantity(row.original.returned_quantity),
  },
  {
    id: 'net-issued',
    header: 'Net issued',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatQuantity(row.original.net_issued_quantity),
  },
  {
    id: 'unit-cost',
    header: 'Unit cost',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.unit_cost),
  },
  {
    id: 'total-cost',
    header: 'Total cost',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.total_cost),
  },
]

const operationCostColumns: ColumnDef<ProductionCostOperationLine, unknown>[] = [
  {
    id: 'sequence',
    header: 'Seq',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => row.original.sequence,
  },
  {
    id: 'operation',
    header: 'Operation',
    enableSorting: false,
    cell: ({ row }) => <span className="font-medium">{row.original.operation_code}</span>,
  },
  {
    id: 'status',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => humanise(row.original.status),
  },
  {
    id: 'charged-minutes',
    header: 'Charged minutes',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatQuantity(row.original.charged_minutes),
  },
  {
    id: 'rate',
    header: 'Rate / hour',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.cost_per_hour),
  },
  {
    id: 'time-cost',
    header: 'Time cost',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.time_cost),
  },
  {
    id: 'setup-cost',
    header: 'Setup cost',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.setup_cost),
  },
  {
    id: 'process-cost',
    header: 'Process cost',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.process_cost),
  },
  {
    id: 'total-cost',
    header: 'Total cost',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatMoney(row.original.total_cost),
  },
]

function CostingPanel({ order }: { order: ProductionOrderDetail }) {
  const cost = useQuery({
    queryKey: [...queryKeys.production.detail(order.id), 'cost'] as const,
    queryFn: () => api.get<ProductionCost>(`/production/${order.id}/cost`),
    staleTime: staleTimes.transactional,
  })

  if (cost.isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <Skeleton key={index} className="h-24" />
        ))}
      </div>
    )
  }

  if (cost.error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the costing</AlertTitle>
        <AlertDescription>{cost.error.message}</AlertDescription>
        <Button variant="outline" size="sm" className="mt-4" onClick={() => void cost.refetch()}>
          <RefreshCw />
          Try again
        </Button>
      </Alert>
    )
  }

  if (!cost.data) {
    return (
      <EmptyState
        title="No costing yet"
        description="Costs appear once material has been issued and operations booked against this order."
      />
    )
  }

  const data = cost.data

  return (
    <div className="space-y-4">
      {/* A costing taken mid-flight is a running total, not the answer. Saying
          so is the difference between a figure a controller can book and one
          they merely glance at. */}
      {!data.is_final && (
        <Alert variant="info">
          <AlertTitle>Provisional</AlertTitle>
          <AlertDescription>
            {order.code} is still {humanise(data.status).toLowerCase()}. These figures move with
            every issue and booking, and settle when the order is closed.
          </AlertDescription>
        </Alert>
      )}

      {data.uncosted_operation_count > 0 && (
        <Alert variant="warning">
          <AlertTitle>
            {formatNumber(data.uncosted_operation_count)} operation
            {data.uncosted_operation_count === 1 ? ' has' : 's have'} no rate behind them
          </AlertTitle>
          <AlertDescription>
            Their work centre carries no cost per hour, so the operation cost below is an
            understatement rather than a complete figure.
          </AlertDescription>
        </Alert>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Material cost"
          value={formatMoney(data.material_cost)}
          hint="Components issued, at their valuation"
        />
        <StatTile
          label="Operation cost"
          value={formatMoney(data.operation_cost)}
          hint="Machine and labour time booked"
        />
        <StatTile label="Total cost" value={formatMoney(data.total_cost)} />
        <StatTile
          label="Cost per unit"
          // Null until something has been produced to divide by - which is not
          // the same as costing nothing.
          value={
            data.cost_per_unit === null ? 'Nothing produced yet' : formatMoney(data.cost_per_unit)
          }
          hint={`Over ${formatQuantity(data.produced_quantity)} of ${formatQuantity(data.planned_quantity)} planned`}
        />
      </div>

      <p className="text-xs text-muted-foreground">
        Calculated by the API from the issues and bookings posted against this order. The screen
        never re-adds them, so these figures reconcile with the inventory ledger.
      </p>

      <section aria-labelledby="production-material-cost-heading" className="space-y-3">
        <div>
          <h3 id="production-material-cost-heading" className="text-base font-semibold">
            Material cost by component
          </h3>
          <p className="text-sm text-muted-foreground">
            Net issues are valued at the cost recorded on their inventory movements.
          </p>
        </div>
        <DataTable
          columns={materialCostColumns}
          data={data.materials}
          getRowId={(row) => row.component_id}
          emptyTitle="No material cost"
          emptyDescription="This order has no material issue lines to cost."
        />
      </section>

      <section aria-labelledby="production-operation-cost-heading" className="space-y-3">
        <div>
          <h3 id="production-operation-cost-heading" className="text-base font-semibold">
            Operation cost by work order
          </h3>
          <p className="text-sm text-muted-foreground">
            Clocked time is used when available; otherwise the planned setup and run time is costed.
          </p>
        </div>
        <DataTable
          columns={operationCostColumns}
          data={data.operations}
          getRowId={(row) => row.work_order_id}
          emptyTitle="No operation cost"
          emptyDescription="This order has no work orders to cost."
        />
      </section>
    </div>
  )
}

// ---------------------------------------------------------------- page ------

type ActionKey = 'approve' | 'release' | 'start' | 'complete' | 'close'

/**
 * What the five transitions answer with.
 *
 * They do NOT agree: `approve` and `start` return the order on its own, while
 * `release`, `complete` and `close` wrap it in a result that also reports what
 * the transition did. Reading `.order` off all five would be undefined on the
 * bare two, so `orderOf` narrows before anything touches it.
 */
type TransitionResult = ProductionOrderRow | ProductionTransitionEnvelope

interface ProductionTransitionEnvelope {
  message: string
  order: ProductionOrderRow
  fully_reserved?: boolean
  shortages?: ComponentShortage[]
}

function isEnvelope(result: TransitionResult): result is ProductionTransitionEnvelope {
  return typeof (result as Partial<ProductionTransitionEnvelope>).order === 'object'
}

function orderOf(result: TransitionResult): ProductionOrderRow {
  return isEnvelope(result) ? result.order : result
}

export default function ProductionDetailPage() {
  const { orderId = '' } = useParams<{ orderId: string }>()
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [pendingAction, setPendingAction] = useState<ActionKey | null>(null)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [cancelReasonTouched, setCancelReasonTouched] = useState(false)
  const [issueOpen, setIssueOpen] = useState(false)
  const [returnOpen, setReturnOpen] = useState(false)
  const [recordOpen, setRecordOpen] = useState(false)

  const canViewCost = can(P.PRODUCTION_COST_VIEW)
  const canViewCostCentres = can(P.COST_CENTRE_VIEW)

  const orderQuery = useQuery({
    queryKey: queryKeys.production.detail(orderId),
    queryFn: () => api.get<ProductionOrderDetail>(`/production/${orderId}`),
    staleTime: staleTimes.transactional,
    enabled: Boolean(orderId),
  })

  const order = orderQuery.data

  // Fetched at page level rather than inside the tab: the issue dialog and the
  // header both need to know what is outstanding.
  const componentsQuery = useQuery({
    queryKey: [...queryKeys.production.detail(orderId), 'components'] as const,
    // The kit list is bounded at release and the issue dialog needs it whole, so
    // this one asks for a single large page rather than paging the table.
    queryFn: () =>
      api.get<ProductionComponent[]>(`/production/${orderId}/components`, {
        params: {
          limit: LOOKUP_LIST_LIMIT,
          with_total: false,
          sort_by: 'line_number',
          sort_dir: 'asc',
        },
      }),
    // Availability is never served stale (master rules 34).
    staleTime: staleTimes.live,
    enabled: Boolean(orderId),
  })
  const components = useMemo(() => componentsQuery.data ?? [], [componentsQuery.data])

  const tabs = ['components', 'work-orders', 'entries', ...(canViewCost ? ['costing'] : [])]
  const requestedTab = searchParams.get('tab') ?? ''
  const tab = tabs.includes(requestedTab) ? requestedTab : 'components'

  // Switching tab drops the list parameters: a page number typed against the
  // bookings means nothing against the components.
  const setTab = (value: string) =>
    setSearchParams(
      () => {
        const next = new URLSearchParams()
        next.set('tab', value)
        return next
      },
      { replace: true },
    )

  /** A transition moves stock and reservations, so both caches go with it. */
  const invalidateOrderAndStock = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
  }

  /**
   * Every transition takes a JSON body, even when there is nothing to say in it.
   * Completion and closure collect their endpoint-specific capture fields below.
   */
  const transition = useMutation({
    mutationFn: async ({
      action,
      body,
    }: {
      action: ActionKey
      body?: Record<string, string | null>
    }) => {
      const result = await api.post<TransitionResult>(
        `/production/${orderId}/${action}`,
        body ?? {},
      )
      return { action, result }
    },
    onSuccess: ({ action, result }) => {
      invalidateOrderAndStock()
      setPendingAction(null)

      const updated = orderOf(result)
      const shortReleased = isEnvelope(result) && result.fully_reserved === false
      if (action === 'release' && shortReleased) {
        // Releasing does not guarantee the parts are held: `_reserve_components`
        // holds what happened to be free and records the rest as a shortage
        // while the job goes out anyway (docs/assumptions.md 22). A supervisor
        // must be told, or the job reaches the floor unkitted.
        toast.warning(
          `${updated.code} released short`,
          `${formatNumber(isEnvelope(result) ? (result.shortages?.length ?? 0) : 0)} component(s) could not be reserved in full. Issue what there is and plan the rest.`,
        )
        return
      }

      // Past tense per transition; "started" has no catalogue verb of its own.
      const done: Record<ActionKey, string> = {
        approve: success.approved('Production order'),
        release: success.released('Production order'),
        start: 'Production started',
        complete: success.completed('Production order'),
        close: success.closed('Production order'),
      }
      toast.success(
        done[action],
        `${updated.code} is now ${humanise(updated.status).toLowerCase()}.`,
      )
    },
  })

  const cancelOrder = useMutation({
    mutationFn: (reason: string) =>
      api.post<ProductionOrderRow>(`/production/${orderId}/cancel`, { reason }),
    onSuccess: (updated) => {
      invalidateOrderAndStock()
      toast.success(success.cancelled('Production order'), `${updated.code} is cancelled.`)
      setCancelOpen(false)
      setCancelReason('')
      setCancelReasonTouched(false)
    },
  })

  /**
   * `CancelRequest.reason` is bounded 3..255 characters. A 422 keyed on it is
   * one the mutation cache stays quiet about, so the floor is enforced here,
   * the ceiling by the input itself - and any rule the server adds later is
   * still rendered under the box rather than lost.
   */
  const cancelReasonTooShort = cancelReason.trim().length < REASON_MIN_LENGTH
  const cancelReasonServerError =
    cancelOrder.error instanceof ApiError ? cancelOrder.error.fieldErrors.reason?.[0] : undefined

  if (orderQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <Skeleton className="h-9 w-full" />
        <div className="grid gap-4 lg:grid-cols-3">
          <Skeleton className="h-56 lg:col-span-2" />
          <Skeleton className="h-56" />
        </div>
        <Skeleton className="h-72 w-full" />
      </div>
    )
  }

  if (orderQuery.error) {
    return (
      <>
        <PageHeader
          title="Production order"
          breadcrumbs={[{ label: 'Production', to: '/production' }]}
        />
        <Alert variant="destructive">
          <AlertTitle>Could not load this order</AlertTitle>
          <AlertDescription>{orderQuery.error.message}</AlertDescription>
          <Button
            variant="outline"
            size="sm"
            className="mt-4"
            onClick={() => void orderQuery.refetch()}
          >
            <RefreshCw />
            Try again
          </Button>
        </Alert>
      </>
    )
  }

  if (!order) {
    return (
      <>
        <PageHeader
          title="Production order"
          breadcrumbs={[{ label: 'Production', to: '/production' }]}
        />
        <EmptyState
          title="Order not found"
          description="It may have been removed, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/production">Back to production orders</Link>
            </Button>
          }
        />
      </>
    )
  }

  const percent = progressPercent(order.produced_quantity, order.planned_quantity)
  const canIssue = acceptsMaterialIssue(order.status) && can(P.MATERIAL_ISSUE_CREATE)
  const canRecord = acceptsProductionEntry(order.status) && can(P.PRODUCTION_ENTRY_CREATE)
  // Returning is its own right - `POST /{id}/material-returns` asks for
  // `material_return.create`, not the issue right - and it is also pointless
  // unless the job is actually holding something it has not consumed. The
  // button therefore appears with the kit list rather than with the page, which
  // is honest: until the components are known, nothing is known to be out.
  const canReturn =
    acceptsMaterialReturn(order.status) &&
    can(P.MATERIAL_RETURN_CREATE) &&
    components.some((component) => returnableQuantity(component).greaterThan(0))

  const actionLabels: Record<ActionKey, { title: string; description: string; confirm: string }> = {
    approve: {
      title: `Approve ${order.code}?`,
      description:
        'Approval confirms the plan and the quantity. The order can then be released to the shop floor.',
      confirm: 'Approve order',
    },
    release: {
      title: `Release ${order.code}?`,
      description:
        'Releasing makes the order visible to the shop floor and reserves its components against free stock as far as they are available.',
      confirm: 'Release order',
    },
    start: {
      title: `Start ${order.code}?`,
      description:
        'Starting puts the job under way and records its actual start date. From here it can no longer be cancelled, and only from here can it be completed - booking output on its own does not move the order on.',
      confirm: 'Start production',
    },
    complete: {
      title: `Complete ${order.code}?`,
      description:
        'Completing stops further booking against this order. Return anything still issued but not consumed first - a completed order no longer accepts a return.',
      confirm: 'Complete order',
    },
    close: {
      title: `Close ${order.code}?`,
      description: 'Closing settles the order and its costing. A closed order cannot be reopened.',
      confirm: 'Close order',
    },
  }

  return (
    <>
      <PageHeader
        title={order.code}
        description={`Raised ${humanise(order.source_type).toLowerCase()}, for ${formatQuantity(order.planned_quantity)}.`}
        breadcrumbs={[{ label: 'Production', to: '/production' }, { label: order.code }]}
        status={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={order.status} />
            <Badge variant={priorityBand(order.priority).variant}>
              {priorityBand(order.priority).label} ({formatNumber(order.priority)})
            </Badge>
            {order.requires_qc && <Badge variant="warning">QC hold</Badge>}
          </span>
        }
        actions={
          <>
            {/* Every button below gates on a permission, which is UX only: the
                API authorises each transition independently. */}
            {isApprovable(order.status) && can(P.PRODUCTION_ORDER_APPROVE) && (
              <Button onClick={() => setPendingAction('approve')}>
                <CheckCircle2 />
                Approve
              </Button>
            )}
            {isReleasable(order.status) && can(P.PRODUCTION_ORDER_RELEASE) && (
              <Button onClick={() => setPendingAction('release')}>
                <PlayCircle />
                Release
              </Button>
            )}
            {/* The only edge from MATERIAL_ISSUED to IN_PROCESS. Without it a
                kitted job can be booked against for ever and never completed,
                because completion is legal only from IN_PROCESS. */}
            {isStartable(order.status) && can(P.WORK_ORDER_MANAGE) && (
              <Button onClick={() => setPendingAction('start')}>
                <PlayCircle />
                Start
              </Button>
            )}
            {canIssue && (
              <Button variant="outline" onClick={() => setIssueOpen(true)}>
                <PackageMinus />
                Issue material
              </Button>
            )}
            {/* The way back. Material that stays out cannot be cancelled
                around - the API refuses a cancellation while the job is still
                holding stock - and a completed order no longer accepts a
                return, so this belongs beside Issue, not after Complete. */}
            {canReturn && (
              <Button variant="outline" onClick={() => setReturnOpen(true)}>
                <PackagePlus />
                Return material
              </Button>
            )}
            {canRecord && (
              <Button variant="outline" onClick={() => setRecordOpen(true)}>
                <Factory />
                Record production
              </Button>
            )}
            {/* Each of these gates on the permission its own endpoint requires.
                Completion is booked by the person recording output, not by a
                supervisor: `POST /{id}/complete` asks for
                `production_entry.create`, and cancellation for
                `production_order.cancel`. Hanging all three off the close
                permission hid Complete from the operator entitled to press it
                and offered it to a controller who would be refused. */}
            {isCompletable(order.status) && can(P.PRODUCTION_ENTRY_CREATE) && (
              <Button variant="outline" onClick={() => setPendingAction('complete')}>
                <Check />
                Complete
              </Button>
            )}
            {isClosable(order.status) && can(P.PRODUCTION_ORDER_CLOSE) && (
              <Button variant="outline" onClick={() => setPendingAction('close')}>
                <Lock />
                Close
              </Button>
            )}
            {isCancellable(order.status) && can(P.PRODUCTION_ORDER_CANCEL) && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={() => setCancelOpen(true)}
              >
                <Ban />
                Cancel
              </Button>
            )}
          </>
        }
      />

      <div className="space-y-4">
        <StatusStepper status={order.status} />

        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Order</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList
                columns={3}
                items={[
                  {
                    label: 'Item',
                    // The order holds `item_id` only - no code, no name.
                    value: (
                      <Link to={`/items/${order.item_id}`} className="hover:underline">
                        Open item master
                      </Link>
                    ),
                  },
                  {
                    label: 'Bill of materials',
                    value: order.bom_id ? (
                      <Link to={`/bom/${order.bom_id}`} className="hover:underline">
                        View BOM
                      </Link>
                    ) : (
                      'None'
                    ),
                  },
                  {
                    label: 'Routing',
                    value: order.routing_id ? 'Expanded into work orders' : 'None',
                  },
                  {
                    label: 'Raised from',
                    value: `${humanise(order.source_type)}${order.source_id ? '' : ' (no source document)'}`,
                  },
                  // Named only to an account that may read the cost-centre
                  // list - the label is looked up there, and a row that could
                  // only ever say "-" is not information.
                  ...(canViewCostCentres
                    ? [
                        {
                          label: 'Cost centre',
                          value: <CostCentreName id={order.cost_centre_id} />,
                        },
                      ]
                    : []),
                  { label: 'Components', value: formatNumber(order.component_count ?? 0) },
                  { label: 'Planned start', value: formatDate(order.planned_start_date) },
                  { label: 'Planned end', value: formatDate(order.planned_end_date) },
                  { label: 'Actual start', value: formatDate(order.actual_start_date) },
                  { label: 'Actual end', value: formatDate(order.actual_end_date) },
                  { label: 'Approved', value: formatDateTime(order.approved_at) },
                  { label: 'Created', value: formatDateTime(order.created_at) },
                  { label: 'Last updated', value: formatDateTime(order.updated_at) },
                ]}
              />
              {(order.notes || order.closure_note) && (
                <div className="mt-5 space-y-2 border-t pt-4 text-sm text-muted-foreground">
                  {order.notes && <p>{order.notes}</p>}
                  {order.closure_note && <p>Closed: {order.closure_note}</p>}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Output</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-muted"
                role="progressbar"
                aria-valuenow={percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Produced against planned"
              >
                <div
                  className={cn(
                    'h-full rounded-full',
                    percent >= 100 ? 'bg-success' : 'bg-primary',
                  )}
                  style={{ width: `${percent}%` }}
                />
              </div>
              <DescriptionList
                columns={1}
                items={[
                  { label: 'Planned', value: formatQuantity(order.planned_quantity) },
                  {
                    label: 'Produced',
                    value: `${formatQuantity(order.produced_quantity)} (${percent}%)`,
                  },
                  {
                    label: 'Rejected',
                    value: toDecimal(order.rejected_quantity).isZero() ? (
                      'None'
                    ) : (
                      <span className="text-destructive">
                        {formatQuantity(order.rejected_quantity)}
                      </span>
                    ),
                  },
                  {
                    label: 'Scrapped',
                    value: toDecimal(order.scrap_quantity).isZero()
                      ? 'None'
                      : formatQuantity(order.scrap_quantity),
                  },
                ]}
              />
            </CardContent>
          </Card>
        </div>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="components">
              <ClipboardList className="h-3.5 w-3.5" aria-hidden />
              Components
            </TabsTrigger>
            <TabsTrigger value="work-orders">
              <Wrench className="h-3.5 w-3.5" aria-hidden />
              Work orders
            </TabsTrigger>
            <TabsTrigger value="entries">
              <Factory className="h-3.5 w-3.5" aria-hidden />
              Production entries
            </TabsTrigger>
            {canViewCost && (
              <TabsTrigger value="costing">
                <Coins className="h-3.5 w-3.5" aria-hidden />
                Costing
              </TabsTrigger>
            )}
          </TabsList>

          <TabsContent value="components">
            <ComponentsPanel
              components={components}
              isLoading={componentsQuery.isLoading}
              isFetching={componentsQuery.isFetching}
              error={componentsQuery.error as Error | null}
              onRefresh={() => void componentsQuery.refetch()}
              onIssue={() => setIssueOpen(true)}
              canIssue={canIssue}
            />
          </TabsContent>

          <TabsContent value="work-orders">
            <WorkOrdersPanel orderId={order.id} />
          </TabsContent>

          <TabsContent value="entries">
            <EntriesPanel
              orderId={order.id}
              canRecord={canRecord}
              onRecord={() => setRecordOpen(true)}
            />
          </TabsContent>

          {canViewCost && (
            <TabsContent value="costing">
              <CostingPanel order={order} />
            </TabsContent>
          )}
        </Tabs>
      </div>

      {canIssue && (
        <IssueMaterialDialog
          open={issueOpen}
          onOpenChange={setIssueOpen}
          orderId={order.id}
          orderCode={order.code}
          components={components}
        />
      )}

      {canReturn && (
        <ReturnMaterialDialog
          open={returnOpen}
          onOpenChange={setReturnOpen}
          orderId={order.id}
          orderCode={order.code}
          components={components}
        />
      )}

      {canRecord && (
        <RecordProductionDialog
          open={recordOpen}
          onOpenChange={setRecordOpen}
          orderId={order.id}
          orderCode={order.code}
        />
      )}

      <ConfirmDialog
        open={pendingAction !== null && pendingAction !== 'complete' && pendingAction !== 'close'}
        onOpenChange={(open) => !open && setPendingAction(null)}
        title={pendingAction ? actionLabels[pendingAction].title : ''}
        description={pendingAction ? actionLabels[pendingAction].description : undefined}
        confirmLabel={pendingAction ? actionLabels[pendingAction].confirm : 'Confirm'}
        loading={transition.isPending}
        onConfirm={() => pendingAction && transition.mutate({ action: pendingAction })}
      />

      <CompleteProductionDialog
        open={pendingAction === 'complete'}
        onOpenChange={(open) => !open && setPendingAction(null)}
        order={order}
        loading={transition.isPending}
        onConfirm={(body) => transition.mutate({ action: 'complete', body })}
      />

      <CloseProductionDialog
        open={pendingAction === 'close'}
        onOpenChange={(open) => !open && setPendingAction(null)}
        order={order}
        loading={transition.isPending}
        onConfirm={(body) => transition.mutate({ action: 'close', body })}
      />

      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={(open) => {
          setCancelOpen(open)
          if (!open) {
            setCancelReasonTouched(false)
            cancelOrder.reset()
          }
        }}
        title={`Cancel ${order.code}?`}
        description="The order stops here and cannot be reopened. Anything still out on the floor has to be returned to the store first: a job holding material cannot be cancelled at all."
        confirmLabel="Cancel order"
        cancelLabel="Keep order"
        destructive
        loading={cancelOrder.isPending}
        onConfirm={() => {
          if (cancelReasonTooShort) {
            setCancelReasonTouched(true)
            return
          }
          cancelOrder.mutate(cancelReason.trim())
        }}
      >
        <div>
          <Label htmlFor="production-cancel-reason" required>
            Reason
          </Label>
          <Input
            id="production-cancel-reason"
            value={cancelReason}
            onChange={(event) => setCancelReason(event.target.value)}
            autoComplete="off"
            maxLength={REASON_MAX_LENGTH}
            className="mt-1.5"
            aria-invalid={
              (cancelReasonTouched && cancelReasonTooShort) || Boolean(cancelReasonServerError)
            }
          />
          {cancelReasonTouched && cancelReasonTooShort && (
            <FieldError
              message={`A cancellation needs a recorded reason of at least ${REASON_MIN_LENGTH} characters`}
            />
          )}
          <FieldError message={cancelReasonServerError} />
        </div>
      </ConfirmDialog>
    </>
  )
}

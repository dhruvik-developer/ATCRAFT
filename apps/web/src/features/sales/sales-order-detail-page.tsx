/**
 * Sales order detail.
 *
 * The actions offered are derived from the current status rather than shown and
 * disabled: the state machine in docs/database.md section 7 says a confirmed
 * order cannot be confirmed again and a dispatched one cannot be cancelled, so
 * those buttons are simply not there.
 *
 * The allocation panel is deliberately not an error surface. Partial coverage is
 * the normal state of a manufacturing order book - the useful response to a
 * shortfall is to plan it, so the panel says so and links to MRP.
 *
 * Lines are not addressable. `PATCH /sales/{id}` takes the whole line set, so an
 * add, an edit and a removal are all the same request: rebuild the set and send
 * it. The API prices it, re-splits the tax and re-reserves against it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  ArrowLeftRight,
  Ban,
  CheckCircle2,
  ListPlus,
  PackageCheck,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Trash2,
  Truck,
} from 'lucide-react'
import { Suspense, lazy, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  PageHeader,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
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
import { useAuth } from '@/features/auth/auth-context'
import type { CustomerDetail } from '@/features/customers/customer-types'
import { ReallocationRequestDialog } from '@/features/inventory/reallocation-request-dialog'
import { WAREHOUSE_LOOKUP_PARAMS, type WarehouseOption } from '@/features/inventory/types'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { reportFieldErrors } from '@/lib/form-errors'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatPercent,
  formatQuantity,
  toDecimal,
} from '@/lib/format'
import { fulfilment, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { FulfilmentLinks } from './fulfilment-links'
import { SalesOrderFormDialog } from './sales-order-form-dialog'
import { SalesOrderLineDialog } from './sales-order-line-dialog'
import {
  canTopUp,
  isCancellable,
  isConfirmable,
  isDispatchable,
  isDraft,
  renumber,
  toLineInput,
  type AllocationLineStatus,
  type ConfirmResult,
  type SalesOrderAllocation,
  type SalesOrderAvailability,
  type SalesOrderDetail,
  type SalesOrderLine,
  type SalesOrderLineIn,
  type SalesOrderRow,
} from './sales-types'

/**
 * The dispatch dialog is the dispatch feature's, and a large one; loaded on the
 * first press of Dispatch so the order page's own chunk does not carry it.
 */
const CreateDispatchDialog = lazy(() =>
  import('@/features/dispatch/dispatch-dialogs').then((m) => ({
    default: m.CreateDispatchDialog,
  })),
)

// ---------------------------------------------------------- allocation ------

function AllocationPanel({ order }: { order: SalesOrderDetail }) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [reallocationTarget, setReallocationTarget] = useState<AllocationLineStatus | null>(null)
  const canReallocate = can(P.ALLOCATION_REALLOCATE)
  const allocation = useQuery({
    queryKey: [...queryKeys.sales.detail(order.id), 'allocation-status'],
    queryFn: () => api.get<SalesOrderAllocation>(`/sales/${order.id}/allocation-status`),
    // Availability is never served stale (master rules 34).
    staleTime: staleTimes.live,
  })

  // `POST /sales/{id}/allocate` is declared on `sales_order.update`: the top-up
  // changes the order's reservations exactly as a quantity edit does.
  const allocate = useMutation({
    mutationFn: () =>
      api.post<SalesOrderAllocation>(`/sales/${order.id}/allocate`, {
        reason: 'Available stock allocated from the order page',
      }),
    onSuccess: (view) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      toast.success(
        view.fully_allocated
          ? fulfilment.allocatedAll
          : fulfilment.allocatedSome(view.total_shortfall_lines),
        order.code,
      )
    },
    onError: (error) => reportFieldErrors(error, 'Could not allocate stock'),
  })

  const lines = useMemo(() => allocation.data?.lines ?? [], [allocation.data])
  const shortLines = useMemo(
    () => lines.filter((line) => toDecimal(line.shortfall_quantity).greaterThan(0)),
    [lines],
  )

  const columns = useMemo<ColumnDef<AllocationLineStatus, unknown>[]>(
    () => [
      {
        id: 'line_number',
        header: '#',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.line_number}</span>,
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.item_id}`}
            className="block max-w-[20rem] truncate font-medium hover:underline"
          >
            {row.original.item_code ?? row.original.item_id}
          </Link>
        ),
      },
      {
        id: 'ordered',
        header: 'Ordered',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.ordered_quantity),
      },
      {
        id: 'reserved',
        header: fulfilment.reservedColumn,
        enableSorting: false,
        meta: { align: 'right' },
        // The reservation's *live* remaining quantity: one released or expired
        // elsewhere reads as nothing here, and the top-up is what re-reserves it.
        cell: ({ row }) => formatQuantity(row.original.reserved_quantity),
      },
      {
        id: 'dispatchable',
        header: fulfilment.dispatchableColumn,
        enableSorting: false,
        meta: { align: 'right' },
        // What a new consignment may carry for the line now: the live
        // reservation, less what other open consignments already carry.
        cell: ({ row }) => formatQuantity(row.original.dispatchable_quantity),
      },
      {
        id: 'dispatched',
        header: 'Dispatched',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.dispatched_quantity),
      },
      {
        id: 'shortfall',
        header: fulfilment.shortColumn,
        enableSorting: false,
        meta: { align: 'right' },
        // The engine's own number. Nothing here recomputes it: a browser-side
        // subtraction would disagree with the reservation the moment stock moved.
        cell: ({ row }) =>
          toDecimal(row.original.shortfall_quantity).isZero() ? (
            <span className="text-success">Covered</span>
          ) : (
            <span className="font-medium text-warning">
              {formatQuantity(row.original.shortfall_quantity)}
            </span>
          ),
      },
      {
        id: 'available',
        header: 'Free stock',
        enableSorting: false,
        meta: { align: 'right' },
        // Absent means the engine did not report free stock for this line,
        // which is not the same as "none": it is shown as unknown. The contract
        // makes `available_quantity` optional, so it can arrive as `undefined`
        // as well as `null` - a strict `=== null` test let the undefined case
        // fall through to `formatQuantity(undefined)`, which renders a
        // confident `0` for a figure nobody actually reported.
        cell: ({ row }) =>
          row.original.available_quantity == null ? (
            <span className="text-muted-foreground">Not reported</span>
          ) : (
            formatQuantity(row.original.available_quantity)
          ),
      },
      {
        id: 'required_date',
        header: 'Required',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.required_date),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        // A short line can be covered by taking stock from a lower-priority
        // demand before a purchase or production order is raised. The action is
        // absent on a covered line: there is nothing to reallocate towards.
        id: 'actions',
        header: '',
        enableSorting: false,
        cell: ({ row }) =>
          canReallocate && toDecimal(row.original.shortfall_quantity).greaterThan(0) ? (
            <Button variant="outline" size="sm" onClick={() => setReallocationTarget(row.original)}>
              <ArrowLeftRight />
              Request reallocation
            </Button>
          ) : null,
      },
    ],
    [canReallocate],
  )

  // Offered only where the top-up can act (its own status rule) and only while a
  // line is actually short - on a covered order it would have nothing to do.
  const offerTopUp = canTopUp(order.status) && shortLines.length > 0 && can(P.SALES_ORDER_UPDATE)

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
        <CardTitle>Allocation</CardTitle>
        {allocation.data && (
          <Badge variant={allocation.data.fully_allocated ? 'success' : 'warning'}>
            {allocation.data.fully_allocated
              ? 'Fully allocated'
              : `${allocation.data.total_shortfall_lines} line${
                  allocation.data.total_shortfall_lines === 1 ? '' : 's'
                } short`}
          </Badge>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {shortLines.length > 0 && (
          <Alert variant="warning">
            <AlertTitle>
              {shortLines.length} line{shortLines.length === 1 ? '' : 's'} not fully covered by
              stock
            </AlertTitle>
            <AlertDescription className="space-y-3">
              <p>
                What is on hand has been reserved for this order, and what is reserved may ship now.
                The balance waits for stock: stock that arrives is reserved for it automatically
                every few minutes, or straight away with {fulfilment.allocateAction}. MRP nets the
                shortfall against incoming purchase orders and production, then suggests what to
                raise.
              </p>
              <div className="flex flex-wrap gap-2">
                {offerTopUp && (
                  <Button size="sm" onClick={() => allocate.mutate()} loading={allocate.isPending}>
                    <PackageCheck />
                    {fulfilment.allocateAction}
                  </Button>
                )}
                <Button variant="outline" size="sm" asChild>
                  <Link to="/mrp">Run MRP to plan the balance</Link>
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        <DataTable
          columns={columns}
          data={lines}
          isLoading={allocation.isLoading}
          isFetching={allocation.isFetching}
          error={allocation.error as Error | null}
          onRefresh={() => void allocation.refetch()}
          getRowId={(row) => row.line_id}
          emptyTitle="Nothing reserved yet"
          emptyDescription="No reservation exists against these lines."
        />

        {reallocationTarget && (
          <ReallocationRequestDialog
            demand={{
              itemId: reallocationTarget.item_id,
              itemCode: reallocationTarget.item_code,
              warehouseId: order.warehouse_id,
              demandType: 'SALES_ORDER',
              demandId: order.id,
              demandLineId: reallocationTarget.line_id,
              demandCode: order.code,
              priority: order.priority,
              requiredDate: reallocationTarget.required_date,
              shortfallQuantity: reallocationTarget.shortfall_quantity,
            }}
            onClose={() => setReallocationTarget(null)}
          />
        )}
      </CardContent>
    </Card>
  )
}

// --------------------------------------------------------------- totals -----

function TotalsCard({ order }: { order: SalesOrderDetail }) {
  const currency = order.currency

  // Exactly the four figures the API calculates on the header. The tax split
  // into CGST/SGST/IGST is a per-line fact and is shown on the lines, because
  // re-adding line taxes here would put a second, disagreeing total on screen.
  const rows: { label: string; value: string; strong?: boolean }[] = [
    { label: 'Subtotal', value: formatMoney(order.subtotal, { currency }) },
    { label: 'Discount', value: `- ${formatMoney(order.discount_amount, { currency })}` },
    {
      label: order.is_interstate ? 'Tax (IGST)' : 'Tax (CGST + SGST)',
      value: formatMoney(order.tax_amount, { currency }),
    },
    {
      label: 'Grand total',
      value: formatMoney(order.total_amount, { currency }),
      strong: true,
    },
  ]

  return (
    <Card>
      <CardHeader>
        <CardTitle>Totals</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="space-y-2 text-sm">
          {rows.map((row) => (
            <div
              key={row.label}
              className={
                row.strong
                  ? 'flex items-center justify-between gap-4 border-t pt-2 text-base font-semibold'
                  : 'flex items-center justify-between gap-4'
              }
            >
              <dt className={row.strong ? undefined : 'text-muted-foreground'}>{row.label}</dt>
              <dd className="tabular">{row.value}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-4 text-xs text-muted-foreground">
          Calculated by the API from the place of supply. The screen never re-adds them.
        </p>
      </CardContent>
    </Card>
  )
}

// ----------------------------------------------------------------- page -----

export default function SalesOrderDetailPage() {
  const { orderId = '' } = useParams<{ orderId: string }>()
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const [editOpen, setEditOpen] = useState(false)
  const [dispatchOpen, setDispatchOpen] = useState(false)
  const close = useMutation({
    mutationFn: () =>
      api.post(`/sales/${orderId}/close`, { reason: 'Fully invoiced order closed from detail' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success('Sales order closed')
    },
  })
  const [lineEditor, setLineEditor] = useState<{ index: number | null } | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [approveOpen, setApproveOpen] = useState(false)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [cancelReasonTouched, setCancelReasonTouched] = useState(false)
  const [pendingLineDelete, setPendingLineDelete] = useState<{
    line: SalesOrderLine
    index: number
  } | null>(null)

  const orderQuery = useQuery({
    queryKey: queryKeys.sales.detail(orderId),
    queryFn: () => api.get<SalesOrderDetail>(`/sales/${orderId}`),
    staleTime: staleTimes.transactional,
    enabled: Boolean(orderId),
  })

  const order = orderQuery.data

  // The latest completed plan that saw this order, keyed by line. This is the
  // plan's *answer*, not a live stock check: "is there stock now?" is the
  // allocation panel below, and a promised date has to point at the plan it was
  // taken from. Gated on MRP_VIEW because the dates are the planner's output.
  const availability = useQuery({
    queryKey: queryKeys.mrp.salesOrderAvailability(orderId),
    queryFn: () =>
      api.get<SalesOrderAvailability>(`/mrp/sales-orders/${orderId}/expected-availability`),
    enabled: Boolean(orderId) && can(P.MRP_VIEW),
    staleTime: staleTimes.transactional,
  })
  const availabilityByLine = useMemo(() => {
    const dates = new Map<string, string>()
    for (const line of availability.data?.lines ?? []) {
      if (line.expected_available_date) {
        dates.set(line.sales_order_line_id, line.expected_available_date)
      }
    }
    return dates
  }, [availability.data])

  // One customer, addressed by its id - not a per-row lookup. The same query key
  // the customer picker uses, so the record is fetched at most once.
  const customer = useQuery({
    queryKey: queryKeys.customers.detail(order?.customer_id ?? ''),
    queryFn: () => api.get<CustomerDetail>(`/customers/${order?.customer_id}`),
    enabled: Boolean(order?.customer_id) && can(P.CUSTOMER_VIEW),
    staleTime: staleTimes.master,
  })

  // Warehouses are a bounded reference list, shared with every warehouse select.
  const warehouses = useQuery({
    // The one spelling of the lookup, shared with `WarehouseSelect`, so the
    // two read one cache entry rather than fetching the same list twice.
    queryKey: queryKeys.warehouses.list(WAREHOUSE_LOOKUP_PARAMS),
    queryFn: () => api.getPage<WarehouseOption>('/warehouses', { params: WAREHOUSE_LOOKUP_PARAMS }),
    enabled: can(P.WAREHOUSE_VIEW),
    staleTime: staleTimes.master,
  })
  const warehouseLabel =
    warehouses.data?.data.find((row) => row.id === order?.warehouse_id)?.code ??
    order?.warehouse_id ??
    '-'

  /** Confirming reserves stock, so every reservation-shaped cache is dropped. */
  const invalidateOrderAndStock = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
  }

  const confirmOrder = useMutation({
    mutationFn: () =>
      api.post<ConfirmResult>(`/sales/${orderId}/confirm`, {
        reason: 'Order confirmed by sales',
      }),
    onSuccess: (result) => {
      invalidateOrderAndStock()
      toast.success(
        result.allocation.fully_allocated
          ? 'Order confirmed and fully allocated'
          : success.confirmed('Sales order'),
        result.allocation.fully_allocated
          ? `${result.order.code} has stock reserved against every line.`
          : `${result.order.code} is confirmed. ${result.allocation.total_shortfall_lines} line(s) are short and need planning.`,
      )
      setConfirmOpen(false)
    },
    onError: (error) => reportFieldErrors(error, 'Could not confirm this order'),
  })

  const approveOrder = useMutation({
    mutationFn: () =>
      api.post<SalesOrderRow>(`/sales/${orderId}/approve`, {
        reason: 'Pricing approved',
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success(success.approved('Sales order'), `${updated.code} may now be confirmed.`)
      setApproveOpen(false)
    },
    onError: (error) => reportFieldErrors(error, 'Could not approve this order'),
  })

  const cancelOrder = useMutation({
    mutationFn: (reason: string) => api.post<SalesOrderRow>(`/sales/${orderId}/cancel`, { reason }),
    onSuccess: (updated) => {
      invalidateOrderAndStock()
      toast.success(
        success.cancelled('Sales order'),
        `${updated.code} is cancelled and its reservations released.`,
      )
      setCancelOpen(false)
      setCancelReason('')
      setCancelReasonTouched(false)
    },
    onError: (error) => reportFieldErrors(error, 'Could not cancel this order'),
  })

  /**
   * A line change is a whole-set change.
   *
   * The set is rebuilt from what is on screen, narrowed to the input shape and
   * renumbered, then sent as one patch - the API re-prices and re-reserves it.
   */
  const saveLines = useMutation({
    mutationFn: (lines: SalesOrderLineIn[]) =>
      api.patch<SalesOrderDetail>(`/sales/${orderId}`, { lines: renumber(lines) }),
    onSuccess: () => {
      invalidateOrderAndStock()
      toast.success(success.saved('Lines'), 'The order totals have been recalculated by the API.')
      setLineEditor(null)
      setPendingLineDelete(null)
    },
    onError: (error) => reportFieldErrors(error, 'Could not save these lines'),
  })

  /**
   * Make one line's plan date the order's promise.
   *
   * `promised_date` is a header field, so this is the normal PATCH. The API
   * snapshots it in `ORDER_AUDITED_FIELDS` and refuses the change once the order
   * has left the reserving statuses, which is why the action is only offered
   * where `canTopUp` is true. On a multi-line order the promise is still one
   * date; the line chosen is the one the planner is willing to stand behind.
   */
  const promiseDate = useMutation({
    mutationFn: (date: string) =>
      api.patch<SalesOrderDetail>(`/sales/${orderId}`, { promised_date: date }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success(
        success.saved('Promised date'),
        `${updated.code} now promises ${formatDate(updated.promised_date)}.`,
      )
    },
    onError: (error) => reportFieldErrors(error, 'Could not update the promised date'),
  })

  const canEditLines = Boolean(order && isDraft(order.status) && can(P.SALES_ORDER_UPDATE))
  const orderPromisedDate = order?.promised_date ?? null
  // The column is a plan fact: a draft has no reservations and normally no plan,
  // and a dispatched or closed order can no longer take the action. Readers
  // without MRP_VIEW are not shown dates from a module they cannot open.
  const showAvailability = Boolean(order && !isDraft(order.status) && can(P.MRP_VIEW))
  const canPromise = Boolean(order && canTopUp(order.status) && can(P.SALES_ORDER_UPDATE))
  const { mutate: promiseDateMutate, isPending: promiseDatePending } = promiseDate

  const lineColumns = useMemo<ColumnDef<SalesOrderLine, unknown>[]>(() => {
    const defs: ColumnDef<SalesOrderLine, unknown>[] = [
      {
        id: 'line_number',
        header: '#',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.line_number}</span>,
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        // The line read carries an item id and the description that was keyed
        // against it - no code and no name - so the description is the label and
        // the id is the link.
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <Link
              to={`/items/${row.original.item_id}`}
              className="block truncate font-medium hover:underline"
            >
              {row.original.description ?? 'Open item'}
            </Link>
            <span className="block truncate font-mono text-xs text-muted-foreground">
              {row.original.item_id}
            </span>
          </div>
        ),
      },
      {
        id: 'quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.quantity),
      },
      {
        id: 'unit_price',
        header: 'Unit price',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.unit_price),
      },
      {
        id: 'discount',
        header: 'Discount',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span>
            {formatMoney(row.original.discount_amount)}
            <span className="ml-1 text-xs text-muted-foreground">
              {formatPercent(row.original.discount_percentage, 2)}
            </span>
          </span>
        ),
      },
      {
        id: 'tax',
        header: 'Tax',
        enableSorting: false,
        meta: { align: 'right' },
        // The split the API decided from the place of supply, shown where it is
        // actually computed rather than summed into a header figure.
        cell: ({ row }) => (
          <span>
            {formatMoney(row.original.tax_amount)}
            <span className="ml-1 text-xs text-muted-foreground">
              {formatPercent(row.original.tax_rate, 2)}
            </span>
            <span className="block text-xs text-muted-foreground">
              {toDecimal(row.original.igst_amount).greaterThan(0)
                ? `IGST ${formatMoney(row.original.igst_amount)}`
                : `CGST ${formatMoney(row.original.cgst_amount)} + SGST ${formatMoney(row.original.sgst_amount)}`}
            </span>
          </span>
        ),
      },
      {
        id: 'line_total',
        header: 'Line total',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatMoney(row.original.line_total)}</span>
        ),
      },
      {
        id: 'allocated_quantity',
        header: 'Allocated',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.allocated_quantity),
      },
      {
        id: 'dispatched_quantity',
        header: 'Dispatched',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.dispatched_quantity),
      },
      {
        id: 'invoiced_quantity',
        header: 'Invoiced',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.invoiced_quantity),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ]

    if (showAvailability) {
      defs.push({
        id: 'expected_availability',
        header: 'Expected availability',
        enableSorting: false,
        cell: ({ row }) => {
          const when = availabilityByLine.get(row.original.id)
          if (!when) {
            return (
              <span
                className="text-xs text-muted-foreground"
                title="No completed plan has covered this order yet."
              >
                Not planned
              </span>
            )
          }
          const promised = orderPromisedDate === when
          // A promise earlier than what the plan can do is the gap a planner
          // has to close; showing it as a warning is the whole point of putting
          // the two dates side by side.
          const atRisk = Boolean(orderPromisedDate && when > orderPromisedDate)
          return (
            <div className="flex flex-col items-start gap-1">
              <Badge
                variant={promised ? 'success' : atRisk ? 'warning' : 'muted'}
                title={
                  promised
                    ? 'This line matches the order’s promised date.'
                    : 'When the latest completed plan expects this line to be covered.'
                }
              >
                {formatDate(when)}
              </Badge>
              {canPromise && !promised && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-auto px-0 text-xs"
                  disabled={promiseDatePending}
                  onClick={() => promiseDateMutate(when)}
                  title="Set the order's promised date to this line's expected availability"
                >
                  Use as promised date
                </Button>
              )}
            </div>
          )
        },
      })
    }

    // Gating is UX only - the API refuses a line change it does not allow.
    if (canEditLines) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <div className="flex items-center justify-end gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Edit line ${row.original.line_number}`}
              onClick={() => setLineEditor({ index: row.index })}
            >
              <Pencil />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Remove line ${row.original.line_number}`}
              onClick={() => setPendingLineDelete({ line: row.original, index: row.index })}
            >
              <Trash2 className="text-destructive" />
            </Button>
          </div>
        ),
      })
    }

    return defs
  }, [
    canEditLines,
    showAvailability,
    availabilityByLine,
    orderPromisedDate,
    canPromise,
    promiseDateMutate,
    promiseDatePending,
  ])

  if (orderQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <div className="grid gap-4 lg:grid-cols-3">
          <Skeleton className="h-64 lg:col-span-2" />
          <Skeleton className="h-64" />
        </div>
        <Skeleton className="h-72 w-full" />
      </div>
    )
  }

  if (orderQuery.error) {
    return (
      <>
        <PageHeader
          title="Sales order"
          breadcrumbs={[{ label: 'Sales orders', to: '/sales-orders' }]}
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
          title="Sales order"
          breadcrumbs={[{ label: 'Sales orders', to: '/sales-orders' }]}
        />
        <EmptyState
          title="Order not found"
          description="It may have been removed, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/sales-orders">Back to sales orders</Link>
            </Button>
          }
        />
      </>
    )
  }

  const draft = isDraft(order.status)
  const awaitingApproval = order.requires_approval && order.approved_at === null
  // `CancelRequest.reason` is bounded 3..255 by the API. A one-character reason
  // used to pass this form and come back as a 422 against `reason` - a field
  // path the mutation cache stays quiet about, on the assumption the form is
  // showing it inline, which it was not. Nothing appeared on screen at all.
  const isCancelReasonValid = cancelReason.trim().length >= REASON_MIN_LENGTH
  const editingLine =
    lineEditor && lineEditor.index !== null ? (order.lines[lineEditor.index] ?? null) : null

  /** Rebuild the whole set with one line replaced, appended or dropped. */
  const applyLineChange = (change: (lines: SalesOrderLineIn[]) => SalesOrderLineIn[]) =>
    saveLines.mutate(change(order.lines.map(toLineInput)))

  return (
    <>
      <PageHeader
        title={order.code}
        description={customer.data ? `${customer.data.name} (${customer.data.code})` : undefined}
        breadcrumbs={[{ label: 'Sales orders', to: '/sales-orders' }, { label: order.code }]}
        status={
          <span className="flex items-center gap-2">
            <StatusBadge status={order.status} />
            <Badge variant="muted">Priority {order.priority}</Badge>
            {order.is_urgent && <Badge variant="destructive">Urgent</Badge>}
          </span>
        }
        actions={
          <>
            {order.status === 'INVOICED' && can(P.SALES_ORDER_UPDATE) && (
              <Button variant="outline" loading={close.isPending} onClick={() => close.mutate()}>
                Close order
              </Button>
            )}
            {/*
              `dispatch.create`, the right `POST /dispatch` declares. Offered on
              every status a consignment may leave against - a CONFIRMED order
              ships what it has reserved - and the dialog loads only the lines
              that hold a live reservation.
            */}
            {isDispatchable(order.status) && can(P.DISPATCH_CREATE) && (
              <Button onClick={() => setDispatchOpen(true)}>
                <Truck />
                Dispatch
              </Button>
            )}
            {draft && can(P.SALES_ORDER_UPDATE) && (
              <Button variant="outline" onClick={() => setEditOpen(true)}>
                <Pencil />
                Edit
              </Button>
            )}
            {canEditLines && (
              <Button variant="outline" onClick={() => setLineEditor({ index: null })}>
                <ListPlus />
                Add line
              </Button>
            )}
            {awaitingApproval && can(P.SALES_ORDER_APPROVE) && (
              <Button variant="outline" onClick={() => setApproveOpen(true)}>
                <ShieldCheck />
                Approve pricing
              </Button>
            )}
            {isCancellable(order.status) && can(P.SALES_ORDER_CANCEL) && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={() => setCancelOpen(true)}
              >
                <Ban />
                Cancel order
              </Button>
            )}
            {/*
              `sales_order.update`, not `.approve`: POST /sales/{id}/confirm is
              gated on `Permission.SALES_ORDER_UPDATE` (sales/router.py). Asking
              for `.approve` here both hid Confirm from everyone who may do it
              and offered it to approvers who would be refused - the button and
              its endpoint have to name the same right. Approving is the
              separate action above, which does use `.approve`.
            */}
            {isConfirmable(order.status) && can(P.SALES_ORDER_UPDATE) && (
              <Button onClick={() => setConfirmOpen(true)} disabled={awaitingApproval}>
                <CheckCircle2 />
                Confirm
              </Button>
            )}
          </>
        }
      />

      <div className="space-y-4">
        {awaitingApproval && (
          <Alert variant="warning">
            <AlertTitle>This order is waiting for a pricing approval</AlertTitle>
            <AlertDescription>
              A line was priced below the item&apos;s cost, or discounted above the company
              threshold. It cannot be confirmed until somebody with the approval permission signs
              for it, and that may not be the person who raised it.
            </AlertDescription>
          </Alert>
        )}

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
                    label: 'Customer',
                    value: customer.data ? (
                      <Link to="/customers" className="hover:underline">
                        {customer.data.name}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs">{order.customer_id}</span>
                    ),
                  },
                  { label: 'Customer code', value: customer.data?.code ?? '-' },
                  { label: 'GSTIN', value: customer.data?.gstin ?? 'Unregistered' },
                  { label: 'Order date', value: formatDate(order.order_date) },
                  { label: 'Required date', value: formatDate(order.required_date) },
                  { label: 'Promised date', value: formatDate(order.promised_date) },
                  { label: 'Customer PO', value: order.customer_po_number ?? '-' },
                  { label: 'Dispatch from', value: warehouseLabel },
                  { label: 'Currency', value: order.currency },
                  {
                    label: 'Place of supply',
                    value: order.place_of_supply_state_code ?? '-',
                  },
                  {
                    label: 'Supply type',
                    value: order.is_interstate ? 'Inter-state' : 'Intra-state',
                  },
                  {
                    label: 'Pricing approval',
                    value: order.requires_approval
                      ? order.approved_at
                        ? `Approved ${formatDateTime(order.approved_at)}`
                        : 'Outstanding'
                      : 'Not required',
                  },
                  { label: 'Created', value: formatDateTime(order.created_at) },
                  { label: 'Last updated', value: formatDateTime(order.updated_at) },
                ]}
              />
              {order.cancellation_reason && (
                <p className="mt-5 border-t pt-4 text-sm text-destructive">
                  Cancelled: {order.cancellation_reason}
                </p>
              )}
              {order.notes && (
                <p className="mt-5 border-t pt-4 text-sm text-muted-foreground">{order.notes}</p>
              )}
            </CardContent>
          </Card>

          <TotalsCard order={order} />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Lines</CardTitle>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={lineColumns}
              data={order.lines}
              isFetching={orderQuery.isFetching || saveLines.isPending}
              getRowId={(row) => row.id}
              emptyTitle="No lines"
              emptyDescription="This order carries no lines."
              emptyAction={
                canEditLines ? (
                  <Button size="sm" onClick={() => setLineEditor({ index: null })}>
                    <ListPlus />
                    Add line
                  </Button>
                ) : undefined
              }
            />
          </CardContent>
        </Card>

        {draft ? (
          <Alert variant="info">
            <AlertTitle>Allocation starts when the order is confirmed</AlertTitle>
            <AlertDescription>
              Confirming reserves what is available against each line. Whatever is not covered is
              reported here as a shortfall for MRP to plan.
            </AlertDescription>
          </Alert>
        ) : (
          // `GET /sales/{id}/allocation-status` is declared on
          // `sales_order.view` (sales/router.py), the same right that opens
          // this page - not on `allocation.view`, which guards the reservation
          // screens. Gating the panel on the latter refused a reader the API
          // would have answered, and told them their role lacked a right the
          // endpoint never asks for.
          <AllocationPanel order={order} />
        )}

        {!draft && <FulfilmentLinks orderId={order.id} />}
      </div>

      {can(P.SALES_ORDER_UPDATE) && draft && (
        <SalesOrderFormDialog open={editOpen} onOpenChange={setEditOpen} order={order} />
      )}

      {dispatchOpen && can(P.DISPATCH_CREATE) && (
        <Suspense fallback={null}>
          <CreateDispatchDialog open onOpenChange={setDispatchOpen} order={order} />
        </Suspense>
      )}

      {canEditLines && lineEditor !== null && (
        <SalesOrderLineDialog
          open
          onOpenChange={(next) => !next && setLineEditor(null)}
          line={editingLine ? toLineInput(editingLine) : null}
          lineNumber={lineEditor.index === null ? order.lines.length + 1 : lineEditor.index + 1}
          currency={order.currency}
          onSubmit={(line) =>
            applyLineChange((current) =>
              lineEditor.index === null
                ? [...current, line]
                : current.map((existing, at) => (at === lineEditor.index ? line : existing)),
            )
          }
        />
      )}

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Confirm ${order.code}?`}
        description="Stock is reserved against each line as far as it is available, and the order becomes visible to MRP as firm demand. Partial coverage is expected - the balance is planned afterwards."
        confirmLabel="Confirm order"
        loading={confirmOrder.isPending}
        onConfirm={() => confirmOrder.mutate()}
      />

      <ConfirmDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        title={`Approve the pricing on ${order.code}?`}
        description="A line is priced below the item's cost or discounted above the approval threshold. Approving it records who accepted that and releases the order to be confirmed."
        confirmLabel="Approve pricing"
        loading={approveOrder.isPending}
        onConfirm={() => approveOrder.mutate()}
      />

      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={(open) => {
          setCancelOpen(open)
          if (!open) setCancelReasonTouched(false)
        }}
        title={`Cancel ${order.code}?`}
        description="Reservations held for this order are released back to free stock. A cancelled order cannot be reopened."
        confirmLabel="Cancel order"
        cancelLabel="Keep order"
        destructive
        loading={cancelOrder.isPending}
        onConfirm={() => {
          if (!isCancelReasonValid) {
            setCancelReasonTouched(true)
            return
          }
          cancelOrder.mutate(cancelReason.trim())
        }}
      >
        <div>
          <Label htmlFor="cancel-reason" required>
            Reason
          </Label>
          <Input
            id="cancel-reason"
            value={cancelReason}
            onChange={(event) => setCancelReason(event.target.value)}
            autoComplete="off"
            maxLength={REASON_MAX_LENGTH}
            className="mt-1.5"
            aria-invalid={cancelReasonTouched && !isCancelReasonValid}
          />
          {cancelReasonTouched && !isCancelReasonValid && (
            <FieldError
              message={
                cancelReason.trim() === ''
                  ? 'A cancellation needs a recorded reason'
                  : `The reason is recorded in the audit trail and needs at least ${REASON_MIN_LENGTH} characters`
              }
            />
          )}
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={Boolean(pendingLineDelete)}
        onOpenChange={(open) => !open && setPendingLineDelete(null)}
        title="Remove this line?"
        description={
          pendingLineDelete
            ? `Line ${pendingLineDelete.line.line_number} will be removed, the remaining lines renumbered, and the order totals recalculated by the API.`
            : undefined
        }
        confirmLabel="Remove line"
        destructive
        loading={saveLines.isPending}
        onConfirm={() =>
          pendingLineDelete &&
          applyLineChange((current) => current.filter((_, at) => at !== pendingLineDelete.index))
        }
      />
    </>
  )
}

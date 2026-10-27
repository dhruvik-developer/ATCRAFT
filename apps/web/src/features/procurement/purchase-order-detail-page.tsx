/**
 * Purchase order detail.
 *
 * Three things carry this screen.
 *
 * The stepper puts the document somewhere in a ten-stage life, so "what happens
 * next" needs no explanation. The actions are derived from the current status
 * rather than shown and disabled - the state machine in docs/database.md
 * section 7 says an approved order cannot be approved again and a received one
 * cannot be cancelled, so those buttons are simply not there. And the tracking
 * panel answers the question the buyer actually rang up about: where is it, is
 * it late, and what is holding it at the gate.
 *
 * The order carries foreign keys, not names: `vendor_id`, `warehouse_id` and a
 * `uom_id` per line. Each is resolved through the bounded reference lookup its
 * module publishes. The one exception is the item, which has no bulk lookup -
 * one request per line would be the N+1 master rules forbid - so a line is
 * labelled by the description carried on the order and links out to the item.
 * The item category and the vendor user the order is assigned to are named by
 * the API itself, because the buyer reading this page usually holds neither
 * `category.view` nor `user.view`.
 *
 * Send is its own dialog rather than a confirmation: it names the person at the
 * vendor who owns the order, from the users the send endpoint will accept.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  Ban,
  Check,
  CheckCircle2,
  Handshake,
  Pencil,
  RefreshCw,
  Send,
  ShieldCheck,
  Truck,
  XCircle,
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
  PanelLoader,
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
  Checkbox,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useUomOptions } from '@/features/items/item-model'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import {
  daysUntil,
  formatDate,
  formatDateTime,
  formatMoney,
  formatPercent,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { describeError, messages, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn, indexById } from '@/lib/utils'

import {
  OPEN_PO_STATUSES,
  PO_LIFECYCLE,
  PO_TERMINAL_BAD,
  isPoAcknowledgeable,
  isPoCancellable,
  isPoDecidable,
  isPoSendable,
  isPoSubmittable,
  isUrgentPriority,
  pendingQuantity,
  poStageIndex,
  type PurchaseOrderDetail,
  type PurchaseOrderLine,
  type PurchaseOrderRow,
  type PurchaseOrderTracking,
} from './procurement-types'
import { PurchaseOrderSendDialog } from './purchase-order-send-dialog'
import { PurchaseOrderInboundPanel } from './purchase-order-inbound-panel'
import { serverFieldMessage } from './server-field-errors'
import {
  MAX_DUPLICATE_REASON_LENGTH,
  invalidatePurchaseOrderChanges,
  isPoAmendable,
} from './purchase-order-change-model'

const PurchaseOrderAmendDialog = lazy(() => import('./purchase-order-amend-dialog'))
const PurchaseOrderAmendmentsPanel = lazy(() => import('./purchase-order-amendments-panel'))

// -------------------------------------------------------------- stepper -----

function StatusStepper({ status }: { status: string }) {
  const stopped = (PO_TERMINAL_BAD as readonly string[]).includes(status)
  const current = poStageIndex(status)

  return (
    <ol
      className="flex flex-wrap items-center gap-x-1 gap-y-2"
      aria-label="Purchase order lifecycle"
    >
      {PO_LIFECYCLE.map((stage, index) => {
        const done = !stopped && current > index
        const active = !stopped && current === index
        return (
          <li key={stage.label} className="flex items-center gap-1">
            <span
              // aria-current is what tells a screen-reader user where the
              // document actually is; colour alone would not.
              aria-current={active ? 'step' : undefined}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
                done && 'border-success/40 bg-success/10 text-success',
                active && 'border-primary bg-primary/10 text-primary',
                !done && !active && 'border-border text-muted-foreground',
              )}
            >
              {done && <Check className="h-3 w-3" aria-hidden />}
              {stage.label}
            </span>
            {index < PO_LIFECYCLE.length - 1 && (
              <span className={cn('h-px w-4', done ? 'bg-success/50' : 'bg-border')} aria-hidden />
            )}
          </li>
        )
      })}
    </ol>
  )
}

// ------------------------------------------------------------- tracking -----

/**
 * Inbound tracking for one order.
 *
 * Declared once and called from both the header and the panel: TanStack keys
 * the two calls identically, so it is one request, and the vendor's name comes
 * from the endpoint that actually resolves it rather than from a picker list
 * that a suspended vendor would have dropped out of.
 */
function usePurchaseOrderTracking(orderId: string) {
  return useQuery({
    queryKey: queryKeys.procurement.tracking(orderId),
    queryFn: () =>
      api.get<PurchaseOrderTracking>(`/procurement/purchase-orders/${orderId}/tracking`),
    // A consignment moves while the tab is open; a cached position is worse
    // than none for the person chasing it.
    staleTime: staleTimes.transactional,
    enabled: Boolean(orderId),
  })
}

/**
 * Where the consignment is, and what is holding it.
 *
 * `PurchaseOrderTracking` is a quantity view as much as a courier view: it is
 * the only endpoint on this screen that states how much of the order has
 * actually landed, so the receipt progress lives here rather than being derived
 * from a percentage the order payload does not carry.
 */
function TrackingPanel({ orderId }: { orderId: string }) {
  const tracking = usePurchaseOrderTracking(orderId)

  const data = tracking.data
  const dispatched =
    data && Boolean(data.courier_name || data.tracking_reference || data.shipped_at || data.sent_at)

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>Tracking</CardTitle>
        <div className="flex items-center gap-2">
          {data?.is_delayed && (
            <Badge variant="destructive">
              {data.days_late > 0 ? `${data.days_late}d late` : 'Delayed'}
            </Badge>
          )}
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Refresh tracking"
            onClick={() => void tracking.refetch()}
          >
            <RefreshCw className={cn(tracking.isFetching && 'animate-spin')} />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {tracking.isLoading && (
          <div className="space-y-3" aria-hidden>
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-4 w-3/4" />
          </div>
        )}

        {!tracking.isLoading && tracking.error && (
          <Alert variant="destructive">
            <AlertTitle>Could not load tracking</AlertTitle>
            <AlertDescription>{describeError(tracking.error)}</AlertDescription>
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => void tracking.refetch()}
            >
              <RefreshCw />
              Try again
            </Button>
          </Alert>
        )}

        {!tracking.isLoading && !tracking.error && data && (
          <>
            <DescriptionList
              columns={2}
              items={[
                { label: 'Vendor', value: data.vendor_name },
                { label: 'Courier', value: data.courier_name ?? '-' },
                {
                  label: 'Consignment reference',
                  value: data.tracking_reference ? (
                    <span className="tabular">{data.tracking_reference}</span>
                  ) : (
                    '-'
                  ),
                },
                { label: 'Sent to vendor', value: formatDateTime(data.sent_at) },
                { label: 'Vendor acknowledged', value: formatDateTime(data.acknowledged_at) },
                { label: 'Dispatched', value: formatDateTime(data.shipped_at) },
                { label: 'Expected delivery', value: formatDate(data.expected_delivery_date) },
              ]}
            />

            <div className="border-t pt-4">
              <DescriptionList
                columns={3}
                items={[
                  { label: 'Ordered', value: formatQuantity(data.ordered_quantity) },
                  { label: 'Received', value: formatQuantity(data.received_quantity) },
                  { label: 'Still to come', value: formatQuantity(data.pending_quantity) },
                  {
                    label: 'Accepted',
                    value: (
                      <span className="text-success">{formatQuantity(data.accepted_quantity)}</span>
                    ),
                  },
                  {
                    label: 'Rejected',
                    value: toDecimal(data.rejected_quantity).isZero() ? (
                      '-'
                    ) : (
                      <span className="font-medium text-destructive">
                        {formatQuantity(data.rejected_quantity)}
                      </span>
                    ),
                  },
                  {
                    label: 'Lines held for QC',
                    value: data.qc_pending_line_count > 0 ? data.qc_pending_line_count : '-',
                  },
                ]}
              />
            </div>

            <div className="flex flex-wrap items-center gap-2 border-t pt-4">
              {data.qc_pending && <Badge variant="warning">QC pending</Badge>}
              {data.grn_pending && <Badge variant="warning">Delivery still expected</Badge>}
              {!data.qc_pending && !data.grn_pending && (
                <Badge variant="success">Nothing held at the gate</Badge>
              )}
              {!dispatched && (
                <span className="text-xs text-muted-foreground">
                  Nothing dispatched yet - courier and consignment details appear once the vendor
                  ships.
                </span>
              )}
            </div>

            {data.receipts.length > 0 ? (
              <div className="border-t pt-4">
                <h3 className="text-sm font-medium">Receipts booked against this order</h3>
                <ul className="mt-2 divide-y rounded-lg border">
                  {data.receipts.map((receipt) => (
                    <li
                      key={receipt.id}
                      className="flex flex-wrap items-center justify-between gap-2 p-2.5 text-sm"
                    >
                      <span className="flex items-center gap-2">
                        <span className="font-medium">{receipt.code}</span>
                        <StatusBadge status={receipt.status} />
                        <span className="text-xs text-muted-foreground">
                          {formatDate(receipt.receipt_date)}
                        </span>
                      </span>
                      <span className="tabular text-xs text-muted-foreground">
                        {formatQuantity(receipt.total_received_quantity)} received,{' '}
                        {formatQuantity(receipt.total_accepted_quantity)} accepted
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <EmptyState
                icon={Truck}
                title="Nothing received yet"
                description="No goods receipt has been booked against this order yet."
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

// --------------------------------------------------------------- totals -----

/**
 * The order value as the API totalled it.
 *
 * Five figures, which is what `PurchaseOrderSummary` actually carries: subtotal,
 * discount, tax, freight and the total. The GST split (CGST/SGST/IGST) is not on
 * this document - it belongs to the vendor invoice in finance - so it is not
 * shown here rather than shown as three zeroes.
 */
function TotalsCard({ order }: { order: PurchaseOrderDetail }) {
  const currency = order.currency
  const rows: { label: string; value: string; strong?: boolean }[] = [
    { label: 'Subtotal', value: formatMoney(order.subtotal, { currency }) },
  ]

  if (!toDecimal(order.discount).isZero()) {
    rows.push({ label: 'Discount', value: `- ${formatMoney(order.discount, { currency })}` })
  }

  rows.push({ label: 'Tax', value: formatMoney(order.tax_amount, { currency }) })

  if (!toDecimal(order.freight_amount).isZero()) {
    rows.push({ label: 'Freight', value: formatMoney(order.freight_amount, { currency }) })
  }

  rows.push({
    label: 'Order value',
    value: formatMoney(order.total_amount, { currency }),
    strong: true,
  })

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
          Calculated by the API. The screen never re-adds them.
        </p>
      </CardContent>
    </Card>
  )
}

// ----------------------------------------------------------------- page -----

/**
 * The commands this screen confirms with a note, and how each one is presented.
 * Send is not among them: it chooses the vendor's assignee, in its own dialog.
 */
type Command = 'submit' | 'approve' | 'reject' | 'acknowledge' | 'cancel'

const COMMAND_COPY: Record<
  Command,
  {
    title: string
    description: string
    confirmLabel: string
    destructive: boolean
    reasonRequired: boolean
    success: string
  }
> = {
  submit: {
    title: 'Submit for approval?',
    description: 'The order stops being editable and goes to whoever holds the approval right.',
    confirmLabel: 'Submit',
    destructive: false,
    reasonRequired: false,
    success: success.submitted('Purchase order'),
  },
  approve: {
    title: 'Approve this order?',
    description:
      'An approved order can be sent to the vendor. The approval is recorded against your account.',
    confirmLabel: 'Approve order',
    destructive: false,
    reasonRequired: false,
    success: success.approved('Purchase order'),
  },
  reject: {
    title: 'Reject this order?',
    description:
      'The order is closed and nothing is bought against it. The buyer sees the reason you record here.',
    confirmLabel: 'Reject order',
    destructive: true,
    reasonRequired: true,
    success: success.rejected('Purchase order'),
  },
  acknowledge: {
    title: 'Record the vendor acknowledgement?',
    description: 'Confirms the vendor has accepted the order as issued.',
    confirmLabel: 'Record acknowledgement',
    destructive: false,
    reasonRequired: false,
    success: success.statusChanged('Purchase order', 'acknowledged'),
  },
  cancel: {
    title: 'Cancel this order?',
    description:
      'Nothing further can be received against it, and the incoming quantity disappears from MRP. A cancelled order cannot be reopened.',
    confirmLabel: 'Cancel order',
    destructive: true,
    reasonRequired: true,
    success: success.cancelled('Purchase order'),
  },
}

export default function PurchaseOrderDetailPage() {
  const { poId = '' } = useParams<{ poId: string }>()
  const { can, user } = useAuth()
  const queryClient = useQueryClient()

  const [command, setCommand] = useState<Command | null>(null)
  const [sending, setSending] = useState(false)
  const [amending, setAmending] = useState(false)
  const [confirmDuplicate, setConfirmDuplicate] = useState(false)
  const [confirmationError, setConfirmationError] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  // What the API said about the reason, if it refused it. Cleared the moment
  // the text changes so a stale verdict does not outlive the edit.
  const [serverReason, setServerReason] = useState<string | null>(null)

  const resetCommand = () => {
    setCommand(null)
    setReason('')
    setReasonTouched(false)
    setServerReason(null)
    setConfirmDuplicate(false)
    setConfirmationError(null)
  }

  const orderQuery = useQuery({
    queryKey: queryKeys.procurement.purchaseOrder(poId),
    queryFn: () => api.get<PurchaseOrderDetail>(`/procurement/purchase-orders/${poId}`),
    staleTime: staleTimes.transactional,
    enabled: Boolean(poId),
  })

  const order = orderQuery.data

  // The order states ids, not names. The vendor's comes from the tracking
  // payload - the same request the panel below makes - and the rest from the
  // bounded reference lists their modules publish for pickers.
  const tracking = usePurchaseOrderTracking(poId)
  // `||` rather than `??`: the API sends an empty name when the vendor is not
  // in its lookup, and an empty link is worse than the fallback below.
  const vendorName = tracking.data?.vendor_name || null
  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])
  const uoms = useUomOptions(can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  const currency = order?.currency

  // Every transition answers with a PurchaseOrderSummary - the header only, no
  // lines - so this is typed as the row it actually returns and the detail is
  // refetched through the invalidation below rather than read off the reply.
  const run = useMutation({
    mutationFn: ({ action, reason: text }: { action: Command; reason: string }) =>
      api.post<PurchaseOrderRow>(`/procurement/purchase-orders/${poId}/${action}`, {
        reason: text === '' ? null : text,
        ...(action === 'approve' ? { confirm_duplicate: confirmDuplicate } : {}),
      }),
    onSuccess: (updated, variables) => {
      // Approving or cancelling changes what MRP treats as incoming supply, so
      // the planning caches go with the procurement ones.
      invalidatePurchaseOrderChanges(queryClient)
      toast.success(
        COMMAND_COPY[variables.action].success,
        `${updated.code} is now ${humanise(updated.status).toLowerCase()}.`,
      )
      resetCommand()
    },
    // The global handler stays quiet on a 422 that names a field, on the
    // understanding that the form shows it. This dialog is the form.
    onError: (error) => {
      setServerReason(serverFieldMessage(error, 'reason'))
      setConfirmationError(serverFieldMessage(error, 'confirm_duplicate'))
    },
  })

  const lineColumns = useMemo<ColumnDef<PurchaseOrderLine, unknown>[]>(
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
        // The line carries `item_id` and the description recorded on the order.
        // There is no bulk item lookup, and one request per line would be the
        // N+1 the rules forbid, so the link carries the reader to the item.
        cell: ({ row }) => (
          <div className="max-w-[22rem]">
            <Link
              to={`/items/${row.original.item_id}`}
              className="block truncate font-medium hover:underline"
            >
              {row.original.description ?? 'Open item'}
            </Link>
            <div className="mt-1 flex flex-wrap gap-1">
              {row.original.requires_qc && <Badge variant="warning">QC on receipt</Badge>}
            </div>
          </div>
        ),
      },
      {
        id: 'ordered',
        header: 'Ordered',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatQuantity(row.original.quantity, uomById.get(row.original.uom_id)?.code),
      },
      {
        id: 'received',
        header: 'Received',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.received_quantity),
      },
      {
        id: 'accepted',
        header: 'Accepted',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-success">{formatQuantity(row.original.accepted_quantity)}</span>
        ),
      },
      {
        id: 'rejected',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const rejected = toDecimal(row.original.rejected_quantity)
          return rejected.isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="font-medium text-destructive">
              {formatQuantity(row.original.rejected_quantity)}
            </span>
          )
        },
      },
      {
        id: 'returned',
        header: 'Returned',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          toDecimal(row.original.returned_quantity).isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="font-medium text-destructive">
              {formatQuantity(row.original.returned_quantity)}
            </span>
          ),
      },
      {
        id: 'pending',
        header: 'Pending',
        enableSorting: false,
        meta: { align: 'right' },
        // Derived, because the API sends ordered and received but no pending.
        // decimal.js on the two strings, never a float subtraction.
        cell: ({ row }) => {
          const pending = pendingQuantity(row.original)
          return toDecimal(pending).isZero() ? (
            <span className="text-success">Complete</span>
          ) : (
            formatQuantity(pending)
          )
        },
      },
      {
        id: 'unit_price',
        header: 'Unit price',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.unit_price, { currency }),
      },
      {
        id: 'discount',
        header: 'Discount',
        enableSorting: false,
        meta: { align: 'right' },
        // The API takes either form and echoes both back; showing whichever is
        // set keeps the line reconcilable with the vendor's quotation.
        cell: ({ row }) => {
          const percentage = toDecimal(row.original.discount_percentage)
          const flat = toDecimal(row.original.discount)
          if (!percentage.isZero()) return formatPercent(row.original.discount_percentage, 2)
          if (!flat.isZero()) return formatMoney(row.original.discount, { currency })
          return <span className="text-muted-foreground">-</span>
        },
      },
      {
        id: 'tax',
        header: 'Tax',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span>
            {formatMoney(row.original.tax_amount, { currency })}
            <span className="ml-1 text-xs text-muted-foreground">
              {formatPercent(row.original.tax_rate, 2)}
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
          <span className="font-medium">{formatMoney(row.original.line_total, { currency })}</span>
        ),
      },
      {
        id: 'required',
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
    ],
    [currency, uomById],
  )

  if (orderQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <Skeleton className="h-10 w-full" />
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
          title="Purchase order"
          breadcrumbs={[{ label: 'Purchase orders', to: '/procurement/purchase-orders' }]}
        />
        <Alert variant="destructive">
          <AlertTitle>Could not load this order</AlertTitle>
          <AlertDescription>{describeError(orderQuery.error)}</AlertDescription>
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
          title="Purchase order"
          breadcrumbs={[{ label: 'Purchase orders', to: '/procurement/purchase-orders' }]}
        />
        <EmptyState
          title="Order not found"
          description="It may have been removed, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/procurement/purchase-orders">Back to purchase orders</Link>
            </Button>
          }
        />
      </>
    )
  }

  const lateDays = daysIfLate(order)
  const copy = command ? COMMAND_COPY[command] : null
  const duplicateApproval = command === 'approve' && order.duplicate_acknowledged
  const needsAnotherApprover =
    !user?.is_superuser && order.last_amended_by_id != null && order.last_amended_by_id === user?.id
  const reasonRequired = Boolean(copy?.reasonRequired) || duplicateApproval
  const maxReasonLength = command === 'approve' ? MAX_DUPLICATE_REASON_LENGTH : REASON_MAX_LENGTH
  const reasonTooShort = reason.trim().length < REASON_MIN_LENGTH
  const reasonTooLong = reason.trim().length > maxReasonLength
  const reasonInvalid = (reasonRequired && reasonTooShort) || reasonTooLong

  return (
    <>
      <PageHeader
        title={order.code}
        description={vendorName ?? undefined}
        breadcrumbs={[
          { label: 'Purchase orders', to: '/procurement/purchase-orders' },
          { label: order.code },
        ]}
        status={
          <span className="flex items-center gap-2">
            <StatusBadge status={order.status} />
            {(order.amendment_number ?? 0) > 0 && (
              <Badge variant="secondary">Rev {order.amendment_number}</Badge>
            )}
            {lateDays !== null && <Badge variant="destructive">{lateDays}d late</Badge>}
          </span>
        }
        actions={
          <>
            {/* Each button appears only where the transition is legal, and only
                for a principal who holds the right. The gate is UX: the API
                authorises every command independently. */}
            {isPoSubmittable(order.status) && can(P.PURCHASE_ORDER_UPDATE) && (
              <Button variant="outline" onClick={() => setCommand('submit')}>
                <ShieldCheck />
                Submit for approval
              </Button>
            )}
            {isPoAmendable(order) && can(P.PURCHASE_ORDER_UPDATE) && (
              <Button variant="outline" onClick={() => setAmending(true)}>
                <Pencil />
                Amend
              </Button>
            )}
            {isPoDecidable(order.status) && can(P.PURCHASE_ORDER_REJECT) && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={() => setCommand('reject')}
              >
                <XCircle />
                Reject
              </Button>
            )}
            {isPoDecidable(order.status) &&
              can(P.PURCHASE_ORDER_APPROVE) &&
              !needsAnotherApprover && (
                <Button onClick={() => setCommand('approve')}>
                  <CheckCircle2 />
                  Approve
                </Button>
              )}
            {isPoSendable(order.status) && can(P.PURCHASE_ORDER_SEND) && (
              <Button onClick={() => setSending(true)}>
                <Send />
                Send to vendor
              </Button>
            )}
            {isPoAcknowledgeable(order.status) && can(P.PURCHASE_ORDER_UPDATE) && (
              <Button onClick={() => setCommand('acknowledge')}>
                <Handshake />
                Vendor acknowledged
              </Button>
            )}
            {isPoCancellable(order.status) && can(P.PURCHASE_ORDER_CANCEL) && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={() => setCommand('cancel')}
              >
                <Ban />
                Cancel order
              </Button>
            )}
          </>
        }
      />

      <div className="space-y-4">
        <Card>
          <CardContent className="p-4">
            <StatusStepper status={order.status} />
            {isPoDecidable(order.status) && needsAnotherApprover && (
              <p className="mt-3 text-sm text-muted-foreground">
                Another approver must approve this revision.
              </p>
            )}
            {(PO_TERMINAL_BAD as readonly string[]).includes(order.status) && (
              <p className="mt-3 text-sm text-destructive">
                This order stopped at {humanise(order.status).toLowerCase()}
                {(order.rejection_reason ?? order.cancellation_reason)
                  ? `: ${order.rejection_reason ?? order.cancellation_reason}`
                  : '.'}
              </p>
            )}
          </CardContent>
        </Card>

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
                    label: 'Vendor',
                    // The name comes from the tracking request alone, so its
                    // state decides the fallback: "Loading..." only while it
                    // is actually loading, never as a permanent answer to a
                    // request that failed. There is deliberately no vendor
                    // detail route, so the link opens the list.
                    value: (
                      <Link to="/vendors" className="hover:underline" title="Open vendors">
                        {vendorName ||
                          (tracking.isLoading
                            ? 'Loading...'
                            : tracking.error
                              ? 'Not available'
                              : '-')}
                      </Link>
                    ),
                  },
                  { label: 'Vendor reference', value: order.vendor_reference ?? '-' },
                  {
                    label: 'Item category',
                    // Derived from the lines by the API: one category when every
                    // line shares it, none when they span several.
                    value: order.item_category_name ? (
                      <span>
                        {order.item_category_name}
                        {order.item_category_code && (
                          <span className="ml-1 text-xs text-muted-foreground">
                            {order.item_category_code}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">Several or none</span>
                    ),
                  },
                  {
                    label: 'Vendor assignee',
                    value: order.assigned_vendor_user_id ? (
                      (order.assigned_vendor_user_name ?? 'Assigned')
                    ) : (
                      <span className="text-muted-foreground">
                        {order.sent_at ||
                        (PO_TERMINAL_BAD as readonly string[]).includes(order.status)
                          ? 'Nobody'
                          : 'Chosen when the order is sent'}
                      </span>
                    ),
                  },
                  { label: 'PO date', value: formatDate(order.po_date) },
                  {
                    label: 'Expected delivery',
                    value: (
                      <span
                        className={lateDays !== null ? 'font-medium text-destructive' : undefined}
                      >
                        {formatDate(order.expected_delivery_date)}
                      </span>
                    ),
                  },
                  {
                    label: 'Raised from',
                    value:
                      order.source_type === 'MANUAL'
                        ? 'Raised directly'
                        : humanise(order.source_type),
                  },
                  {
                    label: 'Priority',
                    value: (
                      <span
                        className={
                          isUrgentPriority(order.priority) ? 'font-medium text-warning' : undefined
                        }
                      >
                        {order.priority} of 9
                      </span>
                    ),
                  },
                  { label: 'Payment terms', value: `${order.payment_terms_days} days` },
                  {
                    label: 'Deliver to',
                    value: warehouseById.get(order.warehouse_id)?.code ?? '-',
                  },
                  { label: 'Delivery address', value: order.delivery_address ?? '-' },
                  {
                    label: 'Approved',
                    value: order.approved_at
                      ? formatDateTime(order.approved_at)
                      : 'Not approved yet',
                  },
                  { label: 'Sent to vendor', value: formatDateTime(order.sent_at) },
                  { label: 'Created', value: formatDateTime(order.created_at) },
                ]}
              />
              {order.duplicate_acknowledged && (
                <Alert variant="warning" className="mt-5">
                  <AlertTitle>Raised despite a duplicate warning</AlertTitle>
                  <AlertDescription>
                    {order.duplicate_acknowledgement_reason ??
                      'No reason was recorded against the acknowledgement.'}
                  </AlertDescription>
                </Alert>
              )}
              {order.terms && (
                <p className="mt-5 border-t pt-4 text-sm text-muted-foreground">{order.terms}</p>
              )}
              {order.notes && <p className="mt-3 text-sm text-muted-foreground">{order.notes}</p>}
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
              // Optional on the wire, so an order that arrives without its
              // lines renders the empty state rather than crashing the table.
              data={order.lines ?? []}
              isFetching={orderQuery.isFetching}
              getRowId={(row) => row.id}
              emptyTitle="No lines on this order"
              emptyDescription="Nothing is on order against this document."
            />
          </CardContent>
        </Card>

        <TrackingPanel orderId={order.id} />
        <PurchaseOrderInboundPanel order={order} />
        {(order.amendment_number ?? 0) > 0 && (
          <Suspense fallback={<PanelLoader />}>
            <PurchaseOrderAmendmentsPanel orderId={order.id} currency={order.currency} />
          </Suspense>
        )}
      </div>

      {sending && (
        <PurchaseOrderSendDialog
          order={order}
          open
          onOpenChange={(open) => {
            if (!open) setSending(false)
          }}
        />
      )}
      {amending && (
        <Suspense fallback={<PanelLoader />}>
          <PurchaseOrderAmendDialog order={order} onClose={() => setAmending(false)} />
        </Suspense>
      )}

      <ConfirmDialog
        open={Boolean(command)}
        onOpenChange={(open) => {
          if (!open && !run.isPending) resetCommand()
        }}
        title={copy ? `${copy.title.replace('this order', order.code)}` : ''}
        description={copy?.description}
        confirmLabel={copy?.confirmLabel ?? 'Confirm'}
        destructive={copy?.destructive}
        loading={run.isPending}
        onConfirm={() => {
          if (!command) return
          if (duplicateApproval && !confirmDuplicate) {
            setConfirmationError('Confirm that this duplicate demand is required before approving.')
            return
          }
          if (reasonInvalid) {
            setReasonTouched(true)
            return
          }
          run.mutate({ action: command, reason: reason.trim() })
        }}
      >
        {duplicateApproval && (
          <div className="space-y-3">
            <Alert variant="warning">
              <AlertTitle>This order was raised despite duplicate demand</AlertTitle>
              <AlertDescription>{order.duplicate_acknowledgement_reason}</AlertDescription>
            </Alert>
            <Label className="flex items-start gap-2">
              <Checkbox
                checked={confirmDuplicate}
                disabled={run.isPending}
                onCheckedChange={(value) => {
                  setConfirmDuplicate(value === true)
                  setConfirmationError(null)
                }}
                aria-invalid={Boolean(confirmationError)}
              />
              I confirm that the duplicate demand is required
            </Label>
            <FieldError message={confirmationError ?? undefined} />
          </div>
        )}
        <div>
          <Label htmlFor="po-command-reason" required={reasonRequired}>
            {reasonRequired ? 'Reason' : 'Note'}
          </Label>
          <Textarea
            id="po-command-reason"
            value={reason}
            maxLength={maxReasonLength}
            disabled={run.isPending}
            onChange={(event) => {
              setReason(event.target.value)
              setServerReason(null)
            }}
            className="mt-1.5"
            aria-invalid={(reasonTouched && reasonInvalid) || serverReason !== null}
          />
          {reasonTouched && reasonRequired && reasonTooShort && (
            <FieldError message={messages.errors.reasonTooShort} />
          )}
          {reasonTouched && reasonTooLong && (
            <FieldError message={`At most ${maxReasonLength} characters`} />
          )}
          {serverReason !== null && <FieldError message={serverReason} />}
        </div>
      </ConfirmDialog>
    </>
  )
}

/**
 * Days past the expected delivery while a delivery is still expected, or null.
 *
 * "Still expected" is the API's own OPEN_PO_STATUSES, not a hand-kept list of
 * settled ones - the two used to be maintained separately and could disagree.
 */
function daysIfLate(order: PurchaseOrderDetail): number | null {
  const days = daysUntil(order.expected_delivery_date)
  if (days === null || days >= 0) return null
  if (!(OPEN_PO_STATUSES as readonly string[]).includes(order.status)) return null
  return Math.abs(days)
}

/**
 * Dispatch and the tracking board.
 *
 * The board is the screen the master rules ask for: one place where both
 * directions of movement are visible - what has gone out to customers and what
 * is still coming in from vendors - with the tracking reference, the promised
 * delivery date and whether it has been missed.
 *
 * A late consignment is the whole point of the screen, so it is not a subtle
 * tint: the row carries a destructive accent, the days late are spelled out, and
 * the delay can be filtered on directly. Everything else on the board is
 * secondary to "what is late and who do I ring about it".
 *
 * Every state transition is confirmed, permission-gated and carries a recorded
 * reason - the API refuses a bodyless transition. The gate is UX; the API
 * authorises each request independently (master rules 38).
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronDown,
  MapPin,
  PackageCheck,
  Radar,
  Truck,
  X,
} from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import {
  RaiseSalesInvoiceDialog,
  SalesInvoiceDetailDialog,
} from '@/features/finance/sales-invoice-dialogs'
import { useListQuery } from '@/hooks/use-list-query'
import { ApiError, api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_ALL } from '@/lib/constants'
import { formatDate, formatNumber, formatQuantity, humanise } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import { CreateDispatchDialog, DispatchTimelineDialog } from './dispatch-dialogs'
import {
  DISPATCH_STATUSES,
  DISPATCH_TYPES,
  NOTE_MAX_LENGTH,
  delayDays,
  isDeliverable,
  isDispatchCancellable,
  isDispatchable,
  isMarkableInTransit,
  isReadyable,
  isSettled,
  type DispatchRow,
  type DispatchTrackingRow,
} from './dispatch-types'

/**
 * The URL keys the dispatch list filters on. `sales_order_id` has no control of
 * its own: it arrives on the link from a sales order's page, shows as a chip,
 * and is cleared with the rest.
 */
const DISPATCH_FILTER_KEYS = [
  'status',
  'dispatch_type',
  'customer_id',
  'sales_order_id',
  'delayed_only',
  'in_flight_only',
] as const

/** The tracking board takes no customer filter, so it clears one key fewer. */
const TRACKING_FILTER_KEYS = ['status', 'dispatch_type', 'delayed_only', 'in_flight_only'] as const

/**
 * Clear a set of filter keys in one write.
 *
 * Calling `setParam` once per key looked like several clears but was one:
 * react-router's `setSearchParams` closes over the search params of the render
 * that created it, so every call recomputed from the *same* starting point and
 * only the last one survived. The whole set is computed once and written once.
 */
function clearKeys(
  setSearchParams: ReturnType<typeof useSearchParams>[1],
  keys: readonly string[],
): void {
  setSearchParams(
    (current) => {
      const next = new URLSearchParams(current)
      for (const key of keys) next.delete(key)
      // Page 4 of the filtered list does not exist in the unfiltered one.
      next.delete('page')
      return next
    },
    { replace: true },
  )
}

interface PendingAction {
  dispatch: DispatchRow
  path: string
  title: string
  description: string
  confirmLabel: string
  successTitle: string
  successBody: string
  /** What the note box is asking for, and whether the API insists on it. */
  noteLabel: string
  noteRequired: boolean
  /**
   * A delivery confirmed after its promised date must say why: the API refuses
   * it without `delay_reason`, and the confirmation payload is closed, so the
   * box is asked for up front rather than learned from the rejection.
   */
  delayReasonRequired?: boolean
  gateDetails?: boolean
  /** The transition's own payload shape - they are not interchangeable. */
  buildBody: (
    note: string,
    delayReason: string,
    gateDetails: DispatchGateDetails,
  ) => Record<string, unknown>
}

interface DispatchGateDetails {
  dispatchDate: string
  expectedDeliveryDate: string
  lrNumber: string
  vehicleNumber: string
  trackingId: string
  location: string
}

function emptyDispatchGateDetails(): DispatchGateDetails {
  return {
    dispatchDate: '',
    expectedDeliveryDate: '',
    lrNumber: '',
    vehicleNumber: '',
    trackingId: '',
    location: '',
  }
}

/**
 * One confirmation and one mutation for every dispatch transition.
 *
 * They all invalidate the same things, but they do *not* share a payload: three
 * of them demand a `reason` and the delivery confirmation refuses one, so the
 * body is built per action rather than posted as an empty object.
 */
function ActionConfirm({ pending, onDone }: { pending: PendingAction | null; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')
  const [delayReason, setDelayReason] = useState('')
  const [gateDetails, setGateDetails] = useState<DispatchGateDetails>(emptyDispatchGateDetails)
  const [touched, setTouched] = useState(false)

  const run = useMutation({
    mutationFn: (action: PendingAction) =>
      api.post(action.path, action.buildBody(note.trim(), delayReason.trim(), gateDetails)),
    onSuccess: (_result, action) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      // Dispatching consumes the reservation and posts the stock movement, so
      // balances and availability are dropped rather than left to expire.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success(action.successTitle, action.successBody)
      setNote('')
      setDelayReason('')
      setGateDetails(emptyDispatchGateDetails())
      setTouched(false)
      onDone()
    },
  })

  if (!pending) return null

  // `Reason` is bounded 3..255 on every transition that takes one. A 422 keyed
  // on the field is one the mutation cache stays quiet about, on the
  // assumption the form shows it - so the band is checked here, and whatever
  // the server still refuses is rendered under the box rather than lost.
  const trimmedNote = note.trim()
  const reasonTooShort = pending.noteRequired && trimmedNote.length < REASON_MIN_LENGTH
  const reasonTooLong = pending.noteRequired && trimmedNote.length > REASON_MAX_LENGTH
  const noteInvalid = reasonTooShort || reasonTooLong
  const delayReasonMissing = Boolean(pending.delayReasonRequired) && delayReason.trim() === ''
  const serverErrors = run.error instanceof ApiError ? run.error.fieldErrors : {}
  const noteServerError = serverErrors.reason?.[0] ?? serverErrors.delivery_note?.[0]
  const delayServerError = serverErrors.delay_reason?.[0]
  const gateFieldErrors = {
    dispatchDate: serverErrors.dispatch_date?.[0],
    expectedDeliveryDate: serverErrors.expected_delivery_date?.[0],
    lrNumber: serverErrors.lr_number?.[0],
    vehicleNumber: serverErrors.vehicle_number?.[0],
    trackingId: serverErrors.tracking_id?.[0],
    location: serverErrors.location?.[0],
  }

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) {
          setNote('')
          setDelayReason('')
          setGateDetails(emptyDispatchGateDetails())
          setTouched(false)
          run.reset()
          onDone()
        }
      }}
      title={pending.title}
      description={pending.description}
      confirmLabel={pending.confirmLabel}
      loading={run.isPending}
      onConfirm={() => {
        if (noteInvalid || delayReasonMissing) {
          setTouched(true)
          return
        }
        run.mutate(pending)
      }}
    >
      <div className="space-y-3">
        {pending.delayReasonRequired && (
          <div>
            <Label htmlFor="dispatch-action-delay-reason" required>
              Delay reason
            </Label>
            <Input
              id="dispatch-action-delay-reason"
              value={delayReason}
              onChange={(event) => setDelayReason(event.target.value)}
              autoComplete="off"
              maxLength={NOTE_MAX_LENGTH}
              className="mt-1.5"
              aria-invalid={(touched && delayReasonMissing) || Boolean(delayServerError)}
            />
            {touched && delayReasonMissing && (
              <FieldError message="This consignment arrived after its promised date - the API records why before it will confirm the delivery" />
            )}
            <FieldError message={delayServerError} />
          </div>
        )}

        {pending.gateDetails && (
          <section className="space-y-3 rounded-md border p-3">
            <div>
              <h3 className="text-sm font-medium">Gate details</h3>
              <p className="text-xs text-muted-foreground">
                Record what is known when the vehicle leaves. Blank fields keep the planned values
                unchanged.
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <Label htmlFor="dispatch-gate-date">Actual dispatch date</Label>
                <Input
                  id="dispatch-gate-date"
                  type="date"
                  value={gateDetails.dispatchDate}
                  onChange={(event) =>
                    setGateDetails((current) => ({
                      ...current,
                      dispatchDate: event.target.value,
                    }))
                  }
                  className="mt-1.5"
                  aria-invalid={Boolean(gateFieldErrors.dispatchDate)}
                />
                <FieldError message={gateFieldErrors.dispatchDate} />
              </div>
              <div>
                <Label htmlFor="dispatch-gate-expected-date">Updated expected delivery</Label>
                <Input
                  id="dispatch-gate-expected-date"
                  type="date"
                  value={gateDetails.expectedDeliveryDate}
                  onChange={(event) =>
                    setGateDetails((current) => ({
                      ...current,
                      expectedDeliveryDate: event.target.value,
                    }))
                  }
                  className="mt-1.5"
                  aria-invalid={Boolean(gateFieldErrors.expectedDeliveryDate)}
                />
                <FieldError message={gateFieldErrors.expectedDeliveryDate} />
              </div>
              <div>
                <Label htmlFor="dispatch-gate-lr">LR number</Label>
                <Input
                  id="dispatch-gate-lr"
                  value={gateDetails.lrNumber}
                  onChange={(event) =>
                    setGateDetails((current) => ({ ...current, lrNumber: event.target.value }))
                  }
                  maxLength={64}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(gateFieldErrors.lrNumber)}
                />
                <FieldError message={gateFieldErrors.lrNumber} />
              </div>
              <div>
                <Label htmlFor="dispatch-gate-vehicle">Vehicle number</Label>
                <Input
                  id="dispatch-gate-vehicle"
                  value={gateDetails.vehicleNumber}
                  onChange={(event) =>
                    setGateDetails((current) => ({
                      ...current,
                      vehicleNumber: event.target.value,
                    }))
                  }
                  maxLength={32}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(gateFieldErrors.vehicleNumber)}
                />
                <FieldError message={gateFieldErrors.vehicleNumber} />
              </div>
              <div>
                <Label htmlFor="dispatch-gate-tracking">Tracking id</Label>
                <Input
                  id="dispatch-gate-tracking"
                  value={gateDetails.trackingId}
                  onChange={(event) =>
                    setGateDetails((current) => ({
                      ...current,
                      trackingId: event.target.value,
                    }))
                  }
                  maxLength={120}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(gateFieldErrors.trackingId)}
                />
                <FieldError message={gateFieldErrors.trackingId} />
              </div>
              <div>
                <Label htmlFor="dispatch-gate-location">Gate location</Label>
                <Input
                  id="dispatch-gate-location"
                  value={gateDetails.location}
                  onChange={(event) =>
                    setGateDetails((current) => ({
                      ...current,
                      location: event.target.value,
                    }))
                  }
                  maxLength={255}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(gateFieldErrors.location)}
                />
                <FieldError message={gateFieldErrors.location} />
              </div>
            </div>
          </section>
        )}

        <div>
          <Label htmlFor="dispatch-action-note" required={pending.noteRequired}>
            {pending.noteLabel}
          </Label>
          <Input
            id="dispatch-action-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            autoComplete="off"
            // The delivery note is `Notes` (2000), not `Reason` (255): a blanket
            // 255 here would silently truncate a note the API accepts.
            maxLength={pending.noteRequired ? REASON_MAX_LENGTH : NOTE_MAX_LENGTH}
            className="mt-1.5"
            aria-invalid={(touched && noteInvalid) || Boolean(noteServerError)}
          />
          {touched && noteInvalid && (
            <FieldError
              message={`The API records this transition - it needs a reason of ${REASON_MIN_LENGTH} to ${REASON_MAX_LENGTH} characters`}
            />
          )}
          <FieldError message={noteServerError} />
        </div>
      </div>
    </ConfirmDialog>
  )
}

function DelayBadge({ days }: { days: number | null }) {
  if (days === null) return <span className="text-muted-foreground">-</span>
  return (
    <Badge variant="destructive">
      <AlertTriangle className="h-3 w-3" aria-hidden />
      {days} day{days === 1 ? '' : 's'} late
    </Badge>
  )
}

/**
 * A left accent bar on the leading cell.
 *
 * The table renders rows itself, so a delayed row is marked where the eye lands
 * first rather than by tinting a whole row it does not control.
 */
function LeadingCell({ delayed, children }: { delayed: boolean; children: ReactNode }) {
  return (
    <div
      className={cn(
        'flex items-center gap-2 border-l-2 py-0.5 pl-2',
        delayed ? 'border-l-destructive' : 'border-l-transparent',
      )}
    >
      {delayed && <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" aria-hidden />}
      <span className={cn('min-w-0', delayed && 'font-semibold text-destructive')}>{children}</span>
    </div>
  )
}

// ----------------------------------------------------------- dispatches -----

function DispatchesTab({
  onTrack,
  onAction,
  onRaiseInvoice,
  onViewInvoice,
}: {
  onTrack: (dispatch: DispatchRow) => void
  onAction: (action: PendingAction) => void
  onRaiseInvoice: (dispatch: DispatchRow) => void
  onViewInvoice: (id: string) => void
}) {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canUpdate = can(P.DISPATCH_UPDATE)
  // Sending the goods is the stock-moving act, and `POST /{id}/dispatch`
  // declares `dispatch.create` for it - not `dispatch.update`, which the
  // staging, in-transit and cancel transitions take.
  const canCreate = can(P.DISPATCH_CREATE)
  const canConfirmDelivery = can(P.DISPATCH_CONFIRM_DELIVERY)

  const status = searchParams.get('status') ?? ''
  const type = searchParams.get('dispatch_type') ?? ''
  const customerId = searchParams.get('customer_id') ?? ''
  const salesOrderId = searchParams.get('sales_order_id') ?? ''
  const delayedOnly = searchParams.get('delayed_only') === 'true'
  const inFlightOnly = searchParams.get('in_flight_only') === 'true'

  const list = useListQuery<DispatchRow>({
    path: '/dispatch',
    queryKey: queryKeys.dispatch.list,
    defaultSortBy: 'dispatch_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(type ? { dispatch_type: type } : {}),
      ...(customerId ? { customer_id: customerId } : {}),
      ...(salesOrderId ? { sales_order_id: salesOrderId } : {}),
      ...(delayedOnly ? { delayed_only: true } : {}),
      ...(inFlightOnly ? { in_flight_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount =
    (status ? 1 : 0) +
    (type ? 1 : 0) +
    (customerId ? 1 : 0) +
    (salesOrderId ? 1 : 0) +
    (delayedOnly ? 1 : 0) +
    (inFlightOnly ? 1 : 0)

  const columns = useMemo<ColumnDef<DispatchRow, unknown>[]>(() => {
    const defs: ColumnDef<DispatchRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Dispatch',
        cell: ({ row }) => {
          const late = delayDays(
            row.original.expected_delivery_date,
            isSettled(row.original.status),
          )
          return <LeadingCell delayed={late !== null}>{row.original.code}</LeadingCell>
        },
      },
      {
        accessorKey: 'dispatch_type',
        header: 'Type',
        // The API sorts on code, status and the three dates - not on the type.
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{humanise(row.original.dispatch_type)}</span>
        ),
      },
      {
        id: 'against',
        header: 'Against',
        enableSorting: false,
        // The list projection carries ids, not party names. The one id that
        // leads somewhere useful is the sales order, so that is the link.
        cell: ({ row }) =>
          row.original.sales_order_id ? (
            <Link
              to={`/sales-orders/${row.original.sales_order_id}`}
              className="hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              Sales order
            </Link>
          ) : (
            <span className="text-muted-foreground">
              {row.original.vendor_id ? 'Vendor consignment' : '-'}
            </span>
          ),
      },
      {
        accessorKey: 'dispatch_date',
        header: 'Dispatched',
        cell: ({ row }) => formatDate(row.original.dispatch_date),
      },
      {
        accessorKey: 'expected_delivery_date',
        header: 'Expected',
        cell: ({ row }) => formatDate(row.original.expected_delivery_date),
      },
      {
        accessorKey: 'actual_delivery_date',
        header: 'Delivered',
        cell: ({ row }) => formatDate(row.original.actual_delivery_date),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'courier_name',
        header: 'Courier',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[12rem] truncate text-muted-foreground">
            {row.original.courier_name ?? '-'}
          </span>
        ),
      },
      {
        id: 'tracking_id',
        header: 'Tracking id',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.tracking_id ? (
            <span className="font-mono text-xs">{row.original.tracking_id}</span>
          ) : (
            <span className="text-xs text-warning">Not traceable</span>
          ),
      },
      {
        id: 'total_boxes',
        header: 'Boxes',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.total_boxes),
      },
      {
        id: 'total_weight_kg',
        header: 'Weight kg',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.total_weight_kg),
      },
      {
        id: 'delay',
        header: 'Delay',
        enableSorting: false,
        cell: ({ row }) => (
          <DelayBadge
            days={delayDays(row.original.expected_delivery_date, isSettled(row.original.status))}
          />
        ),
      },
    ]

    defs.push({
      id: 'actions',
      header: '',
      enableSorting: false,
      enableHiding: false,
      meta: { align: 'right' },
      cell: ({ row }) => {
        const dispatch = row.original
        const canReady = canUpdate && isReadyable(dispatch.status)
        const canGo = canCreate && isDispatchable(dispatch.status)
        const canTransit = canUpdate && isMarkableInTransit(dispatch.status)
        const canDeliver = canConfirmDelivery && isDeliverable(dispatch.status)
        const canCancel = canUpdate && isDispatchCancellable(dispatch.status)

        return (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" aria-label={`Actions for ${dispatch.code}`}>
                Actions
                <ChevronDown />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onTrack(dispatch)}>
                <MapPin />
                Tracking timeline
              </DropdownMenuItem>

              {can(P.SALES_INVOICE_CREATE) &&
                can(P.SALES_INVOICE_VIEW) &&
                dispatch.dispatch_type === 'SALES' &&
                dispatch.sales_order_id &&
                !dispatch.sales_invoice_id &&
                ['READY_FOR_DISPATCH', 'DISPATCHED', 'IN_TRANSIT', 'DELIVERED'].includes(
                  dispatch.status,
                ) && (
                  <DropdownMenuItem onSelect={() => onRaiseInvoice(dispatch)}>
                    Raise invoice
                  </DropdownMenuItem>
                )}
              {can(P.SALES_INVOICE_VIEW) && dispatch.sales_invoice_id && (
                <DropdownMenuItem onSelect={() => onViewInvoice(dispatch.sales_invoice_id!)}>
                  View invoice
                </DropdownMenuItem>
              )}

              {canReady && (
                <DropdownMenuItem
                  onSelect={() =>
                    onAction({
                      dispatch,
                      path: `/dispatch/${dispatch.id}/ready`,
                      title: `Mark ${dispatch.code} ready?`,
                      description:
                        'The consignment is packed and staged at the gate. Nothing has left yet and no stock has moved - a dispatch can only be released once it is ready.',
                      confirmLabel: 'Mark ready',
                      successTitle: success.statusChanged('Dispatch', 'ready'),
                      successBody: `${dispatch.code} is staged and can now be released.`,
                      noteLabel: 'Reason',
                      noteRequired: true,
                      buildBody: (note) => ({ reason: note }),
                    })
                  }
                >
                  <PackageCheck />
                  Mark ready
                </DropdownMenuItem>
              )}

              {canGo && (
                <DropdownMenuItem
                  onSelect={() =>
                    onAction({
                      dispatch,
                      path: `/dispatch/${dispatch.id}/dispatch`,
                      title: `Dispatch ${dispatch.code}?`,
                      description:
                        'This releases the consignment: the reservation is consumed and the stock leaves the warehouse. A serial-tracked line ships the units named on it. It cannot be undone from this screen.',
                      confirmLabel: 'Dispatch',
                      successTitle: success.released('Consignment'),
                      successBody: `${dispatch.code} has left. Stock has moved out.`,
                      noteLabel: 'Reason',
                      noteRequired: true,
                      gateDetails: true,
                      buildBody: (note, _delayReason, gate) => ({
                        reason: note,
                        ...(gate.dispatchDate ? { dispatch_date: gate.dispatchDate } : {}),
                        ...(gate.expectedDeliveryDate
                          ? { expected_delivery_date: gate.expectedDeliveryDate }
                          : {}),
                        ...(gate.lrNumber.trim() ? { lr_number: gate.lrNumber.trim() } : {}),
                        ...(gate.vehicleNumber.trim()
                          ? { vehicle_number: gate.vehicleNumber.trim() }
                          : {}),
                        ...(gate.trackingId.trim() ? { tracking_id: gate.trackingId.trim() } : {}),
                        ...(gate.location.trim() ? { location: gate.location.trim() } : {}),
                      }),
                    })
                  }
                >
                  <Truck />
                  Dispatch
                </DropdownMenuItem>
              )}

              {canTransit && (
                <DropdownMenuItem
                  onSelect={() =>
                    onAction({
                      dispatch,
                      path: `/dispatch/${dispatch.id}/in-transit`,
                      title: `Mark ${dispatch.code} in transit?`,
                      description:
                        'Records that the carrier has picked the consignment up and it is on the road.',
                      confirmLabel: 'Mark in transit',
                      successTitle: success.statusChanged('Dispatch', 'in transit'),
                      successBody: `${dispatch.code} is on the road.`,
                      noteLabel: 'Reason',
                      noteRequired: true,
                      buildBody: (note) => ({ reason: note, source: 'MANUAL' }),
                    })
                  }
                >
                  <Radar />
                  Mark in transit
                </DropdownMenuItem>
              )}

              {canDeliver && (
                <DropdownMenuItem
                  onSelect={() => {
                    // Past its promised date, the API refuses the confirmation
                    // without a `delay_reason` (`confirm_delivery`), so it is
                    // asked for here rather than learned from the rejection.
                    // The server check stays the backstop: its `today()` and
                    // this browser's clock can disagree at midnight.
                    const daysLate = delayDays(dispatch.expected_delivery_date, false)
                    onAction({
                      dispatch,
                      path: `/dispatch/${dispatch.id}/deliver`,
                      title: `Confirm delivery of ${dispatch.code}?`,
                      description:
                        daysLate === null
                          ? 'Records that the consignment reached its destination. Anything requiring inspection on arrival moves to QC pending.'
                          : `Promised for ${formatDate(dispatch.expected_delivery_date)}, ${daysLate} day${daysLate === 1 ? '' : 's'} late - a reason is required. Anything requiring inspection on arrival moves to QC pending.`,
                      confirmLabel: 'Confirm delivery',
                      successTitle: success.confirmed('Delivery'),
                      successBody: `${dispatch.code} is delivered.`,
                      // The delivery payload has no `reason` - it records a note
                      // against the delivery instead, and it is optional.
                      noteLabel: 'Delivery note (optional)',
                      noteRequired: false,
                      delayReasonRequired: daysLate !== null,
                      buildBody: (note, delayReason) => ({
                        source: 'MANUAL',
                        ...(daysLate !== null ? { delay_reason: delayReason } : {}),
                        ...(note ? { delivery_note: note } : {}),
                      }),
                    })
                  }}
                >
                  <CheckCircle2 />
                  Confirm delivery
                </DropdownMenuItem>
              )}

              {canCancel && (
                <DropdownMenuItem
                  onSelect={() =>
                    onAction({
                      dispatch,
                      path: `/dispatch/${dispatch.id}/cancel`,
                      title: `Cancel ${dispatch.code}?`,
                      description:
                        'The consignment is abandoned before it leaves. Nothing has moved out, so nothing is returned - but the document stays on the record.',
                      confirmLabel: 'Cancel dispatch',
                      successTitle: success.cancelled('Dispatch'),
                      successBody: `${dispatch.code} will not go out.`,
                      noteLabel: 'Reason',
                      noteRequired: true,
                      buildBody: (note) => ({ reason: note }),
                    })
                  }
                >
                  <Ban />
                  Cancel dispatch
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )
      },
    })

    return defs
  }, [
    can,
    canUpdate,
    canCreate,
    canConfirmDelivery,
    onTrack,
    onAction,
    onRaiseInvoice,
    onViewInvoice,
  ])

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
      onSearchChange={list.setSearch}
      onRefresh={() => void list.refetch()}
      searchValue={list.search}
      searchPlaceholder="Search dispatch code or tracking id..."
      getRowId={(row) => row.id}
      emptyTitle="No dispatches found"
      emptyDescription="Nothing matches the current filters. Widen them, or create a dispatch for a consignment leaving today."
      toolbar={
        <>
          <Select
            value={status || SELECT_ALL}
            onValueChange={(value) => setParam('status', value === SELECT_ALL ? undefined : value)}
          >
            <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by status">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
              {DISPATCH_STATUSES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={type || SELECT_ALL}
            onValueChange={(value) =>
              setParam('dispatch_type', value === SELECT_ALL ? undefined : value)
            }
          >
            <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by type">
              <SelectValue placeholder="All types" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All types</SelectItem>
              {DISPATCH_TYPES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <CustomerPicker
            id="dispatch-customer-filter"
            label="Customer"
            hideLabel
            value={customerId || null}
            onChange={(value) => setParam('customer_id', value ?? undefined)}
            placeholder="All customers"
          />

          <div className="flex items-center gap-2">
            <Checkbox
              id="dispatch-delayed"
              checked={delayedOnly}
              onCheckedChange={(checked) =>
                setParam('delayed_only', checked === true ? 'true' : undefined)
              }
            />
            <Label htmlFor="dispatch-delayed" className="font-normal">
              Delayed only
            </Label>
          </div>

          <div className="flex items-center gap-2">
            <Checkbox
              id="dispatch-in-flight"
              checked={inFlightOnly}
              onCheckedChange={(checked) =>
                setParam('in_flight_only', checked === true ? 'true' : undefined)
              }
            />
            <Label htmlFor="dispatch-in-flight" className="font-normal">
              Still moving
            </Label>
          </div>

          {salesOrderId && (
            <Badge variant="muted" className="gap-1">
              One sales order's consignments
              <button
                type="button"
                className="rounded-sm hover:text-foreground"
                aria-label="Show every sales order"
                onClick={() => setParam('sales_order_id', undefined)}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          )}

          {filterCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => clearKeys(setSearchParams, DISPATCH_FILTER_KEYS)}
            >
              <X />
              Clear
            </Button>
          )}
        </>
      }
    />
  )
}

// -------------------------------------------------------- tracking board ----

function TrackingTab() {
  const [searchParams, setSearchParams] = useSearchParams()

  const status = searchParams.get('status') ?? ''
  const type = searchParams.get('dispatch_type') ?? ''
  const delayedOnly = searchParams.get('delayed_only') === 'true'
  const inFlightOnly = searchParams.get('in_flight_only') === 'true'

  const list = useListQuery<DispatchTrackingRow>({
    path: '/dispatch/tracking',
    queryKey: queryKeys.dispatch.tracking,
    // The board takes no sort parameters, so no column offers to sort: an
    // arrow that silently did nothing would be worse than no arrow.
    staleTime: staleTimes.transactional,
    // The board is what people leave open all day; coming back to it must not
    // show a picture from an hour ago.
    refetchOnWindowFocus: true,
    // Only filters the endpoint actually answers. QC-pending and GRN-pending are
    // columns here but not query parameters, and filtering them in the browser
    // would silently drop every match past the first page.
    filters: {
      ...(status ? { status } : {}),
      ...(type ? { dispatch_type: type } : {}),
      ...(delayedOnly ? { delayed_only: true } : {}),
      ...(inFlightOnly ? { in_flight_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount =
    (status ? 1 : 0) + (type ? 1 : 0) + (delayedOnly ? 1 : 0) + (inFlightOnly ? 1 : 0)

  const columns = useMemo<ColumnDef<DispatchTrackingRow, unknown>[]>(
    () => [
      {
        id: 'party',
        header: 'Vendor / customer',
        enableSorting: false,
        cell: ({ row }) => {
          const late = delayDays(
            row.original.expected_delivery_date,
            isSettled(row.original.status),
            row.original.delay_days,
          )
          return (
            <LeadingCell delayed={late !== null || row.original.is_delayed}>
              <span className="block truncate">{row.original.party_name ?? '-'}</span>
              <span className="block truncate text-xs font-normal text-muted-foreground">
                {humanise(row.original.party_type)} - {row.original.code}
              </span>
            </LeadingCell>
          )
        },
      },
      {
        id: 'reference',
        header: 'PO / SO',
        enableSorting: false,
        // The board reports the reference code only - there is no id to link to.
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.reference_code ?? '-'}</span>
        ),
      },
      {
        id: 'carrier',
        header: 'Courier / transporter',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[14rem] truncate text-muted-foreground">
            {row.original.courier_name ?? row.original.transporter_name ?? '-'}
          </span>
        ),
      },
      {
        id: 'tracking_id',
        header: 'Tracking id',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.tracking_id ? (
            <span className="font-mono text-xs">{row.original.tracking_id}</span>
          ) : (
            <span className="text-xs text-warning">Not traceable</span>
          ),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'expected_delivery_date',
        header: 'Expected',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.expected_delivery_date),
      },
      {
        id: 'delay',
        header: 'Delay alert',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <DelayBadge
              days={delayDays(
                row.original.expected_delivery_date,
                isSettled(row.original.status),
                row.original.delay_days,
              )}
            />
            {row.original.delay_reason && (
              <span className="mt-0.5 block max-w-[14rem] truncate text-xs text-muted-foreground">
                {row.original.delay_reason}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'qc_pending',
        header: 'QC',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.qc_pending ? (
            <Badge variant="warning">QC pending</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'grn_pending',
        header: 'GRN',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.grn_pending ? (
            <Badge variant="warning">GRN pending</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
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
      onSearchChange={list.setSearch}
      onRefresh={() => void list.refetch()}
      searchValue={list.search}
      searchPlaceholder="Search party, reference or tracking id..."
      getRowId={(row) => row.dispatch_id}
      emptyTitle="Nothing in transit"
      emptyDescription="No consignment is currently moving in either direction that matches these filters."
      toolbar={
        <>
          <Select
            value={status || SELECT_ALL}
            onValueChange={(value) => setParam('status', value === SELECT_ALL ? undefined : value)}
          >
            <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by status">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
              {DISPATCH_STATUSES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={type || SELECT_ALL}
            onValueChange={(value) =>
              setParam('dispatch_type', value === SELECT_ALL ? undefined : value)
            }
          >
            <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by direction">
              <SelectValue placeholder="All types" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All types</SelectItem>
              {DISPATCH_TYPES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="flex items-center gap-2">
            <Checkbox
              id="tracking-delayed"
              checked={delayedOnly}
              onCheckedChange={(checked) =>
                setParam('delayed_only', checked === true ? 'true' : undefined)
              }
            />
            <Label htmlFor="tracking-delayed" className="font-normal">
              Delayed
            </Label>
          </div>

          <div className="flex items-center gap-2">
            <Checkbox
              id="tracking-in-flight"
              checked={inFlightOnly}
              onCheckedChange={(checked) =>
                setParam('in_flight_only', checked === true ? 'true' : undefined)
              }
            />
            <Label htmlFor="tracking-in-flight" className="font-normal">
              Still moving
            </Label>
          </div>

          {filterCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => clearKeys(setSearchParams, TRACKING_FILTER_KEYS)}
            >
              <X />
              Clear
            </Button>
          )}
        </>
      }
    />
  )
}

// ------------------------------------------------------------------ page ----

const TABS = [
  { value: 'dispatches', label: 'Dispatches', icon: Truck },
  { value: 'tracking', label: 'Tracking board', icon: Radar },
] as const

export default function DispatchPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [createOpen, setCreateOpen] = useState(false)
  const [tracking, setTracking] = useState<DispatchRow | null>(null)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [invoiceDispatch, setInvoiceDispatch] = useState<DispatchRow | null>(null)
  const [invoiceId, setInvoiceId] = useState<string | null>(null)

  const canCreate = can(P.DISPATCH_CREATE)
  const tab = searchParams.get('tab') ?? 'dispatches'

  const changeTab = (next: string) => {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        params.set('tab', next)
        // Page 4 of the dispatch list is meaningless once the table below is
        // the tracking board, and so is a filter the next list does not offer.
        for (const key of [
          'page',
          'q',
          'sort',
          'dir',
          'status',
          'dispatch_type',
          'customer_id',
          'delayed_only',
          'in_flight_only',
        ]) {
          params.delete(key)
        }
        return params
      },
      { replace: true },
    )
  }

  return (
    <div>
      <PageHeader
        title="Dispatch"
        description="Consignments leaving the plant and material still on its way in. Anything past its promised delivery date is flagged here first."
        actions={
          canCreate ? (
            <Button onClick={() => setCreateOpen(true)}>
              <Truck />
              Create dispatch
            </Button>
          ) : null
        }
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

        <TabsContent value="dispatches">
          <DispatchesTab
            onTrack={setTracking}
            onAction={setPending}
            onRaiseInvoice={setInvoiceDispatch}
            onViewInvoice={setInvoiceId}
          />
        </TabsContent>
        <TabsContent value="tracking">
          <TrackingTab />
        </TabsContent>
      </Tabs>

      {canCreate && <CreateDispatchDialog open={createOpen} onOpenChange={setCreateOpen} />}

      <DispatchTimelineDialog
        dispatch={tracking}
        open={Boolean(tracking)}
        onOpenChange={(open) => {
          if (!open) setTracking(null)
        }}
      />

      <ActionConfirm pending={pending} onDone={() => setPending(null)} />
      {can(P.SALES_INVOICE_CREATE) && can(P.SALES_INVOICE_VIEW) && (
        <RaiseSalesInvoiceDialog
          dispatchId={invoiceDispatch?.id ?? null}
          onClose={() => setInvoiceDispatch(null)}
        />
      )}
      {can(P.SALES_INVOICE_VIEW) && (
        <SalesInvoiceDetailDialog invoiceId={invoiceId} onClose={() => setInvoiceId(null)} />
      )}
    </div>
  )
}

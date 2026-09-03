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
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDate, formatNumber, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import { CreateDispatchDialog, DispatchTimelineDialog } from './dispatch-dialogs'
import {
  DISPATCH_STATUSES,
  DISPATCH_TYPES,
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

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/** The URL keys the dispatch list filters on. */
const DISPATCH_FILTER_KEYS = [
  'status',
  'dispatch_type',
  'customer_id',
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
  /** The transition's own payload shape - they are not interchangeable. */
  buildBody: (note: string) => Record<string, unknown>
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
  const [touched, setTouched] = useState(false)

  const run = useMutation({
    mutationFn: (action: PendingAction) => api.post(action.path, action.buildBody(note.trim())),
    onSuccess: (_result, action) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      // Dispatching consumes the reservation and posts the stock movement, so
      // balances and availability are dropped rather than left to expire.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success(action.successTitle, action.successBody)
      setNote('')
      setTouched(false)
      onDone()
    },
  })

  if (!pending) return null

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) {
          setNote('')
          setTouched(false)
          onDone()
        }
      }}
      title={pending.title}
      description={pending.description}
      confirmLabel={pending.confirmLabel}
      loading={run.isPending}
      onConfirm={() => {
        if (pending.noteRequired && note.trim() === '') {
          setTouched(true)
          return
        }
        run.mutate(pending)
      }}
    >
      <div>
        <Label htmlFor="dispatch-action-note" required={pending.noteRequired}>
          {pending.noteLabel}
        </Label>
        <Input
          id="dispatch-action-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={touched && pending.noteRequired && note.trim() === ''}
        />
        {touched && pending.noteRequired && note.trim() === '' && (
          <FieldError message="The API records this transition - it needs a reason" />
        )}
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
}: {
  onTrack: (dispatch: DispatchRow) => void
  onAction: (action: PendingAction) => void
}) {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canUpdate = can(P.DISPATCH_UPDATE)
  const canConfirmDelivery = can(P.DISPATCH_CONFIRM_DELIVERY)

  const status = searchParams.get('status') ?? ''
  const type = searchParams.get('dispatch_type') ?? ''
  const customerId = searchParams.get('customer_id') ?? ''
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
      ...(delayedOnly ? { delayed_only: true } : {}),
      ...(inFlightOnly ? { in_flight_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount =
    (status ? 1 : 0) +
    (type ? 1 : 0) +
    (customerId ? 1 : 0) +
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
        const canGo = canUpdate && isDispatchable(dispatch.status)
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
                      successTitle: 'Ready for dispatch',
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
                      successTitle: 'Dispatched',
                      successBody: `${dispatch.code} has left. Stock has moved out.`,
                      noteLabel: 'Reason',
                      noteRequired: true,
                      buildBody: (note) => ({ reason: note }),
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
                      successTitle: 'Marked in transit',
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
                  onSelect={() =>
                    onAction({
                      dispatch,
                      path: `/dispatch/${dispatch.id}/deliver`,
                      title: `Confirm delivery of ${dispatch.code}?`,
                      description:
                        'Records that the consignment reached its destination. Anything requiring inspection on arrival moves to QC pending.',
                      confirmLabel: 'Confirm delivery',
                      successTitle: 'Delivery confirmed',
                      successBody: `${dispatch.code} is delivered.`,
                      // The delivery payload has no `reason` - it records a note
                      // against the delivery instead, and it is optional.
                      noteLabel: 'Delivery note (optional)',
                      noteRequired: false,
                      buildBody: (note) => ({
                        source: 'MANUAL',
                        ...(note ? { delivery_note: note } : {}),
                      }),
                    })
                  }
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
                      successTitle: 'Dispatch cancelled',
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
  }, [canUpdate, canConfirmDelivery, onTrack, onAction])

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
            value={status || ALL}
            onValueChange={(value) => setParam('status', value === ALL ? undefined : value)}
          >
            <SelectTrigger className="w-[180px]" aria-label="Filter by status">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All statuses</SelectItem>
              {DISPATCH_STATUSES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={type || ALL}
            onValueChange={(value) => setParam('dispatch_type', value === ALL ? undefined : value)}
          >
            <SelectTrigger className="w-[170px]" aria-label="Filter by type">
              <SelectValue placeholder="All types" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All types</SelectItem>
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
            value={status || ALL}
            onValueChange={(value) => setParam('status', value === ALL ? undefined : value)}
          >
            <SelectTrigger className="w-[180px]" aria-label="Filter by status">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All statuses</SelectItem>
              {DISPATCH_STATUSES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={type || ALL}
            onValueChange={(value) => setParam('dispatch_type', value === ALL ? undefined : value)}
          >
            <SelectTrigger className="w-[170px]" aria-label="Filter by direction">
              <SelectValue placeholder="All types" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All types</SelectItem>
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
          <DispatchesTab onTrack={setTracking} onAction={setPending} />
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
    </div>
  )
}

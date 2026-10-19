/**
 * Stock reservations - which demand each promised quantity belongs to.
 *
 * A reservation does not move stock; it claims part of a balance for a specific
 * document (docs/database.md section 4.2). Raising one changes
 * `reserved_quantity` and nothing else - the units stay on hand and stay where
 * they are. Releasing one hands the quantity back to everybody else, and
 * reallocation takes it from one demand and gives it to another - so each of
 * those demands a written reason, and none of the three is offered without the
 * permission that authorises it.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import type { ColumnDef } from '@tanstack/react-table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeftRight, Handshake, MoreHorizontal, Plus, Unlink } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { Link, useSearchParams } from 'react-router-dom'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { daysUntil, formatDate, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import { ItemPicker, WarehouseSelect } from './pickers'
import { DEMAND_ROUTES } from './demand-routes'
import { ReallocationRequestDialog } from './reallocation-request-dialog'
import {
  DEMAND_TYPES,
  RESERVATION_STATUSES,
  RESERVATION_TYPES,
  UUID_PATTERN,
  isDemandType,
  isReservationType,
  type AvailableToPromise,
  type ReallocationRequest,
  type ReservationCreateBody,
  type ReservationRow,
  type ReservationStatus,
} from './types'

const ALL = '__all__'

/**
 * Statuses where there is still a live claim to release or reallocate. There is
 * no ALLOCATED member - the vocabulary is ACTIVE, PARTIALLY_CONSUMED, CONSUMED,
 * RELEASED, EXPIRED - and naming one the enum does not have only ever widened
 * this set with a value no row can hold.
 */
const LIVE_STATUSES: ReadonlySet<ReservationStatus> = new Set<ReservationStatus>([
  'ACTIVE',
  'PARTIALLY_CONSUMED',
])

/** A quantity typed by hand: positive, to the API's four decimal places. */
const QUANTITY_PATTERN = /^\d+(\.\d{1,4})?$/

/**
 * Priority as typed: a whole number, bounded the way `Priority` is in the
 * allocation schemas. Lower is more urgent - the column's own comment says so,
 * "matching how planners already speak".
 */
const PRIORITY_PATTERN = /^\d{1,4}$/

/** What the API defaults `priority` to, restated so the form can show it. */
const DEFAULT_RESERVATION_PRIORITY = '100'

// -------------------------------------------------------------- create -----

const createSchema = z.object({
  item_id: z.string().min(1, 'Choose the item being promised.'),
  warehouse_id: z.string().min(1, 'Choose the warehouse the stock is held at.'),
  quantity: z
    .string()
    .trim()
    .refine(
      (value) => QUANTITY_PATTERN.test(value) && toDecimal(value).greaterThan(0),
      'Enter how much to hold, greater than zero and to 4 decimal places.',
    ),
  // The `: boolean` is load-bearing. Zod narrows a schema's output type when the
  // predicate is a type guard, and a narrowed `DemandType` cannot hold the empty
  // string the select starts on. Wrapping the guard in an arrow used to be enough
  // to hide it; since TypeScript 5.5 the compiler *infers* a type predicate for
  // exactly this shape, so the wrapper narrows too. Annotating the return type
  // states the intent in a way inference cannot undo. The guard still does its
  // real work at the submit boundary, where the value is settled.
  demand_type: z
    .string()
    .refine((value): boolean => isDemandType(value), 'Say what the stock is being held for.'),
  demand_id: z
    .string()
    .trim()
    .refine(
      (value) => UUID_PATTERN.test(value),
      'Paste the id of the document this stock is held for.',
    ),
  demand_code: z
    .string()
    .trim()
    .max(64, 'The document number the API stores is at most 64 characters.'),
  reservation_type: z
    .string()
    .refine((value): boolean => isReservationType(value), 'Choose how firm the claim is.'),
  priority: z
    .string()
    .trim()
    .refine(
      (value) => PRIORITY_PATTERN.test(value),
      'A whole number from 0 to 9999. Lower is more urgent.',
    ),
  required_date: z.string(),
  // 3 to 255 in `ReservationCreate`; asked for at 10 because "stock" is not a
  // reason anybody can audit a promise against six weeks later.
  reason: z
    .string()
    .trim()
    .min(10, 'Say why this stock is being held, in enough detail to audit later.')
    .max(
      REASON_MAX_LENGTH,
      `Keep the reason under ${REASON_MAX_LENGTH} characters - the API stores no more.`,
    ),
})

type CreateForm = z.infer<typeof createSchema>

/**
 * Promise a quantity of stock to a demand.
 *
 * This does **not** move stock. On-hand quantity is untouched; what changes is
 * `reserved_quantity` on the balance, which subtracts from what everybody else
 * can be promised (docs/database.md section 4.2). The units stay exactly where
 * they are until something issues them.
 *
 * The API locks the position, re-reads it and refuses with
 * INSUFFICIENT_AVAILABLE_STOCK if the claim is larger than what is free, so the
 * availability shown here is a courtesy, not the check. It is read fresh on
 * every item/warehouse change and never cached: a stale "yes, we have it" is
 * how an over-promise happens (master rules 34).
 *
 * `demand_id` is keyed rather than picked. It is a uuid in the contract and the
 * six demand types live in six other modules, so this screen has no picker to
 * offer - validating the shape at least turns a mistyped id into a message
 * beside the field instead of a 422. `demand_line_id`, `location_id`,
 * `batch_id` and `expires_at` are all optional in the contract and are left
 * unset: this folder has no line, location or batch picker, and `expires_at` is
 * a timestamp that a date field could only guess the time of day for.
 */
function CreateReservationDialog({ onClose }: { onClose: () => void }) {
  const { can } = useAuth()
  // The availability read is its own endpoint with its own right, and the route
  // that mounts this page is satisfied by RESERVATION_VIEW alone - so somebody
  // can be standing here without it. Asking anyway would 403 on every keystroke.
  const canSeeAvailability = can(P.ALLOCATION_VIEW)

  const queryClient = useQueryClient()
  const form = useForm<CreateForm>({
    resolver: zodResolver(createSchema),
    defaultValues: {
      item_id: '',
      warehouse_id: '',
      quantity: '',
      demand_type: '',
      demand_id: '',
      demand_code: '',
      reservation_type: 'HARD',
      priority: DEFAULT_RESERVATION_PRIORITY,
      required_date: '',
      reason: '',
    },
  })

  const itemId = form.watch('item_id')
  const warehouseId = form.watch('warehouse_id')
  // Trimmed here rather than at each use: `toDecimal(' 5 ')` is 0, so an
  // untrimmed value would quietly compare as nothing at all.
  const quantity = form.watch('quantity').trim()

  const availability = useQuery({
    queryKey: queryKeys.allocation.atp(itemId, warehouseId),
    queryFn: () =>
      api.get<AvailableToPromise>('/allocation/available-to-promise', {
        params: { item_id: itemId, warehouse_id: warehouseId },
      }),
    enabled: canSeeAvailability && Boolean(itemId) && Boolean(warehouseId),
    staleTime: staleTimes.live,
  })

  const free = availability.data ? toDecimal(availability.data.available_quantity) : null
  // A warning, never a block. The number is already historical by the time it
  // renders; the lock the API takes is the only answer that counts.
  const overPromised =
    free !== null && QUANTITY_PATTERN.test(quantity) && toDecimal(quantity).greaterThan(free)

  const create = useMutation({
    mutationFn: (body: ReservationCreateBody) =>
      api.post<ReservationRow>('/allocation/reservations', body),
    onSuccess: (created) => {
      toast.success(
        'Stock reserved',
        formatQuantity(created.quantity) +
          ' of ' +
          (created.item_code ?? 'the item') +
          ' is held for ' +
          (created.demand_code ?? humanise(created.demand_type)) +
          '. It has not moved.',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      // Not on-hand, but `reserved_quantity` - and `available_quantity` is
      // generated from it, so the balances screen is stale either way.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      onClose()
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  const submit = form.handleSubmit((values) => {
    // The schema has already rejected anything outside either vocabulary. These
    // guards are how the compiler learns that, so the body below is typed by
    // the contract rather than asserted into it.
    if (!isDemandType(values.demand_type) || !isReservationType(values.reservation_type)) return

    create.mutate({
      item_id: values.item_id,
      warehouse_id: values.warehouse_id,
      // A decimal string all the way to the wire, never parsed into a double.
      quantity: values.quantity.trim(),
      demand_type: values.demand_type,
      demand_id: values.demand_id.trim(),
      demand_code: values.demand_code.trim() || null,
      reservation_type: values.reservation_type,
      // An integer the contract bounds at 9999, so `Number` is exact here in a
      // way it would never be for a quantity.
      priority: Number(values.priority.trim()),
      required_date: values.required_date || null,
      reason: values.reason.trim(),
    })
  })

  const errors = form.formState.errors

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Reserve stock</DialogTitle>
          <DialogDescription>
            Hold part of a balance for one document. Nothing moves: the units stay where they are
            and stay on hand - they simply stop being available to anybody else.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Controller
                control={form.control}
                name="item_id"
                render={({ field }) => (
                  <ItemPicker
                    id="reservation-item"
                    label="Item"
                    placeholder="Choose an item"
                    value={field.value || null}
                    onChange={(next) => field.onChange(next ?? '')}
                    invalid={Boolean(errors.item_id)}
                  />
                )}
              />
              <FieldError message={errors.item_id?.message} />
            </div>

            <div>
              <Controller
                control={form.control}
                name="warehouse_id"
                render={({ field }) => (
                  <WarehouseSelect
                    id="reservation-warehouse"
                    label="Warehouse"
                    value={field.value}
                    onChange={field.onChange}
                    invalid={Boolean(errors.warehouse_id)}
                  />
                )}
              />
              <FieldError message={errors.warehouse_id?.message} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="reservation-quantity" required>
                Quantity to hold
              </Label>
              {/* Text, not `type="number"`: the browser normalises a numeric
                  field through a double and a 4-decimal quantity does not
                  survive that round trip. */}
              <Input
                id="reservation-quantity"
                inputMode="decimal"
                autoComplete="off"
                placeholder="0"
                className="text-right tabular"
                aria-invalid={Boolean(errors.quantity)}
                aria-describedby="reservation-quantity-hint"
                {...form.register('quantity')}
              />
              <p id="reservation-quantity-hint" className="text-xs text-muted-foreground">
                {!canSeeAvailability
                  ? 'The API checks availability under a lock when the reservation is saved.'
                  : free !== null
                    ? formatQuantity(availability.data?.available_quantity) +
                      ' available in this warehouse right now'
                    : 'Choose an item and a warehouse to see what is free.'}
              </p>
              <FieldError message={errors.quantity?.message} />
            </div>

            <div>
              <Controller
                control={form.control}
                name="reservation_type"
                render={({ field }) => (
                  <div className="space-y-1.5">
                    <Label htmlFor="reservation-type-field" required>
                      Firmness
                    </Label>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="reservation-type-field"
                        aria-label="Firmness"
                        aria-invalid={Boolean(errors.reservation_type)}
                      >
                        <SelectValue placeholder="How firm is this claim?" />
                      </SelectTrigger>
                      <SelectContent>
                        {RESERVATION_TYPES.map((value) => (
                          <SelectItem key={value} value={value}>
                            {value === 'HARD' ? 'Hard - a real commitment' : 'Soft - a plan'}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}
              />
              <p className="mt-1.5 text-xs text-muted-foreground">
                A hard reservation cannot be taken by a reallocation without an approver signing the
                loss.
              </p>
              <FieldError message={errors.reservation_type?.message} />
            </div>
          </div>

          {overPromised && (
            <Alert variant="warning">
              <AlertTitle>More than is free in this warehouse</AlertTitle>
              <AlertDescription>
                {formatQuantity(quantity)} is more than the{' '}
                {formatQuantity(availability.data?.available_quantity)} currently available. The API
                locks the position and re-reads it when the reservation is saved, so this will be
                refused unless stock arrives first.
              </AlertDescription>
            </Alert>
          )}

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Controller
                control={form.control}
                name="demand_type"
                render={({ field }) => (
                  <div className="space-y-1.5">
                    <Label htmlFor="reservation-demand-type" required>
                      Held for
                    </Label>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="reservation-demand-type"
                        aria-label="Demand type"
                        aria-invalid={Boolean(errors.demand_type)}
                      >
                        <SelectValue placeholder="What kind of demand?" />
                      </SelectTrigger>
                      <SelectContent>
                        {DEMAND_TYPES.map((type) => (
                          <SelectItem key={type} value={type}>
                            {humanise(type)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}
              />
              <FieldError message={errors.demand_type?.message} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="reservation-demand-code">Document number</Label>
              <Input
                id="reservation-demand-code"
                autoComplete="off"
                placeholder="e.g. SO/2026-27/00184"
                aria-invalid={Boolean(errors.demand_code)}
                {...form.register('demand_code')}
              />
              <p className="text-xs text-muted-foreground">
                Optional, and only a label - it is what this row shows in the Demand column.
              </p>
              <FieldError message={errors.demand_code?.message} />
            </div>

            <div className="space-y-1.5 sm:col-span-2">
              <Label htmlFor="reservation-demand-id" required>
                Demand id
              </Label>
              <Input
                id="reservation-demand-id"
                autoComplete="off"
                spellCheck={false}
                placeholder="00000000-0000-0000-0000-000000000000"
                className="font-mono text-xs"
                aria-invalid={Boolean(errors.demand_id)}
                aria-describedby="reservation-demand-id-hint"
                {...form.register('demand_id')}
              />
              <p id="reservation-demand-id-hint" className="text-xs text-muted-foreground">
                The id of the order this stock is held for, taken from its own screen. Reservations
                raised from a sales or production order carry it automatically; this form is the
                manual route.
              </p>
              <FieldError message={errors.demand_id?.message} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="reservation-priority" required>
                Priority
              </Label>
              <Input
                id="reservation-priority"
                inputMode="numeric"
                autoComplete="off"
                className="max-w-[8rem] text-right tabular"
                aria-invalid={Boolean(errors.priority)}
                aria-describedby="reservation-priority-hint"
                {...form.register('priority')}
              />
              <p id="reservation-priority-hint" className="text-xs text-muted-foreground">
                Lower is more urgent. 100 is the default, and it is what the reallocation engine
                ranks donors against.
              </p>
              <FieldError message={errors.priority?.message} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="reservation-required-date">Required date</Label>
              <Input
                id="reservation-required-date"
                type="date"
                aria-invalid={Boolean(errors.required_date)}
                {...form.register('required_date')}
              />
              <p className="text-xs text-muted-foreground">
                Optional. A claim with no date sorts last when stock is short.
              </p>
              <FieldError message={errors.required_date?.message} />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="reservation-reason" required>
              Reason
            </Label>
            <Textarea
              id="reservation-reason"
              placeholder="e.g. Holding the escutcheon plates for SO/2026-27/00184, confirmed by the dealer on 28 Aug"
              aria-invalid={Boolean(errors.reason)}
              {...form.register('reason')}
            />
            <FieldError message={errors.reason?.message} />
          </div>

          <FieldError message={errors.root?.message} />
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending}>
            Reserve stock
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------ release ------

const releaseSchema = z.object({
  reason: z
    .string()
    .trim()
    .min(5, 'Record why the reservation is being released - the stock ledger keeps this.'),
})

type ReleaseForm = z.infer<typeof releaseSchema>

function ReleaseReservationDialog({
  reservation,
  onClose,
}: {
  reservation: ReservationRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const form = useForm<ReleaseForm>({
    resolver: zodResolver(releaseSchema),
    defaultValues: { reason: '' },
  })

  const release = useMutation({
    mutationFn: (values: ReleaseForm) =>
      api.post('/allocation/reservations/' + reservation.id + '/release', {
        reason: values.reason.trim(),
      }),
    onSuccess: () => {
      toast.success(
        success.released('Reservation'),
        formatQuantity(reservation.remaining_quantity) +
          ' of ' +
          (reservation.item_code ?? 'this item') +
          ' is available again.',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      // Releasing changes available_quantity, so the balances screen is stale.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      onClose()
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      destructive
      title="Release this reservation?"
      description={
        <>
          {formatQuantity(reservation.remaining_quantity)} of{' '}
          <span className="font-medium">{reservation.item_code ?? reservation.item_id}</span> at{' '}
          {reservation.warehouse_code ?? 'this warehouse'} returns to free stock.{' '}
          {reservation.demand_code ?? humanise(reservation.demand_type)} loses its claim and may not
          get it back.
        </>
      }
      confirmLabel="Release reservation"
      loading={release.isPending}
      onConfirm={form.handleSubmit((values) => release.mutate(values))}
    >
      <div className="space-y-1.5">
        <Label htmlFor="release-reason" required>
          Reason
        </Label>
        <Textarea
          id="release-reason"
          autoFocus
          placeholder="e.g. Order cancelled by the customer on 12 Aug"
          aria-invalid={Boolean(form.formState.errors.reason)}
          {...form.register('reason')}
        />
        <FieldError message={form.formState.errors.reason?.message} />
        <FieldError message={form.formState.errors.root?.message} />
      </div>
    </ConfirmDialog>
  )
}

function ReallocationInbox() {
  const queryClient = useQueryClient()
  const [decision, setDecision] = useState<{
    request: ReallocationRequest
    action: 'approve' | 'reject'
  } | null>(null)
  const list = useListQuery<ReallocationRequest>({
    path: '/allocation/reallocation-requests',
    queryKey: queryKeys.allocation.reallocationRequests,
    namespace: 'reallocation',
    filters: { status: 'PENDING' },
    defaultSortBy: 'created_at',
    defaultSortDir: 'asc',
    staleTime: staleTimes.live,
    refetchOnWindowFocus: true,
  })

  const decide = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'approve' | 'reject' }) =>
      api.post<ReallocationRequest>(`/allocation/reallocation-requests/${id}/${action}`, {}),
    onSuccess: (_result, variables) => {
      setDecision(null)
      toast.success(
        variables.action === 'approve' ? 'Reallocation approved' : 'Reallocation rejected',
        'The approval queue and reservation list were refreshed.',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
    },
    onError: (error) => toast.error('Could not decide reallocation', error.message),
  })

  const columns = useMemo<ColumnDef<ReallocationRequest, unknown>[]>(
    () => [
      {
        id: 'requested',
        header: 'Requested',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <span className="font-medium">
              {row.original.to_demand_code ?? humanise(row.original.to_demand_type)}
            </span>
            <span className="block text-xs text-muted-foreground">
              from{' '}
              {row.original.from_demand_code ?? humanise(row.original.from_demand_type ?? 'donor')}
            </span>
          </div>
        ),
      },
      {
        id: 'item',
        header: 'Item / warehouse',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <span className="font-mono text-xs">
              {row.original.item_code ?? row.original.item_id}
            </span>
            <span className="block text-xs text-muted-foreground">
              {row.original.warehouse_code ?? row.original.warehouse_id}
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
        id: 'reason',
        header: 'Reason',
        enableSorting: false,
        cell: ({ row }) => <span className="max-w-[22rem] truncate">{row.original.reason}</span>,
      },
      {
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              onClick={() => setDecision({ request: row.original, action: 'approve' })}
              loading={decide.isPending}
            >
              Approve
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setDecision({ request: row.original, action: 'reject' })}
              loading={decide.isPending}
            >
              Reject
            </Button>
          </div>
        ),
      },
    ],
    [decide],
  )

  return (
    <section className="mb-6 space-y-3">
      <div>
        <h2 className="text-lg font-semibold">Reallocation approval inbox</h2>
        <p className="text-sm text-muted-foreground">
          Approving releases the donor reservation and links the new reservation to the recipient
          demand line.
        </p>
      </div>
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
        emptyTitle="No reallocation requests await approval"
        emptyDescription="Urgent stock requests will appear here when planners raise them."
      />
      <ConfirmDialog
        open={decision !== null}
        onOpenChange={(open) => {
          if (!open && !decide.isPending) setDecision(null)
        }}
        destructive={decision?.action === 'reject'}
        title={
          decision?.action === 'approve'
            ? 'Approve this reallocation?'
            : 'Reject this reallocation?'
        }
        description={
          decision && (
            <>
              {decision.action === 'approve' ? 'Move' : 'Do not move'}{' '}
              <span className="font-medium">{formatQuantity(decision.request.quantity)}</span> of{' '}
              <span className="font-medium">
                {decision.request.item_code ?? decision.request.item_id}
              </span>{' '}
              from {decision.request.from_demand_code ?? 'the donor demand'} to{' '}
              {decision.request.to_demand_code ?? 'the requesting demand'}?{' '}
              {decision.request.reason}
            </>
          )
        }
        confirmLabel={decision?.action === 'approve' ? 'Approve and reallocate' : 'Reject request'}
        loading={decide.isPending}
        onConfirm={() => {
          if (decision) decide.mutate({ id: decision.request.id, action: decision.action })
        }}
      />
    </section>
  )
}

// --------------------------------------------------------------- page ------

export default function ReservationsPage() {
  const { can } = useAuth()
  // Affordances only. The API authorises each action independently.
  const canReserve = can(P.RESERVATION_CREATE)
  const canRelease = can(P.RESERVATION_RELEASE)
  const canReallocate = can(P.ALLOCATION_REALLOCATE)
  const canApproveReallocation = can(P.ALLOCATION_APPROVE) && can(P.ALLOCATION_VIEW)

  const [createOpen, setCreateOpen] = useState(false)
  const [releaseTarget, setReleaseTarget] = useState<ReservationRow | null>(null)
  const [reallocationTarget, setReallocationTarget] = useState<ReservationRow | null>(null)

  // Filters live in the URL, like every other list screen: `list.setParam`
  // writes the key *and* resets `page` in one update. Held in component state
  // they left `page` behind, so narrowing the filter while on page 3 of the
  // unfiltered list asked the API for page 3 of a two-page result and the grid
  // came back empty.
  const [searchParams] = useSearchParams()
  const warehouseId = searchParams.get('warehouse_id') ?? ''
  const demandType = searchParams.get('demand_type') ?? ''
  const reservationType = searchParams.get('reservation_type') ?? ''
  const status = searchParams.get('status') ?? ''

  const list = useListQuery<ReservationRow>({
    path: '/allocation/reservations',
    queryKey: queryKeys.allocation.reservations,
    filters: {
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(demandType ? { demand_type: demandType } : {}),
      ...(reservationType ? { reservation_type: reservationType } : {}),
      ...(status ? { status } : {}),
    },
    defaultSortBy: 'required_date',
    defaultSortDir: 'asc',
    // A reservation list is a view of live claims on stock (master rules 34).
    staleTime: staleTimes.live,
    refetchOnWindowFocus: true,
  })

  const columns = useMemo<ColumnDef<ReservationRow, unknown>[]>(
    () => [
      {
        id: 'item_code',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="min-w-0">
            <Link
              to={'/items/' + row.original.item_id}
              className="font-mono text-xs hover:underline"
            >
              {row.original.item_code ?? row.original.item_id}
            </Link>
            <span className="block max-w-[16rem] truncate text-xs text-muted-foreground">
              {row.original.item_name ?? ''}
            </span>
          </div>
        ),
      },
      {
        id: 'warehouse_code',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => row.original.warehouse_code ?? '-',
      },
      {
        id: 'demand_code',
        header: 'Demand',
        enableSorting: false,
        cell: ({ row }) => {
          const route = DEMAND_ROUTES[row.original.demand_type]
          const label = row.original.demand_code ?? humanise(row.original.demand_type)
          return (
            <div className="min-w-0">
              {route ? (
                <Link to={route(row.original.demand_id)} className="font-medium hover:underline">
                  {label}
                </Link>
              ) : (
                <span className="font-medium">{label}</span>
              )}
              <span className="block text-xs text-muted-foreground">
                {humanise(row.original.demand_type)}
              </span>
            </div>
          )
        },
      },
      {
        id: 'reservation_type',
        header: 'Type',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={row.original.reservation_type === 'HARD' ? 'warning' : 'secondary'}>
            {row.original.reservation_type}
          </Badge>
        ),
      },
      {
        accessorKey: 'priority',
        header: 'Priority',
        meta: { align: 'right' },
        cell: ({ row }) => row.original.priority,
      },
      {
        id: 'quantity',
        header: 'Reserved',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.quantity),
      },
      {
        id: 'consumed_quantity',
        header: 'Consumed',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.consumed_quantity),
      },
      {
        // What has been handed back rather than used. A claim can end well short
        // of its quantity without anything being consumed, and until this was
        // shown the two cases read identically on the row.
        id: 'released_quantity',
        header: 'Released',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {formatQuantity(row.original.released_quantity)}
          </span>
        ),
      },
      {
        id: 'remaining_quantity',
        header: 'Remaining',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.remaining_quantity)}</span>
        ),
      },
      {
        accessorKey: 'required_date',
        header: 'Required',
        cell: ({ row }) => {
          const days = daysUntil(row.original.required_date)
          const overdue = days !== null && days < 0 && LIVE_STATUSES.has(row.original.status)
          return (
            <span className={cn('whitespace-nowrap', overdue && 'text-destructive')}>
              {formatDate(row.original.required_date)}
              {overdue && <span className="sr-only"> - overdue</span>}
            </span>
          )
        },
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const live = LIVE_STATUSES.has(row.original.status)
          if (!canRelease && !canReallocate) return null
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={'Actions for ' + (row.original.item_code ?? row.original.item_id)}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {canReallocate && (
                  <DropdownMenuItem
                    disabled={
                      !live ||
                      (row.original.demand_type === 'SALES_ORDER' && !row.original.demand_line_id)
                    }
                    onSelect={() => setReallocationTarget(row.original)}
                  >
                    <ArrowLeftRight />
                    {row.original.demand_type === 'SALES_ORDER' && !row.original.demand_line_id
                      ? 'Demand line required to reallocate'
                      : 'Suggest reallocation'}
                  </DropdownMenuItem>
                )}
                {canRelease && (
                  <DropdownMenuItem
                    destructive
                    disabled={!live}
                    onSelect={() => setReleaseTarget(row.original)}
                  >
                    <Unlink />
                    Release
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )
        },
      },
    ],
    [canRelease, canReallocate],
  )

  return (
    <div>
      {canApproveReallocation && <ReallocationInbox />}
      <PageHeader
        title="Reservations"
        description="Stock promised to a specific order. Reserved quantity is still physically present - it is simply spoken for."
      />

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
        // No search box: `GET /allocation/reservations` takes no free-text
        // parameter, so a term typed here would be dropped by the API and the
        // grid would answer an unfiltered list as though it had searched.
        // Narrow with the item and warehouse filters instead.
        onRefresh={() => void list.refetch()}
        getRowId={(row) => row.id}
        emptyTitle="No reservations match these filters"
        emptyDescription="Confirming a sales order or releasing a production order creates reservations here."
        emptyAction={
          <p className="inline-flex items-center gap-2 text-xs text-muted-foreground">
            <Handshake className="h-4 w-4" aria-hidden />
            Every promise made against stock is recorded on this screen.
          </p>
        }
        toolbar={
          <>
            <WarehouseSelect
              id="reservations-warehouse"
              label="Warehouse"
              hideLabel
              includeAll
              className="w-full sm:w-auto"
              value={warehouseId}
              onChange={(next) => list.setParam('warehouse_id', next || undefined)}
            />

            <div className="w-full sm:w-auto">
              <Label htmlFor="reservations-demand-type" className="sr-only">
                Demand type
              </Label>
              <Select
                value={demandType || ALL}
                onValueChange={(next) =>
                  list.setParam('demand_type', next === ALL ? undefined : next)
                }
              >
                <SelectTrigger
                  id="reservations-demand-type"
                  aria-label="Demand type"
                  className="sm:min-w-[170px]"
                >
                  <SelectValue placeholder="All demand types" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All demand types</SelectItem>
                  {DEMAND_TYPES.map((type) => (
                    <SelectItem key={type} value={type}>
                      {humanise(type)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="w-full sm:w-auto">
              <Label htmlFor="reservations-type" className="sr-only">
                Reservation type
              </Label>
              <Select
                value={reservationType || ALL}
                onValueChange={(next) =>
                  list.setParam('reservation_type', next === ALL ? undefined : next)
                }
              >
                <SelectTrigger
                  id="reservations-type"
                  aria-label="Reservation type"
                  className="sm:min-w-[150px]"
                >
                  <SelectValue placeholder="Hard and soft" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>Hard and soft</SelectItem>
                  <SelectItem value="HARD">Hard only</SelectItem>
                  <SelectItem value="SOFT">Soft only</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="w-full sm:w-auto">
              <Label htmlFor="reservations-status" className="sr-only">
                Status
              </Label>
              <Select
                value={status || ALL}
                onValueChange={(next) => list.setParam('status', next === ALL ? undefined : next)}
              >
                <SelectTrigger
                  id="reservations-status"
                  aria-label="Status"
                  className="sm:min-w-[160px]"
                >
                  <SelectValue placeholder="All statuses" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All statuses</SelectItem>
                  {RESERVATION_STATUSES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {humanise(value)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {canReserve && (
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus />
                Reserve stock
              </Button>
            )}
          </>
        }
      />

      {/* Mounted only while open, so the availability read starts fresh each
          time rather than holding a figure from the last time it was used. */}
      {canReserve && createOpen && <CreateReservationDialog onClose={() => setCreateOpen(false)} />}

      {releaseTarget && (
        <ReleaseReservationDialog
          reservation={releaseTarget}
          onClose={() => setReleaseTarget(null)}
        />
      )}

      {reallocationTarget && (
        <ReallocationRequestDialog
          demand={{
            itemId: reallocationTarget.item_id,
            itemCode: reallocationTarget.item_code,
            itemName: reallocationTarget.item_name,
            warehouseId: reallocationTarget.warehouse_id,
            warehouseCode: reallocationTarget.warehouse_code,
            demandType: reallocationTarget.demand_type,
            demandId: reallocationTarget.demand_id,
            demandLineId: reallocationTarget.demand_line_id,
            demandCode: reallocationTarget.demand_code,
            priority: reallocationTarget.priority,
            requiredDate: reallocationTarget.required_date,
            alreadyHolding: reallocationTarget.remaining_quantity,
          }}
          onClose={() => setReallocationTarget(null)}
        />
      )}
    </div>
  )
}

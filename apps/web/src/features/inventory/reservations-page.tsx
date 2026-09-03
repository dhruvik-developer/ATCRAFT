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
import { applyFieldErrors } from '@/lib/form-errors'
import { daysUntil, formatDate, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import { ItemPicker, WarehouseSelect } from './pickers'
import {
  DEMAND_TYPES,
  RESERVATION_STATUSES,
  RESERVATION_TYPES,
  UUID_PATTERN,
  isDemandType,
  isReservationType,
  type AvailableToPromise,
  type DemandType,
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

/** Where the demand behind a reservation is managed. */
const DEMAND_ROUTES: Record<DemandType, ((id: string) => string) | null> = {
  SALES_ORDER: (id) => '/sales-orders/' + id,
  PRODUCTION_ORDER: (id) => '/production/' + id,
  ASSEMBLY_ORDER: () => '/assembly',
  JOB_WORK_ORDER: () => '/job-work',
  TRANSFER: () => '/inventory/movements',
  SPARE: null,
}

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
    .max(255, 'Keep the reason under 255 characters - the API stores no more.'),
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
                A hard reservation cannot be taken by a reallocation without an approver signing
                the loss.
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
        'Reservation released',
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
      </div>
    </ConfirmDialog>
  )
}

// ------------------------------------------------------- reallocation ------

const reallocationSchema = z.object({
  // Asked for rather than assumed. The row's `remaining_quantity` is what this
  // demand already *holds*, not what it is short of - claiming that much again
  // would leave the demand holding twice the stock once an approver signs it,
  // because executing the request opens a second reservation for the recipient.
  // Nothing on this screen knows the shortfall, so the planner states it.
  quantity: z
    .string()
    .trim()
    .refine(
      (value) => QUANTITY_PATTERN.test(value) && toDecimal(value).greaterThan(0),
      'Enter how much more is needed, greater than zero and to 4 decimal places.',
    ),
  reason: z
    .string()
    .trim()
    .min(5, 'Explain why this demand should take priority over the one losing stock.'),
})

type ReallocationForm = z.infer<typeof reallocationSchema>

/**
 * Raise a reallocation request against the donor the engine picks.
 *
 * There is no preview step, and there never was one on the server: ranking the
 * donors and raising the request are a single call, because ranking them first
 * and asking the browser to post the winner back would leave a window in which
 * that reservation is taken by somebody else - and would let a client name a
 * donor the engine never offered. So this dialog asks for the shortfall and the
 * reason, sends one request, and then shows what the engine decided and what it
 * turned down.
 *
 * The row it opens from is the demand being *helped*, not the donor: the engine
 * excludes the recipient's own demand from the candidate pool, so this
 * reservation can never be the one that gives way. What the recipient is short
 * of is not on the row - `remaining_quantity` is what it already holds - so the
 * quantity is typed rather than inferred.
 *
 * Nothing moves either way until an approver acts on the PENDING request.
 */
function ReallocationDialog({
  reservation,
  onClose,
}: {
  reservation: ReservationRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [outcome, setOutcome] = useState<ReallocationRequest | null>(null)

  const form = useForm<ReallocationForm>({
    resolver: zodResolver(reallocationSchema),
    defaultValues: { quantity: '', reason: '' },
  })

  const raise = useMutation({
    mutationFn: (values: ReallocationForm) =>
      api.post<ReallocationRequest>('/allocation/reallocation-requests', {
        item_id: reservation.item_id,
        warehouse_id: reservation.warehouse_id,
        // What this demand is short of, as typed - a decimal string all the way
        // to the wire, never parsed into a double. The engine nets free stock
        // off it before it ranks a single donor.
        quantity: values.quantity.trim(),
        to_demand_type: reservation.demand_type,
        to_demand_id: reservation.demand_id,
        to_demand_code: reservation.demand_code,
        priority: reservation.priority,
        required_date: reservation.required_date,
        reason: values.reason.trim(),
      }),
    onSuccess: (result) => {
      setOutcome(result)
      toast.success(
        'Reallocation requested',
        formatQuantity(result.quantity) +
          ' claimed from ' +
          (result.from_demand_code ?? humanise(result.from_demand_type ?? 'the donor')) +
          '. The request is ' +
          humanise(result.status) +
          '.',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  const chosen = outcome?.candidates.find(
    (candidate) => candidate.reservation_id === outcome.from_reservation_id,
  )
  const alternatives =
    outcome?.candidates.filter(
      (candidate) => candidate.reservation_id !== outcome.from_reservation_id,
    ) ?? []

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{outcome ? 'Reallocation requested' : 'Suggest a reallocation'}</DialogTitle>
          <DialogDescription>
            {outcome ? (
              <>
                The allocation engine chose the donor and raised the request. Nothing has moved: an
                approver has to sign it first.
              </>
            ) : (
              <>
                {reservation.demand_code ?? humanise(reservation.demand_type)} already holds{' '}
                {formatQuantity(reservation.remaining_quantity)} of{' '}
                {reservation.item_code ?? reservation.item_id} at{' '}
                {reservation.warehouse_code ?? 'this warehouse'}. Say how much <em>more</em> it
                needs and the engine ranks the reservations that could give that up, then raises the
                request against the best one.
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {!outcome && (
            <>
              <Alert>
                <AlertTitle>Who gives the stock up is decided by the engine</AlertTitle>
                <AlertDescription>
                  Donors are ranked on priority, required date and how much of the claim is still
                  unconsumed. Scoring it in the browser would mean guessing at priorities the server
                  owns, and the ranking has to be taken under a lock, or two planners promise
                  themselves the same units.
                </AlertDescription>
              </Alert>

              <div className="space-y-1.5">
                <Label htmlFor="reallocation-quantity" required>
                  Quantity still needed
                </Label>
                {/* A text input, not `type="number"`: the browser normalises a
                    numeric field through a double, and a 4-decimal quantity
                    does not survive that round trip. */}
                <Input
                  id="reallocation-quantity"
                  autoFocus
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder="0"
                  className="max-w-[12rem] text-right tabular"
                  aria-invalid={Boolean(form.formState.errors.quantity)}
                  aria-describedby="reallocation-quantity-hint"
                  {...form.register('quantity')}
                />
                <p id="reallocation-quantity-hint" className="text-xs text-muted-foreground">
                  On top of the {formatQuantity(reservation.remaining_quantity)} this demand is
                  already holding. Free stock at the position is netted off before any donor is
                  considered.
                </p>
                <FieldError message={form.formState.errors.quantity?.message} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="reallocation-reason" required>
                  Reason
                </Label>
                <Textarea
                  id="reallocation-reason"
                  placeholder="e.g. Customer escalation - SO/2026-27/00184 ships this week"
                  aria-invalid={Boolean(form.formState.errors.reason)}
                  {...form.register('reason')}
                />
                <FieldError message={form.formState.errors.reason?.message} />
              </div>
            </>
          )}

          {outcome && (
            <>
              <div className="rounded-md border border-primary/50 bg-primary/5 p-3">
                <p className="text-sm font-medium">
                  Taking {formatQuantity(outcome.quantity)} from{' '}
                  {outcome.from_demand_code ??
                    humanise(outcome.from_demand_type ?? 'another reservation')}
                </p>
                <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  {outcome.from_reservation_type && (
                    <Badge
                      variant={outcome.from_reservation_type === 'HARD' ? 'warning' : 'secondary'}
                    >
                      {outcome.from_reservation_type}
                    </Badge>
                  )}
                  <StatusBadge status={outcome.status} />
                </p>
                {chosen && chosen.reasons.length > 0 && (
                  <ul className="mt-2 list-inside list-disc text-xs text-muted-foreground">
                    {chosen.reasons.map((why) => (
                      <li key={why}>{why}</li>
                    ))}
                  </ul>
                )}
                {chosen?.requires_approval && (
                  <p className="mt-2 text-xs text-warning">
                    This donor holds a hard reservation, so the request waits for an allocation
                    approver before any stock moves.
                  </p>
                )}
              </div>

              {alternatives.length > 0 ? (
                <div>
                  <p className="mb-2 text-sm font-medium">Also considered ({alternatives.length})</p>
                  <ul className="space-y-2">
                    {alternatives.map((candidate) => {
                      const route = DEMAND_ROUTES[candidate.demand_type]
                      return (
                        <li
                          key={candidate.reservation_id}
                          className="flex items-start justify-between gap-3 rounded-md border p-3"
                        >
                          <div className="min-w-0">
                            <p className="flex flex-wrap items-center gap-2 text-sm">
                              <span className="font-medium">
                                {candidate.demand_code ?? humanise(candidate.demand_type)}
                              </span>
                              <Badge
                                variant={
                                  candidate.reservation_type === 'HARD' ? 'warning' : 'secondary'
                                }
                              >
                                {candidate.reservation_type}
                              </Badge>
                              <span className="text-xs text-muted-foreground">
                                Priority {candidate.priority}
                              </span>
                            </p>
                            {candidate.reasons.length > 0 && (
                              <p className="mt-1 text-xs text-muted-foreground">
                                {candidate.reasons.join('. ')}
                              </p>
                            )}
                            {route && (
                              <Link
                                to={route(candidate.demand_id)}
                                className="mt-1 inline-block text-xs text-primary hover:underline"
                              >
                                Open the demand
                              </Link>
                            )}
                          </div>
                          <div className="shrink-0 text-right">
                            <p className="tabular font-medium">
                              {formatQuantity(candidate.takeable_quantity)}
                            </p>
                            <p className="text-xs text-muted-foreground">could give up</p>
                            <p className="text-xs text-muted-foreground">
                              {formatDate(candidate.required_date)}
                            </p>
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                </div>
              ) : (
                <Alert variant="warning">
                  <AlertTitle>No alternative donor</AlertTitle>
                  <AlertDescription>
                    Nothing else holding this item at this warehouse could have given stock up. If
                    this request is rejected, the shortage has to be covered by a purchase, a
                    production order or a transfer.
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          {outcome ? (
            <Button onClick={onClose}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={onClose} disabled={raise.isPending}>
                Cancel
              </Button>
              <Button
                onClick={form.handleSubmit((values) => raise.mutate(values))}
                loading={raise.isPending}
              >
                Raise reallocation request
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// --------------------------------------------------------------- page ------

export default function ReservationsPage() {
  const { can } = useAuth()
  // Affordances only. The API authorises each action independently.
  const canReserve = can(P.RESERVATION_CREATE)
  const canRelease = can(P.RESERVATION_RELEASE)
  const canReallocate = can(P.ALLOCATION_REALLOCATE)

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
                    disabled={!live}
                    onSelect={() => setReallocationTarget(row.original)}
                  >
                    <ArrowLeftRight />
                    Suggest reallocation
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
              value={warehouseId}
              onChange={(next) => list.setParam('warehouse_id', next || undefined)}
            />

            <div>
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
                  className="min-w-[170px]"
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

            <div>
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
                  className="min-w-[150px]"
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

            <div>
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
                  className="min-w-[160px]"
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
        <ReallocationDialog
          reservation={reallocationTarget}
          onClose={() => setReallocationTarget(null)}
        />
      )}
    </div>
  )
}

/**
 * The two write paths a work order has: assignment and the state machine.
 *
 * Both endpoints are gated on `work_order.manage` in
 * `apps/api/src/modules/production/router.py` - not on `work_order.view`, and
 * not on any of the production-order rights. The caller mounts these dialogs
 * only for a user who holds that permission; the API authorises each request
 * independently regardless.
 *
 * Neither dialog touches a quantity. `WorkOrderUpdate` says it in as many
 * words - "Quantities move through bookings, never here" - so what came off the
 * machine is recorded on the production order as a booking and never typed in
 * against the operation.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { format, isValid, parseISO } from 'date-fns'
import { useEffect, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { ConfirmDialog, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Alert, AlertDescription, Input, Label, Textarea } from '@/components/ui/primitives'
import { useOperatorOptions } from '@/hooks/use-operator-options'
import { ShiftSelect } from '@/features/shifts/shift-select'
import { useAuth } from '@/features/auth/auth-context'
import { P } from '@/lib/permissions'
import type { SchemaOf } from '@/types/api'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, SELECT_NONE, LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { formatDateTime, humanise } from '@/lib/format'
import { applyFieldErrors } from '@/lib/form-errors'
import { success } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  MAX_NOTES_LENGTH,
  optionLabel,
  useWorkCentreMachineOptions,
  type WorkOrder,
  type WorkOrderTransition,
  type WorkOrderTransitionPayload,
  type WorkOrderUpdatePayload,
} from './work-order-model'

/** `datetime-local` wants `yyyy-MM-ddTHH:mm` in the browser's own zone. */
function toDateTimeInput(value: string | null | undefined): string {
  if (!value) return ''
  const parsed = parseISO(value)
  return isValid(parsed) ? format(parsed, "yyyy-MM-dd'T'HH:mm") : ''
}

/** The API stores UTC; the picker collects local time. */
function fromDateTimeInput(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = new Date(trimmed)
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

// ------------------------------------------------- assign and reschedule -----

const assignSchema = z
  .object({
    operator_user_id: z.string(),
    machine_id: z.string(),
    shift_id: z.string(),
    planned_start_at: z.string(),
    planned_end_at: z.string(),
    notes: z.string().trim().max(MAX_NOTES_LENGTH, `At most ${MAX_NOTES_LENGTH} characters`),
  })
  // The same rule `update_work_order` enforces, checked here so the planner is
  // told before the request rather than by a 422 after it. Both values are
  // `yyyy-MM-ddTHH:mm` in one zone, which compares correctly as text.
  .refine(
    (values) =>
      !values.planned_start_at ||
      !values.planned_end_at ||
      values.planned_end_at >= values.planned_start_at,
    { message: 'The planned finish cannot be before the planned start', path: ['planned_end_at'] },
  )

type AssignFormValues = z.infer<typeof assignSchema>

function formValues(workOrder: WorkOrder): AssignFormValues {
  return {
    operator_user_id: workOrder.operator_user_id ?? SELECT_NONE,
    machine_id: workOrder.machine_id ?? SELECT_NONE,
    shift_id: workOrder.shift_id ?? SELECT_NONE,
    planned_start_at: toDateTimeInput(workOrder.planned_start_at),
    planned_end_at: toDateTimeInput(workOrder.planned_end_at),
    notes: workOrder.notes ?? '',
  }
}

/**
 * Only what the planner actually altered.
 *
 * `WorkOrderUpdate` is applied with `exclude_unset`, so an omitted key leaves
 * the column alone - and that is the safe way to send this form. The operator
 * and machine lists are permissioned separately from the queue and soft-fail to
 * empty; posting every field would let a lookup this account cannot read blank
 * an assignment nobody touched.
 *
 * The two timestamps are compared in the picker's own format rather than as
 * instants: a stored time carries seconds that `datetime-local` cannot show, so
 * an untouched field would otherwise look like an edit that truncates it.
 */
function buildChanges(workOrder: WorkOrder, values: AssignFormValues): WorkOrderUpdatePayload {
  const changes: WorkOrderUpdatePayload = {}

  const operator = values.operator_user_id === SELECT_NONE ? null : values.operator_user_id
  if (operator !== workOrder.operator_user_id) changes.operator_user_id = operator

  const machine = values.machine_id === SELECT_NONE ? null : values.machine_id
  if (machine !== workOrder.machine_id) changes.machine_id = machine

  const shift = values.shift_id === SELECT_NONE ? null : values.shift_id
  if (shift !== workOrder.shift_id) changes.shift_id = shift

  if (values.planned_start_at !== toDateTimeInput(workOrder.planned_start_at)) {
    changes.planned_start_at = fromDateTimeInput(values.planned_start_at)
  }
  if (values.planned_end_at !== toDateTimeInput(workOrder.planned_end_at)) {
    changes.planned_end_at = fromDateTimeInput(values.planned_end_at)
  }

  const notes = orNull(values.notes)
  if (notes !== workOrder.notes) changes.notes = notes

  return changes
}

/**
 * Assign an operation to a person and a machine, and move its planned window.
 *
 * `PATCH /production/work-orders/{id}` - `work_order.manage`.
 *
 * The machine list is scoped to this operation's own work centre because the
 * service refuses anything else ("Machine X does not belong to this
 * operation's work centre"). The work centre itself is not editable here: it is
 * set by the routing when the job is released, and moving an operation to
 * another cell is a routing change, not an assignment.
 */
export function WorkOrderAssignDialog({
  workOrder,
  onOpenChange,
}: {
  workOrder: WorkOrder | null
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<AssignFormValues>({
    resolver: zodResolver(assignSchema),
    defaultValues: workOrder ? formValues(workOrder) : undefined,
  })

  useEffect(() => {
    if (workOrder) reset(formValues(workOrder))
  }, [workOrder, reset])

  const operatorId = watch('operator_user_id')
  const shiftId = watch('shift_id')
  const rosterDate = workOrder?.planned_start_at
    ? toDateTimeInput(workOrder.planned_start_at).slice(0, 10)
    : format(new Date(), 'yyyy-MM-dd')
  const roster = useQuery({
    queryKey: [
      ...queryKeys.routing.all,
      'roster',
      'assignment-hint',
      workOrder?.id,
      operatorId,
      shiftId,
      rosterDate,
    ],
    queryFn: () =>
      api.getPage<SchemaOf<'ShiftAssignmentRead'>>('/routing/roster/assignments', {
        params: {
          date_from: rosterDate,
          date_to: rosterDate,
          work_center_id: workOrder?.work_center_id,
          operator_user_id: operatorId,
          shift_id: shiftId,
          status: 'PLANNED',
          limit: LOOKUP_LIST_LIMIT,
          with_total: false,
        },
      }),
    enabled:
      Boolean(workOrder) &&
      can(P.SHIFT_ROSTER_VIEW) &&
      Boolean(operatorId && operatorId !== SELECT_NONE && shiftId && shiftId !== SELECT_NONE),
  })
  const operators = useOperatorOptions({ enabled: Boolean(workOrder) })
  const machines = useWorkCentreMachineOptions(workOrder?.work_center_id)

  const operatorRows = operators.data ?? []
  const machineRows = machines.data ?? []

  // An id the lookup could not resolve still belongs in the list, or opening
  // the form and saving an unrelated field would silently unassign whoever is
  // on the job today.
  const assignedOperatorId = workOrder?.operator_user_id ?? null
  const assignedMachineId = workOrder?.machine_id ?? null
  const unresolvedOperator =
    assignedOperatorId && !operatorRows.some((row) => row.id === assignedOperatorId)
      ? assignedOperatorId
      : null
  const unresolvedMachine =
    assignedMachineId && !machineRows.some((row) => row.id === assignedMachineId)
      ? assignedMachineId
      : null

  const save = useMutation({
    mutationFn: (values: AssignFormValues) => {
      if (!workOrder) throw new Error('No work order selected')
      return api.patch<WorkOrder>(
        `/production/work-orders/${workOrder.id}`,
        buildChanges(workOrder, values),
      )
    },
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      toast.success(success.updated('Operation'), `${updated.code} - ${updated.operation_name}`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const submit = handleSubmit((values) => {
    if (!workOrder) return
    // An empty PATCH is accepted and does nothing; saying so is more use than a
    // success toast for a change that was never made.
    if (Object.keys(buildChanges(workOrder, values)).length === 0) {
      toast.show('Nothing to save', 'No field on this operation was changed.')
      return
    }
    save.mutate(values)
  })

  return (
    <Dialog open={Boolean(workOrder)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {workOrder
              ? `Assign ${workOrder.operation_code} - ${workOrder.operation_name}`
              : 'Assign operation'}
          </DialogTitle>
          <DialogDescription>
            Who runs this operation, on what, and when it is planned for. Quantities are recorded as
            production bookings against the job, never here.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex min-h-0 flex-col gap-4" noValidate>
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="work-order-operator">Operator</Label>
              <Controller
                control={control}
                name="operator_user_id"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger
                      id="work-order-operator"
                      className="mt-1.5"
                      aria-label="Operator"
                    >
                      <SelectValue placeholder="Unassigned" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_NONE}>Unassigned</SelectItem>
                      {unresolvedOperator && (
                        <SelectItem value={unresolvedOperator}>
                          Currently assigned (name unavailable)
                        </SelectItem>
                      )}
                      {operatorRows.map((operator) => (
                        <SelectItem key={operator.id} value={operator.id}>
                          {operator.full_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError message={errors.operator_user_id?.message} />
            </div>

            <div>
              <Label htmlFor="work-order-machine">Machine</Label>
              <Controller
                control={control}
                name="machine_id"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="work-order-machine" className="mt-1.5" aria-label="Machine">
                      <SelectValue placeholder="Any machine in the cell" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_NONE}>Any machine in the cell</SelectItem>
                      {unresolvedMachine && (
                        <SelectItem value={unresolvedMachine}>
                          Currently assigned (name unavailable)
                        </SelectItem>
                      )}
                      {machineRows.map((machine) => (
                        <SelectItem key={machine.id} value={machine.id}>
                          {optionLabel(machine)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <p className="mt-1 text-xs text-muted-foreground">
                Only machines belonging to this operation&apos;s work centre may be assigned.
              </p>
              <FieldError message={errors.machine_id?.message} />
            </div>

            <div>
              <Label htmlFor="work-order-shift">Shift</Label>
              <Controller
                control={control}
                name="shift_id"
                render={({ field }) => (
                  <ShiftSelect
                    id="work-order-shift"
                    value={field.value === SELECT_NONE ? null : field.value}
                    onChange={(value) => field.onChange(value ?? SELECT_NONE)}
                    enabled={Boolean(workOrder)}
                    legacy={workOrder?.shift}
                  />
                )}
              />
              <FieldError message={errors.shift_id?.message} />
              {Boolean(roster.data?.data.length) && (
                <p className="text-xs text-muted-foreground">
                  Rostered for this cell on {rosterDate}.
                </p>
              )}
            </div>

            <div className="hidden sm:block" aria-hidden />

            <div>
              <Label htmlFor="work-order-planned-start">Planned start</Label>
              <Input
                id="work-order-planned-start"
                type="datetime-local"
                {...register('planned_start_at')}
                className="mt-1.5"
                aria-invalid={Boolean(errors.planned_start_at)}
              />
              <FieldError message={errors.planned_start_at?.message} />
            </div>

            <div>
              <Label htmlFor="work-order-planned-end">Planned finish</Label>
              <Input
                id="work-order-planned-end"
                type="datetime-local"
                {...register('planned_end_at')}
                className="mt-1.5"
                aria-invalid={Boolean(errors.planned_end_at)}
              />
              <FieldError message={errors.planned_end_at?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="work-order-notes">Notes</Label>
              <Textarea
                id="work-order-notes"
                {...register('notes')}
                rows={3}
                className="mt-1.5"
                aria-invalid={Boolean(errors.notes)}
              />
              <FieldError message={errors.notes?.message} />
            </div>
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={save.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending}>
              Save assignment
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------- transitions -----

/** The operation a status change is about to be applied to, and which change. */
export type PendingTransition = {
  workOrder: WorkOrder
  transition: WorkOrderTransition
}

/**
 * Start, complete or cancel one operation.
 *
 * `POST /production/work-orders/{id}/status` - `work_order.manage`.
 *
 * The caller decides which transitions to offer from `transitionsFrom`; this
 * dialog only confirms the one that was chosen. Two things it collects that the
 * contract makes optional:
 *
 * - **Happened at.** Left blank, the API stamps the moment the request lands.
 *   A shift that starts a job at 06:00 and gets to a terminal at 09:00 needs to
 *   say so, because these two timestamps are what the job is costed on.
 * - **Reason.** Optional on the wire; when it is omitted the audit row records
 *   the literal target status as its own reason, which tells the next reader
 *   nothing. A cancellation is therefore made to carry one here - a UI
 *   stricture on top of the contract, not a rule the API enforces.
 */
export function WorkOrderTransitionDialog({
  pending,
  onOpenChange,
}: {
  pending: PendingTransition | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [occurredAt, setOccurredAt] = useState('')
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    if (pending) {
      setReason('')
      setOccurredAt('')
      setTouched(false)
    }
  }, [pending])

  const transition = useMutation({
    mutationFn: ({ workOrder, transition: edge }: PendingTransition) => {
      const payload: WorkOrderTransitionPayload = {
        status: edge.target,
        reason: orNull(reason),
        occurred_at: fromDateTimeInput(occurredAt),
      }
      return api.post<WorkOrder>(`/production/work-orders/${workOrder.id}/status`, payload)
    },
    onSuccess: (updated) => {
      // Starting or completing an operation changes the job's open-operation
      // count and the load on its cell, so the whole production branch goes.
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      toast.success(
        success.statusChanged('Operation', humanise(updated.status)),
        `${updated.code} - ${updated.operation_name}`,
      )
      onOpenChange(false)
    },
  })

  const reasonRequired = pending?.transition.destructive ?? false
  const reasonMissing = reasonRequired && reason.trim().length === 0

  /**
   * A completion cannot predate the start the row already carries - the service
   * refuses it - and an operation that has not started yet has nothing to
   * compare against, which is the `actual_start_at ?? ''` case.
   */
  const startedAt = toDateTimeInput(pending?.workOrder.actual_start_at)
  const endsBeforeStart =
    pending?.transition.target === 'COMPLETED' &&
    Boolean(occurredAt) &&
    Boolean(startedAt) &&
    occurredAt < startedAt

  return (
    <ConfirmDialog
      open={Boolean(pending)}
      onOpenChange={onOpenChange}
      title={pending ? `${pending.transition.label}?` : 'Change this operation'}
      description={
        pending
          ? `${pending.workOrder.code} - ${pending.workOrder.operation_name}. ${pending.transition.description}`
          : undefined
      }
      confirmLabel={pending?.transition.label ?? 'Confirm'}
      destructive={pending?.transition.destructive}
      loading={transition.isPending}
      onConfirm={() => {
        if (!pending) return
        if (reasonMissing || endsBeforeStart) {
          setTouched(true)
          return
        }
        transition.mutate(pending)
      }}
    >
      <div className="space-y-4">
        {pending?.transition.irreversible && (
          <Alert variant="warning">
            <AlertDescription>
              This cannot be undone: {humanise(pending.transition.target)} is a final state for an
              operation.
            </AlertDescription>
          </Alert>
        )}

        <div>
          <Label htmlFor="work-order-occurred-at">Happened at</Label>
          <Input
            id="work-order-occurred-at"
            type="datetime-local"
            value={occurredAt}
            onChange={(event) => setOccurredAt(event.target.value)}
            className="mt-1.5"
            aria-invalid={touched && endsBeforeStart}
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Leave blank to record it as now.
            {startedAt
              ? ` This operation started at ${formatDateTime(pending?.workOrder.actual_start_at)}.`
              : ''}
          </p>
          {touched && endsBeforeStart && (
            <FieldError message="An operation cannot finish before it started" />
          )}
        </div>

        <div>
          <Label htmlFor="work-order-reason" required={reasonRequired}>
            Reason
          </Label>
          <Input
            id="work-order-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            autoComplete="off"
            maxLength={REASON_MAX_LENGTH}
            className="mt-1.5"
            aria-invalid={touched && reasonMissing}
          />
          {touched && reasonMissing ? (
            <FieldError message="Record why this operation is being cancelled" />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              Kept on the audit trail against this operation.
            </p>
          )}
        </div>
      </div>
    </ConfirmDialog>
  )
}

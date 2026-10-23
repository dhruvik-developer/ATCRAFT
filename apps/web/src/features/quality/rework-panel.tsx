/**
 * The rework bench, inside the inspection detail.
 *
 * A rework cycle is one trip to the bench against the inspection's held
 * quantity: OPEN when raised, IN_PROGRESS on the bench, DONE with an outcome.
 * Completing it re-opens the inspection for re-decision, so the quantity is
 * never dispositioned here - even a SCRAPPED outcome leaves the stock in QC
 * hold for the inspector to scrap on re-decision, because the decision is the
 * one release path.
 *
 * Lazy-loaded inside the detail dialog so the bench UI stays out of the
 * quality page's first-load chunk.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { FieldError, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  Checkbox,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useWorkCentreOptions } from '@/features/production/work-order-model'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDateTime, formatMoney, formatQuantity, humanise } from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  QC_REWORK_OUTCOMES,
  type QcInspectionDetail,
  type QcReworkOutcome,
  type QcReworkRecord,
} from './quality-types'

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const MONEY = /^(\d{1,14}(\.\d{1,4})?)?$/

const openSchema = z.object({
  quantity: z
    .string()
    .trim()
    .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => Number(value) > 0, 'Quantity must be more than zero'),
  assign_to_me: z.boolean(),
  work_center_id: z.string(),
  notes: z.string().trim().max(2000, 'At most 2000 characters'),
  reason: z
    .string()
    .trim()
    .min(REASON_MIN_LENGTH, `At least ${REASON_MIN_LENGTH} characters`)
    .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
})

type OpenFormValues = z.infer<typeof openSchema>

function OpenCycleForm({
  inspection,
  onDone,
}: {
  inspection: QcInspectionDetail
  onDone: () => void
}) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const workCentres = useWorkCentreOptions()

  const {
    register,
    control,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<OpenFormValues>({
    resolver: zodResolver(openSchema),
    defaultValues: {
      quantity: inspection.rework_quantity,
      assign_to_me: true,
      work_center_id: SELECT_NONE,
      notes: '',
      reason: '',
    },
  })

  const open = useMutation({
    mutationFn: (values: OpenFormValues) =>
      api.post<QcReworkRecord>('/quality/rework', {
        inspection_id: inspection.id,
        quantity: values.quantity.trim(),
        assigned_to: values.assign_to_me ? (user?.id ?? null) : null,
        work_center_id: values.work_center_id === SELECT_NONE ? null : values.work_center_id,
        notes: values.notes.trim() === '' ? null : values.notes.trim(),
        reason: values.reason.trim(),
      }),
    onSuccess: (record) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
      toast.success(success.created('Rework cycle'), `${record.code} opened on the bench.`)
      onDone()
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <form onSubmit={handleSubmit((values) => open.mutate(values))} className="space-y-3" noValidate>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="rework-quantity">Quantity</Label>
          <Input
            id="rework-quantity"
            inputMode="decimal"
            {...register('quantity')}
            className="mt-1.5"
            aria-invalid={Boolean(errors.quantity)}
          />
          <FieldError message={errors.quantity?.message} />
          <p className="mt-1 text-xs text-muted-foreground">
            Of the {formatQuantity(inspection.rework_quantity)} held for rework.
          </p>
        </div>
        <div>
          <Label htmlFor="rework-centre">Work centre (optional)</Label>
          <Controller
            control={control}
            name="work_center_id"
            render={({ field }) => (
              <Select value={field.value || SELECT_NONE} onValueChange={field.onChange}>
                <SelectTrigger id="rework-centre" className="mt-1.5" aria-label="Work centre">
                  <SelectValue placeholder="No centre" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_NONE}>No centre</SelectItem>
                  {(workCentres.data ?? []).map((centre) => (
                    <SelectItem key={centre.id} value={centre.id}>
                      {centre.code} - {centre.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          <FieldError message={errors.work_center_id?.message} />
        </div>
      </div>

      <div className="flex items-start gap-2">
        <Controller
          control={control}
          name="assign_to_me"
          render={({ field }) => (
            <Checkbox
              id="rework-assign-me"
              checked={field.value}
              onCheckedChange={(checked) => field.onChange(checked === true)}
            />
          )}
        />
        <Label htmlFor="rework-assign-me" className="font-normal">
          Assign to me
        </Label>
      </div>

      <div>
        <Label htmlFor="rework-notes">Notes (optional)</Label>
        <Textarea
          id="rework-notes"
          {...register('notes')}
          rows={2}
          maxLength={2000}
          className="mt-1.5"
          aria-invalid={Boolean(errors.notes)}
        />
        <FieldError message={errors.notes?.message} />
      </div>

      <div>
        <Label htmlFor="rework-reason">Reason</Label>
        <Textarea
          id="rework-reason"
          {...register('reason')}
          rows={2}
          maxLength={REASON_MAX_LENGTH}
          className="mt-1.5"
          aria-invalid={Boolean(errors.reason)}
        />
        <FieldError message={errors.reason?.message} />
      </div>

      {open.error && (
        <Alert variant="destructive">
          <AlertDescription>{describeError(open.error)}</AlertDescription>
        </Alert>
      )}

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onDone} disabled={open.isPending}>
          Cancel
        </Button>
        <Button type="submit" loading={open.isPending}>
          Open cycle
        </Button>
      </div>
    </form>
  )
}

const completeSchema = z.object({
  outcome: z.enum(QC_REWORK_OUTCOMES),
  cost: z.string().trim().regex(MONEY, 'Enter an amount with up to 4 decimal places'),
  notes: z.string().trim().max(2000, 'At most 2000 characters'),
  reason: z
    .string()
    .trim()
    .min(REASON_MIN_LENGTH, `At least ${REASON_MIN_LENGTH} characters`)
    .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
})

type CompleteFormValues = z.infer<typeof completeSchema>

function CompleteCycleForm({ record, onDone }: { record: QcReworkRecord; onDone: () => void }) {
  const queryClient = useQueryClient()
  const {
    register,
    control,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<CompleteFormValues>({
    resolver: zodResolver(completeSchema),
    defaultValues: { outcome: 'REWORKED_OK', cost: '', notes: '', reason: '' },
  })

  const complete = useMutation({
    mutationFn: (values: CompleteFormValues) =>
      api.post<QcReworkRecord>(`/quality/rework/${record.id}/complete`, {
        outcome: values.outcome,
        cost: values.cost.trim() === '' ? null : values.cost.trim(),
        notes: values.notes.trim() === '' ? null : values.notes.trim(),
        reason: values.reason.trim(),
      }),
    onSuccess: (done) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
      toast.success(
        success.updated('Rework cycle'),
        `${done.code} completed ${done.outcome} - the inspection is owed a re-decision.`,
      )
      onDone()
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <form
      onSubmit={handleSubmit((values) => complete.mutate(values))}
      className="space-y-3"
      noValidate
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor={`rework-outcome-${record.id}`}>Outcome</Label>
          <Controller
            control={control}
            name="outcome"
            render={({ field }) => (
              <Select
                value={field.value}
                onValueChange={(value: QcReworkOutcome) => field.onChange(value)}
              >
                <SelectTrigger
                  id={`rework-outcome-${record.id}`}
                  className="mt-1.5"
                  aria-label="Outcome"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="REWORKED_OK">Reworked - back for re-inspection</SelectItem>
                  <SelectItem value="SCRAPPED">Scrapped - inspector to write off</SelectItem>
                  <SelectItem value="RETURNED">Returned - owed to the vendor</SelectItem>
                </SelectContent>
              </Select>
            )}
          />
          <FieldError message={errors.outcome?.message} />
        </div>
        <div>
          <Label htmlFor={`rework-cost-${record.id}`}>Cost (optional)</Label>
          <Input
            id={`rework-cost-${record.id}`}
            inputMode="decimal"
            {...register('cost')}
            className="mt-1.5"
            aria-invalid={Boolean(errors.cost)}
          />
          <FieldError message={errors.cost?.message} />
        </div>
      </div>

      <div>
        <Label htmlFor={`rework-complete-notes-${record.id}`}>Notes (optional)</Label>
        <Textarea
          id={`rework-complete-notes-${record.id}`}
          {...register('notes')}
          rows={2}
          maxLength={2000}
          className="mt-1.5"
          aria-invalid={Boolean(errors.notes)}
        />
        <FieldError message={errors.notes?.message} />
      </div>

      <div>
        <Label htmlFor={`rework-complete-reason-${record.id}`}>Reason</Label>
        <Textarea
          id={`rework-complete-reason-${record.id}`}
          {...register('reason')}
          rows={2}
          maxLength={REASON_MAX_LENGTH}
          className="mt-1.5"
          aria-invalid={Boolean(errors.reason)}
        />
        <FieldError message={errors.reason?.message} />
      </div>

      {complete.error && (
        <Alert variant="destructive">
          <AlertDescription>{describeError(complete.error)}</AlertDescription>
        </Alert>
      )}

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onDone} disabled={complete.isPending}>
          Cancel
        </Button>
        <Button type="submit" loading={complete.isPending}>
          Complete cycle
        </Button>
      </div>
    </form>
  )
}

function CycleCard({ record }: { record: QcReworkRecord }) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [completing, setCompleting] = useState(false)
  const [starting, setStarting] = useState(false)

  const canInspect = can(P.QC_INSPECT)
  const canDecide = can(P.QC_DECIDE)

  const start = useMutation({
    mutationFn: () =>
      api.post<QcReworkRecord>(`/quality/rework/${record.id}/start`, {
        reason: `Cycle ${record.code} taken to the bench`,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
    },
  })

  return (
    <li className="rounded border px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-medium">{record.code}</span>
        <StatusBadge status={record.status} />
        {record.outcome && <StatusBadge status={record.outcome} />}
        <span className="ml-auto text-xs text-muted-foreground">
          {formatQuantity(record.quantity)}
        </span>
      </div>
      <dl className="mt-1.5 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        {record.assigned_to && (
          <div className="flex gap-1.5">
            <dt className="text-muted-foreground">Assignee</dt>
            <dd className="font-mono">{record.assigned_to.slice(0, 8)}</dd>
          </div>
        )}
        {record.work_center_id && (
          <div className="flex gap-1.5">
            <dt className="text-muted-foreground">Centre</dt>
            <dd className="font-mono">{record.work_center_id.slice(0, 8)}</dd>
          </div>
        )}
        {record.started_at && (
          <div className="flex gap-1.5">
            <dt className="text-muted-foreground">Started</dt>
            <dd>{formatDateTime(record.started_at)}</dd>
          </div>
        )}
        {record.completed_at && (
          <div className="flex gap-1.5">
            <dt className="text-muted-foreground">Completed</dt>
            <dd>{formatDateTime(record.completed_at)}</dd>
          </div>
        )}
        {record.cost !== null && (
          <div className="flex gap-1.5">
            <dt className="text-muted-foreground">Cost</dt>
            <dd>{formatMoney(record.cost)}</dd>
          </div>
        )}
      </dl>
      {record.notes && <p className="mt-1.5 text-xs text-muted-foreground">{record.notes}</p>}

      <div className="mt-2 flex flex-wrap gap-2">
        {record.status === 'OPEN' && canInspect && (
          <Button
            variant="outline"
            size="sm"
            disabled={start.isPending}
            onClick={() => {
              setStarting(true)
              start.mutate(undefined, { onSettled: () => setStarting(false) })
            }}
          >
            {starting || start.isPending ? 'Starting…' : 'Start'}
          </Button>
        )}
        {record.status === 'IN_PROGRESS' && canDecide && !completing && (
          <Button variant="outline" size="sm" onClick={() => setCompleting(true)}>
            Complete
          </Button>
        )}
      </div>
      {start.error && <p className="mt-2 text-xs text-destructive">{describeError(start.error)}</p>}
      {completing && <CompleteCycleForm record={record} onDone={() => setCompleting(false)} />}
    </li>
  )
}

export function ReworkPanel({ inspection }: { inspection: QcInspectionDetail }) {
  const { can } = useAuth()
  const [opening, setOpening] = useState(false)
  const canInspect = can(P.QC_INSPECT)

  const records = inspection.rework_records ?? []
  const openCount = records.filter((record) => record.status !== 'DONE').length

  return (
    <section aria-label="Rework cycles">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">
          Rework cycles
          {openCount > 0 && (
            <span className="ml-2 text-xs text-muted-foreground">
              {openCount} open of {records.length}
            </span>
          )}
        </h3>
        {canInspect && inspection.status === 'REWORK' && !opening && (
          <Button variant="outline" size="sm" onClick={() => setOpening(true)}>
            Open a cycle
          </Button>
        )}
      </div>

      {opening && <OpenCycleForm inspection={inspection} onDone={() => setOpening(false)} />}

      {records.length === 0 && !opening ? (
        <p className="text-xs text-muted-foreground">
          {inspection.status === 'REWORK'
            ? 'Nothing on the bench yet. Open a cycle to send quantity for rework.'
            : 'No rework cycles. A cycle is opened from a lot the verdict sent for rework.'}
        </p>
      ) : (
        <ul className="space-y-2">
          {records.map((record) => (
            <CycleCard key={record.id} record={record} />
          ))}
        </ul>
      )}
      <p className="mt-2 text-xs text-muted-foreground">
        Completing a cycle hands the lot back for re-decision - the verdict history below keeps
        every earlier decision. {humanise('REWORKED_OK')} means re-inspect; {humanise('SCRAPPED')}{' '}
        and {humanise('RETURNED')} are the bench&apos;s recommendation for the inspector to
        disposition.
      </p>
    </section>
  )
}

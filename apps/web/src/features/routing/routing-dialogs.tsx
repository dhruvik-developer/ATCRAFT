/**
 * Overlays for the routing screen: the routing detail panel, the three
 * create/edit forms, and the machine condition change.
 *
 * They live together because all four speak the same vocabulary - a work centre
 * rate is what an operation is costed at, a machine belongs to a work centre -
 * and keeping them in one file is what stops the three forms drifting into three
 * different ideas of what a rate is.
 *
 * The costing rule this file exists to hold: **an operation carries times, not a
 * rate.** `RoutingOperationRead` has setup, run, queue and move minutes and a
 * flat `process_cost`; the cost per hour lives on the work centre. Rather than
 * join the two in the browser and risk a second, quieter answer, the detail
 * panel asks `GET /routing/{id}/cost`, which is the API's own roll-up.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { DescriptionList, EmptyState, FieldError, StatusBadge } from '@/components/shared'
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
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Checkbox,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { CostCentreSelect } from '@/features/admin/cost-centre-select'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatDate,
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

import {
  MACHINE_STATUSES,
  MACHINE_STATUS_VARIANT,
  isApprovable,
  isObsoletable,
  type Machine,
  type RoutingCost,
  type RoutingDetail,
  type RoutingOperation,
  type RoutingOperationCost,
  type RoutingRow,
  type WorkCenter,
} from './routing-model'

const DECIMAL = /^\d{1,10}(\.\d{1,4})?$/

function useWorkCenterOptions(enabled: boolean) {
  const params = { limit: LOOKUP_LIST_LIMIT, sort_by: 'code', sort_dir: 'asc', with_total: false }
  return useQuery({
    queryKey: queryKeys.routing.workCenters(params),
    queryFn: () => api.getPage<WorkCenter>('/routing/work-centers', { params }),
    staleTime: staleTimes.master,
    enabled,
  })
}

/** Work centre id -> its row, so an operation can be shown with its centre. */
function useWorkCenterIndex(enabled: boolean) {
  const query = useWorkCenterOptions(enabled)
  return useMemo(() => {
    const map = new Map<string, WorkCenter>()
    for (const centre of query.data?.data ?? []) map.set(centre.id, centre)
    return map
  }, [query.data])
}

// ------------------------------------------------------- routing detail -----

export function RoutingDetailDialog({
  routingId,
  canManage,
  onOpenChange,
}: {
  /** Null closes the panel; an id opens it on that routing. */
  routingId: string | null
  /** Gates the affordance only - the API authorises each move itself. */
  canManage: boolean
  onOpenChange: (open: boolean) => void
}) {
  const open = Boolean(routingId)
  const queryClient = useQueryClient()
  const [statusAction, setStatusAction] = useState<'approve' | 'obsolete' | null>(null)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)

  const detail = useQuery({
    queryKey: queryKeys.routing.routing(routingId ?? ''),
    // `/routing/{id}`, not `/routing/routings/{id}`.
    queryFn: () => api.get<RoutingDetail>(`/routing/${routingId}`),
    enabled: open,
    staleTime: staleTimes.master,
  })

  /**
   * The API's roll-up for one unit. The rate an operation is charged at lives on
   * its work centre, and the working day it converts through lives there too, so
   * this is the only place the two are correctly combined.
   */
  const cost = useQuery({
    queryKey: [...queryKeys.routing.routing(routingId ?? ''), 'cost'] as const,
    queryFn: () => api.get<RoutingCost>(`/routing/${routingId}/cost`, { params: { quantity: 1 } }),
    enabled: open,
    staleTime: staleTimes.master,
  })

  const centres = useWorkCenterIndex(open)

  const operations = useMemo(
    () => [...(detail.data?.operations ?? [])].sort((a, b) => a.sequence - b.sequence),
    [detail.data],
  )

  /** Sequence -> the costed line for it, when the roll-up resolved. */
  const costBySequence = useMemo(() => {
    const map = new Map<number, RoutingOperationCost>()
    for (const line of cost.data?.operations ?? []) map.set(line.sequence, line)
    return map
  }, [cost.data])

  const columns = useMemo<ColumnDef<RoutingOperation, unknown>[]>(
    () => [
      {
        id: 'sequence',
        header: 'Seq',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular font-medium">{row.original.sequence}</span>,
      },
      {
        id: 'operation_name',
        header: 'Operation',
        enableSorting: false,
        cell: ({ row }) => (
          <span>
            <span className="font-medium">{row.original.operation_name}</span>
            <span className="block text-2xs text-muted-foreground">
              {row.original.operation_code}
            </span>
          </span>
        ),
      },
      {
        id: 'work_center',
        header: 'Work centre',
        enableSorting: false,
        cell: ({ row }) => {
          const centre = centres.get(row.original.work_center_id)
          return (
            <span>
              {centre ? centre.code : 'Assigned'}
              {centre && (
                <span className="block max-w-[12rem] truncate text-2xs text-muted-foreground">
                  {centre.name}
                </span>
              )}
            </span>
          )
        },
      },
      {
        id: 'setup_time_minutes',
        header: 'Setup (min)',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.setup_time_minutes),
      },
      {
        id: 'run_time_per_unit_minutes',
        header: 'Run / unit (min)',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.run_time_per_unit_minutes),
      },
      {
        id: 'wait_minutes',
        header: 'Queue + move (min)',
        enableSorting: false,
        meta: { align: 'right' },
        // Lead time, not cost: waiting and moving lengthen the job without
        // charging a rate, which is exactly why they are shown apart from the
        // two columns beside them.
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {formatQuantity(row.original.queue_time_minutes)} +{' '}
            {formatQuantity(row.original.move_time_minutes)}
          </span>
        ),
      },
      {
        id: 'rate',
        header: 'Rate / hour',
        enableSorting: false,
        meta: { align: 'right' },
        // The operation carries no rate of its own; this is its work centre's.
        cell: ({ row }) => {
          const line = costBySequence.get(row.original.sequence)
          const centre = centres.get(row.original.work_center_id)
          const rate = line?.work_center_cost_per_hour ?? centre?.cost_per_hour
          return rate === undefined ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatMoney(rate)
          )
        },
      },
      {
        id: 'process_cost',
        header: 'Process charge',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.process_cost),
      },
      {
        id: 'cost_per_unit',
        header: 'Cost / unit',
        enableSorting: false,
        meta: { align: 'right' },
        // Straight from the API's roll-up at quantity one. Blank rather than
        // zero when the roll-up is unavailable: a costing nobody computed is not
        // the same as a costing of nothing.
        cell: ({ row }) => {
          const line = costBySequence.get(row.original.sequence)
          return line === undefined ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="font-medium">{formatMoney(line.total_cost)}</span>
          )
        },
      },
      {
        id: 'flags',
        header: '',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex flex-wrap gap-1">
            {row.original.is_outsourced && <Badge variant="warning">Job work</Badge>}
            {row.original.qc_required && <Badge variant="secondary">QC</Badge>}
          </span>
        ),
      },
    ],
    [centres, costBySequence],
  )

  const routing = detail.data

  /**
   * A draft is approved; an approved routing is retired. Both take a recorded
   * reason - the request body is required, so posting nothing is a 422 - and
   * both answer with the summary rather than the detail.
   */
  const changeStatus = useMutation({
    mutationFn: ({ key, why }: { key: 'approve' | 'obsolete'; why: string }) =>
      api.post<RoutingRow>(`/routing/${routingId}/${key}`, { reason: why }),
    onSuccess: (saved, { key }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success(
        key === 'approve'
          ? success.approved('Routing')
          : success.statusChanged('Routing', saved.status),
        `${saved.code} is now ${humanise(saved.status).toLowerCase()}.`,
      )
      setStatusAction(null)
      setReason('')
      setReasonTouched(false)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>
            {routing ? `${routing.code}${routing.name ? ` - ${routing.name}` : ''}` : 'Routing'}
          </DialogTitle>
          <DialogDescription>
            Operations run in sequence. Setup time is paid once per batch and run time for every
            unit; queue and move time lengthen the lead time without charging a rate.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {detail.isLoading && (
            <div className="space-y-2">
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          )}

          {detail.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this routing</AlertTitle>
              <AlertDescription>{(detail.error as Error).message}</AlertDescription>
            </Alert>
          )}

          {routing && (
            <>
              <DescriptionList
                columns={4}
                items={[
                  { label: 'Version', value: `v${routing.version}` },
                  { label: 'Status', value: <StatusBadge status={routing.status} /> },
                  { label: 'Default', value: routing.is_default ? 'Yes' : 'No' },
                  { label: 'Operations', value: formatNumber(operations.length) },
                  { label: 'Effective from', value: formatDate(routing.effective_from) },
                  {
                    label: 'Effective to',
                    value: routing.effective_to ? formatDate(routing.effective_to) : 'Open ended',
                  },
                  {
                    label: 'Total time / unit',
                    value: cost.data
                      ? `${formatQuantity(cost.data.total_minutes)} min`
                      : 'Not costed',
                  },
                  {
                    label: 'Lead time',
                    value: cost.data
                      ? `${formatNumber(cost.data.lead_time_days)} day(s) at ${formatQuantity(cost.data.working_hours_per_day)}h`
                      : 'Not costed',
                  },
                  {
                    label: 'Time cost / unit',
                    value: cost.data ? formatMoney(cost.data.time_cost) : 'Not costed',
                  },
                  {
                    label: 'Process charges',
                    value: cost.data ? formatMoney(cost.data.process_cost) : 'Not costed',
                  },
                  {
                    label: 'Cost / unit',
                    value: cost.data ? formatMoney(cost.data.cost_per_unit) : 'Not costed',
                  },
                  { label: 'Notes', value: routing.notes ?? '-' },
                ]}
              />

              {cost.error && (
                <Alert variant="warning">
                  <AlertTitle>Costs are not available for this routing</AlertTitle>
                  <AlertDescription>
                    The times below are the routing&apos;s own. The money columns are blank rather
                    than zero, because the rate lives on each operation&apos;s work centre and this
                    roll-up could not be read: {(cost.error as Error).message}
                  </AlertDescription>
                </Alert>
              )}

              {operations.length === 0 ? (
                <EmptyState
                  title="No operations on this routing"
                  description="A routing without operations contributes no time and no cost to a production order."
                />
              ) : (
                <DataTable
                  columns={columns}
                  data={operations}
                  getRowId={(row) => row.id}
                  emptyTitle="No operations"
                  emptyDescription="This routing carries no operations."
                />
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          {routing && canManage && isApprovable(routing.status) && (
            <Button
              className="mr-auto"
              onClick={() => {
                setStatusAction('approve')
                setReasonTouched(false)
              }}
            >
              Approve
            </Button>
          )}
          {routing && canManage && isObsoletable(routing.status) && (
            <Button
              variant="outline"
              className="mr-auto text-destructive"
              onClick={() => {
                setStatusAction('obsolete')
                setReasonTouched(false)
              }}
            >
              Mark obsolete
            </Button>
          )}
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>

        {statusAction !== null && (
          <div className="border-t pt-4">
            <Label htmlFor="routing-status-reason" required>
              Reason to {statusAction === 'approve' ? 'approve' : 'retire'} {routing?.code}
            </Label>
            <div className="mt-1.5 flex flex-wrap items-start gap-2">
              <Input
                id="routing-status-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                autoComplete="off"
                className="min-w-0 flex-1 basis-full sm:basis-0"
                aria-invalid={reasonTouched && reason.trim() === ''}
              />
              <Button
                loading={changeStatus.isPending}
                onClick={() => {
                  if (reason.trim() === '') {
                    setReasonTouched(true)
                    return
                  }
                  changeStatus.mutate({ key: statusAction, why: reason.trim() })
                }}
              >
                Confirm
              </Button>
              <Button variant="ghost" onClick={() => setStatusAction(null)}>
                Cancel
              </Button>
            </div>
            {reasonTouched && reason.trim() === '' && (
              <FieldError message="The API records a reason against every status change" />
            )}
            <p className="mt-2 text-xs text-muted-foreground">
              {statusAction === 'approve'
                ? 'An approved routing can no longer be edited; changes need a new version.'
                : 'Production orders already planned against this version keep it, so history stays intact.'}
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------- routing form ------

/**
 * One operation on the form.
 *
 * `RoutingCreate.operations` has `minItems: 1` - a routing with no operations is
 * rejected outright - so the form collects at least one rather than posting a
 * bare header and earning a 422 nobody can read.
 */
const operationSchema = z.object({
  operation_code: z
    .string()
    .trim()
    .min(1, 'An operation code is required')
    .max(64, 'At most 64 characters'),
  operation_name: z
    .string()
    .trim()
    .min(1, 'An operation name is required')
    .max(255, 'At most 255 characters'),
  work_center_id: z.string().min(1, 'Every operation runs on a work centre'),
  setup_time_minutes: z.string().trim().regex(DECIMAL, 'Enter minutes, up to 4 decimals'),
  run_time_per_unit_minutes: z.string().trim().regex(DECIMAL, 'Enter minutes, up to 4 decimals'),
  queue_time_minutes: z.string().trim().regex(DECIMAL, 'Enter minutes, up to 4 decimals'),
  move_time_minutes: z.string().trim().regex(DECIMAL, 'Enter minutes, up to 4 decimals'),
  process_cost: z.string().trim().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),
  is_outsourced: z.boolean(),
  qc_required: z.boolean(),
  // Carried, not edited. `RoutingUpdate.operations` replaces the list wholesale,
  // so anything the form does not send back is destroyed - and a machine
  // assignment or an outworker silently dropped on an unrelated edit is exactly
  // the kind of loss nobody notices until the job reaches the floor.
  machine_id: z.string().nullable(),
  job_work_vendor_id: z.string().nullable(),
  notes: z.string().nullable(),
})

const routingSchema = z.object({
  code: z.string().trim().min(1, 'Code is required').max(64, 'Code is at most 64 characters'),
  name: z.string().trim().max(255, 'Name is at most 255 characters'),
  // A routing always belongs to an item: `RoutingCreate.item_id` is required and
  // there is no such thing as a shared routing.
  item_id: z.string().min(1, 'Choose the item this routing makes'),
  // No version and no status field: the API mints the version and moves the
  // status through its own approve/obsolete endpoints.
  notes: z.string().trim().max(1000, 'Notes are at most 1000 characters'),
  effective_from: z.string(),
  effective_to: z.string(),
  is_default: z.boolean(),
  operations: z.array(operationSchema).min(1, 'A routing needs at least one operation'),
})

type RoutingValues = z.infer<typeof routingSchema>

function emptyOperation(): z.infer<typeof operationSchema> {
  return {
    operation_code: '',
    operation_name: '',
    work_center_id: '',
    setup_time_minutes: '0',
    run_time_per_unit_minutes: '0',
    queue_time_minutes: '0',
    move_time_minutes: '0',
    process_cost: '0',
    is_outsourced: false,
    qc_required: false,
    machine_id: null,
    job_work_vendor_id: null,
    notes: null,
  }
}

function routingDefaults(routing?: RoutingRow | null): RoutingValues {
  return {
    code: routing?.code ?? '',
    name: routing?.name ?? '',
    item_id: routing?.item_id ?? '',
    notes: '',
    effective_from: routing?.effective_from?.slice(0, 10) ?? '',
    effective_to: routing?.effective_to?.slice(0, 10) ?? '',
    is_default: routing?.is_default ?? false,
    operations: [emptyOperation()],
  }
}

export function RoutingFormDialog({
  open,
  onOpenChange,
  routing,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  routing?: RoutingRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(routing)
  const workCenters = useWorkCenterOptions(open)
  const centreRows = workCenters.data?.data ?? []

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<RoutingValues>({
    resolver: zodResolver(routingSchema),
    defaultValues: routingDefaults(routing),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'operations' })

  /**
   * An edit has to send the whole operation list back - `RoutingUpdate.operations`
   * replaces it wholesale - so the existing operations are loaded before the
   * form can be submitted safely.
   */
  const existing = useQuery({
    queryKey: queryKeys.routing.routing(routing?.id ?? ''),
    queryFn: () => api.get<RoutingDetail>(`/routing/${routing?.id}`),
    enabled: open && Boolean(routing?.id),
    staleTime: staleTimes.master,
  })

  // Seeded once per opening and keyed on the record's id: a background refetch
  // must not rewrite what the user is typing.
  const editingId = routing?.id ?? null
  const loadedId = existing.data?.id ?? null
  useEffect(() => {
    if (!open) return
    if (!editingId) {
      reset(routingDefaults(null))
      return
    }
    if (loadedId !== editingId) return
    const detail = existing.data
    reset({
      code: detail?.code ?? routing?.code ?? '',
      name: detail?.name ?? '',
      item_id: detail?.item_id ?? routing?.item_id ?? '',
      notes: detail?.notes ?? '',
      effective_from: detail?.effective_from?.slice(0, 10) ?? '',
      effective_to: detail?.effective_to?.slice(0, 10) ?? '',
      is_default: detail?.is_default ?? false,
      operations:
        detail && detail.operations.length > 0
          ? [...detail.operations]
              .sort((a, b) => a.sequence - b.sequence)
              .map((operation) => ({
                operation_code: operation.operation_code,
                operation_name: operation.operation_name,
                work_center_id: operation.work_center_id,
                setup_time_minutes: operation.setup_time_minutes,
                run_time_per_unit_minutes: operation.run_time_per_unit_minutes,
                queue_time_minutes: operation.queue_time_minutes,
                move_time_minutes: operation.move_time_minutes,
                process_cost: operation.process_cost,
                is_outsourced: operation.is_outsourced,
                qc_required: operation.qc_required,
                machine_id: operation.machine_id,
                job_work_vendor_id: operation.job_work_vendor_id,
                notes: operation.notes,
              }))
          : [emptyOperation()],
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, loadedId, reset])

  const mutation = useMutation({
    mutationFn: (values: RoutingValues) => {
      // Sequence is positional: the order on screen is the order on the floor.
      const operations = values.operations.map((operation, index) => ({
        sequence: (index + 1) * 10,
        operation_code: operation.operation_code.trim(),
        operation_name: operation.operation_name.trim(),
        work_center_id: operation.work_center_id,
        // Times and money stay decimal strings all the way to the wire.
        setup_time_minutes: operation.setup_time_minutes.trim(),
        run_time_per_unit_minutes: operation.run_time_per_unit_minutes.trim(),
        queue_time_minutes: operation.queue_time_minutes.trim(),
        move_time_minutes: operation.move_time_minutes.trim(),
        process_cost: operation.process_cost.trim(),
        is_outsourced: operation.is_outsourced,
        qc_required: operation.qc_required,
        machine_id: operation.machine_id,
        job_work_vendor_id: operation.job_work_vendor_id,
        notes: operation.notes,
      }))

      const payload = {
        name: values.name.trim() || null,
        notes: values.notes.trim() || null,
        effective_from: values.effective_from || null,
        effective_to: values.effective_to || null,
        is_default: values.is_default,
        operations,
      }

      return routing
        ? api.patch<RoutingDetail>(`/routing/${routing.id}`, payload)
        : api.post<RoutingDetail>('/routing', {
            ...payload,
            code: values.code.trim(),
            item_id: values.item_id,
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success(
        isEdit ? success.updated('Routing') : success.created('Routing'),
        `${saved.code}${saved.name ? ` - ${saved.name}` : ''}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${routing?.code}` : 'New routing'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The operations below replace the routing wholesale, so they are loaded from the server before anything is sent.'
              : 'A routing belongs to one item and needs at least one operation. The version number is minted by the server.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            {isEdit && existing.isLoading && (
              <Alert variant="info">
                <AlertTitle>Loading the current operations</AlertTitle>
                <AlertDescription>
                  Saving before they arrive would replace the routing with an empty list.
                </AlertDescription>
              </Alert>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="routing-code" required>
                  Code
                </Label>
                <Input
                  id="routing-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="routing-name">Name</Label>
                <Input
                  id="routing-name"
                  {...register('name')}
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>
            </div>

            <div>
              <Controller
                control={control}
                name="item_id"
                render={({ field }) => (
                  <ItemPicker
                    id="routing-item"
                    label="Item produced"
                    value={field.value || null}
                    onChange={(itemId) => field.onChange(itemId ?? '')}
                    placeholder="Search the item master"
                    invalid={Boolean(errors.item_id)}
                    disabled={isEdit}
                  />
                )}
              />
              <FieldError message={errors.item_id?.message} />
              {isEdit && (
                <p className="mt-1 text-xs text-muted-foreground">
                  The item cannot be changed on an existing routing - copy it as a new version
                  instead.
                </p>
              )}
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="routing-from">Effective from</Label>
                <Input
                  id="routing-from"
                  type="date"
                  {...register('effective_from')}
                  className="mt-1.5"
                />
                <FieldError message={errors.effective_from?.message} />
              </div>
              <div>
                <Label htmlFor="routing-to">Effective to</Label>
                <Input
                  id="routing-to"
                  type="date"
                  {...register('effective_to')}
                  className="mt-1.5"
                />
                <FieldError message={errors.effective_to?.message} />
              </div>
            </div>

            <div className="flex items-center gap-2.5">
              <Controller
                control={control}
                name="is_default"
                render={({ field }) => (
                  <Checkbox
                    id="routing-default"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <Label htmlFor="routing-default">Default routing for the item</Label>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Operations</Label>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => append(emptyOperation())}
                >
                  <Plus />
                  Add operation
                </Button>
              </div>

              {fields.map((field, index) => (
                <div key={field.id} className="space-y-3 rounded-md border p-3">
                  <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-[7rem_minmax(0,1fr)_minmax(0,1fr)_auto]">
                    <div>
                      <Label htmlFor={`op-code-${index}`} className="text-xs">
                        Code
                      </Label>
                      <Input
                        id={`op-code-${index}`}
                        {...register(`operations.${index}.operation_code`)}
                        autoComplete="off"
                        aria-invalid={Boolean(errors.operations?.[index]?.operation_code)}
                        className="mt-1"
                      />
                      <FieldError message={errors.operations?.[index]?.operation_code?.message} />
                    </div>

                    <div>
                      <Label htmlFor={`op-name-${index}`} className="text-xs">
                        Operation
                      </Label>
                      <Input
                        id={`op-name-${index}`}
                        {...register(`operations.${index}.operation_name`)}
                        aria-invalid={Boolean(errors.operations?.[index]?.operation_name)}
                        className="mt-1"
                      />
                      <FieldError message={errors.operations?.[index]?.operation_name?.message} />
                    </div>

                    <div>
                      <Label htmlFor={`op-wc-${index}`} className="text-xs">
                        Work centre
                      </Label>
                      <Controller
                        control={control}
                        name={`operations.${index}.work_center_id`}
                        render={({ field: centre }) => (
                          <Select
                            value={centre.value || SELECT_NONE}
                            onValueChange={(value) =>
                              centre.onChange(value === SELECT_NONE ? '' : value)
                            }
                          >
                            <SelectTrigger
                              id={`op-wc-${index}`}
                              className="mt-1"
                              aria-label="Work centre"
                            >
                              <SelectValue
                                placeholder={workCenters.isLoading ? 'Loading...' : 'Choose one'}
                              />
                            </SelectTrigger>
                            <SelectContent>
                              {centreRows.map((row) => (
                                <SelectItem key={row.id} value={row.id}>
                                  {row.code} - {row.name} ({formatMoney(row.cost_per_hour)}/h)
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        )}
                      />
                      <FieldError message={errors.operations?.[index]?.work_center_id?.message} />
                    </div>

                    <div className="flex items-end pb-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        // The API will not take a routing with no operations.
                        disabled={fields.length === 1}
                        aria-label={`Remove operation ${index + 1}`}
                        onClick={() => remove(index)}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
                    <div>
                      <Label htmlFor={`op-setup-${index}`} className="text-xs">
                        Setup (min)
                      </Label>
                      <Input
                        id={`op-setup-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.setup_time_minutes`)}
                        aria-invalid={Boolean(errors.operations?.[index]?.setup_time_minutes)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError
                        message={errors.operations?.[index]?.setup_time_minutes?.message}
                      />
                    </div>

                    <div>
                      <Label htmlFor={`op-run-${index}`} className="text-xs">
                        Run / unit (min)
                      </Label>
                      <Input
                        id={`op-run-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.run_time_per_unit_minutes`)}
                        aria-invalid={Boolean(
                          errors.operations?.[index]?.run_time_per_unit_minutes,
                        )}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError
                        message={errors.operations?.[index]?.run_time_per_unit_minutes?.message}
                      />
                    </div>

                    <div>
                      <Label htmlFor={`op-queue-${index}`} className="text-xs">
                        Queue (min)
                      </Label>
                      <Input
                        id={`op-queue-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.queue_time_minutes`)}
                        aria-invalid={Boolean(errors.operations?.[index]?.queue_time_minutes)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError
                        message={errors.operations?.[index]?.queue_time_minutes?.message}
                      />
                    </div>

                    <div>
                      <Label htmlFor={`op-move-${index}`} className="text-xs">
                        Move (min)
                      </Label>
                      <Input
                        id={`op-move-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.move_time_minutes`)}
                        aria-invalid={Boolean(errors.operations?.[index]?.move_time_minutes)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError
                        message={errors.operations?.[index]?.move_time_minutes?.message}
                      />
                    </div>

                    <div>
                      <Label htmlFor={`op-process-${index}`} className="text-xs">
                        Process charge
                      </Label>
                      <Input
                        id={`op-process-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.process_cost`)}
                        aria-invalid={Boolean(errors.operations?.[index]?.process_cost)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError message={errors.operations?.[index]?.process_cost?.message} />
                    </div>

                    <div className="flex flex-wrap items-end gap-4 pb-2 sm:col-span-3 lg:col-span-5">
                      <div className="flex items-center gap-2">
                        <Controller
                          control={control}
                          name={`operations.${index}.is_outsourced`}
                          render={({ field: flag }) => (
                            <Checkbox
                              id={`op-outsourced-${index}`}
                              checked={flag.value}
                              onCheckedChange={(checked) => flag.onChange(checked === true)}
                            />
                          )}
                        />
                        <Label htmlFor={`op-outsourced-${index}`} className="text-xs font-normal">
                          Job work
                        </Label>
                      </div>
                      <div className="flex items-center gap-2">
                        <Controller
                          control={control}
                          name={`operations.${index}.qc_required`}
                          render={({ field: flag }) => (
                            <Checkbox
                              id={`op-qc-${index}`}
                              checked={flag.value}
                              onCheckedChange={(checked) => flag.onChange(checked === true)}
                            />
                          )}
                        />
                        <Label htmlFor={`op-qc-${index}`} className="text-xs font-normal">
                          QC required
                        </Label>
                      </div>
                    </div>
                  </div>

                  {/* A rule about the operation as a whole - an outsourced step
                      naming an in-house machine, say - lands on the row rather
                      than on a field, and would otherwise render nowhere. */}
                  <FieldError message={errors.operations?.[index]?.message} />
                </div>
              ))}

              <FieldError
                message={errors.operations?.message ?? errors.operations?.root?.message}
              />
              <p className="text-xs text-muted-foreground">
                The rate an operation is costed at comes from its work centre, not from the
                operation, so changing a centre&apos;s rate re-costs every routing that uses it.
              </p>
            </div>

            <div>
              <Label htmlFor="routing-notes">Notes</Label>
              <Textarea id="routing-notes" {...register('notes')} className="mt-1.5" />
              <FieldError message={errors.notes?.message} />
            </div>

            <FieldError message={errors.root?.message} />
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              loading={mutation.isPending}
              disabled={isEdit && existing.isLoading}
            >
              {isEdit ? 'Save changes' : 'Create routing'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------- work centre form ------

const workCenterSchema = z.object({
  code: z.string().trim().min(1, 'Code is required').max(64, 'Code is at most 64 characters'),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  description: z.string().trim().max(1000, 'Description is at most 1000 characters'),
  capacity_per_hour: z.string().trim().regex(DECIMAL, 'Enter a number with up to 4 decimals'),
  cost_per_hour: z.string().trim().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),
  // A flat charge per batch. There is no efficiency percentage on this record.
  setup_cost: z.string().trim().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),
  working_hours_per_day: z
    .string()
    .trim()
    .regex(DECIMAL, 'Enter hours with up to 4 decimals')
    // Compared as a decimal, like every other measured value in this form: a
    // `parseFloat` here is the same defect near the working day that it is near
    // money, and "24.00001" is not 24 hours however an IEEE-754 double reads it.
    .refine((value) => toDecimal(value).greaterThan(0), 'A working day must be longer than zero')
    .refine(
      (value) => toDecimal(value).lessThanOrEqualTo(24),
      'A working day cannot exceed 24 hours',
    ),
  /** Empty means the centre's hours are pooled into no cost centre. */
  cost_centre_id: z.string(),
})

type WorkCenterValues = z.infer<typeof workCenterSchema>

function workCenterDefaults(workCenter?: WorkCenter | null): WorkCenterValues {
  return {
    code: workCenter?.code ?? '',
    name: workCenter?.name ?? '',
    description: workCenter?.description ?? '',
    capacity_per_hour: workCenter?.capacity_per_hour ?? '0',
    cost_per_hour: workCenter?.cost_per_hour ?? '0',
    setup_cost: workCenter?.setup_cost ?? '0',
    working_hours_per_day: workCenter?.working_hours_per_day ?? '8',
    cost_centre_id: workCenter?.cost_centre_id ?? '',
  }
}

export function WorkCenterFormDialog({
  open,
  onOpenChange,
  workCenter,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  workCenter?: WorkCenter | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(workCenter)
  // `GET /organization/cost-centres` is its own right. Without it the picker is
  // not offered and the centre's current pool travels back unchanged.
  const canPickCostCentre = can(P.COST_CENTRE_VIEW)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<WorkCenterValues>({
    resolver: zodResolver(workCenterSchema),
    defaultValues: workCenterDefaults(workCenter),
  })

  const editingId = workCenter?.id ?? null
  useEffect(() => {
    if (open) reset(workCenterDefaults(workCenter))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: WorkCenterValues) => {
      const payload = {
        name: values.name.trim(),
        description: values.description.trim() || null,
        // Rates stay decimal strings all the way to the wire.
        capacity_per_hour: values.capacity_per_hour.trim(),
        cost_per_hour: values.cost_per_hour.trim(),
        setup_cost: values.setup_cost.trim(),
        working_hours_per_day: values.working_hours_per_day.trim(),
        cost_centre_id: values.cost_centre_id || null,
      }
      return workCenter
        ? api.patch<WorkCenter>(`/routing/work-centers/${workCenter.id}`, payload)
        : api.post<WorkCenter>('/routing/work-centers', { ...payload, code: values.code.trim() })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success(
        isEdit ? success.updated('Work centre') : success.created('Work centre'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${workCenter?.code}` : 'New work centre'}</DialogTitle>
          <DialogDescription>
            The hourly rate here is what every operation on this centre is costed at, so a change
            flows into the next costing of every routing that uses it.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="wc-code" required>
                  Code
                </Label>
                <Input
                  id="wc-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>
              <div>
                <Label htmlFor="wc-name" required>
                  Name
                </Label>
                <Input
                  id="wc-name"
                  {...register('name')}
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="wc-capacity">Capacity / hour</Label>
                <Input
                  id="wc-capacity"
                  inputMode="decimal"
                  {...register('capacity_per_hour')}
                  aria-invalid={Boolean(errors.capacity_per_hour)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.capacity_per_hour?.message} />
              </div>
              <div>
                <Label htmlFor="wc-rate">Rate / hour</Label>
                <Input
                  id="wc-rate"
                  inputMode="decimal"
                  {...register('cost_per_hour')}
                  aria-invalid={Boolean(errors.cost_per_hour)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.cost_per_hour?.message} />
              </div>
              <div>
                <Label htmlFor="wc-setup-cost">Setup charge / batch</Label>
                <Input
                  id="wc-setup-cost"
                  inputMode="decimal"
                  {...register('setup_cost')}
                  aria-invalid={Boolean(errors.setup_cost)}
                  aria-describedby="wc-setup-cost-hint"
                  className="mt-1.5 text-right tabular"
                />
                <p id="wc-setup-cost-hint" className="mt-1 text-xs text-muted-foreground">
                  Charged once per batch on top of the setup time.
                </p>
                <FieldError message={errors.setup_cost?.message} />
              </div>
              <div>
                <Label htmlFor="wc-hours">Working hours / day</Label>
                <Input
                  id="wc-hours"
                  inputMode="decimal"
                  {...register('working_hours_per_day')}
                  aria-invalid={Boolean(errors.working_hours_per_day)}
                  aria-describedby="wc-hours-hint"
                  className="mt-1.5 text-right tabular"
                />
                <p id="wc-hours-hint" className="mt-1 text-xs text-muted-foreground">
                  Turns an operation&apos;s minutes into lead-time days.
                </p>
                <FieldError message={errors.working_hours_per_day?.message} />
              </div>
            </div>

            {canPickCostCentre && (
              <Controller
                control={control}
                name="cost_centre_id"
                render={({ field }) => (
                  <CostCentreSelect
                    id="wc-cost-centre"
                    value={field.value || null}
                    onChange={(next) => field.onChange(next ?? '')}
                    enabled={open}
                    hint="The cost pool this centre's machine hours are charged to."
                    error={errors.cost_centre_id?.message}
                  />
                )}
              />
            )}

            <div>
              <Label htmlFor="wc-description">Description</Label>
              <Textarea id="wc-description" {...register('description')} className="mt-1.5" />
              <FieldError message={errors.description?.message} />
            </div>

            {isEdit && (
              <Alert variant="info">
                <AlertDescription>
                  Whether a work centre is active is a state change with a recorded reason, not a
                  field on this form. Use activate or deactivate on the centre itself.
                </AlertDescription>
              </Alert>
            )}

            <FieldError message={errors.root?.message} />
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create work centre'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------- machine form ------

const machineSchema = z.object({
  code: z.string().trim().min(1, 'Code is required').max(64, 'Code is at most 64 characters'),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  // Required: the API has no unassigned machine.
  work_center_id: z.string().min(1, 'A machine belongs to a work centre'),
  machine_type: z.string().trim().max(64, 'Type is at most 64 characters'),
  capacity_per_hour: z.string().trim().regex(DECIMAL, 'Enter a number with up to 4 decimals'),
  cost_per_hour: z.string().trim().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),
  last_maintenance_date: z.string(),
  next_maintenance_date: z.string(),
})

type MachineValues = z.infer<typeof machineSchema>

function machineDefaults(machine?: Machine | null): MachineValues {
  return {
    code: machine?.code ?? '',
    name: machine?.name ?? '',
    work_center_id: machine?.work_center_id ?? '',
    machine_type: machine?.machine_type ?? '',
    capacity_per_hour: machine?.capacity_per_hour ?? '0',
    cost_per_hour: machine?.cost_per_hour ?? '0',
    last_maintenance_date: machine?.last_maintenance_date?.slice(0, 10) ?? '',
    next_maintenance_date: machine?.next_maintenance_date?.slice(0, 10) ?? '',
  }
}

export function MachineFormDialog({
  open,
  onOpenChange,
  machine,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  machine?: Machine | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(machine)
  const workCenters = useWorkCenterOptions(open)

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<MachineValues>({
    resolver: zodResolver(machineSchema),
    defaultValues: machineDefaults(machine),
  })

  const editingId = machine?.id ?? null
  useEffect(() => {
    if (open) reset(machineDefaults(machine))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: MachineValues) => {
      const payload = {
        name: values.name.trim(),
        work_center_id: values.work_center_id,
        machine_type: values.machine_type.trim() || null,
        capacity_per_hour: values.capacity_per_hour.trim(),
        cost_per_hour: values.cost_per_hour.trim(),
        last_maintenance_date: values.last_maintenance_date || null,
        next_maintenance_date: values.next_maintenance_date || null,
      }
      return machine
        ? api.patch<Machine>(`/routing/machines/${machine.id}`, payload)
        : api.post<Machine>('/routing/machines', { ...payload, code: values.code.trim() })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success(
        isEdit ? success.updated('Machine') : success.created('Machine'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${machine?.code}` : 'New machine'}</DialogTitle>
          <DialogDescription>
            A machine belongs to a work centre. Operations may name a specific machine when the
            choice changes the run time.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="machine-code" required>
                  Code
                </Label>
                <Input
                  id="machine-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>
              <div>
                <Label htmlFor="machine-name" required>
                  Name
                </Label>
                <Input
                  id="machine-name"
                  {...register('name')}
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>
            </div>

            <div>
              <Label htmlFor="machine-wc" required>
                Work centre
              </Label>
              <Controller
                control={control}
                name="work_center_id"
                render={({ field }) => (
                  <Select
                    value={field.value || SELECT_NONE}
                    onValueChange={(value) => field.onChange(value === SELECT_NONE ? '' : value)}
                  >
                    <SelectTrigger id="machine-wc" className="mt-1.5" aria-label="Work centre">
                      <SelectValue
                        placeholder={workCenters.isLoading ? 'Loading...' : 'Choose a work centre'}
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {(workCenters.data?.data ?? []).map((center) => (
                        <SelectItem key={center.id} value={center.id}>
                          {center.code} - {center.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError message={errors.work_center_id?.message} />
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <Label htmlFor="machine-type">Type</Label>
                <Input
                  id="machine-type"
                  {...register('machine_type')}
                  aria-invalid={Boolean(errors.machine_type)}
                  className="mt-1.5"
                />
                <FieldError message={errors.machine_type?.message} />
              </div>
              <div>
                <Label htmlFor="machine-capacity">Capacity / hour</Label>
                <Input
                  id="machine-capacity"
                  inputMode="decimal"
                  {...register('capacity_per_hour')}
                  aria-invalid={Boolean(errors.capacity_per_hour)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.capacity_per_hour?.message} />
              </div>
              <div>
                <Label htmlFor="machine-rate">Rate / hour</Label>
                <Input
                  id="machine-rate"
                  inputMode="decimal"
                  {...register('cost_per_hour')}
                  aria-invalid={Boolean(errors.cost_per_hour)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.cost_per_hour?.message} />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="machine-last-service">Last service</Label>
                <Input
                  id="machine-last-service"
                  type="date"
                  {...register('last_maintenance_date')}
                  className="mt-1.5"
                />
                <FieldError message={errors.last_maintenance_date?.message} />
              </div>
              <div>
                <Label htmlFor="machine-next-service">Next service due</Label>
                <Input
                  id="machine-next-service"
                  type="date"
                  {...register('next_maintenance_date')}
                  className="mt-1.5"
                />
                <FieldError message={errors.next_maintenance_date?.message} />
              </div>
            </div>

            {isEdit && (
              <Alert variant="info">
                <AlertDescription>
                  A machine&apos;s condition is a state change with a recorded reason, not a field
                  on this form. Use the spanner in the grid to move it.
                </AlertDescription>
              </Alert>
            )}

            <FieldError message={errors.root?.message} />
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create machine'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------- machine condition ----

const statusSchema = z.object({
  status: z.enum(MACHINE_STATUSES),
  reason: z
    .string()
    .trim()
    .min(1, 'A condition change is recorded with a reason')
    .max(255, 'At most 255 characters'),
  next_maintenance_date: z.string(),
})

type StatusValues = z.infer<typeof statusSchema>

/**
 * Moving a machine between conditions.
 *
 * Its own endpoint, because it is a state change and not an edit: the API takes
 * a reason with it and can carry a revised service date at the same time, which
 * is exactly the moment a breakdown is being recorded.
 */
export function MachineStatusDialog({
  machine,
  onOpenChange,
}: {
  /** Null closes the dialog; a machine opens it on that machine. */
  machine: Machine | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const open = machine !== null

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<StatusValues>({
    resolver: zodResolver(statusSchema),
    defaultValues: { status: 'AVAILABLE', reason: '', next_maintenance_date: '' },
  })

  // Seeded once per machine, in an effect rather than during render: calling
  // `reset` in the render pass is what makes a form fight the user's typing.
  const machineId = machine?.id ?? null
  const machineStatus = machine?.status ?? 'AVAILABLE'
  const nextService = machine?.next_maintenance_date?.slice(0, 10) ?? ''
  useEffect(() => {
    if (!machineId) return
    reset({ status: machineStatus, reason: '', next_maintenance_date: nextService })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [machineId, reset])

  const mutation = useMutation({
    mutationFn: (values: StatusValues) =>
      api.post<Machine>(`/routing/machines/${machine?.id}/status`, {
        status: values.status,
        reason: values.reason.trim(),
        next_maintenance_date: values.next_maintenance_date || null,
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success(
        success.updated('Condition'),
        `${saved.code} is now ${humanise(saved.status).toLowerCase()}.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Condition of {machine?.code}</DialogTitle>
          <DialogDescription>
            A machine out of action stops being offered on a booking, so the change is recorded with
            a reason rather than edited quietly.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div>
              <Label htmlFor="machine-status" required>
                Condition
              </Label>
              <Controller
                control={control}
                name="status"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="machine-status" className="mt-1.5" aria-label="Condition">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MACHINE_STATUSES.map((value) => (
                        <SelectItem key={value} value={value}>
                          {humanise(value)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError message={errors.status?.message} />
              {machine && (
                <p className="mt-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                  Currently
                  <Badge variant={MACHINE_STATUS_VARIANT[machine.status] ?? 'secondary'}>
                    {humanise(machine.status)}
                  </Badge>
                </p>
              )}
            </div>

            <div>
              <Label htmlFor="machine-status-reason" required>
                Reason
              </Label>
              <Input
                id="machine-status-reason"
                {...register('reason')}
                autoComplete="off"
                aria-invalid={Boolean(errors.reason)}
                className="mt-1.5"
              />
              <FieldError message={errors.reason?.message} />
            </div>

            <div>
              <Label htmlFor="machine-status-next">Next service due</Label>
              <Input
                id="machine-status-next"
                type="date"
                {...register('next_maintenance_date')}
                className="mt-1.5"
              />
              <p className="mt-1 text-xs text-muted-foreground">
                Recording a breakdown is the moment the next service date usually moves.
              </p>
              <FieldError message={errors.next_maintenance_date?.message} />
            </div>

            <FieldError message={errors.root?.message} />
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              Update condition
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

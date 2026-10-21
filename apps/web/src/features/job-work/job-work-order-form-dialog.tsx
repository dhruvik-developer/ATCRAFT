/**
 * Raise a job work order, and amend one while it is still a draft.
 *
 * The screen could approve, issue, receive and close an order it had no way of
 * creating, so this is the missing front of the lifecycle. Both endpoints sit
 * behind `job_work.create` - see the note in `job-work-page.tsx` about the
 * gating defect this folder has already had - and the caller gates the
 * affordance; this module only builds the payloads.
 *
 * Three properties of the contract shape the form and are not preferences:
 *
 * 1. **A draft is a plan.** `POST /job-work` reserves no stock and moves none,
 *    so nothing here has an inventory consequence. Approval is what takes hard
 *    reservations, and it is a separate command held by a different right.
 * 2. **`PATCH` carries no `vendor_id`.** Sending the same material to a
 *    different processor is a different order, not an edit - the challan would
 *    name the wrong party - so the vendor is shown and locked when amending.
 * 3. **`lines` replaces the whole set.** A partial send deletes the lines it
 *    left out, so an amendment always posts every line.
 *
 * Item choice is narrowed server-side rather than validated after the fact: the
 * API refuses an inactive item, one not flagged `can_job_work`, and serial-
 * tracked items, because this module has no contract for naming each unit's
 * disposition. Batch-tracked items select their lot before approval so the
 * reservation is held against the same stock grain the issue will consume.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useEffect } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { z } from 'zod'

import { FieldError } from '@/components/shared'
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
  Checkbox,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { CostCentreSelect } from '@/features/admin/cost-centre-select'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatMoney, formatQuantity, humanise, toDateInput, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import {
  JOB_WORK_PROCESSES,
  RATE_BASES,
  jobWorkKeys,
  linesOf,
  useJobWorkOrder,
  type JobWorkOrderCreate,
  type JobWorkOrderDetail,
  type JobWorkOrderLineCreate,
  type JobWorkOrderRow,
  type JobWorkOrderUpdate,
} from './job-work-model'

/** `Quantity` is NUMERIC(18,4), `UnitPrice` NUMERIC(18,6), `Percentage` (9,4). */
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const OPTIONAL_QUANTITY = /^(\d{1,14}(\.\d{1,4})?)?$/
const RATE = /^\d{1,12}(\.\d{1,6})?$/
const PERCENT = /^\d{1,3}(\.\d{1,4})?$/

/**
 * `MAX_LINES_PER_ORDER` in the API's job-work models.
 *
 * Stated here so a 201st line is refused with a sentence rather than by a
 * business-rule error after the whole order has been keyed in.
 */
const MAX_LINES_PER_ORDER = 200

/**
 * The items this order may send out.
 *
 * All three narrowings are the API's own refusals, moved into the search so
 * they are never reached: an inactive item, an item not flagged for job work,
 * and - per the module's assumptions - anything serial tracked, for which
 * there is no contract for naming each unit's disposition.
 */
const JOB_WORK_INPUT_FILTERS = {
  can_job_work: true,
  is_serial_tracked: false,
}

/**
 * The items processed output may come back as.
 *
 * Deliberately wider than the input filter, because the API is: it asks only
 * that the output item is active and - when it differs from the input - that it
 * is neither batch nor serial tracked. It does not ask for `can_job_work`, and
 * it should not be asked for here. That flag says an item may be *sent out* for
 * processing; a plated or coated code that is only ever received back need not
 * carry it, and filtering on it would hide the very item the line exists to
 * name.
 */
const JOB_WORK_OUTPUT_FILTERS = {
  is_batch_tracked: false,
  is_serial_tracked: false,
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

const today = (): string => toDateInput(new Date())

// ------------------------------------------------------------------ form ----

const lineSchema = z
  .object({
    item_id: z.string().min(1, 'Choose the material going out'),
    is_batch_tracked: z.boolean(),
    batch_id: z.string(),
    quantity_to_issue: z
      .string()
      .trim()
      .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
      .refine((value) => toDecimal(value).greaterThan(0), 'A line has to send something out'),
    /** Blank means the same item comes back, which is what the contract defaults to. */
    expected_output_item_id: z.string(),
    /** Blank means a 1:1 yield - one piece out, one piece back. */
    expected_output_quantity: z
      .string()
      .trim()
      .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places')
      .refine(
        (value) => value === '' || toDecimal(value).greaterThan(0),
        'Expect more than nothing back, or leave it blank for a 1:1 yield',
      ),
    expected_loss_percentage: z
      .string()
      .trim()
      .regex(PERCENT, 'Enter a percentage with up to 4 decimal places')
      .refine((value) => toDecimal(value).lessThanOrEqualTo(100), 'At most 100%'),
    notes: z.string().trim().max(2000, 'At most 2000 characters'),
  })
  .refine((line) => !line.is_batch_tracked || Boolean(line.batch_id), {
    path: ['batch_id'],
    message: 'Choose a batch for batch-tracked material',
  })

const schema = z.object({
  vendor_id: z.string().min(1, 'Choose the vendor who will do the work'),
  process_type: z.enum(JOB_WORK_PROCESSES),
  process_description: z.string().trim().max(2000, 'At most 2000 characters'),
  expected_return_date: z.string().min(1, 'Say when the material is due back'),
  warehouse_id: z.string().min(1, 'Choose the store the material leaves'),
  /** Blank means "the store it left", resolved explicitly before posting. */
  return_warehouse_id: z.string(),
  /** Blank means unallocated: the charges belong to no cost centre. */
  cost_centre_id: z.string(),
  rate_per_unit: z.string().trim().regex(RATE, 'Enter a rate with up to 6 decimal places'),
  rate_basis: z.enum(RATE_BASES),
  /** Blank means the documented default GST rate - the client confirms it. */
  tax_rate: z
    .string()
    .trim()
    .regex(/^(\d{1,3}(\.\d{1,4})?)?$/, 'Enter a percentage with up to 4 decimal places')
    .refine((value) => value === '' || toDecimal(value).lessThanOrEqualTo(100), 'At most 100%'),
  /** Blank means the default SAC for manufacturing services on others' goods. */
  sac_code: z.string().trim().max(16, 'At most 16 characters'),
  requires_qc: z.boolean(),
  notes: z.string().trim().max(2000, 'At most 2000 characters'),
  lines: z
    .array(lineSchema)
    .min(1, 'A job work order needs at least one line')
    .max(MAX_LINES_PER_ORDER, `At most ${MAX_LINES_PER_ORDER} lines on one order`),
})

type JobWorkFormValues = z.infer<typeof schema>

function emptyLine(): JobWorkFormValues['lines'][number] {
  return {
    item_id: '',
    is_batch_tracked: false,
    batch_id: '',
    quantity_to_issue: '',
    expected_output_item_id: '',
    expected_output_quantity: '',
    expected_loss_percentage: '0',
    notes: '',
  }
}

/**
 * A blank order.
 *
 * The two enums start on a member rather than on nothing: Radix has no
 * "unselected" option value, and both defaults are visible in the control the
 * moment the dialog opens, so neither is a rule invented behind the user's
 * back. `PER_PIECE` is also the contract's own default for `rate_basis`.
 */
function emptyValues(): JobWorkFormValues {
  return {
    vendor_id: '',
    process_type: 'POLISHING',
    process_description: '',
    expected_return_date: '',
    warehouse_id: '',
    return_warehouse_id: '',
    cost_centre_id: '',
    rate_per_unit: '0',
    rate_basis: 'PER_PIECE',
    tax_rate: '',
    sac_code: '',
    requires_qc: false,
    notes: '',
    lines: [emptyLine()],
  }
}

/**
 * The stored order, back in the shape the form edits.
 *
 * A saved line always names an output item, even when it is the input item
 * itself - the API fills the default in on the way down. Showing that as an
 * empty picker keeps "the same thing comes back" reading as the ordinary case
 * rather than as a deliberate substitution.
 */
function valuesFrom(detail: JobWorkOrderDetail): JobWorkFormValues {
  return {
    vendor_id: detail.vendor_id,
    process_type: detail.process_type,
    process_description: detail.process_description ?? '',
    expected_return_date: toDateInput(detail.expected_return_date),
    warehouse_id: detail.warehouse_id,
    return_warehouse_id: detail.return_warehouse_id,
    cost_centre_id: detail.cost_centre_id ?? '',
    rate_per_unit: detail.rate_per_unit,
    rate_basis: detail.rate_basis,
    tax_rate: detail.tax_rate ?? '',
    sac_code: detail.sac_code ?? '',
    requires_qc: detail.requires_qc,
    notes: detail.notes ?? '',
    lines: linesOf(detail).map((line) => ({
      item_id: line.item_id,
      is_batch_tracked: line.is_batch_tracked,
      batch_id: line.batch_id ?? '',
      quantity_to_issue: line.quantity_to_issue,
      expected_output_item_id:
        line.expected_output_item_id === line.item_id ? '' : line.expected_output_item_id,
      expected_output_quantity: line.expected_output_quantity,
      expected_loss_percentage: line.expected_loss_percentage,
      notes: line.notes ?? '',
    })),
  }
}

/**
 * Line numbers are positional and rewritten on every save.
 *
 * They have to be unique within the order, and a `PATCH` replaces the whole
 * set, so renumbering from the visible order is the only version that survives
 * a line being removed from the middle. It is safe precisely because only a
 * DRAFT is editable: nothing has been issued or received against these numbers
 * yet, and the line number is how the challan and receipt dialogs address a
 * line once it has.
 */
function buildLines(values: JobWorkFormValues): JobWorkOrderLineCreate[] {
  return values.lines.map((line, index) => ({
    line_number: index + 1,
    item_id: line.item_id,
    batch_id: line.batch_id || null,
    // Decimals stay strings all the way to the wire - never parsed into a double.
    quantity_to_issue: line.quantity_to_issue.trim(),
    expected_output_item_id: line.expected_output_item_id || null,
    expected_output_quantity: line.expected_output_quantity.trim() || null,
    expected_loss_percentage: line.expected_loss_percentage.trim(),
    notes: orNull(line.notes),
  }))
}

/**
 * Where processed output comes back to, always stated.
 *
 * `POST` would default it to the issuing store, but `PATCH` ignores an explicit
 * null on a NOT NULL column - so leaving it out of an amendment would silently
 * keep whatever was there before, and "send it back to the store it left" would
 * be an instruction the API never received.
 */
const returnWarehouse = (values: JobWorkFormValues): string =>
  values.return_warehouse_id || values.warehouse_id

function buildCreate(values: JobWorkFormValues, rework?: ReworkPrefill | null): JobWorkOrderCreate {
  return {
    vendor_id: values.vendor_id,
    process_type: values.process_type,
    process_description: orNull(values.process_description),
    expected_return_date: values.expected_return_date,
    warehouse_id: values.warehouse_id,
    return_warehouse_id: returnWarehouse(values),
    // Explicit or absent: a job-work order inherits no centre from anything it
    // was raised against (docs/assumptions.md §42).
    cost_centre_id: orNull(values.cost_centre_id),
    rate_per_unit: values.rate_per_unit.trim(),
    rate_basis: values.rate_basis,
    // Blank means the documented defaults: the API fills the GST rate and the
    // SAC in when the payload omits them.
    tax_rate: values.tax_rate.trim() === '' ? null : values.tax_rate.trim(),
    sac_code: values.sac_code.trim() === '' ? null : values.sac_code.trim(),
    requires_qc: values.requires_qc,
    // A rework order names the receipt whose rejected output it reprocesses;
    // anything else is MANUAL with no source. The pairing is the API's:
    // MANUAL must carry no source id, any other source type must carry one.
    source_type: rework ? 'REWORK' : 'MANUAL',
    source_id: rework ? rework.sourceId : null,
    notes: orNull(values.notes),
    lines: buildLines(values),
  }
}

/** `vendor_id` is absent on purpose - the update payload does not accept it. */
function buildUpdate(values: JobWorkFormValues): JobWorkOrderUpdate {
  return {
    process_type: values.process_type,
    process_description: orNull(values.process_description),
    expected_return_date: values.expected_return_date,
    warehouse_id: values.warehouse_id,
    return_warehouse_id: returnWarehouse(values),
    // Always stated. `cost_centre_id` is one of the draft fields an explicit
    // null clears, and the form holds the order's own value - so an account
    // shown no picker sends back exactly what the order already carries.
    cost_centre_id: orNull(values.cost_centre_id),
    rate_per_unit: values.rate_per_unit.trim(),
    rate_basis: values.rate_basis,
    // Always stated. Both fields are ones an explicit null clears, and the
    // form holds the order's own values - so an account clearing neither sends
    // back exactly what the order already carries.
    tax_rate: values.tax_rate.trim() === '' ? null : values.tax_rate.trim(),
    sac_code: values.sac_code.trim() === '' ? null : values.sac_code.trim(),
    requires_qc: values.requires_qc,
    notes: orNull(values.notes),
    lines: buildLines(values),
  }
}

/**
 * What the order is expected to cost, computed the way the API computes it.
 *
 * `estimate_charges` multiplies the rate by the total expected *output*, except
 * on a LOT rate, which is a flat charge for the consignment. Shown so a
 * mis-keyed rate is obvious before the order is raised; the figure the order
 * carries is always the API's, recomputed on every save.
 */
function indicativeCharge(
  lines: JobWorkFormValues['lines'],
  ratePerUnit: string,
  rateBasis: JobWorkFormValues['rate_basis'],
): string {
  const rate = toDecimal(ratePerUnit)
  if (rateBasis === 'LOT') return rate.toFixed(4)

  const output = lines.reduce(
    (sum, line) =>
      sum.plus(
        toDecimal(
          line.expected_output_quantity.trim() === ''
            ? line.quantity_to_issue
            : line.expected_output_quantity,
        ),
      ),
    toDecimal(0),
  )
  return rate.times(output).toFixed(4)
}

// ---------------------------------------------------------------- dialog ----

/**
 * A rejected receipt line, ready to go back to the vendor for rework.
 *
 * The rejected output item becomes the new order's input: rework restores it
 * rather than transforming it into something else, so input and expected
 * output start as the same item and the planner adjusts from there. The vendor
 * stays editable - the same vendor, or one the user picks.
 */
export interface ReworkPrefill {
  /** The receipt whose rejected output is being reprocessed. */
  sourceId: string
  vendorId: string
  lines: { item_id: string; quantity_to_issue: string }[]
}

function reworkValues(rework: ReworkPrefill): JobWorkFormValues {
  return {
    ...emptyValues(),
    vendor_id: rework.vendorId,
    notes: '',
    lines: rework.lines.map((line) => ({
      ...emptyLine(),
      item_id: line.item_id,
      quantity_to_issue: line.quantity_to_issue,
    })),
  }
}

export function JobWorkOrderFormDialog({
  open,
  onOpenChange,
  order = null,
  rework = null,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The draft being amended. Absent when raising a new order. */
  order?: JobWorkOrderRow | null
  /** Rejected output to send back out. Only read when raising, never amending. */
  rework?: ReworkPrefill | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const amending = order !== null

  // The list row carries no lines, so an amendment starts by fetching them.
  const detailQuery = useJobWorkOrder(order?.id ?? '', open && amending)
  const detail = detailQuery.data

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<JobWorkFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'lines' })

  // A new order is seeded when the dialog opens; an amendment waits for the
  // lines to land, exactly as the issue and receive dialogs do. A rework
  // prefill seeds the same blank form from the rejected receipt line.
  useEffect(() => {
    if (!open) return
    if (!amending) {
      reset(rework ? reworkValues(rework) : emptyValues())
      return
    }
    if (detail) reset(valuesFrom(detail))
  }, [open, amending, detail, rework, reset])

  const save = useMutation({
    mutationFn: (values: JobWorkFormValues) =>
      order
        ? api.patch<JobWorkOrderDetail>(`/job-work/${order.id}`, buildUpdate(values))
        : api.post<JobWorkOrderDetail>('/job-work', buildCreate(values, rework)),
    onSuccess: (saved) => {
      // Only the job-work caches. A draft holds no reservation and has moved no
      // stock, so inventory and allocation are not stale - approval is the step
      // that touches them.
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      toast.success(
        amending ? success.updated('Job work order') : success.created('Job work order'),
        amending
          ? `${saved.code} is updated. It still needs approval before material can be issued.`
          : rework
            ? `${saved.code} is saved as a rework draft linked to the rejected receipt.`
            : `${saved.code} is saved as a draft. Approve it before issuing material to the vendor.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  // Both warehouses are required by the API, and the lookup behind them is a
  // 403 for a principal who cannot read the warehouse master - so the form says
  // so rather than failing on a field it never managed to show.
  const canPickWarehouse = can(P.WAREHOUSE_VIEW)
  // The cost-centre list is its own right too. Without it the picker is not
  // offered - an order is raised unallocated, an amendment keeps its centre.
  const canPickCostCentre = can(P.COST_CENTRE_VIEW)
  const ready = !amending || Boolean(detail)

  // Only the three fields the estimate reads are watched. Watching the whole
  // form would re-render every picker on every keystroke in a notes box.
  const lines = watch('lines')
  const batchLists = useQueries({
    queries: lines.map((line) => ({
      queryKey: queryKeys.inventory.batches({ item_id: line.item_id }),
      queryFn: async () =>
        (
          await api.getPage<SchemaOf<'StockBatchRead'>>('/inventory/batches', {
            params: { item_id: line.item_id, limit: 100, with_total: false },
          })
        ).data,
      enabled: open && line.is_batch_tracked,
    })),
  })
  const ratePerUnit = watch('rate_per_unit')
  const rateBasis = watch('rate_basis')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>
            {order ? `Amend ${order.code}` : rework ? 'New rework order' : 'New job work order'}
          </DialogTitle>
          <DialogDescription>
            {amending
              ? 'Only a draft can be amended: from approval the order holds hard reservations, and from issue it holds company material on somebody else’s premises. The vendor cannot be changed - that would be a different order.'
              : rework
                ? 'Start a draft linked to this receipt, with the rejected item and quantity prefilled.'
                : 'The order is raised as a draft. Nothing is reserved and no stock moves until it is approved and issued under a challan.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((formValues) => {
            // `POST` refuses a return date in the past; `PATCH` does not
            // re-check one, so a draft whose promised date has already slipped
            // stays amendable rather than becoming uneditable.
            if (!amending && formValues.expected_return_date < today()) {
              setError('expected_return_date', {
                type: 'manual',
                message: 'The material cannot be due back before today',
              })
              return
            }
            save.mutate(formValues)
          })}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            {(rework || detail?.source_type === 'REWORK') && (
              <Alert variant="warning">
                <AlertDescription>
                  Creating a draft does not move rejected stock. This order can issue available
                  stock only.
                </AlertDescription>
              </Alert>
            )}
            {amending && detailQuery.isLoading && <Skeleton className="h-40 w-full" />}

            {amending && detailQuery.error && (
              <Alert variant="destructive">
                <AlertDescription>{detailQuery.error.message}</AlertDescription>
              </Alert>
            )}

            {!canPickWarehouse && (
              <Alert variant="warning">
                <AlertTitle>You cannot raise a job work order from here</AlertTitle>
                <AlertDescription>
                  An order has to state the store the material leaves and the store the processed
                  output returns to, and your account cannot read the warehouse list. Ask somebody
                  who can to raise it.
                </AlertDescription>
              </Alert>
            )}

            {ready && (
              <>
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  <div className="sm:col-span-2 lg:col-span-1">
                    <Controller
                      control={control}
                      name="vendor_id"
                      render={({ field }) => (
                        <VendorPicker
                          id="job-work-vendor"
                          label="Vendor"
                          value={field.value || null}
                          onChange={(vendorId) => field.onChange(vendorId ?? '')}
                          placeholder="Search the vendor master"
                          // Locked on an amendment: `JobWorkOrderUpdate` does
                          // not carry `vendor_id` at all.
                          disabled={amending}
                          invalid={Boolean(errors.vendor_id)}
                        />
                      )}
                    />
                    <FieldError message={errors.vendor_id?.message} />
                    {amending && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Fixed. Sending this material to a different processor is a different order.
                      </p>
                    )}
                  </div>

                  <div>
                    <Label htmlFor="job-work-process" required>
                      Process
                    </Label>
                    <Controller
                      control={control}
                      name="process_type"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="job-work-process" className="mt-1.5">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {JOB_WORK_PROCESSES.map((value) => (
                              <SelectItem key={value} value={value}>
                                {humanise(value)}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    <FieldError message={errors.process_type?.message} />
                  </div>

                  <div>
                    <Label htmlFor="job-work-return-date" required>
                      Expected return
                    </Label>
                    <Input
                      id="job-work-return-date"
                      type="date"
                      min={amending ? undefined : today()}
                      {...register('expected_return_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.expected_return_date)}
                    />
                    <FieldError message={errors.expected_return_date?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      What the ageing and the overdue flag are measured against.
                    </p>
                  </div>

                  {canPickWarehouse && (
                    <>
                      <div>
                        <Controller
                          control={control}
                          name="warehouse_id"
                          render={({ field }) => (
                            <WarehouseSelect
                              id="job-work-warehouse"
                              label="Issue from"
                              value={field.value}
                              onChange={field.onChange}
                              invalid={Boolean(errors.warehouse_id)}
                            />
                          )}
                        />
                        <FieldError message={errors.warehouse_id?.message} />
                        <p className="mt-1 text-xs text-muted-foreground">
                          The job-work pool that holds the material while it is away belongs to this
                          store.
                        </p>
                      </div>

                      <div>
                        <Controller
                          control={control}
                          name="return_warehouse_id"
                          render={({ field }) => (
                            <WarehouseSelect
                              id="job-work-return-warehouse"
                              label="Return to"
                              value={field.value}
                              onChange={field.onChange}
                              includeAll
                              allLabel="Same as the issuing store"
                            />
                          )}
                        />
                        <p className="mt-1 text-xs text-muted-foreground">
                          Processed output often comes back to a different store from the one it
                          left.
                        </p>
                      </div>
                    </>
                  )}

                  {canPickCostCentre && (
                    <Controller
                      control={control}
                      name="cost_centre_id"
                      render={({ field }) => (
                        <CostCentreSelect
                          id="job-work-cost-centre"
                          value={field.value || null}
                          onChange={(next) => field.onChange(next ?? '')}
                          enabled={open}
                          hint="Where the vendor's processing charges are booked."
                          error={errors.cost_centre_id?.message}
                        />
                      )}
                    />
                  )}

                  <div>
                    <Label htmlFor="job-work-rate-basis" required>
                      Rate basis
                    </Label>
                    <Controller
                      control={control}
                      name="rate_basis"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="job-work-rate-basis" className="mt-1.5">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {RATE_BASES.map((value) => (
                              <SelectItem key={value} value={value}>
                                {humanise(value)}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    <FieldError message={errors.rate_basis?.message} />
                  </div>

                  <div>
                    <Label htmlFor="job-work-rate" required>
                      Rate
                    </Label>
                    <Input
                      id="job-work-rate"
                      {...register('rate_per_unit')}
                      inputMode="decimal"
                      autoComplete="off"
                      className="mt-1.5 text-right tabular"
                      aria-invalid={Boolean(errors.rate_per_unit)}
                    />
                    <FieldError message={errors.rate_per_unit?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Frozen on the order, so a later rate-card change does not restate what was
                      approved.
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="job-work-tax-rate">GST rate %</Label>
                    <Input
                      id="job-work-tax-rate"
                      {...register('tax_rate')}
                      inputMode="decimal"
                      autoComplete="off"
                      placeholder="Default 18"
                      className="mt-1.5 text-right tabular"
                      aria-invalid={Boolean(errors.tax_rate)}
                    />
                    <FieldError message={errors.tax_rate?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      On the processing service. Blank takes the documented default - confirm it
                      with the vendor.
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="job-work-sac">SAC</Label>
                    <Input
                      id="job-work-sac"
                      {...register('sac_code')}
                      autoComplete="off"
                      placeholder="Default 9988"
                      maxLength={16}
                      className="mt-1.5 font-mono"
                      aria-invalid={Boolean(errors.sac_code)}
                    />
                    <FieldError message={errors.sac_code?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Copied onto the vendor-invoice line.
                    </p>
                  </div>

                  <div className="flex items-start">
                    <label className="flex items-center gap-2.5 text-sm lg:mt-6">
                      <Controller
                        control={control}
                        name="requires_qc"
                        render={({ field }) => (
                          <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                        )}
                      />
                      Output must be inspected
                    </label>
                  </div>
                </div>

                <div>
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="text-sm font-medium">Lines</h3>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={fields.length >= MAX_LINES_PER_ORDER}
                      onClick={() => append(emptyLine())}
                    >
                      <Plus />
                      Add line
                    </Button>
                  </div>
                  <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />

                  <div className="mt-3 space-y-3">
                    {fields.map((field, index) => (
                      <div key={field.id} className="rounded-lg border p-3">
                        <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_auto]">
                          <div>
                            <Controller
                              control={control}
                              name={`lines.${index}.item_id`}
                              render={({ field: itemField }) => (
                                <ItemPicker
                                  id={`job-work-line-item-${index}`}
                                  label={`Line ${index + 1} material`}
                                  value={itemField.value || null}
                                  onChange={(itemId, item) => {
                                    itemField.onChange(itemId ?? '')
                                    setValue(`lines.${index}.batch_id`, '')
                                    setValue(
                                      `lines.${index}.is_batch_tracked`,
                                      item?.is_batch_tracked ?? false,
                                    )
                                  }}
                                  placeholder="Search items flagged for job work"
                                  filters={JOB_WORK_INPUT_FILTERS}
                                  emptyMessage="No item matches. Only active items flagged for job work and not serial tracked can be sent out."
                                  invalid={Boolean(errors.lines?.[index]?.item_id)}
                                />
                              )}
                            />
                            <FieldError message={errors.lines?.[index]?.item_id?.message} />
                            {lines[index]?.is_batch_tracked && (
                              <>
                                <Label
                                  htmlFor={`job-work-line-batch-${index}`}
                                  className="mt-3 block"
                                  required
                                >
                                  Batch
                                </Label>
                                <Controller
                                  control={control}
                                  name={`lines.${index}.batch_id`}
                                  render={({ field: batchField }) => (
                                    <Select
                                      value={batchField.value || SELECT_NONE}
                                      onValueChange={(value) =>
                                        batchField.onChange(value === SELECT_NONE ? '' : value)
                                      }
                                    >
                                      <SelectTrigger
                                        id={`job-work-line-batch-${index}`}
                                        className="mt-1.5"
                                        aria-invalid={Boolean(errors.lines?.[index]?.batch_id)}
                                      >
                                        <SelectValue
                                          placeholder={
                                            batchLists[index]?.isLoading
                                              ? 'Loading batches...'
                                              : 'Choose a batch'
                                          }
                                        />
                                      </SelectTrigger>
                                      <SelectContent>
                                        <SelectItem value={SELECT_NONE} disabled>
                                          Choose a batch
                                        </SelectItem>
                                        {(batchLists[index]?.data ?? []).map((batch) => (
                                          <SelectItem key={batch.id} value={batch.id}>
                                            {batch.batch_number} · available{' '}
                                            {formatQuantity(batch.available_quantity)}
                                          </SelectItem>
                                        ))}
                                      </SelectContent>
                                    </Select>
                                  )}
                                />
                                {batchLists[index]?.error && (
                                  <p className="mt-1 text-xs text-destructive">
                                    Could not load batches: {batchLists[index].error.message}
                                  </p>
                                )}
                                <FieldError message={errors.lines?.[index]?.batch_id?.message} />
                                <p className="mt-1 text-xs text-muted-foreground">
                                  This lot is reserved at approval and stays with the order through
                                  receipt. The server checks stock in the selected warehouse.
                                </p>
                              </>
                            )}
                          </div>

                          <div>
                            <Label htmlFor={`job-work-line-qty-${index}`} required>
                              Quantity out
                            </Label>
                            <Input
                              id={`job-work-line-qty-${index}`}
                              {...register(`lines.${index}.quantity_to_issue`)}
                              inputMode="decimal"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(errors.lines?.[index]?.quantity_to_issue)}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.quantity_to_issue?.message}
                            />
                          </div>

                          <div className="flex items-end">
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              aria-label={`Remove line ${index + 1}`}
                              disabled={fields.length === 1}
                              onClick={() => remove(index)}
                            >
                              <Trash2 className="text-destructive" />
                            </Button>
                          </div>
                        </div>

                        <div className="mt-3 grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)]">
                          <div>
                            <Controller
                              control={control}
                              name={`lines.${index}.expected_output_item_id`}
                              render={({ field: outputField }) => (
                                <ItemPicker
                                  id={`job-work-line-output-${index}`}
                                  label={`Line ${index + 1} comes back as`}
                                  value={outputField.value || null}
                                  onChange={(itemId) => outputField.onChange(itemId ?? '')}
                                  placeholder="The same item"
                                  filters={JOB_WORK_OUTPUT_FILTERS}
                                  emptyMessage="No item matches. A processed item need not be flagged for job work, but it cannot be batch or serial tracked."
                                />
                              )}
                            />
                            <p className="mt-1 text-xs text-muted-foreground">
                              Leave it empty when polishing a handle returns a handle. PVD, coating
                              and machining usually return a different code.
                            </p>
                          </div>

                          <div>
                            <Label htmlFor={`job-work-line-output-qty-${index}`}>
                              Quantity back
                            </Label>
                            <Input
                              id={`job-work-line-output-qty-${index}`}
                              {...register(`lines.${index}.expected_output_quantity`)}
                              inputMode="decimal"
                              placeholder="Same as out"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(
                                errors.lines?.[index]?.expected_output_quantity,
                              )}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.expected_output_quantity?.message}
                            />
                            <p className="mt-1 text-xs text-muted-foreground">
                              The yield, frozen on the order. Set it only when it is not 1:1.
                            </p>
                          </div>

                          <div>
                            <Label htmlFor={`job-work-line-loss-${index}`}>Agreed loss %</Label>
                            <Input
                              id={`job-work-line-loss-${index}`}
                              {...register(`lines.${index}.expected_loss_percentage`)}
                              inputMode="decimal"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(
                                errors.lines?.[index]?.expected_loss_percentage,
                              )}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.expected_loss_percentage?.message}
                            />
                            <p className="mt-1 text-xs text-muted-foreground">
                              Loss within it posts as scrap; loss beyond it debits the vendor.
                            </p>
                          </div>
                        </div>

                        <div className="mt-3">
                          <Label htmlFor={`job-work-line-notes-${index}`}>
                            Line {index + 1} notes
                          </Label>
                          <Input
                            id={`job-work-line-notes-${index}`}
                            {...register(`lines.${index}.notes`)}
                            autoComplete="off"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.notes)}
                          />
                          <FieldError message={errors.lines?.[index]?.notes?.message} />
                        </div>
                      </div>
                    ))}
                  </div>

                  <p className="mt-3 text-sm text-muted-foreground">
                    Indicative charge{' '}
                    <span className="tabular font-medium text-foreground">
                      {formatMoney(indicativeCharge(lines, ratePerUnit, rateBasis))}
                    </span>{' '}
                    at the rate above. The API freezes its own estimate onto the order.
                  </p>
                </div>

                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="job-work-process-description">What the vendor is doing</Label>
                    <Textarea
                      id="job-work-process-description"
                      {...register('process_description')}
                      className="mt-1.5"
                    />
                    <FieldError message={errors.process_description?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Searched by the order list, so a finish or a spec is worth spelling out.
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="job-work-notes">Notes</Label>
                    <Textarea id="job-work-notes" {...register('notes')} className="mt-1.5" />
                    <FieldError message={errors.notes?.message} />
                  </div>
                </div>

                <Alert variant="info">
                  <AlertDescription>
                    A draft reserves nothing and moves nothing. Approval takes hard reservations
                    away from every other document competing for the same material, and only then
                    can a challan be raised.
                  </AlertDescription>
                </Alert>
              </>
            )}
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
            <Button type="submit" loading={save.isPending} disabled={!ready || !canPickWarehouse}>
              {amending ? 'Save draft' : 'Create draft order'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

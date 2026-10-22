/**
 * The two movement dialogs of job work: issuing material to a vendor against a
 * challan, and receiving the processed output back.
 *
 * Both move stock that never stops being company property - an issue changes the
 * stock state to JOB_WORK rather than removing the value from the books - so
 * both capture the paperwork the movement is defended with: the challan on the
 * way out, and the split between good output, rejection and unworked returns on
 * the way back. An unworked return is material that came back as it went, and
 * recording it is the
 * only way the ledger and the vendor account ever reconcile.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query'
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
  Checkbox,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useTransporterOptions } from '@/features/transporters/transporter-model'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatQuantity, humanise, toDateInput, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { success, validation } from '@/lib/messages'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import {
  EXCESS_RECEIPT_TOLERANCE_PERCENT,
  REJECTION_ACTIONS,
  jobWorkKeys,
  linesOf,
  useJobWorkOrder,
  type JobWorkOrderDetail,
  type JobWorkOrderLine,
  type RejectionAction,
} from './job-work-model'

const OPTIONAL_QUANTITY = /^(\d{1,14}(\.\d{1,4})?)?$/

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())
/** Decimals stay strings all the way to the wire - never parsed into a double. */
const asDecimal = (value: string): string => (value.trim() === '' ? '0' : value.trim())

function today(): string {
  return toDateInput(new Date())
}

/** Ordered less already issued - what a fresh challan would normally carry. */
function stillToIssue(line: JobWorkOrderLine): string {
  const outstanding = toDecimal(line.quantity_to_issue).minus(toDecimal(line.issued_quantity))
  return outstanding.greaterThan(0) ? outstanding.toFixed() : '0'
}

/**
 * Issued less what has come back in any form - what is still at the vendor.
 *
 * `received_quantity` is the input accounted back out of the job-work pool
 * (output-equivalent plus unworked returns plus scrap), so it is the whole
 * subtrahend: subtracting rejected or scrap again would count them twice.
 */
function stillAtVendor(line: JobWorkOrderLine): string {
  const outstanding = toDecimal(line.issued_quantity).minus(toDecimal(line.received_quantity))
  return outstanding.greaterThan(0) ? outstanding.toFixed() : '0'
}

// ----------------------------------------------------------------- issue ----

const issueSchema = z.object({
  challan_date: z.string().min(1, 'A challan needs a date'),
  expected_return_date: z.string(),
  vehicle_number: z.string().trim().max(32, 'At most 32 characters'),
  /** Blank means the vendor's own vehicle collected the goods. */
  transporter_id: z.string(),
  remarks: z.string().trim().max(500, 'At most 500 characters'),
  /** Required by the API on every issue - it is the audit reason. */
  reason: z.string().trim().min(1, 'Say why this material is going out'),
  lines: z.array(
    z.object({
      // The API addresses a line by its number on the order, not by its id.
      line_number: z.number(),
      label: z.string(),
      batch_id: z.string(),
      quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    }),
  ),
})

type IssueFormValues = z.infer<typeof issueSchema>

function issueDefaults(order: JobWorkOrderDetail | undefined): IssueFormValues {
  return {
    challan_date: today(),
    expected_return_date: toDateInput(order?.expected_return_date ?? null),
    vehicle_number: '',
    transporter_id: '',
    remarks: '',
    reason: '',
    // The line carries an item id and nothing else - no code, no name - so the
    // label is the line number and the id it points at.
    lines: linesOf(order).map((line) => ({
      line_number: line.line_number,
      label: `Line ${line.line_number} - ${line.item_id.slice(0, 8)}`,
      batch_id: line.batch_id ?? '',
      quantity: stillToIssue(line),
    })),
  }
}

export function JobWorkIssueDialog({
  open,
  onOpenChange,
  orderId,
  orderCode,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  orderCode: string
}) {
  const queryClient = useQueryClient()
  const orderQuery = useJobWorkOrder(orderId, open)
  const order = orderQuery.data

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<IssueFormValues>({
    resolver: zodResolver(issueSchema),
    defaultValues: issueDefaults(undefined),
  })

  const { fields } = useFieldArray({ control, name: 'lines' })

  // The carrier named on the challan. The options list is bounded and
  // active-only; whoever collected the goods with their own vehicle leaves it
  // blank, which the endpoint reads as no transporter.
  const transporters = useTransporterOptions({ enabled: open })
  const batchLists = useQueries({
    queries: linesOf(order).map((line) => ({
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

  // The lines arrive with the order, so the form is seeded once it lands rather
  // than when the dialog opens.
  useEffect(() => {
    if (open && order) reset(issueDefaults(order))
  }, [open, order, reset])

  const issue = useMutation({
    mutationFn: (values: IssueFormValues) =>
      api.post(`/job-work/${orderId}/issue`, {
        reason: values.reason,
        challan_date: values.challan_date,
        expected_return_date: orNull(values.expected_return_date),
        vehicle_number: orNull(values.vehicle_number),
        transporter_id: values.transporter_id === '' ? null : values.transporter_id,
        // The field is `challan_notes` on this endpoint, not `remarks`.
        challan_notes: orNull(values.remarks),
        lines: values.lines
          .filter((line) => toDecimal(line.quantity).greaterThan(0))
          .map((line) => ({
            line_number: line.line_number,
            issued_quantity: asDecimal(line.quantity),
            batch_id: line.batch_id || null,
          })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      // The stock does not leave the company, but it does change state, so the
      // balances and any reservation against it move with it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      toast.success(success.raised('Challan'), `Material was issued against ${orderCode}.`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Issue material on {orderCode}</DialogTitle>
          <DialogDescription>
            This raises the outward challan and moves the stock to the vendor. It stays company
            property throughout and is still counted in valuation.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            if (!values.lines.some((line) => toDecimal(line.quantity).greaterThan(0))) {
              setError('root', {
                type: 'manual',
                message: 'Enter a quantity against at least one line',
              })
              return
            }
            issue.mutate(values)
          })}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            {orderQuery.isLoading && <Skeleton className="h-40 w-full" />}

            {orderQuery.error && (
              <Alert variant="destructive">
                <AlertDescription>{orderQuery.error.message}</AlertDescription>
              </Alert>
            )}

            {order && (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="challan-date" required>
                      Challan date
                    </Label>
                    <Input
                      id="challan-date"
                      type="date"
                      {...register('challan_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.challan_date)}
                    />
                    <FieldError message={errors.challan_date?.message} />
                  </div>

                  <div>
                    <Label htmlFor="challan-expected-return">Expected return</Label>
                    <Input
                      id="challan-expected-return"
                      type="date"
                      {...register('expected_return_date')}
                      className="mt-1.5"
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Drives the ageing warning on the pending-at-vendor list.
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="challan-vehicle">Vehicle number</Label>
                    <Input
                      id="challan-vehicle"
                      {...register('vehicle_number')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.vehicle_number)}
                    />
                    <FieldError message={errors.vehicle_number?.message} />
                  </div>

                  <div>
                    <Label htmlFor="challan-transporter">Transporter</Label>
                    <Controller
                      control={control}
                      name="transporter_id"
                      render={({ field }) => (
                        <Select
                          value={field.value === '' ? SELECT_NONE : field.value}
                          onValueChange={(value) =>
                            field.onChange(value === SELECT_NONE ? '' : value)
                          }
                        >
                          <SelectTrigger id="challan-transporter" className="mt-1.5">
                            <SelectValue placeholder="Vendor's own vehicle" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>Vendor&apos;s own vehicle</SelectItem>
                            {(transporters.data ?? []).map((option) => (
                              <SelectItem key={option.id} value={option.id}>
                                {option.code} - {option.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      The carrier named on the delivery challan.
                    </p>
                  </div>

                  <div className="sm:col-span-2">
                    <Label htmlFor="challan-reason" required>
                      Reason
                    </Label>
                    <Input
                      id="challan-reason"
                      {...register('reason')}
                      autoComplete="off"
                      className="mt-1.5"
                      placeholder="Why this material is going out"
                      aria-invalid={Boolean(errors.reason)}
                      aria-describedby="challan-reason-hint"
                    />
                    <p id="challan-reason-hint" className="mt-1 text-xs text-muted-foreground">
                      Required by the API and recorded against the stock movement.
                    </p>
                    <FieldError message={errors.reason?.message} />
                  </div>
                </div>

                <div className="space-y-2">
                  <p className="text-sm font-medium">Lines</p>
                  {fields.map((field, index) => {
                    const line = linesOf(order)[index]
                    return (
                      <div
                        key={field.id}
                        className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-[1fr_10rem]"
                      >
                        <div className="min-w-0">
                          <Label htmlFor={`challan-line-${index}`} className="block truncate">
                            {field.label}
                          </Label>
                          {line && (
                            <p className="mt-1 text-xs text-muted-foreground">
                              Ordered {formatQuantity(line.quantity_to_issue)}
                              {' - already issued '}
                              {formatQuantity(line.issued_quantity)}
                            </p>
                          )}
                          {line?.is_batch_tracked && (
                            <div className="mt-3">
                              <Label htmlFor={`challan-batch-${index}`} required>
                                Batch
                              </Label>
                              <Controller
                                control={control}
                                name={`lines.${index}.batch_id`}
                                render={({ field }) => (
                                  <Select
                                    value={field.value || undefined}
                                    onValueChange={field.onChange}
                                    disabled={Boolean(line.batch_id)}
                                  >
                                    <SelectTrigger
                                      id={`challan-batch-${index}`}
                                      className="mt-1.5"
                                      aria-invalid={Boolean(errors.lines?.[index]?.batch_id)}
                                    >
                                      <SelectValue
                                        placeholder={
                                          batchLists[index]?.isLoading
                                            ? 'Loading batches...'
                                            : 'Select batch'
                                        }
                                      />
                                    </SelectTrigger>
                                    <SelectContent>
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
                            </div>
                          )}
                        </div>
                        <div>
                          <Input
                            id={`challan-line-${index}`}
                            {...register(`lines.${index}.quantity`)}
                            inputMode="decimal"
                            className="text-right tabular"
                            aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                          />
                          <FieldError message={errors.lines?.[index]?.quantity?.message} />
                        </div>
                      </div>
                    )
                  })}
                  <FieldError message={errors.root?.message} />
                </div>

                <div>
                  <Label htmlFor="challan-remarks">Remarks</Label>
                  <Textarea id="challan-remarks" {...register('remarks')} className="mt-1.5" />
                  <FieldError message={errors.remarks?.message} />
                </div>
              </>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={issue.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={issue.isPending} disabled={!order}>
              Issue and create challan
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// --------------------------------------------------------------- receive ----

const receiveSchema = z.object({
  receipt_date: z.string().min(1, 'A receipt needs a date'),
  remarks: z.string().trim().max(500, 'At most 500 characters'),
  /** Required by the API on every receipt - it is the audit reason. */
  reason: z.string().trim().min(1, 'Say what this receipt covers'),
  vendor_challan_number: z.string().trim().max(64, 'At most 64 characters'),
  /** The vendor's own delivery document is dated by them, not by us. */
  vendor_challan_date: z.string(),
  /**
   * Acknowledge loss beyond the agreed allowance. Honoured only from a caller
   * holding `job_work.close` - the excess debits the vendor ledger, so the
   * storekeeper keying the lot cannot accept it alone.
   */
  approve_excess_loss: z.boolean(),
  lines: z.array(
    z.object({
      line_number: z.number(),
      label: z.string(),
      received_quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
      rejected_quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
      /**
       * Material that went out and is coming back untouched. The API models
       * this as `returned_unprocessed_quantity`; there is no free-standing
       * "loss" bucket on a receipt.
       */
      returned_quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
      /** Where rejected output physically is - ignored when nothing was rejected. */
      rejection_action: z.enum(REJECTION_ACTIONS),
      /** Why the output was rejected. Sent as the receipt line's notes. */
      rejection_reason: z.string().trim().max(500, 'At most 500 characters'),
      /**
       * Close the line: everything still at the vendor that this lot does not
       * account for is written off as process loss, within the allowance as
       * scrap and beyond it - with approval - as a vendor debit.
       */
      is_final: z.boolean(),
    }),
  ),
})

type ReceiveFormValues = z.infer<typeof receiveSchema>

function receiveDefaults(order: JobWorkOrderDetail | undefined): ReceiveFormValues {
  return {
    receipt_date: today(),
    remarks: '',
    reason: '',
    vendor_challan_number: '',
    vendor_challan_date: '',
    approve_excess_loss: false,
    lines: linesOf(order).map((line) => ({
      line_number: line.line_number,
      label: `Line ${line.line_number} - ${line.item_id.slice(0, 8)}`,
      // Outstanding is in input units; Received is in output units. Apply the
      // order's frozen yield, just as the receipt service does in reverse.
      received_quantity: toDecimal(stillAtVendor(line))
        .times(toDecimal(line.expected_output_quantity))
        .div(toDecimal(line.quantity_to_issue))
        .toDecimalPlaces(4)
        .toFixed(),
      rejected_quantity: '',
      returned_quantity: '',
      rejection_action: 'RETURN_TO_STOCK',
      rejection_reason: '',
      is_final: false,
    })),
  }
}

/**
 * What one receipt line leaves unaccounted for, in input units.
 *
 * The yield ratio is frozen on the order line: output units become input units
 * through it, and unworked returns already are input units. Rework the vendor
 * keeps consumes nothing, so it is outside the accounting by design.
 */
function lineShortfall(
  line: JobWorkOrderLine,
  values: ReceiveFormValues['lines'][number],
): { shortfall: string; allowance: string; excess: string } {
  const outstanding = toDecimal(stillAtVendor(line))
  const good = toDecimal(values.received_quantity || '0')
  const rejected = toDecimal(values.rejected_quantity || '0')
  const returned = toDecimal(values.returned_quantity || '0')
  const ratio = toDecimal(line.expected_output_quantity).div(toDecimal(line.quantity_to_issue))
  const delivered = values.rejection_action === 'REWORK_AT_VENDOR' ? good : good.plus(rejected)
  const forOutput = ratio.isZero() ? toDecimal(0) : delivered.div(ratio)
  const accounted = forOutput.plus(returned)
  const shortfall = outstanding.minus(accounted)
  const allowance = outstanding.times(toDecimal(line.expected_loss_percentage)).div(100)
  const excess = shortfall.minus(allowance)
  const floor = (value: typeof shortfall) => (value.greaterThan(0) ? value.toFixed(4) : '0')
  return { shortfall: floor(shortfall), allowance: floor(allowance), excess: floor(excess) }
}

/** Hint for each rejection disposition, stated where the choice is made. */
const REJECTION_HINTS: Record<RejectionAction, string> = {
  RETURN_TO_STOCK: 'The bad pieces came back and sit in damaged stock.',
  REWORK_AT_VENDOR: 'The vendor keeps them and reprocesses them. Nothing moves.',
  SCRAP_AT_VENDOR: 'Destroyed there. The input share leaves the job-work pool as scrap.',
}

export function JobWorkReceiveDialog({
  open,
  onOpenChange,
  orderId,
  orderCode,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  orderCode: string
}) {
  const queryClient = useQueryClient()
  const { can } = useAuth()
  const orderQuery = useJobWorkOrder(orderId, open)
  const order = orderQuery.data

  // Accepting loss beyond the agreed allowance debits the vendor, so the
  // approval is the senior `job_work.close` right - the same right that
  // approves the order - not the storekeeper's `job_work.receive`. The API
  // enforces this; the checkbox is simply not offered to anyone else.
  const canApproveExcess = can(P.JOB_WORK_CLOSE)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<ReceiveFormValues>({
    resolver: zodResolver(receiveSchema),
    defaultValues: receiveDefaults(undefined),
  })

  const { fields } = useFieldArray({ control, name: 'lines' })
  // Live per-line arithmetic: the close-line write-off and the excess over the
  // allowance are shown before the request rather than after a refusal.
  const watchedLines = watch('lines')
  const approveExcess = watch('approve_excess_loss')

  useEffect(() => {
    if (open && order) reset(receiveDefaults(order))
  }, [open, order, reset])

  const receive = useMutation({
    mutationFn: (values: ReceiveFormValues) =>
      api.post(`/job-work/${orderId}/receive`, {
        reason: values.reason,
        receipt_date: values.receipt_date,
        vendor_challan_number: orNull(values.vendor_challan_number),
        vendor_challan_date: orNull(values.vendor_challan_date),
        approve_excess_loss: values.approve_excess_loss,
        notes: orNull(values.remarks),
        lines: values.lines
          .filter(
            (line) =>
              toDecimal(line.received_quantity).greaterThan(0) ||
              toDecimal(line.rejected_quantity).greaterThan(0) ||
              toDecimal(line.returned_quantity).greaterThan(0) ||
              line.is_final,
          )
          .map((line) => ({
            line_number: line.line_number,
            // Good output that came back processed.
            output_quantity: asDecimal(line.received_quantity),
            rejected_quantity: asDecimal(line.rejected_quantity),
            rejection_action: line.rejection_action,
            returned_unprocessed_quantity: asDecimal(line.returned_quantity),
            is_final: line.is_final,
            notes: orNull(line.rejection_reason),
            batch_id:
              linesOf(order).find((orderLine) => orderLine.line_number === line.line_number)
                ?.batch_id ?? null,
          })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      toast.success(success.recorded('Receipt'), `Output was received against ${orderCode}.`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Receive output on {orderCode}</DialogTitle>
          <DialogDescription>
            Good output returns to stock. Rejection and unworked returns are recorded separately:
            what the vendor spoiled and what simply came back untouched are different conversations.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            const anything = values.lines.some(
              (line) =>
                toDecimal(line.received_quantity).greaterThan(0) ||
                toDecimal(line.rejected_quantity).greaterThan(0) ||
                toDecimal(line.returned_quantity).greaterThan(0) ||
                line.is_final,
            )
            if (!anything) {
              setError('root', {
                type: 'manual',
                message:
                  'Enter an output, rejected or returned quantity on at least one line, or close a line',
              })
              return
            }
            receive.mutate(values)
          })}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            {orderQuery.isLoading && <Skeleton className="h-40 w-full" />}

            {orderQuery.error && (
              <Alert variant="destructive">
                <AlertDescription>{orderQuery.error.message}</AlertDescription>
              </Alert>
            )}

            {order && (
              <>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <div>
                    <Label htmlFor="receipt-date" required>
                      Receipt date
                    </Label>
                    <Input
                      id="receipt-date"
                      type="date"
                      {...register('receipt_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.receipt_date)}
                    />
                    <FieldError message={errors.receipt_date?.message} />
                  </div>

                  <div>
                    <Label htmlFor="receive-vendor-challan">Vendor challan number</Label>
                    <Input
                      id="receive-vendor-challan"
                      {...register('vendor_challan_number')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.vendor_challan_number)}
                    />
                    <FieldError message={errors.vendor_challan_number?.message} />
                  </div>

                  <div>
                    <Label htmlFor="receive-vendor-challan-date">Vendor challan date</Label>
                    <Input
                      id="receive-vendor-challan-date"
                      type="date"
                      {...register('vendor_challan_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.vendor_challan_date)}
                    />
                    <FieldError message={errors.vendor_challan_date?.message} />
                  </div>

                  <div>
                    <Label htmlFor="receive-reason" required>
                      Reason
                    </Label>
                    <Input
                      id="receive-reason"
                      {...register('reason')}
                      autoComplete="off"
                      className="mt-1.5"
                      placeholder="What this receipt covers"
                      aria-invalid={Boolean(errors.reason)}
                    />
                    <FieldError message={errors.reason?.message} />
                  </div>
                </div>

                {canApproveExcess && (
                  <Controller
                    control={control}
                    name="approve_excess_loss"
                    render={({ field }) => (
                      <div className="flex items-start gap-2.5 rounded-md border p-3">
                        <Checkbox
                          id="receive-approve-excess"
                          checked={field.value}
                          onCheckedChange={(checked) => field.onChange(checked === true)}
                        />
                        <div>
                          <Label htmlFor="receive-approve-excess">
                            Approve excess loss (debit vendor)
                          </Label>
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            Loss beyond a line&apos;s agreed allowance is refused without this. Its
                            value is debited to the vendor at the material&apos;s own cost.
                          </p>
                        </div>
                      </div>
                    )}
                  />
                )}

                <p className="text-xs text-muted-foreground">
                  Up to {EXCESS_RECEIPT_TOLERANCE_PERCENT} more output than the order expected is
                  accepted; beyond that the lot is refused rather than clamped.
                </p>

                <div className="space-y-2">
                  <p className="text-sm font-medium">Lines</p>
                  {fields.map((field, index) => {
                    const line = linesOf(order)[index]
                    const live = watchedLines?.[index]
                    const rejectedLive = toDecimal(live?.rejected_quantity || '0')
                    const closing = live?.is_final === true
                    const shortfall = line && live ? lineShortfall(line, live) : null
                    return (
                      <div key={field.id} className="rounded-md border p-3">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium">{field.label}</p>
                          {line && (
                            <p className="mt-0.5 text-xs text-muted-foreground">
                              Issued {formatQuantity(line.issued_quantity)}
                              {' - still at vendor '}
                              {formatQuantity(stillAtVendor(line))}
                              {' - returns as '}
                              {line.expected_output_item_id.slice(0, 8)}
                              {' - loss allowance '}
                              {formatQuantity(line.expected_loss_percentage)}%
                            </p>
                          )}
                        </div>

                        <div className="mt-3 grid gap-3 sm:grid-cols-3">
                          <div>
                            <Label htmlFor={`receive-good-${index}`}>Received</Label>
                            <Input
                              id={`receive-good-${index}`}
                              {...register(`lines.${index}.received_quantity`)}
                              inputMode="decimal"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(errors.lines?.[index]?.received_quantity)}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.received_quantity?.message}
                            />
                          </div>
                          <div>
                            <Label htmlFor={`receive-rejected-${index}`}>Rejected</Label>
                            <Input
                              id={`receive-rejected-${index}`}
                              {...register(`lines.${index}.rejected_quantity`)}
                              inputMode="decimal"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(errors.lines?.[index]?.rejected_quantity)}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.rejected_quantity?.message}
                            />
                          </div>
                          <div>
                            <Label htmlFor={`receive-returned-${index}`}>Returned unworked</Label>
                            <Input
                              id={`receive-returned-${index}`}
                              {...register(`lines.${index}.returned_quantity`)}
                              inputMode="decimal"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(errors.lines?.[index]?.returned_quantity)}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.returned_quantity?.message}
                            />
                          </div>
                        </div>

                        {rejectedLive.greaterThan(0) && (
                          <div className="mt-3 grid gap-3 sm:grid-cols-2">
                            <div>
                              <Label htmlFor={`receive-action-${index}`}>Rejection action</Label>
                              <Controller
                                control={control}
                                name={`lines.${index}.rejection_action`}
                                render={({ field: action }) => (
                                  <Select value={action.value} onValueChange={action.onChange}>
                                    <SelectTrigger
                                      id={`receive-action-${index}`}
                                      className="mt-1.5"
                                    >
                                      <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                      {REJECTION_ACTIONS.map((value) => (
                                        <SelectItem key={value} value={value}>
                                          {humanise(value)}
                                        </SelectItem>
                                      ))}
                                    </SelectContent>
                                  </Select>
                                )}
                              />
                              <p className="mt-1 text-xs text-muted-foreground">
                                {REJECTION_HINTS[live?.rejection_action ?? 'RETURN_TO_STOCK']}
                              </p>
                            </div>
                            <div>
                              <Label htmlFor={`receive-reason-${index}`}>Rejection reason</Label>
                              <Input
                                id={`receive-reason-${index}`}
                                {...register(`lines.${index}.rejection_reason`)}
                                autoComplete="off"
                                className="mt-1.5"
                                placeholder="Why the output was rejected"
                                aria-invalid={Boolean(errors.lines?.[index]?.rejection_reason)}
                              />
                              <FieldError
                                message={errors.lines?.[index]?.rejection_reason?.message}
                              />
                            </div>
                          </div>
                        )}

                        <Controller
                          control={control}
                          name={`lines.${index}.is_final`}
                          render={({ field: closingField }) => (
                            <div className="mt-3 flex items-start gap-2.5 rounded-md bg-muted/50 p-2.5">
                              <Checkbox
                                id={`receive-close-${index}`}
                                checked={closingField.value}
                                onCheckedChange={(checked) =>
                                  closingField.onChange(checked === true)
                                }
                              />
                              <div>
                                <Label htmlFor={`receive-close-${index}`}>
                                  Close line - write off shortfall
                                </Label>
                                <p className="mt-0.5 text-xs text-muted-foreground">
                                  {shortfall && closing ? (
                                    <>
                                      {formatQuantity(shortfall.shortfall)} still out becomes
                                      process loss ({formatQuantity(shortfall.allowance)} within the
                                      allowance
                                      {toDecimal(shortfall.excess).greaterThan(0) && (
                                        <>
                                          {', '}
                                          <span className="font-medium text-destructive">
                                            {formatQuantity(shortfall.excess)} beyond it
                                            {!canApproveExcess
                                              ? ' - needs a closer\u2019s approval to post'
                                              : approveExcess
                                                ? ' - debited to the vendor'
                                                : ' - tick the excess approval above to post it'}
                                          </span>
                                        </>
                                      )}
                                      ).
                                    </>
                                  ) : (
                                    'Everything still at the vendor that this lot does not account for becomes process loss.'
                                  )}
                                </p>
                              </div>
                            </div>
                          )}
                        />
                      </div>
                    )
                  })}
                  <FieldError message={errors.root?.message} />
                </div>

                <div>
                  <Label htmlFor="receipt-remarks">Remarks</Label>
                  <Textarea id="receipt-remarks" {...register('remarks')} className="mt-1.5" />
                  <FieldError message={errors.remarks?.message} />
                </div>

                <Alert variant="warning">
                  <AlertDescription>
                    Material returned unworked comes back into stock as it went out - it is not a
                    write-off. Rejected output is recorded with where it physically is: back in
                    stock, kept by the vendor for rework, or scrapped at the vendor. Anything still
                    at the vendor that a closed line does not account for is written off as process
                    loss, and loss beyond the agreed allowance is debited to the vendor once
                    approved.
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
              disabled={receive.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={receive.isPending} disabled={!order}>
              Record receipt
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------- QC verdict ------

/**
 * The inspection verdict on output held for QC.
 *
 * This screen was missing, and its absence stranded documents. A job-work order
 * whose output is held for inspection sits in QC_PENDING, and the state machine
 * gives that status exactly ONE way out - record the verdict, which moves it to
 * COMPLETED. Nothing in the application called that endpoint, while the order
 * list offered a Close button on the same status that the server refuses,
 * because there is no QC_PENDING -> CLOSED edge. So the order could not be
 * closed, could not be completed, and its output stayed on QC hold: real stock,
 * owned and valued, that nothing could release and nobody could explain.
 *
 * The verdict is stated per output item and must account for every held unit,
 * which the server enforces. That is not pedantry: releasing part of a held lot
 * and leaving the rest in limbo is how stock becomes permanently unusable
 * without anyone deciding that it should be.
 */

const verdictSchema = z.object({
  reason: z.string().trim().min(1, validation.required('Reason')).max(500),
  lines: z.array(
    z.object({
      line_number: z.number(),
      output_item_id: z.string(),
      held_quantity: z.string(),
      accepted_quantity: z.string(),
      rejected_quantity: z.string(),
    }),
  ),
})

type VerdictFormValues = z.infer<typeof verdictSchema>

/** Everything held is proposed as accepted; rejecting is the deliberate edit. */
function verdictDefaults(order: JobWorkOrderDetail | undefined): VerdictFormValues {
  return {
    reason: '',
    lines: (order?.lines ?? []).map((line) => ({
      line_number: line.line_number,
      output_item_id: line.expected_output_item_id,
      held_quantity: line.received_output_quantity,
      accepted_quantity: line.received_output_quantity,
      rejected_quantity: '0',
    })),
  }
}

export function JobWorkQcResultDialog({
  open,
  onOpenChange,
  orderId,
  orderCode,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  orderCode: string
}) {
  const queryClient = useQueryClient()
  const orderQuery = useJobWorkOrder(orderId, open)
  const order = orderQuery.data

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<VerdictFormValues>({
    resolver: zodResolver(verdictSchema),
    defaultValues: verdictDefaults(undefined),
  })

  const { fields } = useFieldArray({ control, name: 'lines' })
  const lines = watch('lines')

  useEffect(() => {
    if (open && order) reset(verdictDefaults(order))
  }, [open, order, reset])

  const record = useMutation({
    mutationFn: (values: VerdictFormValues) =>
      api.post(`/job-work/${orderId}/qc-result`, {
        reason: values.reason,
        items: values.lines.map((line) => ({
          output_item_id: line.output_item_id,
          accepted_quantity: asDecimal(line.accepted_quantity),
          rejected_quantity: asDecimal(line.rejected_quantity),
        })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      // Accepted output leaves QC hold and becomes promisable, so every stock
      // figure on every other screen has moved.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      toast.success(
        success.recorded('Verdict'),
        `The held output on ${orderCode} has been decided.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Inspection verdict on {orderCode}</DialogTitle>
          <DialogDescription>
            Accepted output is released and can be promised and sold. Rejected output stays on the
            books, out of the available pool. Every held unit has to be decided - the order cannot
            leave inspection with part of a lot undecided.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit((values) => record.mutate(values))}>
          <DialogBody className="space-y-4">
            {orderQuery.isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}

            {fields.map((field, index) => {
              const line = lines[index]
              const held = toDecimal(line?.held_quantity ?? '0')
              const decided = toDecimal(line?.accepted_quantity ?? '0').plus(
                toDecimal(line?.rejected_quantity ?? '0'),
              )
              const shortfall = held.minus(decided)

              return (
                <div key={field.id} className="rounded-md border p-3">
                  <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                    <p className="text-sm font-medium">Line {field.line_number}</p>
                    <p className="text-xs text-muted-foreground">
                      {formatQuantity(field.held_quantity)} held for inspection
                    </p>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <Label htmlFor={`accepted-${index}`}>Accepted</Label>
                      <Input
                        id={`accepted-${index}`}
                        className="mt-1.5"
                        inputMode="decimal"
                        {...register(`lines.${index}.accepted_quantity`)}
                      />
                    </div>
                    <div>
                      <Label htmlFor={`rejected-${index}`}>Rejected</Label>
                      <Input
                        id={`rejected-${index}`}
                        className="mt-1.5"
                        inputMode="decimal"
                        {...register(`lines.${index}.rejected_quantity`)}
                      />
                    </div>
                  </div>

                  {/* Shown before the request rather than after the refusal: the
                      server enforces this and a person filling the form should
                      not have to press Save to discover the arithmetic. */}
                  {!shortfall.isZero() && (
                    <p className="mt-2 text-xs text-destructive">
                      {shortfall.greaterThan(0)
                        ? `${formatQuantity(shortfall.toString())} still undecided.`
                        : `${formatQuantity(shortfall.negated().toString())} more than is held.`}
                    </p>
                  )}
                </div>
              )
            })}

            <div>
              <Label htmlFor="verdict-reason" required>
                Reason
              </Label>
              <Input id="verdict-reason" className="mt-1.5" {...register('reason')} />
              <FieldError message={errors.reason?.message} />
            </div>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={record.isPending}>
              Record verdict
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

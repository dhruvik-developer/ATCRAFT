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
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useFieldArray, useForm } from 'react-hook-form'
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
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatQuantity, toDateInput, toDecimal } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  jobWorkKeys,
  linesOf,
  useJobWorkOrder,
  type JobWorkOrderDetail,
  type JobWorkOrderLine,
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
 * The line records scrap rather than a separate loss quantity; material the
 * vendor spoiled is scrapped at their end and debited to them.
 */
function stillAtVendor(line: JobWorkOrderLine): string {
  const outstanding = toDecimal(line.issued_quantity)
    .minus(toDecimal(line.received_quantity))
    .minus(toDecimal(line.rejected_quantity))
    .minus(toDecimal(line.scrap_quantity))
  return outstanding.greaterThan(0) ? outstanding.toFixed() : '0'
}

// ----------------------------------------------------------------- issue ----

const issueSchema = z.object({
  challan_date: z.string().min(1, 'A challan needs a date'),
  expected_return_date: z.string(),
  vehicle_number: z.string().trim().max(32, 'At most 32 characters'),
  remarks: z.string().trim().max(500, 'At most 500 characters'),
  /** Required by the API on every issue - it is the audit reason. */
  reason: z.string().trim().min(1, 'Say why this material is going out'),
  lines: z.array(
    z.object({
      // The API addresses a line by its number on the order, not by its id.
      line_number: z.number(),
      label: z.string(),
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
    remarks: '',
    reason: '',
    // The line carries an item id and nothing else - no code, no name - so the
    // label is the line number and the id it points at.
    lines: linesOf(order).map((line) => ({
      line_number: line.line_number,
      label: `Line ${line.line_number} - ${line.item_id.slice(0, 8)}`,
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
        // The field is `challan_notes` on this endpoint, not `remarks`.
        challan_notes: orNull(values.remarks),
        lines: values.lines
          .filter((line) => toDecimal(line.quantity).greaterThan(0))
          .map((line) => ({
            line_number: line.line_number,
            issued_quantity: asDecimal(line.quantity),
          })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      // The stock does not leave the company, but it does change state, so the
      // balances and any reservation against it move with it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      toast.success('Challan raised', `Material was issued against ${orderCode}.`)
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
      loss_quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
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
    lines: linesOf(order).map((line) => ({
      line_number: line.line_number,
      label: `Line ${line.line_number} - ${line.item_id.slice(0, 8)}`,
      // Pre-filled with everything still at the vendor: a full return is the
      // common case, and the rejection and loss columns are the exceptions.
      received_quantity: stillAtVendor(line),
      rejected_quantity: '',
      loss_quantity: '',
    })),
  }
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
  const orderQuery = useJobWorkOrder(orderId, open)
  const order = orderQuery.data

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<ReceiveFormValues>({
    resolver: zodResolver(receiveSchema),
    defaultValues: receiveDefaults(undefined),
  })

  const { fields } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (open && order) reset(receiveDefaults(order))
  }, [open, order, reset])

  const receive = useMutation({
    mutationFn: (values: ReceiveFormValues) =>
      api.post(`/job-work/${orderId}/receive`, {
        reason: values.reason,
        receipt_date: values.receipt_date,
        vendor_challan_number: orNull(values.vendor_challan_number),
        notes: orNull(values.remarks),
        lines: values.lines
          .filter(
            (line) =>
              toDecimal(line.received_quantity).greaterThan(0) ||
              toDecimal(line.rejected_quantity).greaterThan(0) ||
              toDecimal(line.loss_quantity).greaterThan(0),
          )
          .map((line) => ({
            line_number: line.line_number,
            // Good output that came back processed.
            output_quantity: asDecimal(line.received_quantity),
            rejected_quantity: asDecimal(line.rejected_quantity),
            returned_unprocessed_quantity: asDecimal(line.loss_quantity),
          })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      toast.success('Receipt recorded', `Output was received against ${orderCode}.`)
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
            what the vendor spoiled and what simply came back untouched are different
            conversations.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            const anything = values.lines.some(
              (line) =>
                toDecimal(line.received_quantity).greaterThan(0) ||
                toDecimal(line.rejected_quantity).greaterThan(0) ||
                toDecimal(line.loss_quantity).greaterThan(0),
            )
            if (!anything) {
              setError('root', {
                type: 'manual',
                message: 'Enter an output, rejected or returned quantity on at least one line',
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
                <div className="grid gap-3 sm:grid-cols-3">
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

                <div className="space-y-2">
                  <p className="text-sm font-medium">Lines</p>
                  {fields.map((field, index) => {
                    const line = linesOf(order)[index]
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
                            <Label htmlFor={`receive-loss-${index}`}>Returned unworked</Label>
                            <Input
                              id={`receive-loss-${index}`}
                              {...register(`lines.${index}.loss_quantity`)}
                              inputMode="decimal"
                              className="mt-1.5 text-right tabular"
                              aria-invalid={Boolean(errors.lines?.[index]?.loss_quantity)}
                            />
                            <FieldError message={errors.lines?.[index]?.loss_quantity?.message} />
                          </div>
                        </div>
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
                    write-off. Anything the vendor spoiled is recorded as rejection, and the API
                    decides from the order whether that is scrapped at the vendor or debited to
                    them.
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

/**
 * Raise a purchase order.
 *
 * Lines are collected here rather than on the detail screen, unlike a sales
 * order. The API's duplicate check reads the vendor, the warehouse and the items
 * being bought, so an empty header would tell it nothing and the whole point of
 * the check - catching the same material being ordered twice - would be lost.
 *
 * The line totals shown are indicative. Discounts, tax and freight are the API's
 * calculation, and the screen never presents its own arithmetic as the order
 * value.
 *
 * `PurchaseOrderCreate` forbids unknown keys, so every field here is one the API
 * declares: `warehouse_id` is required (not "decide at receipt"), payment terms
 * are a whole number of days rather than free text, and each line carries its
 * own `line_number`.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
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
  AlertTitle,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatMoney, toDateInput, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { PurchaseOrderDuplicateDialog } from './purchase-order-duplicate-dialog'
import {
  DEFAULT_PRIORITY,
  MAX_PRIORITY,
  MIN_PRIORITY,
  parseDuplicateConflict,
  type DuplicateConflict,
  type PurchaseOrderDetail,
} from './procurement-types'

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const PRICE = /^\d{1,14}(\.\d{1,6})?$/
const WHOLE_DAYS = /^\d{0,4}$/

const schema = z
  .object({
    vendor_id: z.string().min(1, 'Choose a vendor'),
    po_date: z.string().min(1, 'Order date is required'),
    expected_delivery_date: z.string().min(1, 'An expected delivery date is required'),
    // Required by the API: an order has to say where the material is going,
    // because that is half of what the duplicate check matches on.
    warehouse_id: z.string().min(1, 'Choose where the material is delivered'),
    priority: z.coerce
      .number()
      .int('Whole numbers only')
      .min(MIN_PRIORITY, `Between ${MIN_PRIORITY} and ${MAX_PRIORITY}`)
      .max(MAX_PRIORITY, `Between ${MIN_PRIORITY} and ${MAX_PRIORITY}`),
    payment_terms_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
    delivery_address: z.string().trim().max(4000, 'At most 4000 characters'),
    terms: z.string().trim().max(4000, 'At most 4000 characters'),
    vendor_reference: z.string().trim().max(64, 'At most 64 characters'),
    notes: z.string().trim().max(2000, 'At most 2000 characters'),
    lines: z
      .array(
        z.object({
          item_id: z.string().min(1, 'Choose an item'),
          quantity: z
            .string()
            .trim()
            .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
            .refine((v) => toDecimal(v).greaterThan(0), 'Quantity must be greater than zero'),
          unit_price: z.string().trim().regex(PRICE, 'Enter a price with up to 6 decimal places'),
        }),
      )
      .min(1, 'A purchase order needs at least one line'),
  })
  // A delivery expected before the order is placed is a data-entry slip that
  // would immediately show up as a delay on the tracking board.
  .superRefine((values, ctx) => {
    if (
      values.expected_delivery_date &&
      values.po_date &&
      values.expected_delivery_date < values.po_date
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['expected_delivery_date'],
        message: 'Delivery cannot be expected before the order date',
      })
    }
  })

type PurchaseOrderFormValues = z.infer<typeof schema>

function emptyValues(): PurchaseOrderFormValues {
  return {
    vendor_id: '',
    po_date: toDateInput(new Date()),
    expected_delivery_date: '',
    warehouse_id: '',
    priority: DEFAULT_PRIORITY,
    payment_terms_days: '',
    delivery_address: '',
    terms: '',
    vendor_reference: '',
    notes: '',
    lines: [{ item_id: '', quantity: '', unit_price: '' }],
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/**
 * The create payload, exactly as `PurchaseOrderCreate` declares it.
 *
 * `acknowledgement` is the one supported answer to a duplicate refusal: the same
 * order resubmitted with `acknowledge_duplicates` set and the buyer's reason
 * recorded on the document. The API refuses the pair without approval rights,
 * and refuses the flag without a reason.
 */
function buildPayload(
  values: PurchaseOrderFormValues,
  acknowledgement: string | null,
): Record<string, unknown> {
  return {
    vendor_id: values.vendor_id,
    warehouse_id: values.warehouse_id,
    po_date: values.po_date,
    expected_delivery_date: values.expected_delivery_date,
    priority: values.priority,
    // Omitted rather than zeroed: the API falls back to the vendor's agreed
    // terms when this is absent, and zero days is a different statement.
    ...(values.payment_terms_days.trim() === ''
      ? {}
      : { payment_terms_days: Number(values.payment_terms_days) }),
    delivery_address: orNull(values.delivery_address),
    terms: orNull(values.terms),
    vendor_reference: orNull(values.vendor_reference),
    notes: orNull(values.notes),
    lines: values.lines.map((line, index) => ({
      line_number: index + 1,
      item_id: line.item_id,
      // Quantities and prices stay strings on the wire; the API parses Decimals.
      quantity: line.quantity.trim(),
      unit_price: line.unit_price.trim(),
    })),
    ...(acknowledgement
      ? {
          acknowledge_duplicates: true,
          duplicate_acknowledgement_reason: acknowledgement,
        }
      : {}),
  }
}

/**
 * A duplicate is a business answer, not a transport failure.
 *
 * Returning it as a successful outcome keeps it off the global error path,
 * where it would raise a toast on top of the dialog that actually explains the
 * situation. Everything else is re-thrown and handled as usual.
 */
type CreateOutcome =
  | { kind: 'created'; order: PurchaseOrderDetail }
  | { kind: 'duplicate'; conflict: DuplicateConflict }

export function PurchaseOrderFormDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [conflict, setConflict] = useState<DuplicateConflict | null>(null)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    getValues,
    formState: { errors },
  } = useForm<PurchaseOrderFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (open) {
      reset(emptyValues())
      setConflict(null)
    }
  }, [open, reset])

  const create = useMutation({
    mutationFn: async ({
      values,
      acknowledgement,
    }: {
      values: PurchaseOrderFormValues
      acknowledgement: string | null
    }): Promise<CreateOutcome> => {
      try {
        const order = await api.post<PurchaseOrderDetail>(
          '/procurement/purchase-orders',
          buildPayload(values, acknowledgement),
        )
        return { kind: 'created', order }
      } catch (error) {
        const duplicate = parseDuplicateConflict(error)
        if (duplicate) return { kind: 'duplicate', conflict: duplicate }
        throw error
      }
    },
    onSuccess: (outcome) => {
      if (outcome.kind === 'duplicate') {
        setConflict(outcome.conflict)
        return
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      // `POST /procurement/purchase-orders` raises a DRAFT and stops there - it
      // does not submit it. Saying the order was "awaiting approval" sent buyers
      // away from a document that was still sitting in their own queue; the
      // submit step is a separate command on the order's own screen.
      toast.success(
        success.created('Purchase order'),
        `${outcome.order.code} is saved as a draft. Submit it for approval when the lines are right.`,
      )
      setConflict(null)
      onOpenChange(false)
      navigate(`/procurement/purchase-orders/${outcome.order.id}`)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const lines = watch('lines')

  // Indicative only - shown so a fat-fingered quantity is obvious before the
  // order is raised, never presented as the order value.
  const indicativeTotal = lines.reduce(
    (sum, line) => sum.plus(toDecimal(line.quantity).times(toDecimal(line.unit_price))),
    toDecimal(0),
  )

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent size="xl">
          <DialogHeader>
            <DialogTitle>New purchase order</DialogTitle>
            <DialogDescription>
              The order is created as a draft and goes through approval before it can be sent. Only
              approved vendors can be chosen - the API refuses the rest.
            </DialogDescription>
          </DialogHeader>

          <form
            onSubmit={handleSubmit((values) => create.mutate({ values, acknowledgement: null }))}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-5">
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div className="sm:col-span-2 lg:col-span-1">
                  <Controller
                    control={control}
                    name="vendor_id"
                    render={({ field }) => (
                      <VendorPicker
                        id="po-vendor"
                        label="Vendor"
                        value={field.value || null}
                        onChange={(vendorId) => field.onChange(vendorId ?? '')}
                        placeholder="Search approved vendors"
                        approvedOnly
                        invalid={Boolean(errors.vendor_id)}
                      />
                    )}
                  />
                  <FieldError message={errors.vendor_id?.message} />
                </div>

                <div>
                  <Label htmlFor="po-date" required>
                    Order date
                  </Label>
                  <Input
                    id="po-date"
                    type="date"
                    {...register('po_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.po_date)}
                  />
                  <FieldError message={errors.po_date?.message} />
                </div>

                <div>
                  <Label htmlFor="po-expected" required>
                    Expected delivery
                  </Label>
                  <Input
                    id="po-expected"
                    type="date"
                    {...register('expected_delivery_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.expected_delivery_date)}
                  />
                  <FieldError message={errors.expected_delivery_date?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    The date the delay indicator is measured against.
                  </p>
                </div>

                {/* Required by the API, and only offered to a principal who
                    can read warehouses: firing the lookup regardless would earn
                    a 403 they cannot act on. */}
                {can(P.WAREHOUSE_VIEW) ? (
                  <div>
                    <Controller
                      control={control}
                      name="warehouse_id"
                      render={({ field }) => (
                        <WarehouseSelect
                          id="po-warehouse"
                          label="Deliver to"
                          value={field.value}
                          onChange={field.onChange}
                          invalid={Boolean(errors.warehouse_id)}
                        />
                      )}
                    />
                    <FieldError message={errors.warehouse_id?.message} />
                  </div>
                ) : (
                  // The API requires a warehouse, so without the right to read
                  // the list there is nothing to choose and the form would fail
                  // silently on a field that was never shown. Say so instead.
                  <div className="sm:col-span-2">
                    <Alert variant="warning">
                      <AlertTitle>You cannot raise an order from here</AlertTitle>
                      <AlertDescription>
                        A purchase order has to state the warehouse the material is delivered to,
                        and your account cannot read the warehouse list. Ask somebody who can to
                        raise it.
                      </AlertDescription>
                    </Alert>
                  </div>
                )}

                <div>
                  <Label htmlFor="po-priority">Priority</Label>
                  <Input
                    id="po-priority"
                    type="number"
                    min={MIN_PRIORITY}
                    max={MAX_PRIORITY}
                    step={1}
                    {...register('priority')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.priority)}
                  />
                  <FieldError message={errors.priority?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    {MIN_PRIORITY} is the most urgent, {MAX_PRIORITY} the least. {DEFAULT_PRIORITY}{' '}
                    is routine.
                  </p>
                </div>

                <div>
                  <Label htmlFor="po-payment-terms">Payment terms (days)</Label>
                  <Input
                    id="po-payment-terms"
                    inputMode="numeric"
                    {...register('payment_terms_days')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.payment_terms_days)}
                  />
                  <FieldError message={errors.payment_terms_days?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Left blank, the vendor&rsquo;s agreed terms apply.
                  </p>
                </div>

                <div>
                  <Label htmlFor="po-vendor-reference">Vendor reference</Label>
                  <Input
                    id="po-vendor-reference"
                    {...register('vendor_reference')}
                    autoComplete="off"
                    className="mt-1.5"
                  />
                  <FieldError message={errors.vendor_reference?.message} />
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium">Lines</h3>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => append({ item_id: '', quantity: '', unit_price: '' })}
                  >
                    <Plus />
                    Add line
                  </Button>
                </div>
                <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />

                <div className="mt-3 space-y-3">
                  {fields.map((field, index) => (
                    <div
                      key={field.id}
                      className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
                    >
                      <div className="sm:col-span-2 lg:col-span-1">
                        <Controller
                          control={control}
                          name={`lines.${index}.item_id`}
                          render={({ field: itemField }) => (
                            <ItemPicker
                              id={`po-line-item-${index}`}
                              label={`Line ${index + 1} item`}
                              value={itemField.value || null}
                              onChange={(itemId) => itemField.onChange(itemId ?? '')}
                              placeholder="Search the item master"
                              invalid={Boolean(errors.lines?.[index]?.item_id)}
                            />
                          )}
                        />
                        <FieldError message={errors.lines?.[index]?.item_id?.message} />
                      </div>

                      <div>
                        <Label htmlFor={`po-line-qty-${index}`} required>
                          Quantity
                        </Label>
                        <Input
                          id={`po-line-qty-${index}`}
                          {...register(`lines.${index}.quantity`)}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                        />
                        <FieldError message={errors.lines?.[index]?.quantity?.message} />
                      </div>

                      <div>
                        <Label htmlFor={`po-line-price-${index}`} required>
                          Unit price
                        </Label>
                        <Input
                          id={`po-line-price-${index}`}
                          {...register(`lines.${index}.unit_price`)}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.unit_price)}
                        />
                        <FieldError message={errors.lines?.[index]?.unit_price?.message} />
                      </div>

                      <div className="flex items-end sm:col-span-2 sm:justify-end lg:col-span-1 lg:justify-start">
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
                  ))}
                </div>

                <p className="mt-3 text-sm text-muted-foreground">
                  Indicative value{' '}
                  <span className="tabular font-medium text-foreground">
                    {formatMoney(indicativeTotal.toFixed(4))}
                  </span>{' '}
                  before discount, GST and rounding. The API calculates the order total.
                </p>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="po-delivery-address">Delivery address</Label>
                  <Textarea
                    id="po-delivery-address"
                    {...register('delivery_address')}
                    className="mt-1.5"
                  />
                  <FieldError message={errors.delivery_address?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Printed on the order when the material must not go to the warehouse&rsquo;s own
                    address.
                  </p>
                </div>

                <div>
                  <Label htmlFor="po-terms">Terms</Label>
                  <Textarea id="po-terms" {...register('terms')} className="mt-1.5" />
                  <FieldError message={errors.terms?.message} />
                </div>
              </div>

              <div>
                <Label htmlFor="po-notes">Notes</Label>
                <Textarea id="po-notes" {...register('notes')} className="mt-1.5" />
                <FieldError message={errors.notes?.message} />
              </div>
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={create.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" loading={create.isPending} disabled={!can(P.WAREHOUSE_VIEW)}>
                Create draft order
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {conflict && (
        <PurchaseOrderDuplicateDialog
          conflict={conflict}
          open
          onOpenChange={(next) => !next && setConflict(null)}
          isResolving={create.isPending}
          canAcknowledge={can(P.PURCHASE_ORDER_APPROVE)}
          onAcknowledge={(reason) =>
            create.mutate({ values: getValues(), acknowledgement: reason })
          }
        />
      )}
    </>
  )
}

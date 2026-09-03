/**
 * Create / edit a sales order.
 *
 * A create carries its lines. `POST /sales` takes the header *and* at least one
 * line in one payload - it has no notion of an empty draft - so the lines are
 * collected here and posted with the header. Editing is the reverse: the header
 * is patched on its own, and the line set is managed from the detail screen,
 * which is where the priced, taxed, partly-allocated lines actually live.
 *
 * Every field on this form is a field the API's own schema names. Both payload
 * schemas refuse unknown keys, so a leftover field would not be ignored - it
 * would fail the whole request.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ListPlus, Pencil, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import type { AddressType, CustomerAddress } from '@/features/customers/customer-types'
import { WarehouseSelect } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatQuantity, toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { SalesOrderLineDialog } from './sales-order-line-dialog'
import {
  PRIORITY_DEFAULT,
  PRIORITY_MAX,
  PRIORITY_MIN,
  renumber,
  type SalesOrderDetail,
  type SalesOrderLineIn,
} from './sales-types'

/** Radix Select forbids an empty option value, so "not stated" needs a sentinel. */
const NONE = '__none'

const schema = z
  .object({
    customer_id: z.string().min(1, 'Choose a customer'),
    warehouse_id: z.string().min(1, 'Choose the warehouse this ships from'),
    order_date: z.string(),
    required_date: z.string().min(1, 'Required date is required'),
    promised_date: z.string(),
    priority: z.string(),
    is_urgent: z.boolean(),
    customer_po_number: z.string().trim().max(64, 'At most 64 characters'),
    billing_address_id: z.string(),
    shipping_address_id: z.string(),
    notes: z.string().trim().max(4000, 'At most 4000 characters'),
  })
  // A required date before the order date is a data-entry slip that would flow
  // straight into MRP demand, so it is caught here rather than at confirm time.
  .superRefine((values, ctx) => {
    if (values.required_date && values.order_date && values.required_date < values.order_date) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['required_date'],
        message: 'The required date cannot fall before the order date',
      })
    }
    if (values.promised_date && values.order_date && values.promised_date < values.order_date) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['promised_date'],
        message: 'A date cannot be promised before the order was taken',
      })
    }
  })

type OrderFormValues = z.infer<typeof schema>

function emptyValues(): OrderFormValues {
  return {
    customer_id: '',
    warehouse_id: '',
    order_date: toDateInput(new Date()),
    required_date: '',
    promised_date: '',
    priority: String(PRIORITY_DEFAULT),
    is_urgent: false,
    customer_po_number: '',
    billing_address_id: NONE,
    shipping_address_id: NONE,
    notes: '',
  }
}

function valuesFrom(order: SalesOrderDetail): OrderFormValues {
  return {
    customer_id: order.customer_id,
    warehouse_id: order.warehouse_id,
    order_date: toDateInput(order.order_date),
    required_date: toDateInput(order.required_date),
    promised_date: toDateInput(order.promised_date),
    priority: String(order.priority),
    is_urgent: order.is_urgent,
    customer_po_number: order.customer_po_number ?? '',
    billing_address_id: order.billing_address_id ?? NONE,
    shipping_address_id: order.shipping_address_id ?? NONE,
    notes: order.notes ?? '',
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())
const idOrNull = (value: string): string | null => (value === NONE ? null : value)

/** 1..9, clamped, because the API rejects anything outside that band outright. */
function priorityOf(raw: string): number {
  const parsed = Number.parseInt(raw, 10)
  if (Number.isNaN(parsed)) return PRIORITY_DEFAULT
  return Math.min(PRIORITY_MAX, Math.max(PRIORITY_MIN, parsed))
}

// ------------------------------------------------------- address pickers ----

/**
 * One customer's billing or shipping addresses.
 *
 * Filtered by the API on `address_type`, not here - and it only asks once a
 * customer has been chosen, because an address list has no meaning before that.
 */
function CustomerAddressSelect({
  id,
  label,
  customerId,
  addressType,
  value,
  onChange,
}: {
  id: string
  label: string
  customerId: string
  addressType: AddressType
  value: string
  onChange: (value: string) => void
}) {
  const params = { address_type: addressType, is_active: true, limit: 100, with_total: false }
  const addresses = useQuery({
    queryKey: [...queryKeys.customers.detail(customerId), 'addresses', params],
    queryFn: () => api.getPage<CustomerAddress>(`/customers/${customerId}/addresses`, { params }),
    enabled: Boolean(customerId),
    staleTime: staleTimes.master,
  })

  const rows = addresses.data?.data ?? []

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Select value={value} onValueChange={onChange} disabled={!customerId}>
        <SelectTrigger id={id}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>
            {customerId ? "The customer's default" : 'Choose a customer first'}
          </SelectItem>
          {rows.map((address) => (
            <SelectItem key={address.id} value={address.id}>
              {address.label ?? address.address_line1}
              {address.city ? ` - ${address.city}` : ''}
              {address.is_default ? ' (default)' : ''}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

// ---------------------------------------------------------------- dialog ----

export function SalesOrderFormDialog({
  open,
  onOpenChange,
  order,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  order?: SalesOrderDetail | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const isEdit = Boolean(order)

  const [lines, setLines] = useState<SalesOrderLineIn[]>([])
  const [lineEditor, setLineEditor] = useState<{ index: number | null } | null>(null)
  const [linesTouched, setLinesTouched] = useState(false)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<OrderFormValues>({ resolver: zodResolver(schema), defaultValues: emptyValues() })

  // Keyed on the order id rather than its identity: a background refetch must
  // not resolve mid-edit and wipe what is being typed.
  const editingId = order?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(order ? valuesFrom(order) : emptyValues())
    setLines([])
    setLinesTouched(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const customerId = watch('customer_id')

  const save = useMutation({
    mutationFn: (values: OrderFormValues) => {
      const header = {
        warehouse_id: values.warehouse_id,
        required_date: values.required_date,
        promised_date: orNull(values.promised_date),
        priority: priorityOf(values.priority),
        is_urgent: values.is_urgent,
        customer_po_number: orNull(values.customer_po_number),
        billing_address_id: idOrNull(values.billing_address_id),
        shipping_address_id: idOrNull(values.shipping_address_id),
        notes: orNull(values.notes),
      }

      // `lines` is omitted from the patch entirely: sending it would replace the
      // set, and the detail screen is what owns that edit.
      return order
        ? api.patch<SalesOrderDetail>(`/sales/${order.id}`, header)
        : api.post<SalesOrderDetail>('/sales', {
            ...header,
            customer_id: values.customer_id,
            order_date: orNull(values.order_date),
            lines,
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success(isEdit ? 'Order updated' : 'Order created', saved.code)
      onOpenChange(false)
      if (!isEdit) navigate(`/sales-orders/${saved.id}`)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const submit = (values: OrderFormValues) => {
    // The API refuses an order with no lines; saying so here beats a 422 that
    // points at a field the form does not show.
    if (!isEdit && lines.length === 0) {
      setLinesTouched(true)
      return
    }
    save.mutate(values)
  }

  const editingLine =
    lineEditor && lineEditor.index !== null ? (lines[lineEditor.index] ?? null) : null

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent size="xl">
          <DialogHeader>
            <DialogTitle>{isEdit ? `Edit ${order?.code}` : 'New sales order'}</DialogTitle>
            <DialogDescription>
              {isEdit
                ? 'Header changes only. Lines are amended on the order itself, because the API prices and re-reserves the whole set together.'
                : 'The order is created as a draft with its lines. Confirming it afterwards reserves stock.'}
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={handleSubmit(submit)} className="flex min-h-0 flex-col gap-4" noValidate>
            <DialogBody className="space-y-5">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <Controller
                    control={control}
                    name="customer_id"
                    render={({ field }) => (
                      <CustomerPicker
                        id="order-customer"
                        label="Customer"
                        value={field.value || null}
                        onChange={(picked) => field.onChange(picked ?? '')}
                        placeholder="Search for a customer"
                        invalid={Boolean(errors.customer_id)}
                        // The API refuses to move an order to another account.
                        disabled={isEdit}
                      />
                    )}
                  />
                  <FieldError message={errors.customer_id?.message} />
                </div>

                <div>
                  <Label htmlFor="order-date" required={!isEdit}>
                    Order date
                  </Label>
                  <Input
                    id="order-date"
                    type="date"
                    {...register('order_date')}
                    disabled={isEdit}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.order_date)}
                  />
                  <FieldError message={errors.order_date?.message} />
                </div>

                <div>
                  <Label htmlFor="order-required-date" required>
                    Required date
                  </Label>
                  <Input
                    id="order-required-date"
                    type="date"
                    {...register('required_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.required_date)}
                  />
                  <FieldError message={errors.required_date?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    This is the demand date MRP plans against.
                  </p>
                </div>

                <div>
                  <Label htmlFor="order-promised-date">Promised date</Label>
                  <Input
                    id="order-promised-date"
                    type="date"
                    {...register('promised_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.promised_date)}
                  />
                  <FieldError message={errors.promised_date?.message} />
                </div>

                <div>
                  <Label htmlFor="order-priority">Priority</Label>
                  <Input
                    id="order-priority"
                    type="number"
                    min={PRIORITY_MIN}
                    max={PRIORITY_MAX}
                    step={1}
                    {...register('priority')}
                    className="mt-1.5 text-right tabular"
                  />
                  <p className="mt-1 text-xs text-muted-foreground">
                    {PRIORITY_MIN} to {PRIORITY_MAX}; {PRIORITY_DEFAULT} is the default rank.
                  </p>
                </div>

                {/* Only offered to a principal who can read warehouses: firing
                    the lookup regardless would earn a 403 they cannot act on. */}
                {can(P.WAREHOUSE_VIEW) ? (
                  <div>
                    <Controller
                      control={control}
                      name="warehouse_id"
                      render={({ field }) => (
                        <WarehouseSelect
                          id="order-warehouse"
                          label="Dispatch from"
                          value={field.value}
                          onChange={field.onChange}
                          invalid={Boolean(errors.warehouse_id)}
                        />
                      )}
                    />
                    <FieldError message={errors.warehouse_id?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Allocation reserves stock at this warehouse, so it is decided for the whole
                      order rather than per line.
                    </p>
                  </div>
                ) : (
                  <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                    An order names the warehouse it ships from, and your role cannot read the
                    warehouse list. Ask someone with warehouse access to raise this order.
                  </div>
                )}

                <div>
                  <Label htmlFor="order-po-number">Customer PO number</Label>
                  <Input
                    id="order-po-number"
                    {...register('customer_po_number')}
                    autoComplete="off"
                    className="mt-1.5"
                  />
                  <FieldError message={errors.customer_po_number?.message} />
                </div>

                <Controller
                  control={control}
                  name="billing_address_id"
                  render={({ field }) => (
                    <CustomerAddressSelect
                      id="order-billing-address"
                      label="Bill to"
                      customerId={customerId}
                      addressType="BILLING"
                      value={field.value}
                      onChange={field.onChange}
                    />
                  )}
                />

                <Controller
                  control={control}
                  name="shipping_address_id"
                  render={({ field }) => (
                    <CustomerAddressSelect
                      id="order-shipping-address"
                      label="Ship to"
                      customerId={customerId}
                      addressType="SHIPPING"
                      value={field.value}
                      onChange={field.onChange}
                    />
                  )}
                />

                <div className="flex items-center gap-2.5 sm:col-span-2">
                  <Controller
                    control={control}
                    name="is_urgent"
                    render={({ field }) => (
                      <Checkbox
                        id="order-urgent"
                        checked={field.value}
                        onCheckedChange={(checked) => field.onChange(checked === true)}
                      />
                    )}
                  />
                  <Label htmlFor="order-urgent" className="font-normal">
                    Urgent - show this order on the urgent-only filter
                  </Label>
                </div>

                <div className="sm:col-span-2">
                  <Label htmlFor="order-notes">Notes</Label>
                  <Textarea id="order-notes" {...register('notes')} className="mt-1.5" />
                  <FieldError message={errors.notes?.message} />
                </div>
              </div>

              {!isEdit && (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-sm font-medium">
                      Lines <span className="text-muted-foreground">({lines.length})</span>
                    </h3>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setLineEditor({ index: null })}
                    >
                      <ListPlus />
                      Add line
                    </Button>
                  </div>

                  {lines.length === 0 ? (
                    <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
                      An order is created with its lines. Add at least one.
                    </p>
                  ) : (
                    <ul className="divide-y rounded-lg border">
                      {lines.map((line, index) => (
                        <li
                          key={`${line.line_number}-${line.item_id}`}
                          className="flex items-start gap-3 p-3 text-sm"
                        >
                          <span className="tabular text-muted-foreground">{line.line_number}</span>
                          <div className="min-w-0 flex-1">
                            <span className="block truncate font-medium">
                              {line.description ?? 'Item'}
                            </span>
                            <span className="block truncate text-xs text-muted-foreground">
                              {formatQuantity(line.quantity)}
                              {line.unit_price ? ` at ${line.unit_price}` : ' at list price'}
                              {line.required_date ? ` - required ${line.required_date}` : ''}
                            </span>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              aria-label={`Edit line ${line.line_number}`}
                              onClick={() => setLineEditor({ index })}
                            >
                              <Pencil />
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              aria-label={`Remove line ${line.line_number}`}
                              onClick={() =>
                                setLines((current) =>
                                  renumber(current.filter((_, at) => at !== index)),
                                )
                              }
                            >
                              <Trash2 className="text-destructive" />
                            </Button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}

                  {linesTouched && lines.length === 0 && (
                    <FieldError message="An order needs at least one line" />
                  )}
                </div>
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
              <Button type="submit" loading={save.isPending}>
                {isEdit ? 'Save changes' : 'Create draft order'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {lineEditor && (
        <SalesOrderLineDialog
          open
          onOpenChange={(next) => !next && setLineEditor(null)}
          line={editingLine}
          lineNumber={
            lineEditor.index === null ? lines.length + 1 : lines[lineEditor.index]!.line_number
          }
          currency="INR"
          onSubmit={(line) => {
            setLines((current) =>
              renumber(
                lineEditor.index === null
                  ? [...current, line]
                  : current.map((existing, at) => (at === lineEditor.index ? line : existing)),
              ),
            )
            setLineEditor(null)
          }}
        />
      )}
    </>
  )
}

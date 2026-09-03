/**
 * Creating a dispatch, and the tracking timeline of one already on the road.
 *
 * A consignment is a loading plan: it names the warehouse it leaves, and it
 * names its lines. Nothing moves when it is created - stock leaves on
 * `POST /{id}/dispatch` - but the lines are validated against stock there and
 * then, which is why they are collected here rather than added later.
 *
 * Traceability, docs/assumptions.md section 23. A serial-tracked item is gated
 * on every movement that changes whose hands hold the unit, and a dispatch is
 * exactly that: the line has to name one serial per unit, or the API refuses the
 * consignment with SERIAL_REQUIRED. So the line editor makes the units pickable
 * from stock at that warehouse, counts them against the quantity, and says
 * plainly when it cannot offer any - an operator who cannot supply a serial
 * needs to know that before the lorry is loaded, not after.
 *
 * The create form is long on purpose. A consignment that leaves the gate with no
 * LR number, no vehicle and no driver mobile is a consignment nobody can chase
 * when the customer rings, and the tracking board downstream is only as good as
 * what is captured here.
 *
 * Permission gates on the actions are UX only; the API authorises every request
 * independently (master rules 38).
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Boxes, MapPin, Plus, ScanBarcode, Trash2, Truck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

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
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import type { SalesOrderRow } from '@/features/sales/sales-types'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
  toDateInput,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { BatchSelect, SerialPicker } from './stock-pickers'
import {
  DISPATCH_TYPES,
  FREIGHT_PAID_BY,
  MANUAL_EVENT_STATUSES,
  MONEY_INPUT,
  QUANTITY_INPUT,
  WEIGHT_INPUT,
  dispatchKeys,
  useItemFacts,
  type DispatchDetail,
  type DispatchLineIn,
  type DispatchRow,
  type DispatchTrackingEvent,
} from './dispatch-types'

/** Which counterparty a type needs, mirroring `_counterparty_matches_type`. */
const CUSTOMER_TYPES = new Set(['SALES'])
const VENDOR_TYPES = new Set(['JOB_WORK', 'PURCHASE_RETURN'])

const MOBILE = /^$|^[0-9+\- ]{7,15}$/
const BOXES = /^$|^\d{1,6}$/

/** Radix Select forbids an empty option value, so "not stated" needs a sentinel. */
const NONE = '__none'

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

// ----------------------------------------------------------- line editor ----

function LineEditor({
  warehouseId,
  onAdd,
}: {
  warehouseId: string
  onAdd: (line: DispatchLineIn, label: string) => void
}) {
  const { can } = useAuth()
  const [itemId, setItemId] = useState('')
  const [quantity, setQuantity] = useState('1')
  const [batchId, setBatchId] = useState('')
  const [serialIds, setSerialIds] = useState<string[]>([])
  const [notes, setNotes] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const facts = useItemFacts(itemId, can(P.ITEM_VIEW))
  const item = facts.data

  // The batch and serial pickers appear on the item's tracking flags. If those
  // cannot be read the form does not know what the line needs, and a
  // serial-tracked item sent with an empty `serial_ids` is refused at create
  // with SERIAL_REQUIRED - a message about a control the operator was never
  // shown. Blocked here instead, with the reason.
  const trackingUnknown = Boolean(itemId) && !facts.isLoading && !item

  useEffect(() => {
    setBatchId('')
    setSerialIds([])
    setProblem(null)
  }, [itemId])

  const submit = () => {
    if (!itemId) return setProblem('Choose the item being loaded.')
    if (trackingUnknown) {
      return setProblem(
        'Whether this item is batch or serial tracked cannot be read, so the line cannot be ' +
          'loaded here - reading the item master needs the item.view permission.',
      )
    }
    if (!QUANTITY_INPUT.test(quantity.trim()) || !toDecimal(quantity).greaterThan(0)) {
      return setProblem('Enter a quantity with up to 4 decimal places.')
    }
    if (item?.is_batch_tracked && !batchId) {
      return setProblem(`${item.code} is batch tracked - name the batch being shipped.`)
    }
    if (item?.is_serial_tracked) {
      if (serialIds.length === 0) {
        return setProblem(
          `${item.code} is serial tracked, so every unit shipped must be identified. Pick its serials.`,
        )
      }
      if (!toDecimal(quantity).equals(serialIds.length)) {
        return setProblem(
          `${serialIds.length} serial(s) chosen for a quantity of ${formatQuantity(quantity)} - a serialised line needs exactly one per unit.`,
        )
      }
    }

    onAdd(
      {
        item_id: itemId,
        quantity: quantity.trim(),
        batch_id: batchId || null,
        serial_ids: item?.is_serial_tracked ? serialIds : [],
        carton_ids: [],
        notes: orNull(notes),
      },
      item ? `${item.code} - ${item.name}` : itemId,
    )

    setItemId('')
    setQuantity('1')
    setBatchId('')
    setSerialIds([])
    setNotes('')
    setProblem(null)
  }

  return (
    <div className="space-y-3 rounded-lg border border-dashed p-3">
      <h4 className="text-sm font-medium">Load a line</h4>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="sm:col-span-2">
          <ItemPicker
            id="dispatch-line-item"
            label="Item"
            value={itemId || null}
            onChange={(next) => setItemId(next ?? '')}
            placeholder="Search the item master"
          />
        </div>

        <div>
          <Label htmlFor="dispatch-line-quantity" required>
            Quantity
          </Label>
          <Input
            id="dispatch-line-quantity"
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
            inputMode="decimal"
            className="mt-1.5 text-right tabular"
          />
        </div>

        <div>
          <Label htmlFor="dispatch-line-notes">Line notes</Label>
          <Input
            id="dispatch-line-notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            autoComplete="off"
            className="mt-1.5"
          />
        </div>

        {item?.is_batch_tracked && (
          <div className="sm:col-span-2">
            <BatchSelect
              id="dispatch-line-batch"
              itemId={itemId}
              value={batchId}
              onChange={setBatchId}
            />
          </div>
        )}
      </div>

      {item?.is_serial_tracked && (
        <SerialPicker
          idPrefix="dispatch-line"
          itemId={itemId}
          warehouseId={warehouseId}
          quantity={quantity}
          selected={serialIds}
          onChange={setSerialIds}
        />
      )}

      {problem && <FieldError message={problem} />}

      <Button type="button" variant="outline" size="sm" onClick={submit}>
        <Plus />
        Add line
      </Button>
    </div>
  )
}

// ------------------------------------------------------ sales order link ----

/**
 * Which order this consignment is shipping against.
 *
 * Optional, but it is what lets the release consume the order's reservations and
 * move its lines on, so it is offered whenever the customer is known. Only the
 * customer's open orders are asked for - the server does the narrowing.
 */
function SalesOrderSelect({
  customerId,
  value,
  onChange,
}: {
  customerId: string | null
  value: string
  onChange: (value: string) => void
}) {
  const params = { customer_id: customerId, open_only: true, limit: 50, with_total: false }
  const orders = useQuery({
    queryKey: queryKeys.sales.list(params),
    queryFn: () => api.getPage<SalesOrderRow>('/sales', { params }),
    enabled: Boolean(customerId),
    staleTime: staleTimes.transactional,
  })

  const rows = orders.data?.data ?? []

  return (
    <div className="space-y-1.5">
      <Label htmlFor="dispatch-sales-order">Against sales order</Label>
      <Select value={value} onValueChange={onChange} disabled={!customerId}>
        <SelectTrigger id="dispatch-sales-order">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>
            {customerId ? 'Not against an order' : 'Choose a customer first'}
          </SelectItem>
          {rows.map((order) => (
            <SelectItem key={order.id} value={order.id}>
              {order.code} - required {formatDate(order.required_date)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p className="text-xs text-muted-foreground">
        Naming it lets the release consume the order's reservations and move its lines on.
      </p>
    </div>
  )
}

// ----------------------------------------------------------- create form ----

const createSchema = z
  .object({
    dispatch_type: z.enum(DISPATCH_TYPES),
    customer_id: z.string().nullable(),
    vendor_id: z.string().nullable(),
    warehouse_id: z.string().min(1, 'Choose the warehouse this leaves from'),
    sales_order_id: z.string(),

    dispatch_date: z.string().min(1, 'Dispatch date is required'),
    expected_delivery_date: z.string(),

    courier_name: z.string().trim().max(120, 'At most 120 characters'),
    transport_name: z.string().trim().max(120, 'At most 120 characters'),
    tracking_id: z.string().trim().max(64, 'At most 64 characters'),
    lr_number: z.string().trim().max(64, 'At most 64 characters'),
    vehicle_number: z.string().trim().max(24, 'At most 24 characters'),
    driver_name: z.string().trim().max(120, 'At most 120 characters'),
    driver_mobile: z.string().trim().regex(MOBILE, 'Enter a reachable phone number'),
    eway_bill_number: z.string().trim().max(64, 'At most 64 characters'),

    total_boxes: z.string().trim().regex(BOXES, 'Whole number of boxes'),
    total_weight_kg: z.string().trim().regex(WEIGHT_INPUT, 'Up to 3 decimal places'),
    freight_amount: z.string().trim().regex(MONEY_INPUT, 'Up to 2 decimal places'),
    freight_paid_by: z.string(),

    notes: z.string().trim().max(2000, 'At most 2000 characters'),
  })
  .superRefine((values, ctx) => {
    if (CUSTOMER_TYPES.has(values.dispatch_type) && !values.customer_id) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['customer_id'],
        message: 'A sales consignment must name the customer it is going to',
      })
    }

    if (VENDOR_TYPES.has(values.dispatch_type) && !values.vendor_id) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['vendor_id'],
        message: 'Choose the vendor this is going to',
      })
    }

    if (
      values.dispatch_date !== '' &&
      values.expected_delivery_date !== '' &&
      values.expected_delivery_date < values.dispatch_date
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['expected_delivery_date'],
        message: 'Delivery cannot be expected before the consignment leaves',
      })
    }

    // Without one of these the consignment cannot be traced, and the tracking
    // board has nothing to show when the customer asks where it is.
    if (values.tracking_id === '' && values.lr_number === '' && values.vehicle_number === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['tracking_id'],
        message: 'Record a tracking id, an LR number or a vehicle number so this can be traced',
      })
    }
  })

type CreateFormValues = z.infer<typeof createSchema>

function emptyCreate(): CreateFormValues {
  return {
    dispatch_type: 'SALES',
    customer_id: null,
    vendor_id: null,
    warehouse_id: '',
    sales_order_id: NONE,
    dispatch_date: toDateInput(new Date()),
    expected_delivery_date: '',
    courier_name: '',
    transport_name: '',
    tracking_id: '',
    lr_number: '',
    vehicle_number: '',
    driver_name: '',
    driver_mobile: '',
    eway_bill_number: '',
    total_boxes: '',
    total_weight_kg: '',
    freight_amount: '',
    freight_paid_by: NONE,
    notes: '',
  }
}

interface DraftLine {
  line: DispatchLineIn
  label: string
}

export function CreateDispatchDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [lines, setLines] = useState<DraftLine[]>([])
  const [linesTouched, setLinesTouched] = useState(false)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<CreateFormValues>({
    resolver: zodResolver(createSchema),
    defaultValues: emptyCreate(),
  })

  useEffect(() => {
    if (open) return
    reset(emptyCreate())
    setLines([])
    setLinesTouched(false)
  }, [open, reset])

  const dispatchType = watch('dispatch_type')
  const warehouseId = watch('warehouse_id')
  const needsCustomer = CUSTOMER_TYPES.has(dispatchType)
  const needsVendor = VENDOR_TYPES.has(dispatchType)

  const create = useMutation({
    mutationFn: (values: CreateFormValues) =>
      api.post<DispatchDetail>('/dispatch', {
        dispatch_type: values.dispatch_type,
        warehouse_id: values.warehouse_id,
        customer_id: needsCustomer ? values.customer_id : null,
        vendor_id: needsVendor ? values.vendor_id : null,
        // Only a SALES consignment may name a sales order; the API refuses the
        // pairing outright on any other type.
        sales_order_id:
          needsCustomer && values.sales_order_id !== NONE ? values.sales_order_id : null,
        dispatch_date: values.dispatch_date,
        expected_delivery_date: orNull(values.expected_delivery_date),
        courier_name: orNull(values.courier_name),
        transport_name: orNull(values.transport_name),
        tracking_id: orNull(values.tracking_id),
        lr_number: orNull(values.lr_number),
        vehicle_number: orNull(values.vehicle_number),
        driver_name: orNull(values.driver_name),
        driver_mobile: orNull(values.driver_mobile),
        eway_bill_number: orNull(values.eway_bill_number),
        total_boxes: values.total_boxes.trim() === '' ? null : Number(values.total_boxes.trim()),
        // Weight and freight stay decimal strings on the wire.
        total_weight_kg: orNull(values.total_weight_kg),
        freight_amount: orNull(values.freight_amount),
        freight_paid_by: values.freight_paid_by === NONE ? null : values.freight_paid_by,
        notes: orNull(values.notes),
        lines: lines.map((draft) => draft.line),
      }),
    onSuccess: (dispatch) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      // Creating a consignment moves nothing - stock leaves on the dispatch
      // transition - but allocation now shows the units spoken for.
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      toast.success(
        'Dispatch created',
        `${dispatch.code} is staged. Mark it ready, then release it to move the stock.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const submit = (values: CreateFormValues) => {
    if (lines.length === 0) {
      setLinesTouched(true)
      return
    }
    create.mutate(values)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Create a dispatch</DialogTitle>
          <DialogDescription>
            What is going out, from where, and everything it can be traced by - recorded once, at
            the gate. Nothing leaves stock until the consignment is released.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(submit)} className="flex min-h-0 flex-col gap-4" noValidate>
          <DialogBody className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <Label htmlFor="dispatch-type" required>
                  Dispatch type
                </Label>
                <Controller
                  control={control}
                  name="dispatch_type"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="dispatch-type"
                        className="mt-1.5"
                        aria-label="Dispatch type"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {DISPATCH_TYPES.map((type) => (
                          <SelectItem key={type} value={type}>
                            {humanise(type)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.dispatch_type?.message} />
              </div>

              {needsCustomer && (
                <div>
                  <Controller
                    control={control}
                    name="customer_id"
                    render={({ field }) => (
                      <CustomerPicker
                        id="dispatch-customer"
                        label="Customer"
                        value={field.value}
                        onChange={field.onChange}
                        invalid={Boolean(errors.customer_id)}
                        placeholder="Choose a customer"
                      />
                    )}
                  />
                  <FieldError message={errors.customer_id?.message} />
                </div>
              )}

              {needsVendor && (
                <div>
                  <Controller
                    control={control}
                    name="vendor_id"
                    render={({ field }) => (
                      <VendorPicker
                        id="dispatch-vendor"
                        label="Vendor"
                        value={field.value}
                        onChange={field.onChange}
                        invalid={Boolean(errors.vendor_id)}
                        placeholder="Choose a vendor"
                      />
                    )}
                  />
                  <FieldError message={errors.vendor_id?.message} />
                </div>
              )}

              {needsCustomer && (
                <Controller
                  control={control}
                  name="sales_order_id"
                  render={({ field }) => (
                    <SalesOrderSelect
                      customerId={watch('customer_id')}
                      value={field.value}
                      onChange={field.onChange}
                    />
                  )}
                />
              )}

              {can(P.WAREHOUSE_VIEW) ? (
                <div>
                  <Controller
                    control={control}
                    name="warehouse_id"
                    render={({ field }) => (
                      <WarehouseSelect
                        id="dispatch-warehouse"
                        label="Leaves from"
                        value={field.value}
                        onChange={field.onChange}
                        invalid={Boolean(errors.warehouse_id)}
                      />
                    )}
                  />
                  <FieldError message={errors.warehouse_id?.message} />
                </div>
              ) : (
                <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                  A consignment names the warehouse it leaves, and your role cannot read the
                  warehouse list. Ask someone with warehouse access to raise this dispatch.
                </div>
              )}

              <div>
                <Label htmlFor="dispatch-date" required>
                  Dispatch date
                </Label>
                <Input
                  id="dispatch-date"
                  type="date"
                  {...register('dispatch_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.dispatch_date)}
                />
                <FieldError message={errors.dispatch_date?.message} />
              </div>

              <div>
                <Label htmlFor="dispatch-expected">Expected delivery</Label>
                <Input
                  id="dispatch-expected"
                  type="date"
                  {...register('expected_delivery_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.expected_delivery_date)}
                />
                <FieldError message={errors.expected_delivery_date?.message} />
              </div>
            </div>

            <div>
              <h3 className="text-sm font-medium">Carrier</h3>
              <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <Label htmlFor="dispatch-courier">Courier name</Label>
                  <Input
                    id="dispatch-courier"
                    {...register('courier_name')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.courier_name)}
                  />
                  <FieldError message={errors.courier_name?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-transport">Transport name</Label>
                  <Input
                    id="dispatch-transport"
                    {...register('transport_name')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.transport_name)}
                  />
                  <FieldError message={errors.transport_name?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-tracking">Tracking id</Label>
                  <Input
                    id="dispatch-tracking"
                    {...register('tracking_id')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.tracking_id)}
                  />
                  <FieldError message={errors.tracking_id?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-lr">LR number</Label>
                  <Input
                    id="dispatch-lr"
                    {...register('lr_number')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.lr_number)}
                  />
                  <FieldError message={errors.lr_number?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-vehicle">Vehicle number</Label>
                  <Input
                    id="dispatch-vehicle"
                    {...register('vehicle_number')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.vehicle_number)}
                  />
                  <FieldError message={errors.vehicle_number?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-driver">Driver name</Label>
                  <Input
                    id="dispatch-driver"
                    {...register('driver_name')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.driver_name)}
                  />
                  <FieldError message={errors.driver_name?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-driver-mobile">Driver mobile</Label>
                  <Input
                    id="dispatch-driver-mobile"
                    type="tel"
                    {...register('driver_mobile')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.driver_mobile)}
                  />
                  <FieldError message={errors.driver_mobile?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-eway">E-way bill number</Label>
                  <Input
                    id="dispatch-eway"
                    {...register('eway_bill_number')}
                    autoComplete="off"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.eway_bill_number)}
                  />
                  <FieldError message={errors.eway_bill_number?.message} />
                </div>
              </div>
            </div>

            <div>
              <h3 className="text-sm font-medium">Consignment</h3>
              <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <Label htmlFor="dispatch-boxes">Boxes</Label>
                  <Input
                    id="dispatch-boxes"
                    {...register('total_boxes')}
                    inputMode="numeric"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.total_boxes)}
                  />
                  <FieldError message={errors.total_boxes?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-weight">Weight (kg)</Label>
                  <Input
                    id="dispatch-weight"
                    {...register('total_weight_kg')}
                    inputMode="decimal"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.total_weight_kg)}
                  />
                  <FieldError message={errors.total_weight_kg?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-freight">Freight amount</Label>
                  <Input
                    id="dispatch-freight"
                    {...register('freight_amount')}
                    inputMode="decimal"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.freight_amount)}
                  />
                  <FieldError message={errors.freight_amount?.message} />
                </div>

                <div>
                  <Label htmlFor="dispatch-freight-paid-by">Freight paid by</Label>
                  <Controller
                    control={control}
                    name="freight_paid_by"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger id="dispatch-freight-paid-by" className="mt-1.5">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={NONE}>Not stated</SelectItem>
                          {FREIGHT_PAID_BY.map((who) => (
                            <SelectItem key={who} value={who}>
                              {humanise(who)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                </div>
              </div>
            </div>

            <div className="space-y-2">
              <h3 className="text-sm font-medium">
                Lines <span className="text-muted-foreground">({lines.length})</span>
              </h3>

              {lines.length === 0 ? (
                <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
                  A consignment is created with what it carries. Add at least one line.
                </p>
              ) : (
                <ul className="divide-y rounded-lg border">
                  {lines.map((draft, index) => (
                    <li key={`${draft.line.item_id}-${index}`} className="flex gap-3 p-3 text-sm">
                      <Boxes
                        className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <span className="block truncate font-medium">{draft.label}</span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {formatQuantity(draft.line.quantity)}
                          {draft.line.batch_id ? ' - batch named' : ''}
                          {draft.line.serial_ids.length > 0
                            ? ` - ${draft.line.serial_ids.length} serial(s) identified`
                            : ''}
                        </span>
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Remove line ${index + 1}`}
                        onClick={() =>
                          setLines((current) => current.filter((_, at) => at !== index))
                        }
                      >
                        <Trash2 className="text-destructive" />
                      </Button>
                    </li>
                  ))}
                </ul>
              )}

              {linesTouched && lines.length === 0 && (
                <FieldError message="A consignment needs at least one line" />
              )}

              <LineEditor
                warehouseId={warehouseId}
                onAdd={(line, label) => {
                  setLines((current) => [...current, { line, label }])
                  setLinesTouched(false)
                }}
              />
            </div>

            <div>
              <Label htmlFor="dispatch-notes">Notes</Label>
              <Textarea
                id="dispatch-notes"
                {...register('notes')}
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
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              <Truck />
              Create dispatch
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------- timeline ----

const eventSchema = z.object({
  event_status: z.enum(['IN_TRANSIT', 'DELIVERED', 'QC_PENDING', 'RECEIVED']),
  event_time: z.string().min(1, 'When did this happen?'),
  location: z.string().trim().max(160, 'At most 160 characters'),
  remark: z.string().trim().max(500, 'At most 500 characters'),
})

type EventFormValues = z.infer<typeof eventSchema>

/** `datetime-local` wants `yyyy-MM-ddTHH:mm` in the browser's own zone. */
function nowForInput(): string {
  const now = new Date()
  const offset = now.getTimezoneOffset() * 60_000
  return new Date(now.getTime() - offset).toISOString().slice(0, 16)
}

function emptyEvent(): EventFormValues {
  return { event_status: 'IN_TRANSIT', event_time: nowForInput(), location: '', remark: '' }
}

function AddTrackingEventForm({ dispatchId }: { dispatchId: string }) {
  const queryClient = useQueryClient()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<EventFormValues>({
    resolver: zodResolver(eventSchema),
    defaultValues: emptyEvent(),
  })

  const addEvent = useMutation({
    mutationFn: (values: EventFormValues) =>
      api.post<DispatchTrackingEvent>(`/dispatch/${dispatchId}/tracking-events`, {
        event_status: values.event_status,
        // The API stores UTC; the picker collects local time.
        event_time: new Date(values.event_time).toISOString(),
        location: orNull(values.location),
        remark: orNull(values.remark),
        source: 'MANUAL',
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      toast.success('Tracking event added', 'The timeline and the tracking board are updated.')
      reset(emptyEvent())
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <form
      onSubmit={handleSubmit((values) => addEvent.mutate(values))}
      className="grid gap-3 rounded-lg border border-dashed p-3 sm:grid-cols-2 lg:grid-cols-5"
      noValidate
    >
      <div>
        <Label htmlFor="event-status" required>
          What happened
        </Label>
        <Controller
          control={control}
          name="event_status"
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger id="event-status" className="mt-1.5" aria-label="Event status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MANUAL_EVENT_STATUSES.map((status) => (
                  <SelectItem key={status} value={status}>
                    {humanise(status)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        />
        <FieldError message={errors.event_status?.message} />
      </div>

      <div>
        <Label htmlFor="event-time" required>
          When
        </Label>
        <Input
          id="event-time"
          type="datetime-local"
          {...register('event_time')}
          className="mt-1.5"
          aria-invalid={Boolean(errors.event_time)}
        />
        <FieldError message={errors.event_time?.message} />
      </div>

      <div>
        <Label htmlFor="event-location">Location</Label>
        <Input
          id="event-location"
          {...register('location')}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={Boolean(errors.location)}
        />
        <FieldError message={errors.location?.message} />
      </div>

      <div>
        <Label htmlFor="event-remark">Remark</Label>
        <Input
          id="event-remark"
          {...register('remark')}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={Boolean(errors.remark)}
        />
        <FieldError message={errors.remark?.message} />
      </div>

      <div className="flex items-end">
        <Button type="submit" size="sm" className="w-full" loading={addEvent.isPending}>
          <Plus />
          Add event
        </Button>
      </div>
    </form>
  )
}

export function DispatchTimelineDialog({
  dispatch,
  open,
  onOpenChange,
}: {
  dispatch: DispatchRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const canUpdate = can(P.DISPATCH_UPDATE)

  const dispatchId = dispatch?.id ?? ''

  // The list row is a summary: no LR number, no driver, no lines. The transport
  // detail and what is actually on the lorry come from the consignment itself.
  const detail = useQuery({
    queryKey: dispatchKeys.detail(dispatchId),
    queryFn: () => api.get<DispatchDetail>(`/dispatch/${dispatchId}`),
    enabled: open && Boolean(dispatchId),
    staleTime: staleTimes.transactional,
  })

  const events = useQuery({
    queryKey: dispatchKeys.events(dispatchId),
    queryFn: () =>
      api.get<DispatchTrackingEvent[]>(`/dispatch/${dispatchId}/tracking-events`, {
        params: { limit: 50 },
      }),
    enabled: open && Boolean(dispatchId),
    staleTime: staleTimes.transactional,
  })

  const timeline = events.data ?? []
  const consignment = detail.data
  const serialised = (consignment?.lines ?? []).filter((line) => line.serial_ids.length > 0)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{dispatch ? `Tracking ${dispatch.code}` : 'Tracking'}</DialogTitle>
          <DialogDescription>
            Every reported movement of this consignment, newest first.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {dispatch && (
            <div className="rounded-lg border bg-muted/30 p-4">
              <DescriptionList
                columns={3}
                items={[
                  { label: 'Status', value: <StatusBadge status={dispatch.status} /> },
                  { label: 'Type', value: humanise(dispatch.dispatch_type) },
                  { label: 'Courier', value: dispatch.courier_name ?? '-' },
                  { label: 'Transport', value: consignment?.transport_name ?? '-' },
                  { label: 'Tracking id', value: dispatch.tracking_id ?? '-' },
                  { label: 'LR number', value: consignment?.lr_number ?? '-' },
                  { label: 'Vehicle', value: consignment?.vehicle_number ?? '-' },
                  {
                    label: 'Driver',
                    value: consignment?.driver_name
                      ? `${consignment.driver_name}${consignment.driver_mobile ? ` - ${consignment.driver_mobile}` : ''}`
                      : '-',
                  },
                  { label: 'E-way bill', value: consignment?.eway_bill_number ?? '-' },
                  { label: 'Dispatched', value: formatDate(dispatch.dispatch_date) },
                  { label: 'Expected', value: formatDate(dispatch.expected_delivery_date) },
                  { label: 'Delivered', value: formatDate(dispatch.actual_delivery_date) },
                  {
                    label: 'Boxes / weight',
                    value: `${formatNumber(dispatch.total_boxes)} / ${formatQuantity(dispatch.total_weight_kg)} kg`,
                  },
                  {
                    label: 'Freight',
                    value: consignment
                      ? `${formatMoney(consignment.freight_amount)}${
                          consignment.freight_paid_by
                            ? ` (${humanise(consignment.freight_paid_by)})`
                            : ''
                        }`
                      : '-',
                  },
                  { label: 'Lines', value: formatNumber(consignment?.lines.length ?? null) },
                ]}
              />
              {consignment?.delay_reason && (
                <p className="mt-3 border-t pt-3 text-sm text-destructive">
                  Delay: {consignment.delay_reason}
                </p>
              )}
            </div>
          )}

          {/* Which exact units left, so a warranty claim can be answered from
              the consignment rather than from the serial register. */}
          {serialised.length > 0 && (
            <div className="rounded-lg border p-3">
              <p className="flex items-center gap-1.5 text-sm font-medium">
                <ScanBarcode className="h-3.5 w-3.5" aria-hidden />
                Identified units
              </p>
              <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                {serialised.map((line) => (
                  <li key={line.id}>
                    Line {line.line_number}: {line.serial_ids.length} serial
                    {line.serial_ids.length === 1 ? '' : 's'} for a quantity of{' '}
                    {formatQuantity(line.quantity)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {canUpdate && dispatchId && <AddTrackingEventForm dispatchId={dispatchId} />}

          {events.isLoading && (
            <div className="space-y-2" aria-hidden>
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          )}

          {!events.isLoading && events.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load the tracking events</AlertTitle>
              <AlertDescription>{events.error.message}</AlertDescription>
            </Alert>
          )}

          {!events.isLoading && !events.error && timeline.length === 0 && (
            <EmptyState
              icon={MapPin}
              title="No tracking events yet"
              description="Nothing has been reported against this consignment since it was created."
            />
          )}

          {timeline.length > 0 && (
            <ol className="relative space-y-4 border-l pl-5">
              {timeline.map((event) => (
                <li key={event.id} className="relative">
                  <span
                    className="absolute -left-[1.4375rem] top-1.5 h-2.5 w-2.5 rounded-full bg-primary ring-4 ring-background"
                    aria-hidden
                  />
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={event.event_status} />
                    <span className="text-xs text-muted-foreground">
                      {formatDateTime(event.event_time)}
                    </span>
                    {event.source !== 'MANUAL' && (
                      <Badge variant="muted">{humanise(event.source)}</Badge>
                    )}
                  </div>
                  {event.location && <p className="mt-1 text-sm">{event.location}</p>}
                  {event.remark && (
                    <p className="mt-0.5 text-sm text-muted-foreground">{event.remark}</p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

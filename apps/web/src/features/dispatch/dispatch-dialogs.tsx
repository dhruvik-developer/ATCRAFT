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
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
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
import type {
  SalesOrderAllocation,
  SalesOrderDetail,
  SalesOrderRow,
} from '@/features/sales/sales-types'
import { useTransporterOptions } from '@/features/transporters/transporter-model'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, SELECT_NONE } from '@/lib/constants'
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
import { fulfilment, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asId } from '@/lib/utils'

import { BatchSelect, SerialPicker } from './stock-pickers'
import {
  DISPATCH_TYPES,
  FREIGHT_PAID_BY,
  MANUAL_EVENT_STATUSES,
  MONEY_INPUT,
  QUANTITY_INPUT,
  WEIGHT_INPUT,
  dispatchKeys,
  itemFactsQuery,
  useItemFacts,
  type DispatchDetail,
  type DispatchLineIn,
  type DispatchRow,
  type DispatchTrackingEvent,
  type ItemFacts,
} from './dispatch-types'

/** Which counterparty a type needs, mirroring `_counterparty_matches_type`. */
const CUSTOMER_TYPES = new Set(['SALES'])
const VENDOR_TYPES = new Set(['JOB_WORK', 'PURCHASE_RETURN'])

const MOBILE = /^$|^[0-9+\- ]{7,15}$/
const BOXES = /^$|^\d{1,6}$/

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
 * Pick the carrier from the Transporters register.
 *
 * `courier_name` beside it is free text and stays: a one-off carrier does not
 * earn a master record. But the register exists, it has an activate/deactivate
 * lifecycle, and `DispatchCreate.transporter_id` has always accepted it - the
 * screen simply never offered it, so `transporter_name` on the dispatch list
 * was null on every row and the register an operator maintained was referenced
 * by nothing.
 *
 * Soft-failing by design: `useTransporterOptions` returns an empty list when
 * this account holds `dispatch.create` without `transporter.view`, and the
 * field then offers only "Not from the register" rather than blocking a
 * dispatch over a lookup.
 */
function TransporterSelect({
  value,
  onChange,
  enabled,
  id = 'dispatch-transporter',
}: {
  value: string
  onChange: (value: string) => void
  enabled: boolean
  id?: string
}) {
  const transporters = useTransporterOptions({ enabled })
  const rows = transporters.data ?? []

  return (
    <div>
      <Label htmlFor={id}>Transporter</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} className="mt-1.5">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SELECT_NONE}>Not from the register</SelectItem>
          {rows.map((transporter) => (
            <SelectItem key={transporter.id} value={transporter.id}>
              {transporter.code} - {transporter.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p className="mt-1.5 text-xs text-muted-foreground">
        Choosing a registered carrier is what puts a name against this consignment on the dispatch
        register. Use Courier name below for a one-off.
      </p>
    </div>
  )
}

/**
 * Which order this consignment is shipping against.
 *
 * Optional - a SALES consignment naming no order is a direct sale from free
 * stock - but choosing one is what makes the release consume the order's
 * reservations and move its lines on, so it is offered whenever the customer is
 * known. Only the customer's open orders are asked for; the server narrows.
 * Once an order is chosen the lines come from it (see `OrderLines`).
 */
function SalesOrderSelect({
  customerId,
  value,
  onChange,
  disabled = false,
}: {
  customerId: string | null
  value: string
  onChange: (value: string) => void
  disabled?: boolean
}) {
  const params = {
    customer_id: customerId,
    open_only: true,
    limit: LOOKUP_LIST_LIMIT,
    with_total: false,
  }
  const orders = useQuery({
    queryKey: queryKeys.sales.list(params),
    queryFn: () => api.getPage<SalesOrderRow>('/sales', { params }),
    enabled: Boolean(customerId),
    staleTime: staleTimes.transactional,
  })

  const rows = orders.data?.data ?? []
  // A plain select has no search, so a customer with more open orders than one
  // page holds is told the list is cut rather than left to hunt for a missing one.
  const truncated = orders.data?.pagination?.has_next ?? false

  return (
    <div className="space-y-1.5">
      <Label htmlFor="dispatch-sales-order">Against sales order</Label>
      <Select value={value} onValueChange={onChange} disabled={disabled || !customerId}>
        <SelectTrigger id="dispatch-sales-order">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SELECT_NONE}>
            {customerId ? 'Not against an order' : 'Choose a customer first'}
          </SelectItem>
          {rows.map((order) => (
            <SelectItem key={order.id} value={order.id}>
              {order.code} - required {formatDate(order.required_date)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {truncated && (
        <p className="text-xs text-warning">
          Showing the first {LOOKUP_LIST_LIMIT} open orders for this customer - an older one is not
          offered here.
        </p>
      )}
      <p className="text-xs text-muted-foreground">{fulfilment.linkedDispatch}</p>
    </div>
  )
}

// ------------------------------------------------------------ order lines ----

/**
 * One line of the chosen sales order, pre-filled with what it may carry now.
 *
 * The quantity starts at `dispatchable_quantity` - the line's live reservation
 * less what other open consignments carry - and may only be lowered: the server
 * refuses anything above it, and a line the order does not have cannot be added
 * at all, because the order's lines are the only ones offered.
 */
interface OrderDraftLine {
  lineId: string
  lineNumber: number
  itemId: string
  itemCode: string
  /** The most this line may carry now. */
  dispatchable: string
  quantity: string
  batchId: string
  serialIds: string[]
}

/** The lines a consignment against this order may carry, at their full allowance. */
function orderDraftLines(allocation: SalesOrderAllocation): OrderDraftLine[] {
  return allocation.lines
    .filter((line) => toDecimal(line.dispatchable_quantity).greaterThan(0))
    .map((line) => ({
      lineId: line.line_id,
      lineNumber: line.line_number,
      itemId: line.item_id,
      itemCode: line.item_code ?? line.item_id,
      dispatchable: String(line.dispatchable_quantity),
      quantity: toDecimal(line.dispatchable_quantity).toString(),
      batchId: '',
      serialIds: [],
    }))
}

/** Why a pre-filled line cannot be sent as it stands, or null when it can. */
function orderLineProblem(line: OrderDraftLine, item: ItemFacts | undefined): string | null {
  const quantity = line.quantity.trim()
  if (!QUANTITY_INPUT.test(quantity) || !toDecimal(quantity).greaterThan(0)) {
    return fulfilment.quantityInvalid
  }
  if (toDecimal(quantity).greaterThan(toDecimal(line.dispatchable))) {
    return fulfilment.quantityTooHigh(formatQuantity(line.dispatchable))
  }
  if (item?.is_batch_tracked && !line.batchId) {
    return `${item.code} is batch tracked - name the batch being shipped.`
  }
  if (item?.is_serial_tracked && !toDecimal(quantity).equals(line.serialIds.length)) {
    return `${line.serialIds.length} serial(s) chosen for a quantity of ${formatQuantity(quantity)} - a serialised line needs exactly one per unit.`
  }
  return null
}

function toOrderLineIn(line: OrderDraftLine, item: ItemFacts | undefined): DispatchLineIn {
  return {
    item_id: line.itemId,
    sales_order_line_id: line.lineId,
    quantity: line.quantity.trim(),
    batch_id: line.batchId || null,
    serial_ids: item?.is_serial_tracked ? line.serialIds : [],
    carton_ids: [],
  }
}

function OrderLines({
  lines,
  facts,
  warehouseId,
  showProblems,
  onChange,
  onRemove,
}: {
  lines: OrderDraftLine[]
  facts: (ItemFacts | undefined)[]
  warehouseId: string
  showProblems: boolean
  onChange: (lineId: string, patch: Partial<OrderDraftLine>) => void
  onRemove: (lineId: string) => void
}) {
  return (
    <ul className="divide-y rounded-lg border">
      {lines.map((line, index) => {
        const item = facts[index]
        const problem = orderLineProblem(line, item)
        const inputId = `dispatch-order-line-${line.lineNumber}`
        return (
          <li key={line.lineId} className="space-y-3 p-3 text-sm">
            <div className="flex flex-wrap items-end gap-3">
              <Boxes className="mb-2 h-4 w-4 text-muted-foreground" aria-hidden />
              <div className="min-w-0 flex-1">
                <span className="block truncate font-medium">
                  Line {line.lineNumber} - {line.itemCode}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {formatQuantity(line.dispatchable)} may leave now
                </span>
              </div>
              <div className="w-full sm:w-36">
                <Label htmlFor={inputId}>Quantity</Label>
                <Input
                  id={inputId}
                  aria-label={`Quantity for line ${line.lineNumber}`}
                  inputMode="decimal"
                  value={line.quantity}
                  onChange={(event) => onChange(line.lineId, { quantity: event.target.value })}
                  aria-invalid={showProblems && problem !== null}
                  className="mt-1.5 text-right tabular"
                />
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Remove line ${line.lineNumber}`}
                onClick={() => onRemove(line.lineId)}
              >
                <Trash2 className="text-destructive" />
              </Button>
            </div>
            {item?.is_batch_tracked && (
              <BatchSelect
                id={`${inputId}-batch`}
                itemId={line.itemId}
                value={line.batchId}
                onChange={(batchId) => onChange(line.lineId, { batchId })}
              />
            )}
            {item?.is_serial_tracked && (
              <SerialPicker
                idPrefix={inputId}
                itemId={line.itemId}
                warehouseId={warehouseId}
                quantity={line.quantity}
                selected={line.serialIds}
                onChange={(serialIds) => onChange(line.lineId, { serialIds })}
              />
            )}
            {showProblems && problem && <FieldError message={problem} />}
          </li>
        )
      })}
    </ul>
  )
}

// ----------------------------------------------------------- create form ----

const createSchema = z
  .object({
    dispatch_type: z.enum(DISPATCH_TYPES),
    customer_id: z.string().nullable(),
    vendor_id: z.string().nullable(),
    warehouse_id: z.string().min(1, validation.choose('the warehouse this leaves from')),
    sales_order_id: z.string(),

    dispatch_date: z.string().min(1, validation.required('Dispatch date')),
    expected_delivery_date: z.string(),

    transporter_id: z.string(),
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
    sales_order_id: SELECT_NONE,
    dispatch_date: toDateInput(new Date()),
    expected_delivery_date: '',
    transporter_id: SELECT_NONE,
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
    freight_paid_by: SELECT_NONE,
    notes: '',
  }
}

interface DraftLine {
  line: DispatchLineIn
  label: string
}

/**
 * Raise a consignment.
 *
 * `order` opens the dialog from a sales order's own page: the type, customer,
 * order and warehouse are then fixed to that order, and only its lines are
 * offered. The same happens when an order is chosen in the dialog - a
 * consignment against an order ships that order's reserved lines and nothing
 * else (the server refuses anything else; this keeps the form from offering it).
 */
export function CreateDispatchDialog({
  open,
  onOpenChange,
  order,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  order?: SalesOrderDetail
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [lines, setLines] = useState<DraftLine[]>([])
  const [linesTouched, setLinesTouched] = useState(false)
  // Keyed by the order they were loaded for: a refetch of the same order never
  // overwrites what the dispatcher has lowered or removed, and lines loaded for
  // one order are never shown against another.
  const [orderLines, setOrderLines] = useState<{
    orderId: string
    lines: OrderDraftLine[]
  } | null>(null)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<CreateFormValues>({
    resolver: zodResolver(createSchema),
    defaultValues: emptyCreate(),
  })

  useEffect(() => {
    if (!open) {
      reset(emptyCreate())
      setLines([])
      setLinesTouched(false)
      setOrderLines(null)
      return
    }
    if (order) {
      reset({
        ...emptyCreate(),
        customer_id: order.customer_id,
        warehouse_id: order.warehouse_id,
        sales_order_id: order.id,
      })
    }
  }, [open, order, reset])

  const dispatchType = watch('dispatch_type')
  const warehouseId = watch('warehouse_id')
  const salesOrderId = watch('sales_order_id')
  const needsCustomer = CUSTOMER_TYPES.has(dispatchType)
  const needsVendor = VENDOR_TYPES.has(dispatchType)
  const againstOrder = needsCustomer && salesOrderId !== SELECT_NONE

  // The same read, under the same key, as the order page's allocation panel.
  const allocation = useQuery({
    queryKey: [...queryKeys.sales.detail(salesOrderId), 'allocation-status'],
    queryFn: () => api.get<SalesOrderAllocation>(`/sales/${salesOrderId}/allocation-status`),
    enabled: open && againstOrder,
    staleTime: staleTimes.live,
  })

  useEffect(() => {
    const data = allocation.data
    if (!againstOrder || !data || data.order_id !== salesOrderId) return
    if (orderLines?.orderId === salesOrderId) return
    setOrderLines({ orderId: salesOrderId, lines: orderDraftLines(data) })
    // The order's stock is reserved at its own warehouse, and it leaves from there.
    setValue('warehouse_id', data.warehouse_id, { shouldValidate: true })
  }, [againstOrder, allocation.data, orderLines, salesOrderId, setValue])

  const shownOrderLines =
    againstOrder && orderLines?.orderId === salesOrderId ? orderLines.lines : []
  const orderLineFacts = useQueries({
    queries: shownOrderLines.map((line) => itemFactsQuery(line.itemId, can(P.ITEM_VIEW))),
  }).map((query) => query.data)

  const changeOrderLine = (lineId: string, patch: Partial<OrderDraftLine>) =>
    setOrderLines((current) =>
      current === null
        ? current
        : {
            ...current,
            lines: current.lines.map((line) =>
              line.lineId === lineId ? { ...line, ...patch } : line,
            ),
          },
    )
  const removeOrderLine = (lineId: string) =>
    setOrderLines((current) =>
      current === null
        ? current
        : { ...current, lines: current.lines.filter((line) => line.lineId !== lineId) },
    )

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
          needsCustomer && values.sales_order_id !== SELECT_NONE ? values.sales_order_id : null,
        dispatch_date: values.dispatch_date,
        expected_delivery_date: orNull(values.expected_delivery_date),
        transporter_id: asId(values.transporter_id),
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
        freight_paid_by: values.freight_paid_by === SELECT_NONE ? null : values.freight_paid_by,
        notes: orNull(values.notes),
        lines: againstOrder
          ? shownOrderLines.map((line, index) => toOrderLineIn(line, orderLineFacts[index]))
          : lines.map((draft) => draft.line),
      }),
    onSuccess: (dispatch) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      // Creating a consignment moves nothing - stock leaves on the dispatch
      // transition - but allocation now shows the units spoken for, and the
      // order's dispatchable-now figures count this plan.
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      toast.success(
        success.created('Dispatch'),
        `${dispatch.code} is staged. Mark it ready, then release it to move the stock.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const submit = (values: CreateFormValues) => {
    setLinesTouched(true)
    if (againstOrder) {
      const blocked = shownOrderLines.some(
        (line, index) => orderLineProblem(line, orderLineFacts[index]) !== null,
      )
      if (shownOrderLines.length === 0 || blocked) return
    } else if (lines.length === 0) {
      return
    }
    create.mutate(values)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{order ? `Dispatch ${order.code}` : 'Create a dispatch'}</DialogTitle>
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
                    <Select
                      value={field.value}
                      onValueChange={field.onChange}
                      disabled={Boolean(order)}
                    >
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
                        onChange={(next) => {
                          field.onChange(next)
                          // Another customer's order cannot travel on this lorry.
                          setValue('sales_order_id', SELECT_NONE)
                        }}
                        invalid={Boolean(errors.customer_id)}
                        placeholder="Choose a customer"
                        disabled={Boolean(order)}
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
                      disabled={Boolean(order)}
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
                        disabled={againstOrder}
                      />
                    )}
                  />
                  <FieldError message={errors.warehouse_id?.message} />
                  {againstOrder && (
                    <p className="mt-1.5 text-xs text-muted-foreground">
                      {fulfilment.orderWarehouse}
                    </p>
                  )}
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
                <Controller
                  control={control}
                  name="transporter_id"
                  render={({ field }) => (
                    <TransporterSelect
                      value={field.value}
                      onChange={field.onChange}
                      enabled={open && can(P.TRANSPORTER_VIEW)}
                    />
                  )}
                />

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
                          <SelectItem value={SELECT_NONE}>Not stated</SelectItem>
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

            {againstOrder ? (
              <div className="space-y-2">
                <h3 className="text-sm font-medium">
                  Order lines{' '}
                  <span className="text-muted-foreground">({shownOrderLines.length})</span>
                </h3>
                {allocation.isLoading && (
                  <p className="text-sm text-muted-foreground">{fulfilment.loadingDispatchable}</p>
                )}
                {allocation.error && <FieldError message={allocation.error.message} />}
                {allocation.data && shownOrderLines.length === 0 && (
                  <Alert variant="warning">
                    <AlertDescription>{fulfilment.nothingDispatchable}</AlertDescription>
                  </Alert>
                )}
                {shownOrderLines.length > 0 && (
                  <OrderLines
                    lines={shownOrderLines}
                    facts={orderLineFacts}
                    warehouseId={warehouseId}
                    showProblems={linesTouched}
                    onChange={changeOrderLine}
                    onRemove={removeOrderLine}
                  />
                )}
              </div>
            ) : (
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
            )}

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
      toast.success(
        success.added('Tracking event'),
        'The timeline and the tracking board are updated.',
      )
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

const editDispatchSchema = z
  .object({
    dispatch_date: z.string().min(1, validation.required('Dispatch date')),
    expected_delivery_date: z.string(),
    transporter_id: z.string(),
    courier_name: z.string().trim().max(120, 'At most 120 characters'),
    transport_name: z.string().trim().max(160, 'At most 160 characters'),
    tracking_id: z.string().trim().max(120, 'At most 120 characters'),
    lr_number: z.string().trim().max(64, 'At most 64 characters'),
    vehicle_number: z.string().trim().max(32, 'At most 32 characters'),
    driver_name: z.string().trim().max(120, 'At most 120 characters'),
    driver_mobile: z.string().trim().max(20, 'At most 20 characters'),
    eway_bill_number: z.string().trim().max(64, 'At most 64 characters'),
    total_boxes: z.string().trim().regex(BOXES, 'Whole number of boxes'),
    total_weight_kg: z.string().trim().regex(WEIGHT_INPUT, 'Up to 3 decimal places'),
    freight_amount: z.string().trim().regex(MONEY_INPUT, 'Up to 2 decimal places'),
    freight_paid_by: z.string(),
    notes: z.string().trim().max(2000, 'At most 2000 characters'),
  })
  .superRefine((values, ctx) => {
    if (
      values.expected_delivery_date !== '' &&
      values.expected_delivery_date < values.dispatch_date
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['expected_delivery_date'],
        message: 'Delivery cannot be expected before the consignment leaves',
      })
    }

    if (values.total_boxes !== '' && Number(values.total_boxes) > 100_000) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['total_boxes'],
        message: 'At most 100,000 boxes',
      })
    }
  })

type EditDispatchFormValues = z.infer<typeof editDispatchSchema>

function editDispatchValues(dispatch: DispatchDetail): EditDispatchFormValues {
  return {
    dispatch_date: dispatch.dispatch_date,
    expected_delivery_date: dispatch.expected_delivery_date ?? '',
    transporter_id: dispatch.transporter_id ?? SELECT_NONE,
    courier_name: dispatch.courier_name ?? '',
    transport_name: dispatch.transport_name ?? '',
    tracking_id: dispatch.tracking_id ?? '',
    lr_number: dispatch.lr_number ?? '',
    vehicle_number: dispatch.vehicle_number ?? '',
    driver_name: dispatch.driver_name ?? '',
    driver_mobile: dispatch.driver_mobile ?? '',
    eway_bill_number: dispatch.eway_bill_number ?? '',
    total_boxes: String(dispatch.total_boxes),
    total_weight_kg: String(dispatch.total_weight_kg),
    freight_amount: String(dispatch.freight_amount),
    freight_paid_by: dispatch.freight_paid_by ?? SELECT_NONE,
    notes: dispatch.notes ?? '',
  }
}

export function EditDispatchDialog({
  dispatch,
  open,
  onOpenChange,
}: {
  dispatch: DispatchRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const dispatchId = dispatch?.id ?? null
  const detail = useQuery({
    queryKey: dispatchKeys.detail(dispatchId ?? ''),
    queryFn: () => api.get<DispatchDetail>(`/dispatch/${dispatchId}`),
    enabled: open && dispatchId !== null,
    staleTime: staleTimes.transactional,
  })
  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<EditDispatchFormValues>({
    resolver: zodResolver(editDispatchSchema),
    defaultValues: {
      dispatch_date: '',
      expected_delivery_date: '',
      transporter_id: SELECT_NONE,
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
      freight_paid_by: SELECT_NONE,
      notes: '',
    },
  })

  const update = useMutation({
    mutationFn: (values: EditDispatchFormValues) =>
      api.patch<DispatchDetail>(`/dispatch/${dispatchId}`, {
        dispatch_date: values.dispatch_date,
        expected_delivery_date: orNull(values.expected_delivery_date),
        transporter_id: values.transporter_id === SELECT_NONE ? null : values.transporter_id,
        courier_name: orNull(values.courier_name),
        transport_name: orNull(values.transport_name),
        tracking_id: orNull(values.tracking_id),
        lr_number: orNull(values.lr_number),
        vehicle_number: orNull(values.vehicle_number),
        driver_name: orNull(values.driver_name),
        driver_mobile: orNull(values.driver_mobile),
        eway_bill_number: orNull(values.eway_bill_number),
        total_boxes: values.total_boxes.trim() === '' ? 0 : Number(values.total_boxes.trim()),
        total_weight_kg: values.total_weight_kg.trim() || '0',
        freight_amount: values.freight_amount.trim() || '0',
        freight_paid_by: values.freight_paid_by === SELECT_NONE ? null : values.freight_paid_by,
        notes: orNull(values.notes),
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      toast.success(success.updated('Dispatch'), saved.code)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  useEffect(() => {
    if (open && detail.data?.id === dispatchId) reset(editDispatchValues(detail.data))
  }, [detail.data, dispatchId, open, reset])

  const onSubmit = (values: EditDispatchFormValues) => update.mutate(values)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Edit dispatch details</DialogTitle>
          <DialogDescription>
            Update the transport plan before release. The warehouse, destination and loaded lines
            stay unchanged.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)}>
          <DialogBody className="space-y-4">
            {detail.isLoading && (
              <div className="space-y-2" aria-hidden>
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            )}

            {detail.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load the dispatch details</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            )}

            {detail.data && (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="edit-dispatch-date" required>
                      Dispatch date
                    </Label>
                    <Input
                      id="edit-dispatch-date"
                      type="date"
                      {...register('dispatch_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.dispatch_date)}
                    />
                    <FieldError message={errors.dispatch_date?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-expected-delivery-date">Expected delivery</Label>
                    <Input
                      id="edit-expected-delivery-date"
                      type="date"
                      {...register('expected_delivery_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.expected_delivery_date)}
                    />
                    <FieldError message={errors.expected_delivery_date?.message} />
                  </div>

                  <div className="sm:col-span-2">
                    <Controller
                      control={control}
                      name="transporter_id"
                      render={({ field }) => (
                        <TransporterSelect
                          id="edit-dispatch-transporter"
                          value={field.value}
                          onChange={field.onChange}
                          enabled={open && can(P.TRANSPORTER_VIEW)}
                        />
                      )}
                    />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-courier">Courier name</Label>
                    <Input
                      id="edit-dispatch-courier"
                      {...register('courier_name')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.courier_name)}
                    />
                    <FieldError message={errors.courier_name?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-transport-name">Transport name</Label>
                    <Input
                      id="edit-dispatch-transport-name"
                      {...register('transport_name')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.transport_name)}
                    />
                    <FieldError message={errors.transport_name?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-tracking">Tracking id</Label>
                    <Input
                      id="edit-dispatch-tracking"
                      {...register('tracking_id')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.tracking_id)}
                    />
                    <FieldError message={errors.tracking_id?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-lr">LR number</Label>
                    <Input
                      id="edit-dispatch-lr"
                      {...register('lr_number')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.lr_number)}
                    />
                    <FieldError message={errors.lr_number?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-vehicle">Vehicle number</Label>
                    <Input
                      id="edit-dispatch-vehicle"
                      {...register('vehicle_number')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.vehicle_number)}
                    />
                    <FieldError message={errors.vehicle_number?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-driver">Driver name</Label>
                    <Input
                      id="edit-dispatch-driver"
                      {...register('driver_name')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.driver_name)}
                    />
                    <FieldError message={errors.driver_name?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-driver-mobile">Driver mobile</Label>
                    <Input
                      id="edit-dispatch-driver-mobile"
                      {...register('driver_mobile')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.driver_mobile)}
                    />
                    <FieldError message={errors.driver_mobile?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-eway">E-way bill number</Label>
                    <Input
                      id="edit-dispatch-eway"
                      {...register('eway_bill_number')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.eway_bill_number)}
                    />
                    <FieldError message={errors.eway_bill_number?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-boxes">Total boxes</Label>
                    <Input
                      id="edit-dispatch-boxes"
                      inputMode="numeric"
                      {...register('total_boxes')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.total_boxes)}
                    />
                    <FieldError message={errors.total_boxes?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-weight">Total weight (kg)</Label>
                    <Input
                      id="edit-dispatch-weight"
                      inputMode="decimal"
                      {...register('total_weight_kg')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.total_weight_kg)}
                    />
                    <FieldError message={errors.total_weight_kg?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-freight">Freight amount</Label>
                    <Input
                      id="edit-dispatch-freight"
                      inputMode="decimal"
                      {...register('freight_amount')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.freight_amount)}
                    />
                    <FieldError message={errors.freight_amount?.message} />
                  </div>

                  <div>
                    <Label htmlFor="edit-dispatch-freight-paid">Freight paid by</Label>
                    <Controller
                      control={control}
                      name="freight_paid_by"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="edit-dispatch-freight-paid" className="mt-1.5">
                            <SelectValue placeholder="Not specified" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>Not specified</SelectItem>
                            {FREIGHT_PAID_BY.map((value) => (
                              <SelectItem key={value} value={value}>
                                {humanise(value)}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                  </div>

                  <div className="sm:col-span-2">
                    <Label htmlFor="edit-dispatch-notes">Notes</Label>
                    <Textarea
                      id="edit-dispatch-notes"
                      {...register('notes')}
                      rows={3}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.notes)}
                    />
                    <FieldError message={errors.notes?.message} />
                  </div>
                </div>

                <div className="space-y-2 rounded-md border p-3">
                  <h3 className="text-sm font-medium">Loaded lines (unchanged)</h3>
                  {detail.data.lines.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No lines are recorded.</p>
                  ) : (
                    <ul className="space-y-1 text-sm">
                      {detail.data.lines.map((line) => (
                        <li key={line.id} className="flex justify-between gap-3">
                          <span className="truncate text-muted-foreground">{line.item_id}</span>
                          <span className="shrink-0 tabular">{formatQuantity(line.quantity)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            )}
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={update.isPending} disabled={!detail.data}>
              Save dispatch details
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
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
                  { label: 'Gate time', value: formatDateTime(consignment?.dispatched_at) },
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

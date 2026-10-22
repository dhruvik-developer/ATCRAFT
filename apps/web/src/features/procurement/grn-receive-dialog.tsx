/**
 * Receive material against a purchase order.
 *
 * The flow is deliberately two-step: choose the order, then count what actually
 * arrived line by line. Receiving without an order in front of you is how a
 * challan gets booked against the wrong PO and the vendor's delivery
 * performance ends up measured on somebody else's consignment.
 *
 * What the storekeeper must not have to remember:
 *
 * * A line whose item requires incoming inspection does **not** become
 *   available stock when this is posted - it goes to QC hold. That is said on
 *   the line itself, before posting, not discovered afterwards when the stock
 *   is not there to issue.
 * * Accepted plus rejected must equal received, and a rejected quantity needs a
 *   reason. Both are API rules, checked here so the failing line is named
 *   rather than the whole document refused.
 *
 * Two things the API decides and this form does not:
 *
 * * Whether a batch number is compulsory. `PurchaseOrderLineRead` does not say
 *   whether an item is batch tracked, so the field is offered on every line and
 *   the API refuses the receipt if a tracked item arrives without one.
 * * Which warehouse the stock lands in. `GoodsReceiptCreate` takes no
 *   warehouse: the receipt goes to the one on the order, and the form says so
 *   rather than offering a choice that would be silently discarded.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, PackageCheck, Search, ShieldAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { z } from 'zod'

import { EmptyState, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Popover,
  PopoverContent,
  PopoverTrigger,
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
import { LocationPicker } from '@/features/inventory/location-picker'
import { SerialNumbersField } from '@/features/inventory/serial-numbers-field'
import { splitSerialNumbers } from '@/features/inventory/serial-numbers'
import { useUomOptions } from '@/features/items/item-model'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { ApiError, api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { applyFieldErrors, remapArrayIndices } from '@/lib/form-errors'
import { formatDate, formatQuantity, toDateInput, toDecimal } from '@/lib/format'
import { errorDescription, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn, indexById } from '@/lib/utils'

import {
  isPoReceivable,
  pendingQuantity,
  type GoodsReceiptCreatePayload,
  type GoodsReceiptDetail,
  type GoodsReceiptPostResult,
  type PurchaseOrderDetail,
  type PurchaseOrderRow,
} from './procurement-types'
import { sentLineIndices } from './server-field-errors'

const QUANTITY = /^$|^\d{1,14}(\.\d{1,4})?$/

// ---------------------------------------------------------- order picker ----

/**
 * Open purchase orders, searched on the server.
 *
 * The API's `status` filter takes one value, not a set, so the list is narrowed
 * with `open_only` - every order still expecting a delivery - and the ones a
 * receipt cannot legally be posted against are shown but not selectable, with
 * the reason on the row. Dropping them in the browser instead would silently
 * shorten the page and hide matches further down it.
 */
function OpenPurchaseOrderPicker({
  value,
  onChange,
  invalid,
  vendorName,
}: {
  value: PurchaseOrderRow | null
  onChange: (order: PurchaseOrderRow) => void
  invalid: boolean
  vendorName: (vendorId: string) => string
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')

  const debouncedTerm = useDebouncedValue(term)

  const params = {
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    open_only: true,
    sort_by: 'expected_delivery_date',
    sort_dir: 'asc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.procurement.purchaseOrders(params),
    queryFn: () => api.getPage<PurchaseOrderRow>('/procurement/purchase-orders', { params }),
    enabled: open,
    staleTime: staleTimes.transactional,
  })

  const rows = results.data?.data ?? []

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id="grn-po"
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-label="Purchase order"
          aria-invalid={invalid}
          className={cn(
            'h-9 w-full justify-between font-normal',
            !value && 'text-muted-foreground',
            invalid && 'border-destructive',
          )}
        >
          <span className="truncate">
            {value ? `${value.code} - ${vendorName(value.vendor_id)}` : 'Search open orders'}
          </span>
          <ChevronsUpDown className="opacity-50" aria-hidden />
        </Button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        collisionPadding={8}
        className="w-[26rem] max-w-[var(--radix-popover-content-available-width)] p-0"
      >
        <div className="relative border-b p-2">
          <Search
            className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            autoFocus
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Search by order code or vendor reference"
            aria-label="Search open purchase orders"
            className="pl-8"
          />
        </div>

        <div
          className="max-h-72 overflow-y-auto p-1"
          role="listbox"
          aria-label="Open purchase orders"
        >
          {results.isLoading && (
            <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              Searching...
            </p>
          )}

          {!results.isLoading && results.error && (
            <p className="px-2 py-6 text-sm text-destructive">{(results.error as Error).message}</p>
          )}

          {!results.isLoading && !results.error && rows.length === 0 && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              No open purchase order matches.
            </p>
          )}

          {rows.map((order) => {
            const receivable = isPoReceivable(order.status)
            return (
              <button
                key={order.id}
                type="button"
                role="option"
                aria-selected={order.id === value?.id}
                aria-disabled={!receivable}
                disabled={!receivable}
                onClick={() => {
                  onChange(order)
                  setOpen(false)
                }}
                className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
              >
                <Check
                  className={cn('mt-0.5 h-4 w-4 shrink-0', order.id !== value?.id && 'invisible')}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{order.code}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {vendorName(order.vendor_id)} - expected{' '}
                    {formatDate(order.expected_delivery_date)}
                  </span>
                  {!receivable && (
                    <span className="block truncate text-xs text-warning">
                      Not yet sent to the vendor - nothing can be received against it
                    </span>
                  )}
                </span>
              </button>
            )
          })}
        </div>
      </PopoverContent>
    </Popover>
  )
}

// ------------------------------------------------------------------ form ----

const lineSchema = z.object({
  location_id: z.string().optional(),
  purchase_order_line_id: z.string(),
  line_number: z.number(),
  item_id: z.string(),
  description: z.string(),
  uom_id: z.string(),
  pending_quantity: z.string(),
  requires_qc: z.boolean(),
  received_quantity: z.string().trim().regex(QUANTITY, 'Up to 4 decimal places'),
  accepted_quantity: z.string().trim().regex(QUANTITY, 'Up to 4 decimal places'),
  rejected_quantity: z.string().trim().regex(QUANTITY, 'Up to 4 decimal places'),
  batch_number: z.string().trim().max(64, 'At most 64 characters'),
  manufactured_on: z.string(),
  expires_on: z.string(),
  serial_numbers: z.string(),
  rejection_reason: z.string().trim().max(2000, 'At most 2000 characters'),
})

const schema = z
  .object({
    receipt_date: z.string().min(1, validation.required('Receipt date')),
    vendor_delivery_note: z.string().trim().max(64, 'At most 64 characters'),
    vendor_invoice_number: z.string().trim().max(64, 'At most 64 characters'),
    gate_entry_number: z.string().trim().max(64, 'At most 64 characters'),
    vehicle_number: z.string().trim().max(32, 'At most 32 characters'),
    notes: z.string().trim().max(2000, 'At most 2000 characters'),
    lines: z.array(lineSchema),
  })
  // Every one of these mirrors a rule the API enforces. Catching them here
  // gives the storekeeper the failing line rather than a whole-document 422.
  .superRefine((values, ctx) => {
    let anyReceived = false

    values.lines.forEach((line, index) => {
      const received = toDecimal(line.received_quantity)
      if (received.lessThanOrEqualTo(0)) return
      anyReceived = true

      if (received.greaterThan(toDecimal(line.pending_quantity))) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'received_quantity'],
          message: `More than the ${formatQuantity(line.pending_quantity)} still pending on this line`,
        })
      }

      const accepted = toDecimal(line.accepted_quantity)
      const rejected = toDecimal(line.rejected_quantity)
      if (!accepted.plus(rejected).equals(received)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'accepted_quantity'],
          message: 'Accepted plus rejected must equal what was received',
        })
      }

      if (rejected.greaterThan(0) && line.rejection_reason === '') {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'rejection_reason'],
          message: 'Say why the quantity was rejected',
        })
      }

      // A serial identifies one physical unit, so a captured run must be
      // exactly as long as what was accepted into stock. The API enforces the
      // same count; catching it here names the line before the request.
      const serials = splitSerialNumbers(line.serial_numbers)
      if (serials.length > 0 && !accepted.equals(serials.length)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'serial_numbers'],
          message: `Capture one serial per accepted unit - ${accepted.toString()} accepted, ${serials.length} captured`,
        })
      }
      if (serials.length > 500) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'serial_numbers'],
          message: 'At most 500 serials can be captured on one line',
        })
      }
    })

    if (!anyReceived) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['lines'],
        message: 'Enter a received quantity on at least one line',
      })
    }
  })

type ReceiptFormValues = z.infer<typeof schema>

/**
 * Whether a form line goes into the payload.
 *
 * One predicate for both the filter that builds the payload and the index map
 * that lands a server error back on the right form line - the two must agree,
 * or a 422 for payload line 0 is shown on a line the storekeeper never touched.
 */
const isSentLine = (line: ReceiptFormValues['lines'][number]): boolean =>
  toDecimal(line.received_quantity).greaterThan(0)

/**
 * What one submit actually achieved.
 *
 * Creating the receipt and posting it are two requests, and the draft is
 * committed the moment the first succeeds. A post failure is therefore *not*
 * a failure of the mutation: rethrowing it would leave the dialog open with
 * the counts filled in, and the natural second click would create a second
 * draft for the same lines. It is returned as an outcome instead, so the
 * dialog closes over the draft that exists and says where to find it.
 */
type ReceiveOutcome =
  | { kind: 'posted'; posted: GoodsReceiptPostResult }
  | { kind: 'draft'; code: string }
  | { kind: 'post_failed'; code: string; error: ApiError }

function emptyHeader(): Omit<ReceiptFormValues, 'lines'> {
  return {
    receipt_date: toDateInput(new Date()),
    vendor_delivery_note: '',
    vendor_invoice_number: '',
    gate_entry_number: '',
    vehicle_number: '',
    notes: '',
  }
}

function linesFrom(order: PurchaseOrderDetail): ReceiptFormValues['lines'] {
  return (
    // `lines` is optional on `PurchaseOrderDetail`: the API declares it with a
    // default, so a reader must cope with its absence rather than assume an
    // array is there.
    (order.lines ?? [])
      // A line with nothing outstanding has nothing to receive, and offering it
      // only invites a second booking of material that already arrived. The API
      // sends ordered and received, so the outstanding figure is derived from
      // the two decimal strings rather than read off a field it does not send.
      .filter((line) => toDecimal(pendingQuantity(line)).greaterThan(0))
      .map((line) => ({
        purchase_order_line_id: line.id,
        line_number: line.line_number,
        item_id: line.item_id,
        description: line.description ?? '',
        // The id, not the resolved code: the unit list may arrive after the
        // lines do, and rebuilding the form when it lands would wipe counts
        // already typed. The label is resolved at render instead.
        uom_id: line.uom_id,
        pending_quantity: pendingQuantity(line),
        requires_qc: line.requires_qc,
        received_quantity: '',
        accepted_quantity: '',
        rejected_quantity: '',
        batch_number: '',
        manufactured_on: '',
        expires_on: '',
        serial_numbers: '',
        rejection_reason: '',
      }))
  )
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

export function GrnReceiveDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [order, setOrder] = useState<PurchaseOrderRow | null>(null)
  const [orderTouched, setOrderTouched] = useState(false)

  const canPost = can(P.GRN_POST)

  // The order and its lines carry ids; these name them.
  const vendors = useVendorOptions(open && can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const uoms = useUomOptions(open && can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])
  const warehouses = useWarehouseOptions(open && can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const detail = useQuery({
    queryKey: queryKeys.procurement.purchaseOrder(order?.id ?? ''),
    queryFn: () => api.get<PurchaseOrderDetail>(`/procurement/purchase-orders/${order?.id}`),
    enabled: open && Boolean(order),
    staleTime: staleTimes.transactional,
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<ReceiptFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { ...emptyHeader(), lines: [] },
  })

  const { fields } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (!open) return
    setOrder(null)
    setOrderTouched(false)
    reset({ ...emptyHeader(), lines: [] })
  }, [open, reset])

  // Rebuilt when a different order is chosen, and only once its lines have
  // arrived - resetting on every refetch would wipe counts already typed.
  const loadedOrderId = detail.data?.id ?? null
  useEffect(() => {
    if (!detail.data) return
    reset({ ...emptyHeader(), lines: linesFrom(detail.data) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedOrderId, reset])

  const post = useMutation({
    mutationFn: async (values: ReceiptFormValues): Promise<ReceiveOutcome> => {
      if (!order) throw new Error('No purchase order chosen')

      const received = values.lines.filter(isSentLine)

      // GoodsReceiptCreate forbids unknown keys, so every field here is one it
      // declares - and each line carries its own line_number, which the API
      // requires and validates for gaps and duplicates.
      const payload: GoodsReceiptCreatePayload = {
        purchase_order_id: order.id,
        receipt_date: values.receipt_date,
        vendor_delivery_note: orNull(values.vendor_delivery_note),
        vendor_invoice_number: orNull(values.vendor_invoice_number),
        gate_entry_number: orNull(values.gate_entry_number),
        vehicle_number: orNull(values.vehicle_number),
        notes: orNull(values.notes),
        lines: received.map((line, index) => ({
          line_number: index + 1,
          purchase_order_line_id: line.purchase_order_line_id,
          // Quantities stay strings on the wire; the API parses Decimals.
          received_quantity: line.received_quantity.trim(),
          accepted_quantity: line.accepted_quantity.trim() || '0',
          rejected_quantity: line.rejected_quantity.trim() || '0',
          batch_number: orNull(line.batch_number),
          rejection_reason: orNull(line.rejection_reason),
          ...(splitSerialNumbers(line.serial_numbers).length > 0
            ? { serial_numbers: splitSerialNumbers(line.serial_numbers) }
            : {}),
          ...(line.manufactured_on ? { manufactured_on: line.manufactured_on } : {}),
          ...(line.expires_on ? { expires_on: line.expires_on } : {}),
          ...(!line.requires_qc && line.location_id ? { location_id: line.location_id } : {}),
        })),
      }

      const receipt = await api.post<GoodsReceiptDetail>('/procurement/goods-receipts', payload)
      // Creating records the count; posting is what moves stock, advances the
      // purchase order and raises the incoming QC inspections. They are
      // separate permissions, so a receipt is left as a draft for somebody else
      // to post when this user cannot.
      if (!canPost) return { kind: 'draft', code: receipt.code }
      try {
        const posted = await api.post<GoodsReceiptPostResult>(
          `/procurement/goods-receipts/${receipt.id}/post`,
        )
        return { kind: 'posted', posted }
      } catch (error) {
        // The draft is committed; do not let a retry re-create it. An expired
        // session is the one thing worth rethrowing, so the cache can sign out.
        if (!(error instanceof ApiError) || error.isAuthError) throw error
        return { kind: 'post_failed', code: receipt.code, error }
      }
    },
    onSuccess: (outcome) => {
      // A receipt now exists in every case, so the register is refreshed
      // before anything else is said about it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      switch (outcome.kind) {
        case 'posted': {
          // Posting writes stock movements, so every balance and availability
          // read is dropped rather than left to expire (master rules 34).
          void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
          void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
          const { posted } = outcome
          const code = posted.receipt.code
          toast.success(
            success.posted('Goods receipt'),
            posted.qc_hold_line_count > 0
              ? `${code} is posted. ${posted.qc_hold_line_count} line(s) went to QC hold rather than free stock.`
              : `${code} is posted and the accepted quantity is available stock.`,
          )
          break
        }
        case 'draft':
          toast.success(
            success.saved('Goods receipt'),
            `${outcome.code} is saved as a draft. Someone with posting rights must post it before the stock exists.`,
          )
          break
        case 'post_failed':
          toast.warning(
            'Goods receipt saved but not posted',
            `${outcome.code} is a draft. ${errorDescription(outcome.error)} Post it from the receipts list.`,
          )
          break
      }
      onOpenChange(false)
    },
    // Only the create call can reject now, and its line errors are indexed by
    // the payload - which holds just the lines with a received quantity.
    onError: (error, values) =>
      applyFieldErrors(
        error,
        setError,
        remapArrayIndices('lines', sentLineIndices(values.lines, isSentLine)),
      ),
  })

  const lines = watch('lines')
  const qcLines = lines.filter(
    (line) => line.requires_qc && toDecimal(line.received_quantity).greaterThan(0),
  )
  const destination = detail.data ? warehouseById.get(detail.data.warehouse_id) : undefined

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Receive against a purchase order</DialogTitle>
          <DialogDescription>
            Count what physically arrived. Posting moves the accepted quantity into stock, sends
            anything requiring inspection to QC hold, and advances the purchase order.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            if (!order) {
              setOrderTouched(true)
              return
            }
            post.mutate(values)
          })}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <div className="sm:col-span-2 lg:col-span-1">
                <Label htmlFor="grn-po" required>
                  Purchase order
                </Label>
                <div className="mt-1.5">
                  <OpenPurchaseOrderPicker
                    value={order}
                    onChange={(next) => {
                      setOrder(next)
                      setOrderTouched(true)
                    }}
                    invalid={orderTouched && !order}
                    vendorName={(vendorId) => vendorById.get(vendorId)?.name ?? 'vendor'}
                  />
                </div>
                {orderTouched && !order && <FieldError message="Choose the order being received" />}
                {destination && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Receives into {destination.code} - {destination.name}, as stated on the order.
                  </p>
                )}
              </div>

              <div>
                <Label htmlFor="grn-receipt-date" required>
                  Receipt date
                </Label>
                <Input
                  id="grn-receipt-date"
                  type="date"
                  {...register('receipt_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.receipt_date)}
                />
                <FieldError message={errors.receipt_date?.message} />
              </div>

              <div>
                <Label htmlFor="grn-delivery-note">Vendor delivery note</Label>
                <Input
                  id="grn-delivery-note"
                  {...register('vendor_delivery_note')}
                  autoComplete="off"
                  className="mt-1.5"
                />
                <FieldError message={errors.vendor_delivery_note?.message} />
              </div>

              <div>
                <Label htmlFor="grn-invoice">Vendor invoice number</Label>
                <Input
                  id="grn-invoice"
                  {...register('vendor_invoice_number')}
                  autoComplete="off"
                  className="mt-1.5"
                />
                <FieldError message={errors.vendor_invoice_number?.message} />
              </div>

              <div>
                <Label htmlFor="grn-gate-entry">Gate entry number</Label>
                <Input
                  id="grn-gate-entry"
                  {...register('gate_entry_number')}
                  autoComplete="off"
                  className="mt-1.5"
                />
                <FieldError message={errors.gate_entry_number?.message} />
              </div>

              <div>
                <Label htmlFor="grn-vehicle">Vehicle number</Label>
                <Input
                  id="grn-vehicle"
                  {...register('vehicle_number')}
                  autoComplete="off"
                  className="mt-1.5"
                />
                <FieldError message={errors.vehicle_number?.message} />
              </div>
            </div>

            {!order && (
              <EmptyState
                icon={PackageCheck}
                title="Choose the order first"
                description="Its outstanding lines appear here to be counted."
              />
            )}

            {order && detail.isLoading && (
              <div className="space-y-2" aria-hidden>
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-20 w-full" />
              </div>
            )}

            {order && !detail.isLoading && detail.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load the order lines</AlertTitle>
                <AlertDescription>{(detail.error as Error).message}</AlertDescription>
              </Alert>
            )}

            {order && !detail.isLoading && !detail.error && fields.length === 0 && (
              <EmptyState
                title="Nothing outstanding on this order"
                description="Every line has already been received in full. Choose a different order."
              />
            )}

            {fields.length > 0 && (
              <div className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium">Lines</h3>
                  <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />
                </div>

                {fields.map((field, index) => {
                  const line = lines[index]
                  return (
                    <div key={field.id} className="rounded-lg border p-3">
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="font-medium">
                            <span className="tabular mr-2 text-muted-foreground">
                              {field.line_number}
                            </span>
                            {field.description || 'Item'}
                          </p>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge variant="muted">
                            Pending{' '}
                            {formatQuantity(
                              field.pending_quantity,
                              uomById.get(field.uom_id)?.code,
                            )}
                          </Badge>
                          {field.requires_qc && (
                            <Badge variant="warning">
                              <ShieldAlert className="h-3 w-3" aria-hidden />
                              Goes to QC hold
                            </Badge>
                          )}
                        </div>
                      </div>

                      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                        <div>
                          <Label htmlFor={`grn-received-${index}`}>Received</Label>
                          <Input
                            id={`grn-received-${index}`}
                            {...register(`lines.${index}.received_quantity`)}
                            inputMode="decimal"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.received_quantity)}
                          />
                          <FieldError message={errors.lines?.[index]?.received_quantity?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`grn-accepted-${index}`}>Accepted</Label>
                          <Input
                            id={`grn-accepted-${index}`}
                            {...register(`lines.${index}.accepted_quantity`)}
                            inputMode="decimal"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.accepted_quantity)}
                          />
                          <FieldError message={errors.lines?.[index]?.accepted_quantity?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`grn-rejected-${index}`}>Rejected</Label>
                          <Input
                            id={`grn-rejected-${index}`}
                            {...register(`lines.${index}.rejected_quantity`)}
                            inputMode="decimal"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.rejected_quantity)}
                          />
                          <FieldError message={errors.lines?.[index]?.rejected_quantity?.message} />
                        </div>

                        {/* Offered on every line: the order line does not say
                            whether the item is batch tracked, and the API is
                            what refuses a tracked receipt without a number. */}
                        <div>
                          <Label htmlFor={`grn-batch-${index}`}>Batch number</Label>
                          <Input
                            id={`grn-batch-${index}`}
                            {...register(`lines.${index}.batch_number`)}
                            autoComplete="off"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.batch_number)}
                            aria-describedby={`grn-batch-hint-${index}`}
                          />
                          <FieldError message={errors.lines?.[index]?.batch_number?.message} />
                          <p
                            id={`grn-batch-hint-${index}`}
                            className="mt-1 text-xs text-muted-foreground"
                          >
                            Required for a batch-tracked item.
                          </p>
                        </div>

                        <div>
                          <Label htmlFor={`grn-manufactured-${index}`}>Manufactured on</Label>
                          <Input
                            id={`grn-manufactured-${index}`}
                            type="date"
                            {...register(`lines.${index}.manufactured_on`)}
                            className="mt-1.5"
                          />
                          <FieldError message={errors.lines?.[index]?.manufactured_on?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`grn-expires-${index}`}>Expires on</Label>
                          <Input
                            id={`grn-expires-${index}`}
                            type="date"
                            {...register(`lines.${index}.expires_on`)}
                            className="mt-1.5"
                          />
                          <FieldError message={errors.lines?.[index]?.expires_on?.message} />
                        </div>

                        {!field.requires_qc && (
                          <Controller
                            control={control}
                            name={`lines.${index}.location_id`}
                            render={({ field: bin }) => (
                              <LocationPicker
                                id={`grn-bin-${index}`}
                                warehouseId={detail.data?.warehouse_id ?? ''}
                                itemId={line?.item_id ?? ''}
                                value={bin.value || null}
                                onChange={(value) => bin.onChange(value ?? '')}
                              />
                            )}
                          />
                        )}
                        {field.requires_qc && (
                          <p className="text-xs text-muted-foreground">
                            Receive at warehouse level for inspection. Put accepted stock into its
                            bin with a bin move.
                          </p>
                        )}
                        {toDecimal(line?.accepted_quantity ?? '').greaterThan(0) && (
                          <div className="sm:col-span-2 lg:col-span-4">
                            <Controller
                              control={control}
                              name={`lines.${index}.serial_numbers`}
                              render={({ field: serials }) => (
                                <SerialNumbersField
                                  id={`grn-serials-${index}`}
                                  label="Serial numbers"
                                  value={serials.value}
                                  onChange={serials.onChange}
                                  expected={line?.accepted_quantity}
                                  hint="Required for a serial-tracked item: one serial per accepted unit. Type, scan or generate the labels."
                                  error={errors.lines?.[index]?.serial_numbers?.message}
                                />
                              )}
                            />
                          </div>
                        )}
                        {toDecimal(line?.rejected_quantity ?? '').greaterThan(0) && (
                          <div className="sm:col-span-2 lg:col-span-4">
                            <Label htmlFor={`grn-rejection-${index}`} required>
                              Rejection reason
                            </Label>
                            <Input
                              id={`grn-rejection-${index}`}
                              {...register(`lines.${index}.rejection_reason`)}
                              autoComplete="off"
                              className="mt-1.5"
                              aria-invalid={Boolean(errors.lines?.[index]?.rejection_reason)}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.rejection_reason?.message}
                            />
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {qcLines.length > 0 && (
              <Alert variant="warning">
                <ShieldAlert aria-hidden />
                <AlertTitle>
                  {qcLines.length} line{qcLines.length === 1 ? '' : 's'} will not become available
                  stock
                </AlertTitle>
                <AlertDescription>
                  {qcLines
                    .map((line) => line.description)
                    .filter(Boolean)
                    .join(', ') || 'They'}{' '}
                  require incoming inspection. Posting puts the accepted quantity in QC hold; it can
                  only be issued once quality has decided on it.
                </AlertDescription>
              </Alert>
            )}

            {!canPost && fields.length > 0 && (
              <Alert variant="info">
                <AlertTitle>This will be saved as a draft</AlertTitle>
                <AlertDescription>
                  Your account can record a receipt but not post one. No stock moves until somebody
                  with posting rights posts it.
                </AlertDescription>
              </Alert>
            )}

            <div>
              <Label htmlFor="grn-notes">Notes</Label>
              <Textarea id="grn-notes" {...register('notes')} className="mt-1.5" />
              <FieldError message={errors.notes?.message} />
            </div>
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={post.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={post.isPending} disabled={fields.length === 0}>
              {canPost ? 'Post receipt' : 'Save draft receipt'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Record a vendor invoice, or amend one nobody has approved.
 *
 * This is the door the payables queue never had: `vendor-invoices-page.tsx`
 * could read, match and approve a bill that there was no way to enter. Entry is
 * the same form as amendment, because `VendorInvoiceUpdate` replaces the whole
 * line set - a bill is a document, and patching one line of something somebody
 * is about to approve invites an approval of other than what was reviewed.
 *
 * **Start from the documents the system already holds.** A clerk with a paper
 * invoice in front of them should not retype twenty lines the purchase order
 * and the goods receipt already carry. Naming a receipt fills in its order too
 * (the API refuses a receipt booked against a different one), and "Copy lines"
 * seeds the line set from whichever of the two is more specific:
 *
 * * from a **goods receipt**, the quantity is what was *accepted* at the gate -
 *   which is precisely the basis the three-way match compares against, so a
 *   clean delivery matches at zero variance;
 * * from a **purchase order** alone, the quantity is what was ordered, because
 *   there is no receipt to say what arrived.
 *
 * Either way the unit price is the order's *net* price - `orderedNetUnitPrice`,
 * the discount already applied - since that is the figure `_ordered_net_price`
 * on the server matches against. Seeding the gross price and dropping the
 * agreed discount would report a price variance on a perfectly correct
 * delivery.
 *
 * Copying is a button rather than a side effect of choosing a document: it
 * replaces every line, and a clerk who has keyed six of them by hand must not
 * lose them to a change of mind about the order number.
 *
 * **Permission.** Create, amend, delete and submit are all
 * `vendor_invoice.create` - the API declares that one right on all four routes.
 * It is emphatically *not* `vendor_invoice.approve`, which is the separate
 * right the match dialog gates approval on: whoever keys a bill must not be the
 * person who clears it. The reference lookups are their own rights again
 * (`vendor.view`, `purchase_order.view`, `grn.view`, `item.view`, `uom.view`),
 * and each one is checked before its picker is offered, because a picker that
 * 403s on every search looks like an empty master.
 *
 * **Money.** Every figure on this screen is a decimal string computed through
 * decimal.js. The running totals mirror `service.price_invoice_line` and
 * `_invoice_totals` step for step - see `priceInvoiceLine` in `finance-types.ts`
 * for why the mirror is justified and where it rounds. The server's answer is
 * still authoritative: the saved invoice is re-read rather than assumed.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  ChevronsUpDown,
  ClipboardCopy,
  Link2Off,
  Loader2,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { z } from 'zod'

import { FieldError, StatusBadge } from '@/components/shared'
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
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { useUomOptions } from '@/features/items/item-model'
import type {
  GoodsReceiptDetail,
  GoodsReceiptRow,
  PurchaseOrderDetail,
  PurchaseOrderLine,
  PurchaseOrderRow,
} from '@/features/procurement/procurement-types'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, formatMoney, formatQuantity, toDateInput, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn, indexById } from '@/lib/utils'

import {
  MAX_HSN_CODE_LENGTH,
  MAX_INVOICE_LINES,
  MAX_INVOICE_NOTES_LENGTH,
  MAX_LINE_DESCRIPTION_LENGTH,
  MAX_TAX_RATE,
  MAX_VENDOR_INVOICE_NUMBER_LENGTH,
  financeKeys,
  goodsReceiptDetailKey,
  invoiceTotals,
  orderedNetUnitPrice,
  priceInvoiceLine,
  purchaseOrderDetailKey,
  wasInterstate,
  type VendorInvoiceCreatePayload,
  type VendorInvoiceDetail,
  type VendorInvoiceLinePayload,
  type VendorInvoiceUpdatePayload,
} from './finance-types'

/**
 * The NUMERIC scales the contract states, as input patterns.
 *
 * Each one is the JSON-schema pattern for its field read back in a form a
 * person can be told about: quantity is NUMERIC(18,4), a unit price is stored
 * at six places, a tax rate at four. A blank is allowed where the field has a
 * server-side default and is sent as "0" instead.
 */
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const UNIT_PRICE = /^\d{1,12}(\.\d{1,6})?$/
const MONEY_OR_BLANK = /^$|^\d{1,14}(\.\d{1,4})?$/
const SIGNED_MONEY_OR_BLANK = /^$|^-?\d{1,14}(\.\d{1,4})?$/
const RATE_OR_BLANK = /^$|^\d{1,5}(\.\d{1,4})?$/
const CURRENCY_OR_BLANK = /^$|^[A-Za-z]{3}$/

/** How many receipts or orders a search offers before asking for a narrower term. */
const PICKER_LIMIT = 20

/** Radix Select forbids an empty option value, so "no unit" needs a sentinel. */
const NO_UOM = '__none'

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/** A field the API defaults to zero: blank means zero, not "unset". */
const zeroIfBlank = (value: string): string => (value.trim() === '' ? '0' : value.trim())

/** `formatMoney` hands the code to `Intl`, which throws on a half-typed one. */
const usableCurrency = (code: string | undefined): string | undefined =>
  code && /^[A-Z]{3}$/.test(code) ? code : undefined

// ------------------------------------------------------------- the form -----

const lineSchema = z.object({
  item_id: z.string(),
  description: z
    .string()
    .trim()
    .max(MAX_LINE_DESCRIPTION_LENGTH, `At most ${MAX_LINE_DESCRIPTION_LENGTH} characters`),
  hsn_code: z.string().trim().max(MAX_HSN_CODE_LENGTH, `At most ${MAX_HSN_CODE_LENGTH} characters`),
  quantity: z
    .string()
    .trim()
    .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
    .refine((v) => toDecimal(v).greaterThan(0), 'Quantity must be greater than zero'),
  uom_id: z.string(),
  unit_price: z.string().trim().regex(UNIT_PRICE, 'Enter a price with up to 6 decimal places'),
  discount_amount: z.string().trim().regex(MONEY_OR_BLANK, 'Up to 4 decimal places'),
  tax_rate: z
    .string()
    .trim()
    .regex(RATE_OR_BLANK, 'Up to 4 decimal places')
    .refine((v) => toDecimal(v).lessThanOrEqualTo(MAX_TAX_RATE), `At most ${MAX_TAX_RATE}%`),
  is_interstate: z.boolean(),
  // Carried, never typed: both are set by seeding from a document and are what
  // ties the line to the order line the match reads its expected price from.
  purchase_order_line_id: z.string(),
  goods_receipt_line_id: z.string(),
  notes: z
    .string()
    .trim()
    .max(MAX_INVOICE_NOTES_LENGTH, `At most ${MAX_INVOICE_NOTES_LENGTH} characters`),
})

const schema = z
  .object({
    vendor_id: z.string().min(1, 'Choose the vendor who sent the bill'),
    vendor_invoice_number: z
      .string()
      .trim()
      .min(1, "The vendor's own invoice number is required")
      .max(
        MAX_VENDOR_INVOICE_NUMBER_LENGTH,
        `At most ${MAX_VENDOR_INVOICE_NUMBER_LENGTH} characters`,
      ),
    invoice_date: z.string().min(1, 'Invoice date is required'),
    due_date: z.string(),
    // Case is normalised on the way out, so a clerk typing "inr" is not scolded
    // for it; the API's own pattern is `^[A-Z]{3}$`.
    currency: z.string().trim().regex(CURRENCY_OR_BLANK, 'Three letters, such as INR'),
    purchase_order_id: z.string(),
    goods_receipt_id: z.string(),
    round_off_amount: z.string().trim().regex(SIGNED_MONEY_OR_BLANK, 'Up to 4 decimal places'),
    notes: z
      .string()
      .trim()
      .max(MAX_INVOICE_NOTES_LENGTH, `At most ${MAX_INVOICE_NOTES_LENGTH} characters`),
    lines: z
      .array(lineSchema)
      .min(1, 'An invoice needs at least one line')
      .max(MAX_INVOICE_LINES, `At most ${MAX_INVOICE_LINES} lines on one invoice`),
  })
  .superRefine((values, ctx) => {
    // `update_invoice` refuses this outright, and it is a data-entry slip in
    // either direction: a bill that fell due before it was raised would sit at
    // the top of the overdue queue from the moment it was keyed.
    if (values.due_date && values.due_date < values.invoice_date) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['due_date'],
        message: 'The due date cannot fall before the invoice date',
      })
    }

    // Mirrors the ValidationError `price_invoice_line` raises. Caught here so
    // the failing line is named, rather than the whole document refused.
    values.lines.forEach((line, index) => {
      const gross = toDecimal(line.quantity).times(toDecimal(line.unit_price))
      if (toDecimal(line.discount_amount).greaterThan(gross)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'discount_amount'],
          message: "A discount cannot exceed the line's own value",
        })
      }
    })
  })

type InvoiceFormValues = z.infer<typeof schema>
type LineValues = InvoiceFormValues['lines'][number]

function emptyLine(previous?: LineValues): LineValues {
  return {
    item_id: '',
    description: '',
    hsn_code: '',
    quantity: '',
    uom_id: '',
    unit_price: '',
    discount_amount: '',
    // Copied from the line above rather than reset: a bill is taxed at one
    // place of supply throughout, and its lines usually share a slab, so the
    // clerk states both once instead of once per line.
    tax_rate: previous?.tax_rate ?? '',
    is_interstate: previous?.is_interstate ?? false,
    purchase_order_line_id: '',
    goods_receipt_line_id: '',
    notes: '',
  }
}

/**
 * A blank bill.
 *
 * Deliberately no starter line. The first thing to do is name the order or the
 * receipt and copy its lines; an empty row sitting there invites retyping the
 * document the system already holds.
 */
function emptyValues(): InvoiceFormValues {
  return {
    vendor_id: '',
    vendor_invoice_number: '',
    invoice_date: toDateInput(new Date()),
    due_date: '',
    currency: '',
    purchase_order_id: '',
    goods_receipt_id: '',
    round_off_amount: '',
    notes: '',
    lines: [],
  }
}

/** An existing bill, read back into the same form. */
function valuesFrom(invoice: VendorInvoiceDetail): InvoiceFormValues {
  return {
    vendor_id: invoice.vendor_id,
    vendor_invoice_number: invoice.vendor_invoice_number,
    invoice_date: toDateInput(invoice.invoice_date),
    due_date: toDateInput(invoice.due_date),
    currency: invoice.currency,
    purchase_order_id: invoice.purchase_order_id ?? '',
    goods_receipt_id: invoice.goods_receipt_id ?? '',
    round_off_amount: toDecimal(invoice.round_off_amount).toFixed(),
    notes: invoice.notes ?? '',
    // Optional on the wire even though the detail always sends it.
    lines: (invoice.lines ?? []).map((line) => ({
      item_id: line.item_id ?? '',
      description: line.description ?? '',
      hsn_code: line.hsn_code ?? '',
      quantity: toDecimal(line.quantity).toFixed(),
      uom_id: line.uom_id ?? '',
      unit_price: toDecimal(line.unit_price).toFixed(),
      discount_amount: toDecimal(line.discount_amount).toFixed(),
      tax_rate: toDecimal(line.tax_rate).toFixed(),
      // Input-only on the wire, so it is recovered from the split it produced.
      is_interstate: wasInterstate(line),
      purchase_order_line_id: line.purchase_order_line_id ?? '',
      goods_receipt_line_id: line.goods_receipt_line_id ?? '',
      notes: line.notes ?? '',
    })),
  }
}

/** The seeded line for one purchase order line, priced at its net rate. */
function lineFromOrderLine(line: PurchaseOrderLine, quantity: string): LineValues {
  return {
    item_id: line.item_id,
    description: line.description ?? '',
    hsn_code: '',
    quantity,
    uom_id: line.uom_id,
    unit_price: orderedNetUnitPrice(line) ?? toDecimal(line.unit_price).toFixed(),
    // Zero, because the net unit price above already carries the order's
    // discount. Recording it twice would halve the line.
    discount_amount: '0',
    tax_rate: toDecimal(line.tax_rate).toFixed(),
    is_interstate: false,
    purchase_order_line_id: line.id,
    goods_receipt_line_id: '',
    notes: '',
  }
}

/** Every line of an order, at the quantity ordered. */
function linesFromOrder(order: PurchaseOrderDetail): LineValues[] {
  return (order.lines ?? []).map((line) =>
    lineFromOrderLine(line, toDecimal(line.quantity).toFixed()),
  )
}

/**
 * Every accepted line of a receipt, at the quantity accepted.
 *
 * Accepted, not received: material refused at the gate never entered the
 * company's custody and must not be paid for (docs/assumptions.md section 18),
 * and it is what the match compares the billed quantity against. A line with
 * nothing accepted is not offered at all rather than seeded at zero, which the
 * API would refuse.
 *
 * `orderLines` supplies the agreed price. When the order cannot be read - the
 * clerk holds `vendor_invoice.create` but not `purchase_order.view` - the
 * receipt's own `unit_cost` stands in; that is what inventory was valued at,
 * which is the closest thing available and is visible for correction.
 */
function linesFromReceipt(
  receipt: GoodsReceiptDetail,
  orderLines: Map<string, PurchaseOrderLine>,
): LineValues[] {
  return (receipt.lines ?? [])
    .filter((line) => toDecimal(line.accepted_quantity).greaterThan(0))
    .map((line) => {
      const orderLine = orderLines.get(line.purchase_order_line_id)
      const quantity = toDecimal(line.accepted_quantity).toFixed()
      const seeded = orderLine
        ? lineFromOrderLine(orderLine, quantity)
        : {
            ...emptyLine(),
            item_id: line.item_id,
            description: line.description ?? '',
            quantity,
            uom_id: line.uom_id,
            unit_price: toDecimal(line.unit_cost).toFixed(),
            discount_amount: '0',
            tax_rate: '0',
            purchase_order_line_id: line.purchase_order_line_id,
          }
      return { ...seeded, goods_receipt_line_id: line.id }
    })
}

// ---------------------------------------------------------- the pickers -----

/**
 * Purchase orders for one vendor, searched on the server.
 *
 * Narrowed by `vendor_id` rather than filtered in the browser: the API refuses
 * an invoice whose order belongs to another vendor, so offering one would only
 * produce a 409 at save time. No status narrowing beyond that - a bill usually
 * arrives once the order is fully received and closed, which is exactly when an
 * "open orders" filter would hide it - so each row states its own status.
 */
function PurchaseOrderPicker({
  vendorId,
  value,
  selectedLabel,
  onChange,
}: {
  vendorId: string
  value: string
  selectedLabel: string | null
  onChange: (order: PurchaseOrderRow | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [debouncedTerm, setDebouncedTerm] = useState('')

  // One request per keystroke would put a scan of the order book behind every
  // character typed.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedTerm(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const params = {
    limit: PICKER_LIMIT,
    with_total: false,
    vendor_id: vendorId,
    sort_by: 'po_date',
    sort_dir: 'desc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.procurement.purchaseOrders(params),
    queryFn: () => api.getPage<PurchaseOrderRow>('/procurement/purchase-orders', { params }),
    enabled: open && Boolean(vendorId),
    staleTime: staleTimes.transactional,
  })

  const rows = results.data?.data ?? []

  return (
    <div className="flex items-center gap-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id="invoice-purchase-order"
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-label="Purchase order"
            disabled={!vendorId}
            className={cn(
              'h-9 w-full justify-between font-normal',
              !value && 'text-muted-foreground',
            )}
          >
            <span className="truncate">
              {value ? (selectedLabel ?? 'Purchase order') : 'No purchase order'}
            </span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>

        <PopoverContent align="start" className="w-[26rem] p-0">
          <div className="relative border-b p-2">
            <Search
              className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              autoFocus
              value={term}
              onChange={(event) => setTerm(event.target.value)}
              placeholder="Search by order code"
              aria-label="Search this vendor's purchase orders"
              className="pl-8"
            />
          </div>

          <div className="max-h-72 overflow-y-auto p-1" role="listbox" aria-label="Purchase orders">
            {results.isLoading && (
              <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Searching...
              </p>
            )}

            {!results.isLoading && results.error && (
              <p className="px-2 py-6 text-sm text-destructive">{results.error.message}</p>
            )}

            {!results.isLoading && !results.error && rows.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                No purchase order for this vendor matches.
              </p>
            )}

            {rows.map((order) => (
              <button
                key={order.id}
                type="button"
                role="option"
                aria-selected={order.id === value}
                onClick={() => {
                  onChange(order)
                  setOpen(false)
                }}
                className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              >
                <Check
                  className={cn('mt-0.5 h-4 w-4 shrink-0', order.id !== value && 'invisible')}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2">
                    <span className="truncate font-medium">{order.code}</span>
                    <StatusBadge status={order.status} />
                  </span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {formatDate(order.po_date)} -{' '}
                    {formatMoney(order.total_amount, { currency: usableCurrency(order.currency) })}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </PopoverContent>
      </Popover>

      {value && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Clear the purchase order"
          onClick={() => onChange(null)}
        >
          <X />
        </Button>
      )}
    </div>
  )
}

/**
 * Posted goods receipts for one vendor, optionally for one order.
 *
 * Only POSTED receipts are offered. `_assert_invoice_references` is looser -
 * it checks the vendor and the order, not the receipt's status - but a receipt
 * that has not been posted is material the company does not yet own, and a
 * cancelled one never arrived; billing against either would give the three-way
 * match accepted quantities that no stock movement stands behind.
 */
function GoodsReceiptPicker({
  vendorId,
  purchaseOrderId,
  value,
  selectedLabel,
  onChange,
}: {
  vendorId: string
  purchaseOrderId: string
  value: string
  selectedLabel: string | null
  onChange: (receipt: GoodsReceiptRow | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [debouncedTerm, setDebouncedTerm] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedTerm(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const params = {
    limit: PICKER_LIMIT,
    with_total: false,
    status: 'POSTED',
    vendor_id: vendorId,
    // Narrowed to the chosen order when there is one, because the API refuses a
    // receipt booked against a different order than the invoice names.
    ...(purchaseOrderId ? { purchase_order_id: purchaseOrderId } : {}),
    sort_by: 'receipt_date',
    sort_dir: 'desc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.procurement.grns(params),
    queryFn: () => api.getPage<GoodsReceiptRow>('/procurement/goods-receipts', { params }),
    enabled: open && Boolean(vendorId),
    staleTime: staleTimes.transactional,
  })

  const rows = results.data?.data ?? []

  return (
    <div className="flex items-center gap-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id="invoice-goods-receipt"
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-label="Goods receipt"
            disabled={!vendorId}
            className={cn(
              'h-9 w-full justify-between font-normal',
              !value && 'text-muted-foreground',
            )}
          >
            <span className="truncate">
              {value ? (selectedLabel ?? 'Goods receipt') : 'No goods receipt'}
            </span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>

        <PopoverContent align="start" className="w-[26rem] p-0">
          <div className="relative border-b p-2">
            <Search
              className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              autoFocus
              value={term}
              onChange={(event) => setTerm(event.target.value)}
              placeholder="Search by receipt code or delivery note"
              aria-label="Search posted goods receipts for this vendor"
              className="pl-8"
            />
          </div>

          <div
            className="max-h-72 overflow-y-auto p-1"
            role="listbox"
            aria-label="Posted goods receipts"
          >
            {results.isLoading && (
              <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Searching...
              </p>
            )}

            {!results.isLoading && results.error && (
              <p className="px-2 py-6 text-sm text-destructive">{results.error.message}</p>
            )}

            {!results.isLoading && !results.error && rows.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                No posted receipt matches. Only material that was actually taken into stock can be
                billed against a receipt.
              </p>
            )}

            {rows.map((receipt) => (
              <button
                key={receipt.id}
                type="button"
                role="option"
                aria-selected={receipt.id === value}
                onClick={() => {
                  onChange(receipt)
                  setOpen(false)
                }}
                className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              >
                <Check
                  className={cn('mt-0.5 h-4 w-4 shrink-0', receipt.id !== value && 'invisible')}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{receipt.code}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    Received {formatDate(receipt.receipt_date)},{' '}
                    {formatQuantity(receipt.total_accepted_quantity)} accepted
                  </span>
                </span>
              </button>
            ))}
          </div>
        </PopoverContent>
      </Popover>

      {value && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Clear the goods receipt"
          onClick={() => onChange(null)}
        >
          <X />
        </Button>
      )}
    </div>
  )
}

// ----------------------------------------------------------- the dialog -----

/**
 * The document the lines on screen are linked to.
 *
 * Set by copying, and also by opening an amendment - an existing bill arrives
 * already linked, and without this the stale-link warning below would say
 * nothing when somebody moved that bill to a different order.
 */
interface Seeded {
  kind: 'order' | 'receipt'
  id: string
}

function linkedDocumentOf(invoice: VendorInvoiceDetail | null): Seeded | null {
  if (!invoice) return null
  const lines = invoice.lines ?? []
  if (invoice.goods_receipt_id && lines.some((line) => line.goods_receipt_line_id)) {
    return { kind: 'receipt', id: invoice.goods_receipt_id }
  }
  if (invoice.purchase_order_id && lines.some((line) => line.purchase_order_line_id)) {
    return { kind: 'order', id: invoice.purchase_order_id }
  }
  return null
}

export function VendorInvoiceFormDialog({
  open,
  invoice,
  onOpenChange,
  onSaved,
}: {
  open: boolean
  /** Null records a new bill; a detail amends that one. */
  invoice: VendorInvoiceDetail | null
  onOpenChange: (open: boolean) => void
  onSaved?: (invoice: VendorInvoiceDetail) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [seeded, setSeeded] = useState<Seeded | null>(null)

  const amending = invoice !== null

  /**
   * The right the POST and the PATCH both enforce.
   *
   * The page only opens this dialog behind the same check, but the component
   * that makes the call is the honest place for the gate - a second entry point
   * added later would otherwise inherit nothing.
   */
  const canRecord = can(P.VENDOR_INVOICE_CREATE)

  // Each reference lookup is its own right, and none of them is the right that
  // opened this dialog. A picker fired without its own view permission 403s on
  // every search and reads as an empty master.
  const canReadVendors = can(P.VENDOR_VIEW)
  const canReadOrders = can(P.PURCHASE_ORDER_VIEW)
  const canReadReceipts = can(P.GRN_VIEW)
  const canReadItems = can(P.ITEM_VIEW)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<InvoiceFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  const { fields, append, remove, replace } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (!open) return
    reset(invoice ? valuesFrom(invoice) : emptyValues())
    setSeeded(linkedDocumentOf(invoice))
  }, [open, invoice, reset])

  const vendorId = watch('vendor_id')
  const purchaseOrderId = watch('purchase_order_id')
  const goodsReceiptId = watch('goods_receipt_id')
  const currency = watch('currency')
  const invoiceDate = watch('invoice_date')
  const roundOff = watch('round_off_amount')
  const lines = watch('lines')

  const uoms = useUomOptions(open && can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  /**
   * The order and the receipt named on the header.
   *
   * Read here rather than inside the pickers so the chosen document has a code
   * on screen without a second request, and so its lines are in hand the moment
   * "Copy lines" is pressed.
   */
  const orderQuery = useQuery({
    queryKey: purchaseOrderDetailKey(purchaseOrderId),
    queryFn: () => api.get<PurchaseOrderDetail>(`/procurement/purchase-orders/${purchaseOrderId}`),
    enabled: open && canReadOrders && Boolean(purchaseOrderId),
    staleTime: staleTimes.transactional,
  })

  const receiptQuery = useQuery({
    queryKey: goodsReceiptDetailKey(goodsReceiptId),
    queryFn: () => api.get<GoodsReceiptDetail>(`/procurement/goods-receipts/${goodsReceiptId}`),
    enabled: open && canReadReceipts && Boolean(goodsReceiptId),
    staleTime: staleTimes.transactional,
  })

  const orderLinesById = useMemo(() => indexById(orderQuery.data?.lines), [orderQuery.data?.lines])

  /**
   * What "Copy lines" would copy, and how many lines that is.
   *
   * The receipt wins when both are named: it is the more specific document and
   * it knows what was actually accepted, while the order only knows what was
   * asked for.
   */
  const seedSource = useMemo(() => {
    if (receiptQuery.data) {
      const seededLines = linesFromReceipt(receiptQuery.data, orderLinesById)
      return {
        kind: 'receipt' as const,
        id: receiptQuery.data.id,
        code: receiptQuery.data.code,
        lines: seededLines,
      }
    }
    if (orderQuery.data) {
      return {
        kind: 'order' as const,
        id: orderQuery.data.id,
        code: orderQuery.data.code,
        lines: linesFromOrder(orderQuery.data),
      }
    }
    return null
  }, [receiptQuery.data, orderQuery.data, orderLinesById])

  /**
   * Lines still pointing at a document the header no longer names.
   *
   * `_resolve_invoice_lines` refuses a line whose order line belongs to another
   * order, so this would be a 409 at save time. Said here instead, with the one
   * button that fixes it.
   */
  const linksAreStale =
    seeded !== null &&
    ((seeded.kind === 'order' && seeded.id !== purchaseOrderId) ||
      (seeded.kind === 'receipt' && seeded.id !== goodsReceiptId))

  const pricing = useMemo(() => lines.map((line) => priceInvoiceLine(line)), [lines])
  const totals = useMemo(() => invoiceTotals(pricing, zeroIfBlank(roundOff)), [pricing, roundOff])

  const displayCurrency = usableCurrency(invoice?.currency ?? currency)
  const money = (value: string) => formatMoney(value, { currency: displayCurrency })

  const save = useMutation({
    mutationFn: (values: InvoiceFormValues) => {
      const payload = buildLines(values)

      if (invoice) {
        const update: VendorInvoiceUpdatePayload = {
          vendor_invoice_number: values.vendor_invoice_number.trim(),
          invoice_date: values.invoice_date,
          // Omitted when blank: `update_invoice` ignores a null date, so
          // sending one would look like a change and be silently dropped.
          ...(values.due_date ? { due_date: values.due_date } : {}),
          purchase_order_id: orNull(values.purchase_order_id),
          goods_receipt_id: orNull(values.goods_receipt_id),
          round_off_amount: zeroIfBlank(values.round_off_amount),
          // An empty string, not null. The service writes whatever is not null,
          // so this is the one spelling that can actually clear a note.
          notes: values.notes.trim(),
          lines: payload,
        }
        return api.patch<VendorInvoiceDetail>(`/finance/vendor-invoices/${invoice.id}`, update)
      }

      const create: VendorInvoiceCreatePayload = {
        vendor_id: values.vendor_id,
        vendor_invoice_number: values.vendor_invoice_number.trim(),
        invoice_date: values.invoice_date,
        // Both are omitted rather than blanked, because absent means something
        // here: the API falls back to the vendor's agreed credit days and to
        // the company's own denomination.
        ...(values.due_date ? { due_date: values.due_date } : {}),
        ...(values.currency.trim() ? { currency: values.currency.trim().toUpperCase() } : {}),
        purchase_order_id: orNull(values.purchase_order_id),
        goods_receipt_id: orNull(values.goods_receipt_id),
        round_off_amount: zeroIfBlank(values.round_off_amount),
        notes: orNull(values.notes),
        lines: payload,
      }
      return api.post<VendorInvoiceDetail>('/finance/vendor-invoices', create)
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      toast.success(
        amending ? 'Invoice amended' : 'Vendor invoice recorded',
        amending
          ? `${saved.code} has to be matched again before it can be approved.`
          : `${saved.code} is a draft. Submit it for approval when the lines are right.`,
      )
      onOpenChange(false)
      onSaved?.(saved)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  /** Replace the whole line set from the chosen document. */
  const copyLines = () => {
    if (!seedSource) return
    replace(seedSource.lines)
    setSeeded({ kind: seedSource.kind, id: seedSource.id })
  }

  /** Drop the document links, leaving the figures a clerk has already keyed. */
  const clearLinks = () => {
    replace(
      lines.map((line) => ({ ...line, purchase_order_line_id: '', goods_receipt_line_id: '' })),
    )
    setSeeded(null)
  }

  const canSave = canRecord && (amending || canReadVendors)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{invoice ? `Amend ${invoice.code}` : 'Record a vendor invoice'}</DialogTitle>
          <DialogDescription>
            {invoice
              ? `${invoice.vendor_invoice_number} - the vendor and the currency were fixed when the bill was recorded. Any amendment clears the three-way match, so it has to be matched again before approval.`
              : 'The bill is recorded as a draft and nothing reaches the ledger until it is approved. Name the purchase order or the goods receipt and copy its lines rather than retyping them.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            {!amending && !canReadVendors && (
              // The vendor is required and there is nothing to choose from, so
              // the form would fail on a field that was never shown. Say so.
              <Alert variant="warning">
                <AlertTitle>You cannot record a bill from here</AlertTitle>
                <AlertDescription>
                  An invoice has to name the vendor who sent it, and your account cannot read the
                  vendor list. Ask somebody who can to record it.
                </AlertDescription>
              </Alert>
            )}

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {!amending && canReadVendors && (
                <div className="sm:col-span-2 lg:col-span-1">
                  <Controller
                    control={control}
                    name="vendor_id"
                    render={({ field }) => (
                      <VendorPicker
                        id="invoice-vendor"
                        label="Vendor"
                        value={field.value || null}
                        onChange={(next) => {
                          field.onChange(next ?? '')
                          // The order and the receipt both belong to a vendor;
                          // keeping them across a change of supplier would post
                          // a bill the API refuses as another vendor's order.
                          setValue('purchase_order_id', '')
                          setValue('goods_receipt_id', '')
                        }}
                        placeholder="Search vendors"
                        invalid={Boolean(errors.vendor_id)}
                      />
                    )}
                  />
                  <FieldError message={errors.vendor_id?.message} />
                </div>
              )}

              <div>
                <Label htmlFor="invoice-number" required>
                  Vendor&rsquo;s invoice number
                </Label>
                <Input
                  id="invoice-number"
                  {...register('vendor_invoice_number')}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.vendor_invoice_number)}
                />
                <FieldError message={errors.vendor_invoice_number?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  As printed on the bill. The same vendor cannot bill it twice.
                </p>
              </div>

              <div>
                <Label htmlFor="invoice-date" required>
                  Invoice date
                </Label>
                <Input
                  id="invoice-date"
                  type="date"
                  {...register('invoice_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.invoice_date)}
                />
                <FieldError message={errors.invoice_date?.message} />
              </div>

              <div>
                <Label htmlFor="invoice-due">Due date</Label>
                <Input
                  id="invoice-due"
                  type="date"
                  {...register('due_date')}
                  min={invoiceDate || undefined}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.due_date)}
                />
                <FieldError message={errors.due_date?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  {amending
                    ? 'Left blank, the recorded due date stands.'
                    : "Left blank, the vendor's agreed credit days apply."}
                </p>
              </div>

              {!amending && (
                <div>
                  <Label htmlFor="invoice-currency">Currency</Label>
                  <Input
                    id="invoice-currency"
                    {...register('currency')}
                    autoComplete="off"
                    maxLength={3}
                    placeholder="INR"
                    className="mt-1.5 uppercase"
                    aria-invalid={Boolean(errors.currency)}
                  />
                  <FieldError message={errors.currency?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Left blank, the company&rsquo;s own denomination applies. It is frozen once the
                    bill is recorded.
                  </p>
                </div>
              )}

              <div>
                <Label htmlFor="invoice-purchase-order">Purchase order</Label>
                <div className="mt-1.5">
                  {canReadOrders ? (
                    <PurchaseOrderPicker
                      vendorId={vendorId}
                      value={purchaseOrderId}
                      selectedLabel={orderQuery.data?.code ?? null}
                      onChange={(order) => {
                        setValue('purchase_order_id', order?.id ?? '')
                        // A receipt belongs to exactly one order, so one chosen
                        // under the previous order can no longer apply.
                        if (order?.id !== purchaseOrderId) setValue('goods_receipt_id', '')
                      }}
                    />
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      Your account cannot read purchase orders, so the bill is recorded without one.
                      It can be linked later by somebody who can.
                    </p>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  {vendorId
                    ? 'Optional, but it is what the three-way match compares the prices against.'
                    : 'Choose the vendor first.'}
                </p>
              </div>

              <div>
                <Label htmlFor="invoice-goods-receipt">Goods receipt</Label>
                <div className="mt-1.5">
                  {canReadReceipts ? (
                    <GoodsReceiptPicker
                      vendorId={vendorId}
                      purchaseOrderId={purchaseOrderId}
                      value={goodsReceiptId}
                      selectedLabel={receiptQuery.data?.code ?? null}
                      onChange={(receipt) => {
                        setValue('goods_receipt_id', receipt?.id ?? '')
                        // The API refuses a receipt booked against a different
                        // order, so naming one settles the order as well.
                        if (receipt) setValue('purchase_order_id', receipt.purchase_order_id)
                      }}
                    />
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      Your account cannot read goods receipts, so the bill is recorded without one.
                    </p>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  Names the delivery this bill covers. Quantities are then matched against what was
                  accepted at the gate.
                </p>
              </div>
            </div>

            {/* --------------------------------------------------- lines -- */}

            <div>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-sm font-medium">Lines</h3>
                <div className="flex flex-wrap items-center gap-2">
                  {/* Nothing to copy is not an offer: an order with no lines,
                      or a receipt where nothing cleared QC, would replace the
                      set with an empty one the API refuses. */}
                  {seedSource && seedSource.lines.length > 0 && (
                    <Button type="button" variant="outline" size="sm" onClick={copyLines}>
                      <ClipboardCopy />
                      Copy {seedSource.lines.length}{' '}
                      {seedSource.lines.length === 1 ? 'line' : 'lines'} from {seedSource.code}
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={fields.length >= MAX_INVOICE_LINES}
                    onClick={() => append(emptyLine(lines[lines.length - 1]))}
                  >
                    <Plus />
                    Add line
                  </Button>
                </div>
              </div>
              <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />

              {seedSource && seedSource.lines.length > 0 && fields.length > 0 && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Copying replaces every line below.
                </p>
              )}

              {linksAreStale && (
                <Alert variant="warning" className="mt-3">
                  <AlertTitle>These lines are linked to a different document</AlertTitle>
                  <AlertDescription>
                    The {seeded?.kind === 'receipt' ? 'goods receipt' : 'purchase order'} named
                    above has changed since these lines were linked, and the API refuses a line
                    billed against another order&rsquo;s line. Copy the lines again, or drop the
                    links and keep the figures you have keyed.
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-2"
                      onClick={clearLinks}
                    >
                      <Link2Off />
                      Drop the links
                    </Button>
                  </AlertDescription>
                </Alert>
              )}

              {fields.length === 0 && (
                <p className="mt-3 rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
                  No lines yet. Name the purchase order or the goods receipt above and copy its
                  lines, or add one by hand.
                </p>
              )}

              <div className="mt-3 space-y-3">
                {fields.map((field, index) => {
                  const line = lines[index]
                  const linePricing = pricing[index]
                  const uom = line?.uom_id ? uomById.get(line.uom_id) : undefined

                  return (
                    <div key={field.id} className="space-y-3 rounded-lg border p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="muted">Line {index + 1}</Badge>
                          {line?.purchase_order_line_id && (
                            <Badge variant="secondary">Order line</Badge>
                          )}
                          {line?.goods_receipt_line_id && (
                            <Badge variant="secondary">Receipt line</Badge>
                          )}
                        </div>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`Remove line ${index + 1}`}
                          onClick={() => remove(index)}
                        >
                          <Trash2 className="text-destructive" />
                        </Button>
                      </div>

                      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                        {canReadItems ? (
                          <div className="sm:col-span-2">
                            <Controller
                              control={control}
                              name={`lines.${index}.item_id`}
                              render={({ field: itemField }) => (
                                <ItemPicker
                                  id={`invoice-line-item-${index}`}
                                  label={`Line ${index + 1} item`}
                                  value={itemField.value || null}
                                  onChange={(itemId) => itemField.onChange(itemId ?? '')}
                                  placeholder="Search the item master"
                                />
                              )}
                            />
                            <p className="mt-1 text-xs text-muted-foreground">
                              Optional - freight and service charges have no item.
                            </p>
                          </div>
                        ) : null}

                        <div
                          className={canReadItems ? 'sm:col-span-2' : 'sm:col-span-2 lg:col-span-3'}
                        >
                          <Label htmlFor={`invoice-line-description-${index}`}>Description</Label>
                          <Input
                            id={`invoice-line-description-${index}`}
                            {...register(`lines.${index}.description`)}
                            autoComplete="off"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.description)}
                          />
                          <FieldError message={errors.lines?.[index]?.description?.message} />
                          <p className="mt-1 text-xs text-muted-foreground">
                            Left blank, the item&rsquo;s own name is recorded.
                          </p>
                        </div>

                        <div>
                          <Label htmlFor={`invoice-line-hsn-${index}`}>HSN code</Label>
                          <Input
                            id={`invoice-line-hsn-${index}`}
                            {...register(`lines.${index}.hsn_code`)}
                            autoComplete="off"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.hsn_code)}
                          />
                          <FieldError message={errors.lines?.[index]?.hsn_code?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`invoice-line-quantity-${index}`} required>
                            Quantity
                          </Label>
                          <Input
                            id={`invoice-line-quantity-${index}`}
                            {...register(`lines.${index}.quantity`)}
                            inputMode="decimal"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                          />
                          <FieldError message={errors.lines?.[index]?.quantity?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`invoice-line-uom-${index}`}>Unit</Label>
                          <Controller
                            control={control}
                            name={`lines.${index}.uom_id`}
                            render={({ field: uomField }) => (
                              <Select
                                value={uomField.value || NO_UOM}
                                onValueChange={(next) =>
                                  uomField.onChange(next === NO_UOM ? '' : next)
                                }
                              >
                                <SelectTrigger
                                  id={`invoice-line-uom-${index}`}
                                  className="mt-1.5"
                                  aria-label={`Line ${index + 1} unit`}
                                >
                                  {/* The placeholder only shows when the id
                                      matches no loaded option - a unit list
                                      that has not arrived, or an account
                                      without `uom.view`. Saying "None" there
                                      would deny a unit the line actually has. */}
                                  <SelectValue
                                    placeholder={uom?.code ?? (line?.uom_id ? 'Unit' : 'None')}
                                  />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value={NO_UOM}>None</SelectItem>
                                  {(uoms.data ?? []).map((option) => (
                                    <SelectItem key={option.id} value={option.id}>
                                      {option.code} - {option.name}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            )}
                          />
                        </div>

                        <div>
                          <Label htmlFor={`invoice-line-price-${index}`} required>
                            Unit price
                          </Label>
                          <Input
                            id={`invoice-line-price-${index}`}
                            {...register(`lines.${index}.unit_price`)}
                            inputMode="decimal"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.unit_price)}
                          />
                          <FieldError message={errors.lines?.[index]?.unit_price?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`invoice-line-discount-${index}`}>Discount</Label>
                          <Input
                            id={`invoice-line-discount-${index}`}
                            {...register(`lines.${index}.discount_amount`)}
                            inputMode="decimal"
                            placeholder="0"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.discount_amount)}
                          />
                          <FieldError message={errors.lines?.[index]?.discount_amount?.message} />
                          <p className="mt-1 text-xs text-muted-foreground">
                            An amount off the whole line, not a percentage.
                          </p>
                        </div>

                        <div>
                          <Label htmlFor={`invoice-line-rate-${index}`}>GST rate %</Label>
                          <Input
                            id={`invoice-line-rate-${index}`}
                            {...register(`lines.${index}.tax_rate`)}
                            inputMode="decimal"
                            placeholder="0"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.lines?.[index]?.tax_rate)}
                          />
                          <FieldError message={errors.lines?.[index]?.tax_rate?.message} />
                        </div>

                        <div className="flex items-end">
                          <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
                            <Controller
                              control={control}
                              name={`lines.${index}.is_interstate`}
                              render={({ field: flagField }) => (
                                <Checkbox
                                  id={`invoice-line-interstate-${index}`}
                                  checked={flagField.value}
                                  onCheckedChange={(checked) =>
                                    flagField.onChange(checked === true)
                                  }
                                />
                              )}
                            />
                            <Label
                              htmlFor={`invoice-line-interstate-${index}`}
                              className="cursor-pointer text-xs font-normal"
                            >
                              Interstate (IGST)
                            </Label>
                          </div>
                        </div>
                      </div>

                      {linePricing && (
                        <p className="text-xs text-muted-foreground">
                          Taxable{' '}
                          <span className="tabular font-medium text-foreground">
                            {money(linePricing.taxable.toFixed(2))}
                          </span>
                          {' + tax '}
                          <span className="tabular font-medium text-foreground">
                            {money(linePricing.tax.toFixed(2))}
                          </span>
                          {line?.is_interstate
                            ? ' (IGST)'
                            : ` (CGST ${money(linePricing.cgst.toFixed(2))} + SGST ${money(linePricing.sgst.toFixed(2))})`}
                          {' = '}
                          <span className="tabular font-medium text-foreground">
                            {money(linePricing.lineTotal.toFixed(2))}
                          </span>
                        </p>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>

            {/* -------------------------------------------------- totals -- */}

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="invoice-round-off">Round off</Label>
                <Input
                  id="invoice-round-off"
                  {...register('round_off_amount')}
                  inputMode="decimal"
                  placeholder="0"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.round_off_amount)}
                />
                <FieldError message={errors.round_off_amount?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  The adjustment, positive or negative, that makes the total agree with the figure
                  printed on the bill.
                </p>
              </div>

              <dl className="space-y-1 rounded-lg border p-3 text-sm">
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Taxable</dt>
                  <dd className="tabular">{money(totals.subtotal.toFixed(2))}</dd>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Tax</dt>
                  <dd className="tabular">{money(totals.tax.toFixed(2))}</dd>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Round off</dt>
                  <dd className="tabular">{money(zeroIfBlank(roundOff))}</dd>
                </div>
                <div className="flex items-center justify-between gap-4 border-t pt-1 font-medium">
                  <dt>Invoice total</dt>
                  <dd className="tabular">{money(totals.total.toFixed(2))}</dd>
                </div>
              </dl>
            </div>

            <div>
              <Label htmlFor="invoice-notes">Notes</Label>
              <Textarea id="invoice-notes" {...register('notes')} className="mt-1.5" />
              <FieldError message={errors.notes?.message} />
            </div>
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
            <Button type="submit" loading={save.isPending} disabled={!canSave}>
              {amending ? 'Save the amendment' : 'Record the invoice'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The line set, exactly as `VendorInvoiceLineIn` declares it.
 *
 * The model forbids unknown keys, and everything derived - the taxable amount,
 * the CGST/SGST/IGST split and the line total - is deliberately absent: the
 * server prices each line itself.
 */
function buildLines(values: InvoiceFormValues): VendorInvoiceLinePayload[] {
  return values.lines.map((line) => ({
    item_id: orNull(line.item_id),
    uom_id: orNull(line.uom_id),
    description: orNull(line.description),
    hsn_code: orNull(line.hsn_code),
    // Quantities and prices stay strings on the wire; the API parses Decimals.
    quantity: line.quantity.trim(),
    unit_price: line.unit_price.trim(),
    discount_amount: zeroIfBlank(line.discount_amount),
    tax_rate: zeroIfBlank(line.tax_rate),
    is_interstate: line.is_interstate,
    purchase_order_line_id: orNull(line.purchase_order_line_id),
    goods_receipt_line_id: orNull(line.goods_receipt_line_id),
    notes: orNull(line.notes),
  }))
}

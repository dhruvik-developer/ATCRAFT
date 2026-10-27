import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
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
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, toDecimal } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import {
  receiptInvoiceLines,
  type VendorDocument,
  type VendorOrderResponse,
  type VendorReceipt,
} from './vendor-model'

const lineSchema = z.object({
  item_id: z.string(),
  purchase_order_line_id: z.string().nullable(),
  goods_receipt_line_id: z.string(),
  description: z.string().trim().max(500),
  hsn_code: z.string().trim().max(16),
  quantity: z
    .string()
    .regex(/^\d{1,14}(\.\d{1,4})?$/, 'Up to 4 decimal places')
    .refine((v): boolean => toDecimal(v).greaterThan(0), 'Quantity must be greater than zero'),
  uom_id: z.string().nullable(),
  unit_price: z
    .string()
    .regex(/^\d{1,12}(\.\d{1,6})?$/, 'Enter a non-negative price with up to 6 decimal places'),
  discount_amount: z.string().regex(/^\d{1,14}(\.\d{1,4})?$/, 'Enter a non-negative discount'),
  tax_rate: z
    .string()
    .regex(/^\d{1,5}(\.\d{1,4})?$/, 'Up to 4 decimal places')
    .refine((v): boolean => toDecimal(v).lessThanOrEqualTo(100), 'Tax must be between 0 and 100%'),
  is_interstate: z.boolean(),
})
const schema = z
  .object({
    vendor_invoice_number: z.string().trim().min(1, 'Enter your invoice number').max(64),
    invoice_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Choose the invoice date'),
    due_date: z.string(),
    notes: z.string().trim().max(2000),
    round_off_amount: z.string().regex(/^-?\d{1,14}(\.\d{1,4})?$/, 'Up to 4 decimal places'),
    lines: z
      .array(lineSchema)
      .min(1, 'This receipt has no accepted quantities to invoice')
      .max(200),
  })
  .superRefine((values, ctx) => {
    if (values.due_date && values.due_date < values.invoice_date)
      ctx.addIssue({
        code: 'custom',
        path: ['due_date'],
        message: 'Due date cannot precede the invoice date',
      })
  })
type Values = z.infer<typeof schema>

/** Receipt selection is paginated; a bill never uses a staff-only item or vendor lookup. */
export function VendorInvoiceForm({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const [receiptPage, setReceiptPage] = useState(1)
  const [receipt, setReceipt] = useState<VendorReceipt | null>(null)
  const [attachment, setAttachment] = useState<File | null>(null)
  const seededReceipt = useRef<string | null>(null)
  const uploadedAttachment = useRef<{ file: File; orderId: string; documentId: string } | null>(
    null,
  )
  const receipts = useQuery({
    queryKey: queryKeys.vendorPortal.receipts({ page: receiptPage, purpose: 'invoice' }),
    queryFn: ({ signal }) =>
      api.getPage<VendorReceipt>('/vendor-portal/receipts', {
        params: { page: receiptPage, limit: PICKER_PAGE_SIZE },
        signal,
      }),
  })
  const order = useQuery({
    queryKey: queryKeys.vendorPortal.order(receipt?.purchase_order_id ?? ''),
    queryFn: ({ signal }) =>
      api.get<VendorOrderResponse>(`/vendor-portal/purchase-orders/${receipt?.purchase_order_id}`, {
        signal,
      }),
    enabled: Boolean(receipt),
  })
  const issuedOrder = order.data && !order.data.pending_amendment ? order.data : null
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      vendor_invoice_number: '',
      invoice_date: '',
      due_date: '',
      round_off_amount: '0',
      notes: '',
      lines: [],
    },
  })
  const { fields, replace, remove } = useFieldArray({ control: form.control, name: 'lines' })
  useEffect(() => {
    // Refetching a header must not replace invoice figures somebody is editing.
    if (
      receipt &&
      issuedOrder?.id === receipt.purchase_order_id &&
      seededReceipt.current !== receipt.id
    ) {
      replace(receiptInvoiceLines(issuedOrder, receipt))
      seededReceipt.current = receipt.id
    }
  }, [receipt, issuedOrder, replace])
  const mutation = useMutation({
    mutationFn: async (values: Values) => {
      if (!receipt || !issuedOrder)
        throw new Error('Choose a goods receipt with an issued purchase order before submitting')
      let documentId: string | undefined
      if (attachment) {
        // A bill can fail validation after its file succeeded. Reuse that upload
        // on retry so correcting a field does not create duplicate attachments.
        if (
          uploadedAttachment.current?.file === attachment &&
          uploadedAttachment.current.orderId === receipt.purchase_order_id
        )
          documentId = uploadedAttachment.current.documentId
        else {
          const data = new FormData()
          data.append('kind', 'INVOICE')
          data.append('file', attachment)
          const uploaded = await api.post<VendorDocument>(
            `/vendor-portal/purchase-orders/${receipt.purchase_order_id}/documents`,
            data,
          )
          documentId = uploaded.id
          uploadedAttachment.current = {
            file: attachment,
            orderId: receipt.purchase_order_id,
            documentId,
          }
        }
      }
      const payload: SchemaOf<'VendorPortalInvoiceCreate'> = {
        purchase_order_id: receipt.purchase_order_id,
        goods_receipt_id: receipt.id,
        ...(documentId ? { invoice_document_id: documentId } : {}),
        vendor_invoice_number: values.vendor_invoice_number,
        invoice_date: values.invoice_date,
        ...(values.due_date ? { due_date: values.due_date } : {}),
        currency: issuedOrder.currency,
        round_off_amount: values.round_off_amount,
        notes: values.notes || null,
        lines: values.lines.map((line) => ({
          ...line,
          description: line.description || null,
          hsn_code: line.hsn_code || null,
        })),
      }
      return api.post<SchemaOf<'VendorPortalInvoiceDetail'>>('/vendor-portal/invoices', payload)
    },
    onError: (error) =>
      applyFieldErrors(error, form.setError, (path) =>
        ['file', 'invoice_document_id', 'purchase_order_id', 'goods_receipt_id'].includes(path)
          ? 'root'
          : path,
      ),
    onSuccess: async (invoice) => {
      await client.invalidateQueries({ queryKey: queryKeys.vendorPortal.all })
      toast.success(`Invoice ${invoice.vendor_invoice_number} submitted`)
      onClose()
    },
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose()
      }}
    >
      <DialogContent className="max-h-[92svh] max-sm:bottom-0 max-sm:top-auto max-sm:translate-y-0 sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>New invoice against PO</DialogTitle>
          <DialogDescription>
            Choose a posted goods receipt. Accepted quantities and the agreed net prices are copied
            for review before submission.
          </DialogDescription>
        </DialogHeader>
        <form
          className="[&_input]:min-h-11 [&_select]:min-h-11"
          onSubmit={form.handleSubmit((values) => mutation.mutate(values))}
        >
          <DialogBody className="space-y-5">
            <div>
              <Label htmlFor="invoice-receipt">Purchase order and goods receipt</Label>
              <select
                id="invoice-receipt"
                className="h-11 w-full rounded-md border bg-background px-3 text-sm"
                value={receipt?.id ?? ''}
                onChange={(event) => {
                  setReceipt(
                    receipts.data?.data.find((row) => row.id === event.target.value) ?? null,
                  )
                  replace([])
                  seededReceipt.current = null
                  setAttachment(null)
                }}
              >
                <option value="">Choose a receipt</option>
                {receipts.data?.data.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.purchase_order_code} · {row.code} · {formatDate(row.receipt_date)}
                  </option>
                ))}
              </select>
              {receipts.error && (
                <p role="alert" className="text-sm text-destructive">
                  {describeError(receipts.error)}
                </p>
              )}
              {order.error && (
                <p role="alert" className="text-sm text-destructive">
                  {describeError(order.error)}
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setReceiptPage(receiptPage - 1)
                    setReceipt(null)
                    replace([])
                    seededReceipt.current = null
                  }}
                  disabled={receiptPage === 1 || receipts.isFetching}
                >
                  Previous receipts
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setReceiptPage(receiptPage + 1)
                    setReceipt(null)
                    replace([])
                    seededReceipt.current = null
                  }}
                  disabled={!receipts.data?.pagination.has_next || receipts.isFetching}
                >
                  More receipts
                </Button>
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="invoice-number">Your invoice number</Label>
                <Input
                  id="invoice-number"
                  maxLength={64}
                  {...form.register('vendor_invoice_number')}
                />
                <FieldError message={form.formState.errors.vendor_invoice_number?.message} />
              </div>
              <div>
                <Label htmlFor="invoice-date">Invoice date</Label>
                <Input id="invoice-date" type="date" {...form.register('invoice_date')} />
                <FieldError message={form.formState.errors.invoice_date?.message} />
              </div>
              <div>
                <Label htmlFor="invoice-due">Due date (optional)</Label>
                <Input id="invoice-due" type="date" {...form.register('due_date')} />
                <FieldError message={form.formState.errors.due_date?.message} />
              </div>
              <div>
                <Label htmlFor="invoice-round">Round off</Label>
                <Input
                  id="invoice-round"
                  inputMode="decimal"
                  {...form.register('round_off_amount')}
                />
                <FieldError message={form.formState.errors.round_off_amount?.message} />
              </div>
            </div>
            {order.isFetching && receipt && <p role="status">Loading order lines…</p>}
            {order.data?.pending_amendment && (
              <p role="alert" className="text-sm text-muted-foreground">
                {order.data.message}
              </p>
            )}
            <div className="space-y-3">
              {fields.map((field, index) => (
                <fieldset key={field.id} className="rounded-lg border p-4">
                  <legend className="px-1 text-sm font-medium">Invoice line {index + 1}</legend>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {(
                      [
                        ['description', 'Description'],
                        ['hsn_code', 'HSN code'],
                        ['quantity', 'Quantity'],
                        ['unit_price', 'Unit price'],
                        ['discount_amount', 'Discount amount'],
                        ['tax_rate', 'Tax rate (%)'],
                      ] as const
                    ).map(([name, label]) => (
                      <div key={name}>
                        <Label htmlFor={`invoice-line-${index}-${name}`}>{label}</Label>
                        <Input
                          id={`invoice-line-${index}-${name}`}
                          inputMode={
                            ['description', 'hsn_code'].includes(name) ? undefined : 'decimal'
                          }
                          {...form.register(`lines.${index}.${name}`)}
                        />
                        <FieldError
                          message={form.formState.errors.lines?.[index]?.[name]?.message}
                        />
                      </div>
                    ))}
                    <Label className="flex min-h-11 items-center gap-2">
                      <Checkbox
                        checked={form.watch(`lines.${index}.is_interstate`)}
                        onCheckedChange={(checked) =>
                          form.setValue(`lines.${index}.is_interstate`, checked === true)
                        }
                      />
                      Interstate tax (IGST)
                    </Label>
                    <Button type="button" variant="outline" onClick={() => remove(index)}>
                      Remove line {index + 1}
                    </Button>
                  </div>
                </fieldset>
              ))}
            </div>
            <FieldError
              message={
                form.formState.errors.lines?.message ?? form.formState.errors.lines?.root?.message
              }
            />
            <div>
              <Label htmlFor="invoice-attachment">Invoice image or PDF (optional)</Label>
              <Input
                id="invoice-attachment"
                type="file"
                accept="image/*,application/pdf"
                capture="environment"
                onChange={(event) => setAttachment(event.target.files?.[0] ?? null)}
              />
            </div>
            <div>
              <Label htmlFor="invoice-notes">Notes</Label>
              <Textarea id="invoice-notes" maxLength={2000} {...form.register('notes')} />
              <FieldError message={form.formState.errors.notes?.message} />
            </div>
            <FieldError message={form.formState.errors.root?.message} />
          </DialogBody>
          <DialogFooter className="[&>button]:min-h-11">
            <Button type="button" variant="outline" disabled={mutation.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                !receipt ||
                !issuedOrder ||
                order.isFetching ||
                fields.length === 0 ||
                mutation.isPending
              }
            >
              {mutation.isPending ? 'Submitting…' : 'Submit invoice'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

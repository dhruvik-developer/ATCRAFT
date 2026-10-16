import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ConfirmDialog, DescriptionList, FieldError, StatusBadge } from '@/components/shared'
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
import { Alert, AlertDescription, AlertTitle, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { formatDate, formatMoney, formatQuantity } from '@/lib/format'
import { salesInvoiceMessages, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import {
  salesInvoiceKeys,
  type SalesInvoiceDetail,
  type SalesInvoicePreview,
} from './sales-invoice-types'

function InvoiceAmounts({ invoice }: { invoice: SalesInvoicePreview }) {
  return (
    <DescriptionList
      columns={3}
      items={[
        { label: 'Subtotal', value: formatMoney(invoice.subtotal, { currency: invoice.currency }) },
        {
          label: 'Discount',
          value: formatMoney(invoice.discount_amount, { currency: invoice.currency }),
        },
        {
          label: 'Taxable',
          value: formatMoney(invoice.taxable_amount, { currency: invoice.currency }),
        },
        { label: 'CGST', value: formatMoney(invoice.cgst_amount, { currency: invoice.currency }) },
        { label: 'SGST', value: formatMoney(invoice.sgst_amount, { currency: invoice.currency }) },
        { label: 'IGST', value: formatMoney(invoice.igst_amount, { currency: invoice.currency }) },
        {
          label: 'Round off',
          value: formatMoney(invoice.round_off_amount, { currency: invoice.currency }),
        },
        {
          label: 'Total',
          value: formatMoney(invoice.total_amount, { currency: invoice.currency }),
        },
        { label: 'Due date', value: formatDate(invoice.due_date) },
      ]}
    />
  )
}

function InvoiceLines({ invoice }: { invoice: SalesInvoicePreview }) {
  return (
    <div className="overflow-x-auto" role="region" aria-label="Invoice line details" tabIndex={0}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            {[
              'Item',
              'HSN',
              'Quantity',
              'Rate',
              'Discount',
              'Taxable',
              'CGST',
              'SGST',
              'IGST',
              'Total',
            ].map((label) => (
              <th key={label} className="whitespace-nowrap p-2">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {invoice.lines.map((line) => (
            <tr key={line.dispatch_line_id} className="border-b">
              <td className="min-w-40 p-2">{line.description}</td>
              <td className="p-2">{line.hsn_code ?? '-'}</td>
              <td className="whitespace-nowrap p-2">
                {formatQuantity(line.quantity)} {line.uom_code}
              </td>
              {[
                'unit_price',
                'discount_amount',
                'taxable_amount',
                'cgst_amount',
                'sgst_amount',
                'igst_amount',
                'line_total',
              ].map((field) => (
                <td key={field} className="whitespace-nowrap p-2 text-right tabular">
                  {formatMoney(line[field as 'unit_price'], { currency: invoice.currency })}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function RaiseSalesInvoiceDialog({
  dispatchId,
  onClose,
}: {
  dispatchId: string | null
  onClose: () => void
}) {
  const { can } = useAuth()
  const [date, setDate] = useState('')
  const [notes, setNotes] = useState('')
  const queryClient = useQueryClient()
  const preview = useQuery({
    queryKey: salesInvoiceKeys.preview(dispatchId ?? '', date),
    queryFn: () =>
      api.get<SalesInvoicePreview>('/finance/sales-invoices/preview', {
        params: { dispatch_id: dispatchId, ...(date ? { invoice_date: date } : {}) },
      }),
    enabled: Boolean(dispatchId) && can(P.SALES_INVOICE_VIEW),
    staleTime: 0,
  })
  useEffect(() => {
    setDate('')
    setNotes('')
  }, [dispatchId])
  const issue = useMutation({
    mutationFn: () =>
      api.post<SalesInvoiceDetail>('/finance/sales-invoices', {
        dispatch_id: dispatchId,
        ...(date ? { invoice_date: date } : {}),
        notes: notes.trim() || null,
      }),
    onSuccess: (invoice) => {
      void queryClient.invalidateQueries({ queryKey: salesInvoiceKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      void queryClient.invalidateQueries({ queryKey: ['finance'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(salesInvoiceMessages.issued, invoice.code)
      onClose()
    },
  })
  return (
    <Dialog open={Boolean(dispatchId)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Raise sales invoice</DialogTitle>
          <DialogDescription>{salesInvoiceMessages.preview}</DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="sales-invoice-date">Invoice date</Label>
              <Input
                id="sales-invoice-date"
                type="date"
                value={date}
                onChange={(event) => setDate(event.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="sales-invoice-notes">Notes</Label>
              <Input
                id="sales-invoice-notes"
                value={notes}
                maxLength={2000}
                onChange={(event) => setNotes(event.target.value)}
              />
            </div>
          </div>
          {preview.isLoading && <p>Loading invoice preview...</p>}
          {!can(P.SALES_INVOICE_VIEW) && (
            <FieldError message="Viewing sales invoices is required to review the invoice before issuing it." />
          )}
          {preview.error && <FieldError message={preview.error.message} />}
          {issue.error && <FieldError message={issue.error.message} />}
          {preview.data && (
            <>
              <DescriptionList
                items={[
                  { label: 'Seller', value: preview.data.seller_name },
                  { label: 'Buyer', value: preview.data.buyer_name },
                  { label: 'Invoice date', value: formatDate(preview.data.invoice_date) },
                ]}
              />
              <InvoiceLines invoice={preview.data} />
              <InvoiceAmounts invoice={preview.data} />
            </>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          {can(P.SALES_INVOICE_CREATE) && (
            <Button
              onClick={() => issue.mutate()}
              disabled={
                !can(P.SALES_INVOICE_VIEW) ||
                !preview.data ||
                preview.isFetching ||
                Boolean(preview.error)
              }
              loading={issue.isPending}
            >
              Confirm and issue
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function SalesInvoiceDetailDialog({
  invoiceId,
  onClose,
}: {
  invoiceId: string | null
  onClose: () => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const detail = useQuery({
    queryKey: salesInvoiceKeys.detail(invoiceId ?? ''),
    queryFn: () => api.get<SalesInvoiceDetail>(`/finance/sales-invoices/${invoiceId}`),
    enabled: Boolean(invoiceId) && can(P.SALES_INVOICE_VIEW),
  })
  const [cancelOpen, setCancelOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  const job = useReportJob()
  const pdf = useMutation({
    mutationFn: () =>
      api.post<AcceptedJobData>(`/finance/sales-invoices/${invoiceId}/pdf`, undefined),
    onSuccess: (accepted) => job.start(accepted),
  })
  const download = useMutation({
    mutationFn: (documentId: string) =>
      api.download(
        `/documents/${documentId}/download`,
        `tax-invoice-${detail.data?.code ?? 'invoice'}.pdf`,
      ),
  })
  const cancel = useMutation({
    mutationFn: () =>
      api.post<SalesInvoiceDetail>(`/finance/sales-invoices/${invoiceId}/cancel`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['finance'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.sales.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      setCancelOpen(false)
      setReason('')
      setReasonTouched(false)
      toast.success(success.cancelled('Sales invoice'))
    },
  })
  const invoice = detail.data
  return (
    <>
      <Dialog
        open={Boolean(invoiceId)}
        onOpenChange={(open) => {
          if (!open) {
            job.reset()
            onClose()
          }
        }}
      >
        <DialogContent size="xl">
          <DialogHeader>
            <DialogTitle>{invoice?.code ?? 'Sales invoice'}</DialogTitle>
            <DialogDescription>{salesInvoiceMessages.snapshot}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            {detail.error && <FieldError message={detail.error.message} />}
            {detail.isLoading && <p>Loading invoice...</p>}
            {invoice && (
              <>
                <StatusBadge status={invoice.status} />
                <div className="grid gap-4 sm:grid-cols-2">
                  <section>
                    <h3 className="font-medium">Seller</h3>
                    <p>{invoice.seller_name}</p>
                    <p className="text-sm text-muted-foreground">{invoice.seller_address}</p>
                    <p className="text-sm">GSTIN: {invoice.seller_gstin ?? 'Unregistered'}</p>
                  </section>
                  <section>
                    <h3 className="font-medium">Buyer</h3>
                    <p>{invoice.buyer_name}</p>
                    <p className="text-sm text-muted-foreground">{invoice.billing_address}</p>
                    <p className="text-sm">GSTIN: {invoice.buyer_gstin ?? 'Unregistered'}</p>
                    <p className="text-sm">Ship to: {invoice.shipping_address}</p>
                  </section>
                </div>
                <DescriptionList
                  items={[
                    { label: 'Date', value: formatDate(invoice.invoice_date) },
                    { label: 'Place of supply', value: invoice.place_of_supply_state_code ?? '-' },
                    {
                      label: 'Sales order',
                      value: (
                        <Link to={`/sales-orders/${invoice.sales_order_id}`}>Open sales order</Link>
                      ),
                    },
                    {
                      label: 'Paid',
                      value: formatMoney(invoice.paid_amount, { currency: invoice.currency }),
                    },
                    {
                      label: 'Balance',
                      value: formatMoney(invoice.balance_amount, { currency: invoice.currency }),
                    },
                  ]}
                />
                <InvoiceLines invoice={invoice} />
                <h3 className="font-medium">Tax summary and totals</h3>
                <InvoiceAmounts invoice={invoice} />
                <h3 className="font-medium">Allocated receipts</h3>
                {invoice.payments.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No receipts allocated</p>
                ) : (
                  invoice.payments.map((payment) => (
                    <p key={payment.payment_id} className="text-sm">
                      {payment.payment_code} · {formatDate(payment.payment_date)} ·{' '}
                      {formatMoney(payment.amount, { currency: invoice.currency })}
                    </p>
                  ))
                )}
                {invoice.cancellation_reason && (
                  <Alert>
                    <AlertTitle>Cancelled</AlertTitle>
                    <AlertDescription>{invoice.cancellation_reason}</AlertDescription>
                  </Alert>
                )}
                {can(P.SALES_INVOICE_VIEW) && (
                  <div className="flex flex-wrap gap-2">
                    {invoice.pdf_document_id && (
                      <Button
                        variant="outline"
                        loading={download.isPending}
                        onClick={() => download.mutate(invoice.pdf_document_id!)}
                      >
                        Download PDF
                      </Button>
                    )}
                    <Button variant="outline" loading={pdf.isPending} onClick={() => pdf.mutate()}>
                      Generate PDF
                    </Button>
                  </div>
                )}
                <ReportJobPanel controller={job} label="Sales invoice PDF" />
              </>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>
              Close
            </Button>
            {invoice?.can_cancel && can(P.SALES_INVOICE_CANCEL) && (
              <Button variant="destructive" onClick={() => setCancelOpen(true)}>
                Cancel invoice
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={(open) => {
          setCancelOpen(open)
          if (!open) {
            setReason('')
            setReasonTouched(false)
            cancel.reset()
          }
        }}
        title="Cancel this invoice?"
        description={salesInvoiceMessages.cancel}
        confirmLabel="Cancel invoice"
        destructive
        loading={cancel.isPending}
        onConfirm={() => {
          setReasonTouched(true)
          if (reason.trim().length >= REASON_MIN_LENGTH) cancel.mutate()
        }}
      >
        <Label htmlFor="sales-invoice-cancel-reason" required>
          Reason
        </Label>
        <Input
          id="sales-invoice-cancel-reason"
          value={reason}
          maxLength={REASON_MAX_LENGTH}
          aria-invalid={reasonTouched && reason.trim().length < REASON_MIN_LENGTH}
          onChange={(event) => setReason(event.target.value)}
        />
        {reasonTouched && reason.trim().length < REASON_MIN_LENGTH && (
          <FieldError message="A cancellation needs at least three characters." />
        )}
        {cancel.error && <FieldError message={cancel.error.message} />}
      </ConfirmDialog>
    </>
  )
}

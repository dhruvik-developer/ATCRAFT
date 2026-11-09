import { success } from '@/lib/messages'

/**
 * The sales-invoice workflow's own sentences, said by the issue and cancel
 * dialogs and the GST summary.
 *
 * They belong to one feature, so they live with it: lib/messages.ts holds the
 * vocabulary every screen shares, and every byte of it is downloaded before the
 * first screen paints, whether or not the visitor ever opens an invoice.
 */
export const salesInvoiceMessages = {
  issued: success.issued('Sales invoice'),
  preview:
    'Review the dispatch quantities, agreed prices and tax before issuing. The number is assigned when you confirm.',
  snapshot: 'Party details, prices and tax are fixed as they were at issue.',
  cancel:
    'The receivable journal and invoiced quantities are reversed together. Paid invoices or invoices outside the cancellation window need a credit note.',
  gst: 'Output GST comes from issued sales invoice lines by invoice date. Uninvoiced dispatches are shown separately for follow-up.',
} as const

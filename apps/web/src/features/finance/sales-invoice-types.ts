import type { SchemaOf } from '@/types/api'

export type SalesInvoiceSummary = SchemaOf<'SalesInvoiceSummary'>
export type SalesInvoiceDetail = SchemaOf<'SalesInvoiceDetail'>
export type SalesInvoicePreview = SchemaOf<'SalesInvoicePreview'>
export type SalesInvoiceStatus = SchemaOf<'SalesInvoiceStatus'>
export const salesInvoiceKeys = {
  all: ['finance', 'sales-invoices'] as const,
  list: (params: unknown) => ['finance', 'sales-invoices', 'list', params] as const,
  detail: (id: string) => ['finance', 'sales-invoices', 'detail', id] as const,
  preview: (id: string, date: string) =>
    ['finance', 'sales-invoices', 'preview', id, date] as const,
}
export const SALES_INVOICE_STATUSES: SalesInvoiceStatus[] = [
  'ISSUED',
  'PARTIALLY_PAID',
  'PAID',
  'CANCELLED',
]

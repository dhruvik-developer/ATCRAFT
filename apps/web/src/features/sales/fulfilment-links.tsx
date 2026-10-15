/**
 * What has gone out against one sales order: its consignments, newest first,
 * each with its status and a way to open it on the dispatch register.
 *
 * Read from `GET /dispatch?sales_order_id=` - the same list endpoint the
 * register uses, narrowed by the server - so the order page never filters a
 * client-side copy. Gated on the dispatch view right the endpoint declares.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { StatusBadge } from '@/components/shared'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import type { DispatchRow } from '@/features/dispatch/dispatch-types'
import { salesInvoiceKeys, type SalesInvoiceSummary } from '@/features/finance/sales-invoice-types'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDate, formatMoney } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

export function FulfilmentLinks({ orderId }: { orderId: string }) {
  const { can } = useAuth()
  const params = {
    sales_order_id: orderId,
    limit: DEFAULT_PAGE_SIZE,
    with_total: false,
    sort_by: 'dispatch_date',
    sort_dir: 'desc',
  }
  const dispatches = useQuery({
    queryKey: queryKeys.dispatch.list(params),
    queryFn: () => api.getPage<DispatchRow>('/dispatch', { params }),
    enabled: can(P.DISPATCH_VIEW),
    staleTime: staleTimes.transactional,
  })

  if (!can(P.DISPATCH_VIEW)) return <InvoiceLinks orderId={orderId} />

  const rows = dispatches.data?.data ?? []
  return (
    <>
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
          <CardTitle>Dispatches</CardTitle>
          <Link
            className="text-sm text-muted-foreground hover:underline"
            to={`/dispatch?sales_order_id=${orderId}`}
          >
            Open on the dispatch register
          </Link>
        </CardHeader>
        <CardContent className="space-y-2">
          {dispatches.error && (
            <p className="text-sm text-destructive">{dispatches.error.message}</p>
          )}
          {dispatches.isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
          {dispatches.data && rows.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nothing has been dispatched against it yet.
            </p>
          )}
          {rows.length > 0 && (
            <ul className="divide-y rounded-lg border">
              {rows.map((row) => (
                <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 p-3">
                  <div className="min-w-0">
                    <Link
                      className="font-medium hover:underline"
                      to={`/dispatch?q=${encodeURIComponent(row.code)}`}
                    >
                      {row.code}
                    </Link>
                    <span className="block text-xs text-muted-foreground">
                      {formatDate(row.dispatch_date)}
                      {row.tracking_id ? ` - tracking ${row.tracking_id}` : ''}
                    </span>
                  </div>
                  <StatusBadge status={row.status} />
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
      <InvoiceLinks orderId={orderId} />
    </>
  )
}

function InvoiceLinks({ orderId }: { orderId: string }) {
  const { can } = useAuth()
  const params = { sales_order_id: orderId, limit: DEFAULT_PAGE_SIZE, with_total: false }
  const invoices = useQuery({
    queryKey: salesInvoiceKeys.list(params),
    queryFn: () => api.getPage<SalesInvoiceSummary>('/finance/sales-invoices', { params }),
    enabled: can(P.SALES_INVOICE_VIEW),
  })
  if (!can(P.SALES_INVOICE_VIEW)) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Invoices</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {invoices.error && <p className="text-sm text-destructive">{invoices.error.message}</p>}
        {invoices.isLoading && <p>Loading invoices...</p>}
        {invoices.data?.data.map((invoice) => (
          <div key={invoice.id} className="flex flex-wrap items-center justify-between gap-2">
            <Link
              className="font-medium hover:underline"
              to={`/finance/sales-invoices?invoice_id=${invoice.id}`}
            >
              {invoice.code}
            </Link>
            <span>{formatMoney(invoice.total_amount, { currency: invoice.currency })}</span>
            <StatusBadge status={invoice.status} />
          </div>
        ))}
        {invoices.data?.data.length === 0 && <p>No invoices issued against this order</p>}
        <Link
          className="text-sm hover:underline"
          to={`/finance/sales-invoices?sales_order_id=${orderId}`}
        >
          All invoices for this order
        </Link>
      </CardContent>
    </Card>
  )
}

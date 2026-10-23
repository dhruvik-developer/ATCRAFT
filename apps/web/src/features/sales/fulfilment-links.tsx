import { describeError } from '@/lib/messages'
/**
 * The order-to-fulfilment trail: everything raised, moved or issued for one
 * sales order, in the order it happens.
 *
 * Read from `GET /sales/{id}/fulfilment`. The server joins every owning module
 * - MRP suggestions, production, assembly, job work, procurement, goods
 * receipts, quality, transfers, packing, dispatch, invoices, credit notes and
 * returns - so the page does not fire a dozen list queries and then try to
 * reconcile their filters client-side. Each row carries its own URL because the
 * owning module knows where its document lives; a document family the reader
 * cannot open still names the document rather than hiding that it exists.
 *
 * The whole card is behind the order-view right the endpoint declares. A
 * reader without it does not see a broken panel, they see no panel.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { StatusBadge } from '@/components/shared'
import { Badge, Card, CardContent, CardHeader, CardTitle } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import type { FulfilmentDocument, SalesOrderFulfilment } from '@/features/sales/sales-types'
import { api } from '@/lib/api-client'
import { formatDate, formatQuantity } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

type GroupKey =
  | 'mrp_suggestions'
  | 'production_orders'
  | 'assembly_orders'
  | 'job_work_orders'
  | 'requisitions'
  | 'purchase_orders'
  | 'goods_receipts'
  | 'inspections'
  | 'transfers'
  | 'packing_orders'
  | 'dispatches'
  | 'invoices'
  | 'credit_notes'
  | 'returns'

type Group = {
  key: GroupKey
  label: string
  /** A register the reader can open when the section has more rows than fit. */
  register?: { label: string; to: string }
}

const GROUPS: Group[] = [
  { key: 'mrp_suggestions', label: 'MRP suggestions' },
  { key: 'production_orders', label: 'Production orders' },
  { key: 'assembly_orders', label: 'Assembly orders' },
  { key: 'job_work_orders', label: 'Job work' },
  { key: 'requisitions', label: 'Purchase requisitions' },
  { key: 'purchase_orders', label: 'Purchase orders' },
  { key: 'goods_receipts', label: 'Goods receipts' },
  { key: 'inspections', label: 'Quality inspections' },
  { key: 'transfers', label: 'Stock transfers' },
  {
    key: 'packing_orders',
    label: 'Packing',
    register: { label: 'Open on packing', to: '/packing' },
  },
  {
    key: 'dispatches',
    label: 'Dispatches',
    register: { label: 'Open on the dispatch register', to: '/dispatch' },
  },
  {
    key: 'invoices',
    label: 'Invoices',
    register: { label: 'All invoices for this order', to: '/finance/sales-invoices' },
  },
  { key: 'credit_notes', label: 'Credit notes' },
  { key: 'returns', label: 'Returns' },
]

function DocumentRow({ document }: { document: FulfilmentDocument }) {
  const facts = [
    document.item_code,
    document.quantity == null ? null : formatQuantity(document.quantity),
    document.document_date ? formatDate(document.document_date) : null,
    document.expected_date ? `expected ${formatDate(document.expected_date)}` : null,
    document.detail,
  ].filter((fact): fact is string => Boolean(fact))

  return (
    <li className="flex flex-wrap items-start justify-between gap-2 py-2 first:pt-0 last:pb-0">
      <div className="min-w-0">
        <Link className="font-medium hover:underline" to={document.url}>
          {document.code}
        </Link>
        {facts.length > 0 && (
          <span className="block text-xs text-muted-foreground">{facts.join(' · ')}</span>
        )}
      </div>
      <StatusBadge status={document.status} />
    </li>
  )
}

export function FulfilmentLinks({ orderId }: { orderId: string }) {
  const { can } = useAuth()
  const trail = useQuery({
    queryKey: [...queryKeys.sales.detail(orderId), 'fulfilment'],
    queryFn: () => api.get<SalesOrderFulfilment>(`/sales/${orderId}/fulfilment`),
    enabled: can(P.SALES_ORDER_VIEW),
    staleTime: staleTimes.transactional,
  })

  if (!can(P.SALES_ORDER_VIEW)) return null

  const data = trail.data
  const sections = data
    ? GROUPS.map((group) => ({
        ...group,
        documents: data[group.key] ?? [],
      })).filter((section) => section.documents.length > 0)
    : []
  const total = sections.reduce((count, section) => count + section.documents.length, 0)

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
        <CardTitle>Fulfilment trail</CardTitle>
        {data && total > 0 && <Badge variant="muted">{total} document(s)</Badge>}
      </CardHeader>
      <CardContent className="space-y-4">
        {trail.error && <p className="text-sm text-destructive">{describeError(trail.error)}</p>}
        {trail.isLoading && <p className="text-sm text-muted-foreground">Loading the trail...</p>}
        {data && total === 0 && (
          <p className="text-sm text-muted-foreground">
            Nothing has been raised, moved or issued against this order yet.
          </p>
        )}

        {sections.length > 0 && (
          <div className="space-y-4">
            {sections.map((section) => (
              <section key={section.key}>
                <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                  <h3 className="flex items-center gap-2 text-sm font-medium">
                    {section.label}
                    <Badge variant="muted">{section.documents.length}</Badge>
                  </h3>
                  {section.register && (
                    <Link
                      className="text-xs text-muted-foreground hover:underline"
                      to={`${section.register.to}?sales_order_id=${orderId}`}
                    >
                      {section.register.label}
                    </Link>
                  )}
                </div>
                <ul className="divide-y rounded-lg border px-3">
                  {section.documents.map((document) => (
                    <DocumentRow key={`${document.kind}:${document.id}`} document={document} />
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

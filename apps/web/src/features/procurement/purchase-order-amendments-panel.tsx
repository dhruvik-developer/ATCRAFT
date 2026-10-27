import { useInfiniteQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { EmptyState, PanelLoader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDate, formatDateTime, formatMoney, formatQuantity } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { staleTimes } from '@/lib/query-client'

import { purchaseOrderChangeKeys, type PurchaseOrderAmendment } from './purchase-order-change-model'

function Snapshot({
  value,
  currency,
  label,
}: {
  value: PurchaseOrderAmendment['old_values']
  currency: string
  label: string
}) {
  return (
    <section className="min-w-0 space-y-3 rounded-lg border p-3" aria-label={label}>
      <h4 className="font-medium">{label}</h4>
      <StatusBadge status={value.status} />
      <dl className="grid gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Expected delivery</dt>
          <dd>{formatDate(value.expected_delivery_date)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Freight</dt>
          <dd>{formatMoney(value.freight_amount, { currency })}</dd>
        </div>
        {value.terms && (
          <div className="min-w-0 sm:col-span-2">
            <dt className="text-muted-foreground">Terms</dt>
            <dd className="whitespace-pre-wrap break-words">{value.terms}</dd>
          </div>
        )}
        {value.delivery_address && (
          <div className="min-w-0 sm:col-span-2">
            <dt className="text-muted-foreground">Delivery address</dt>
            <dd className="whitespace-pre-wrap break-words">{value.delivery_address}</dd>
          </div>
        )}
      </dl>
      <ul className="divide-y">
        {value.lines.map((line) => (
          <li key={line.id} className="min-w-0 space-y-2 py-3 text-sm">
            <p className="break-words font-medium">
              {line.item_code || `Line ${line.line_number}`}
              {line.item_name || line.description ? ` - ${line.item_name || line.description}` : ''}
            </p>
            <dl className="grid gap-2 sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">Quantity</dt>
                <dd>{formatQuantity(line.quantity, line.uom_code ?? undefined)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Unit price</dt>
                <dd>{formatMoney(line.unit_price, { currency })}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Discount</dt>
                <dd>
                  {formatMoney(line.discount, { currency })} (
                  {formatQuantity(line.discount_percentage)}%)
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Tax rate</dt>
                <dd>{formatQuantity(line.tax_rate)}%</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Required date</dt>
                <dd>{formatDate(line.required_date)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">QC on receipt</dt>
                <dd>{line.requires_qc ? 'Required' : 'Not required'}</dd>
              </div>
            </dl>
            {Boolean(line.source_allocations?.length) && (
              <ul
                className="space-y-1 text-muted-foreground"
                aria-label="Source requisition quantities"
              >
                {line.source_allocations?.map((allocation) => (
                  <li key={allocation.requisition_line_id}>
                    {allocation.requisition_code || 'Requisition'}
                    {allocation.requisition_line_number == null
                      ? ''
                      : ` line ${allocation.requisition_line_number}`}
                    : {formatQuantity(allocation.quantity)}
                    {allocation.is_active === false ? ' (released)' : ''}
                  </li>
                ))}
              </ul>
            )}
            {line.notes && (
              <p className="whitespace-pre-wrap break-words text-muted-foreground">{line.notes}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

/** PO viewers read a bounded revision projection without needing generic audit permissions. */
export default function PurchaseOrderAmendmentsPanel({
  orderId,
  currency,
}: {
  orderId: string
  currency: string
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const history = useInfiniteQuery({
    queryKey: purchaseOrderChangeKeys.amendments(orderId),
    queryFn: ({ pageParam, signal }) =>
      api.getCursorPage<PurchaseOrderAmendment>(
        `/procurement/purchase-orders/${orderId}/amendments`,
        {
          params: { limit: DEFAULT_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) },
          signal,
        },
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) =>
      last.pagination.has_next ? (last.pagination.next_cursor ?? undefined) : undefined,
    staleTime: staleTimes.transactional,
    meta: { suppressErrorToast: true },
  })
  const rows = history.data?.pages.flatMap((page) => page.data) ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle>Amendment history</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {history.isPending && <PanelLoader />}
        {history.error && (
          <Alert variant="destructive">
            <AlertTitle>Could not load amendment history</AlertTitle>
            <AlertDescription>{describeError(history.error)}</AlertDescription>
            <Button className="mt-3" variant="outline" onClick={() => void history.refetch()}>
              Try again
            </Button>
          </Alert>
        )}
        {!history.isPending && !history.error && !rows.length && (
          <EmptyState
            title="No amendment events available"
            description="The recorded revision history will appear here."
          />
        )}
        <ol className="space-y-3">
          {rows.map((revision) => (
            <li key={revision.id} className="min-w-0 space-y-3 rounded-lg border p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 space-y-1">
                  <h3 className="font-medium">Rev {revision.amendment_number}</h3>
                  <p className="whitespace-pre-wrap break-words text-sm">{revision.reason}</p>
                  <p className="text-sm text-muted-foreground">
                    {revision.amended_by_name || 'Authorized user'} -{' '}
                    {formatDateTime(revision.amended_at)}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  aria-expanded={expanded === revision.id}
                  onClick={() =>
                    setExpanded((current) => (current === revision.id ? null : revision.id))
                  }
                >
                  {expanded === revision.id ? 'Hide' : 'Show'} revision {revision.amendment_number}{' '}
                  changes
                </Button>
              </div>
              {expanded === revision.id && (
                <div className="grid min-w-0 gap-3 lg:grid-cols-2">
                  <Snapshot
                    value={revision.old_values}
                    currency={currency}
                    label="Before revision"
                  />
                  <Snapshot
                    value={revision.new_values}
                    currency={currency}
                    label="After revision"
                  />
                </div>
              )}
            </li>
          ))}
        </ol>
        {history.hasNextPage && (
          <Button
            variant="outline"
            loading={history.isFetchingNextPage}
            onClick={() => void history.fetchNextPage()}
          >
            Load older amendments
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

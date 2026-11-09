import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { LoadError, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney } from '@/lib/format'
import { CURSOR_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

type WorklistRow = SchemaOf<'WorklistRow'>
type FollowUpSummary = SchemaOf<'FollowUpSummary'>

export function CustomerCollectionsPanel({ customerId }: { customerId: string }) {
  const filters = { customer_id: customerId }
  const worklist = useQuery({
    queryKey: queryKeys.collections.worklist(filters),
    queryFn: () =>
      api.getPage<WorklistRow>('/collections/worklist', {
        params: { ...filters, limit: CURSOR_PAGE_SIZE },
      }),
    staleTime: staleTimes.transactional,
  })
  const followUps = useQuery({
    queryKey: queryKeys.collections.followUps({ ...filters, live_only: true }),
    queryFn: () =>
      api.getPage<FollowUpSummary>('/collections/follow-ups', {
        params: { ...filters, live_only: true, limit: CURSOR_PAGE_SIZE },
      }),
    staleTime: staleTimes.transactional,
  })

  const error = worklist.error ?? followUps.error
  const overdueRows = worklist.data?.data ?? []
  const activeRows = followUps.data?.data ?? []

  return (
    <div className="space-y-4">
      {error ? (
        <LoadError
          error={error}
          onRetry={() => void Promise.all([worklist.refetch(), followUps.refetch()])}
        />
      ) : worklist.isLoading || followUps.isLoading ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading collections position…
        </p>
      ) : overdueRows.length === 0 && activeRows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No overdue balances or active follow-ups.</p>
      ) : (
        <>
          {overdueRows.map((row) => (
            <Card key={`${row.customer_id}:${row.currency}`}>
              <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-4">
                <div>
                  <p className="font-medium">
                    {formatMoney(row.overdue_amount, { currency: row.currency })} overdue
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {row.overdue_invoice_count} invoice(s) · oldest {row.oldest_overdue_days} days
                    overdue
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={row.status} />
                  <Button variant="outline" size="sm" asChild>
                    <Link to={`/finance/collections?customer_id=${customerId}`}>
                      {row.follow_up_code ? 'Open follow-up' : 'Start follow-up'}
                    </Link>
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
          {activeRows.map((row) => (
            <Card key={row.id}>
              <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-4">
                <div>
                  <p className="font-medium">
                    {row.code} · {row.currency}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Next action {formatDate(row.next_action_date)} ·{' '}
                    {row.assigned_to_name ?? 'Unassigned'}
                  </p>
                  {row.last_note && (
                    <p className="mt-1 max-w-xl whitespace-pre-wrap text-xs text-muted-foreground">
                      Last note
                      {row.last_note_at ? ` · ${formatDate(row.last_note_at)}` : ''}:{' '}
                      {row.last_note}
                    </p>
                  )}
                </div>
                <StatusBadge status={row.status} />
              </CardContent>
            </Card>
          ))}
        </>
      )}
      <Button variant="link" className="h-auto p-0" asChild>
        <Link to={`/finance/collections?customer_id=${customerId}`}>
          View full collections history
        </Link>
      </Button>
    </div>
  )
}

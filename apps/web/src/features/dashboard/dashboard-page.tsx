import { describeError } from '@/lib/messages'
/**
 * Management dashboard.
 *
 * Every number here comes from `GET /reports/dashboard`, which reads the summary
 * tables Celery refreshes (docs/database.md section 9). The page never
 * aggregates hot transactional tables itself, and it never recomputes a tile in
 * the browser.
 */

import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Boxes,
  CalendarClock,
  ClipboardCheck,
  ClipboardList,
  Factory,
  Inbox,
  PackageX,
  RefreshCw,
  ShieldCheck,
  ShoppingCart,
  Truck,
} from 'lucide-react'
import { Link } from 'react-router-dom'

import { EmptyState, PageHeader, StatTile, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Skeleton,
} from '@/components/ui/primitives'
import { entityLink } from '@/features/audit/audit-types'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { formatDateTime, formatMoney, formatNumber, formatRelative, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString } from '@/types/api'

// ---------------------------------------------------------- wire shapes ----

/**
 * `GET /reports/dashboard` (`DashboardSummary`), verbatim.
 *
 * The payload is flat. It carried a `metrics` object and two chart series in an
 * earlier shape; both are gone, and there is no other endpoint that serves the
 * daily stock-movement or open-order-value series, so the two chart cards that
 * read them were removed rather than left permanently empty.
 */
interface DashboardPayload {
  open_sales_orders: number
  overdue_sales_orders: number
  pending_purchase_orders: number
  pending_purchase_value: DecimalString
  production_in_progress: number
  qc_pending: number
  dispatches_in_transit: number
  overdue_deliveries: number
  stock_value: DecimalString
  /** When the stock valuation summary behind `stock_value` was last rebuilt. */
  stock_refreshed_at?: string | null
  /** When these figures were computed. */
  generated_at: string
}

/** One row of `GET /audit/approvals` (`ApprovalRequestRead`). */
interface ApprovalRequestRow {
  id: string
  entity_type: string
  entity_id: string
  entity_code: string | null
  requested_action: string
  reason: string | null
  status: string
  created_at: string
  /** False when the queue lists it but this user may not decide it. */
  can_decide?: boolean
  blocked_reason?: string | null
  required_permission?: string | null
}

interface AuditLogRow {
  id: string
  action: string
  entity_type: string
  entity_id: string | null
  entity_code: string | null
  summary: string
  actor_label: string | null
  created_at: string
}

// ------------------------------------------------------------- constants ----

/**
 * Holding any one of these means an approval queue can contain work for this
 * user, so the "Needs your attention" panel is worth showing. The API still
 * decides what is actually in the queue - this only hides a panel that would be
 * permanently empty for, say, a store keeper.
 *
 * Derived by the same rule `GET /audit/approvals` guards itself with
 * (`DECIDABLE_PERMISSIONS` in `audit/query_service.py`: every right ending in
 * `.approve` or `_approve`) rather than listed by hand. The hand-written list
 * disagreed with it in both directions: it named `qc.decide`, which the
 * endpoint refuses, so a QC inspector was shown the panel and then a 403; and
 * it omitted `payment.approve`, so a payment approver never saw their queue.
 */
const APPROVAL_PERMISSIONS: string[] = Object.values(P).filter(
  (value) => value.endsWith('.approve') || value.endsWith('_approve'),
)

/**
 * Where an audit or approval row's record lives.
 *
 * `entityLink` is the one entity-type-to-route map, shared with the audit
 * screen so the two cannot drift - a dashboard copy of it had already fallen
 * ten record types behind. A document with no dedicated screen is still
 * reachable through the audit trail's entity history - better than a dead link.
 *
 * This used to hand the audit screen a `?q=<code>` free-text search. That
 * parameter is not one `GET /audit/logs` accepts, so the link landed on an
 * unfiltered trail; the entity-history mode addresses the record exactly.
 */
function documentLink(entityType: string, entityId: string | null): string {
  const link = entityLink(entityType, entityId)
  if (link) return link
  if (!entityId) return '/audit'
  const params = new URLSearchParams({
    mode: 'entity',
    entity_type: entityType,
    entity_id: entityId,
  })
  return `/audit?${params.toString()}`
}

// ------------------------------------------------------------------ page ----

export default function DashboardPage() {
  const { user, can, canAny } = useAuth()
  // `GET /reports/dashboard` declares `dashboard.view`, and sign-in lands here
  // by default, so the summary must not be requested without it: every visit
  // to `/` by an account lacking the right answered with a 403 and a "Not
  // permitted" toast. The index route is deliberately not wrapped in a guard -
  // the denied page's only affordance is "Back to dashboard", which would loop.
  const canSeeSummary = can(P.DASHBOARD_VIEW)
  const showApprovals = canAny(...APPROVAL_PERMISSIONS)
  const canSeeActivity = can(P.AUDIT_LOG_VIEW)

  const dashboard = useQuery({
    queryKey: queryKeys.reports.dashboard,
    queryFn: () => api.get<DashboardPayload>('/reports/dashboard'),
    staleTime: staleTimes.dashboard,
    enabled: canSeeSummary,
  })

  // Panels below are deliberately not DataTables: they are fixed-size previews
  // of a queue, capped server-side by `limit`, with a link to the full list
  // screen. Nothing is fetched in bulk and filtered in the browser.
  // The endpoint is "approvals waiting for me" and is pending-only by
  // definition; it takes no `status` parameter.
  const approvalsParams = { limit: 6 }
  const approvals = useQuery({
    queryKey: queryKeys.audit.approvals(approvalsParams),
    // The API scopes the queue to what this user is entitled to decide.
    queryFn: () => api.getPage<ApprovalRequestRow>('/audit/approvals', { params: approvalsParams }),
    staleTime: staleTimes.dashboard,
    enabled: showApprovals,
  })

  const activityParams = { limit: 8 }
  const activity = useQuery({
    queryKey: queryKeys.audit.logs(activityParams),
    queryFn: () => api.getCursorPage<AuditLogRow>('/audit/logs', { params: activityParams }),
    staleTime: staleTimes.dashboard,
    enabled: canSeeActivity,
  })

  const metrics = dashboard.data
  const isLoading = dashboard.isLoading

  // `refetch()` ignores `enabled`, so every gate is restated here.
  const refreshAll = () => {
    if (canSeeSummary) void dashboard.refetch()
    if (showApprovals) void approvals.refetch()
    if (canSeeActivity) void activity.refetch()
  }

  const tiles = [
    {
      key: 'open-sales-orders',
      label: 'Open sales orders',
      value: formatNumber(metrics?.open_sales_orders),
      hint: 'Confirmed, not yet dispatched',
      icon: ClipboardList,
      tone: 'default' as const,
      to: '/sales-orders',
      permissions: [P.SALES_ORDER_VIEW],
    },
    {
      key: 'stock-value',
      label: 'Stock value',
      value: formatMoney(metrics?.stock_value, { compact: true }),
      // The valuation summary is rebuilt on its own schedule, so the figure can
      // legitimately be older than the rest of the page. Saying when avoids it
      // being read as a live number.
      hint: metrics?.stock_refreshed_at
        ? `On-hand at valuation, ${formatRelative(metrics.stock_refreshed_at)}`
        : 'On-hand at current valuation',
      icon: Boxes,
      tone: 'default' as const,
      to: '/inventory',
      permissions: [P.INVENTORY_VALUATION_VIEW, P.INVENTORY_VIEW],
    },
    {
      key: 'po-approvals',
      label: 'POs awaiting approval',
      value: formatNumber(metrics?.pending_purchase_orders),
      // The committed value is the number that decides whether this queue is
      // urgent: six orders worth a lakh is not six worth a crore.
      hint: metrics
        ? `${formatMoney(metrics.pending_purchase_value, { compact: true })} committed`
        : 'Blocking procurement',
      icon: ShoppingCart,
      tone: 'warning' as const,
      to: '/procurement/purchase-orders',
      permissions: [P.PURCHASE_ORDER_VIEW],
    },
    {
      key: 'production-in-progress',
      label: 'Production in progress',
      value: formatNumber(metrics?.production_in_progress),
      hint: 'Released or on the shop floor',
      icon: Factory,
      tone: 'default' as const,
      to: '/production',
      permissions: [P.PRODUCTION_ORDER_VIEW],
    },
    {
      key: 'qc-pending',
      label: 'QC pending',
      value: formatNumber(metrics?.qc_pending),
      hint: 'Stock held until inspected',
      icon: ClipboardCheck,
      tone: 'warning' as const,
      to: '/quality',
      permissions: [P.QC_VIEW],
    },
    {
      key: 'dispatches-in-transit',
      label: 'Dispatches in transit',
      value: formatNumber(metrics?.dispatches_in_transit),
      hint: 'Shipped, not yet delivered',
      icon: Truck,
      tone: 'default' as const,
      to: '/dispatch',
      permissions: [P.DISPATCH_VIEW],
    },
    {
      key: 'overdue-deliveries',
      label: 'Overdue deliveries',
      value: formatNumber(metrics?.overdue_deliveries),
      hint: 'Past the promised date',
      icon: CalendarClock,
      tone: 'destructive' as const,
      to: '/dispatch',
      permissions: [P.DISPATCH_VIEW],
    },
    {
      // Replaces the MRP shortage count, which the summary no longer carries.
      key: 'overdue-sales-orders',
      label: 'Overdue sales orders',
      value: formatNumber(metrics?.overdue_sales_orders),
      hint: 'Past their promised date',
      icon: PackageX,
      tone: 'destructive' as const,
      to: '/sales-orders',
      permissions: [P.SALES_ORDER_VIEW],
    },
    // A tile linking to a screen the user cannot open is a dead end, so tiles
    // follow the same view permissions as the sidebar. UX, not security. Every
    // figure comes from the gated summary, so without `dashboard.view` a tile
    // could only ever show a dash.
  ].filter((tile) => canSeeSummary && canAny(...tile.permissions))

  const firstName = user?.full_name?.split(' ')[0]

  return (
    <div className="space-y-6">
      <PageHeader
        title={firstName ? `Good day, ${firstName}` : 'Dashboard'}
        description={
          dashboard.data
            ? `Summary tables last refreshed ${formatRelative(dashboard.data.generated_at)}.`
            : 'Live operating picture across sales, stock, procurement and production.'
        }
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={refreshAll}
            loading={dashboard.isFetching}
            aria-label="Refresh dashboard"
          >
            <RefreshCw />
            Refresh
          </Button>
        }
      />

      {dashboard.error && !dashboard.data && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the dashboard</AlertTitle>
          <AlertDescription>
            <p>{describeError(dashboard.error)}</p>
            <Button variant="outline" size="sm" className="mt-3" onClick={refreshAll}>
              <RefreshCw />
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {/* ------------------------------------------------------------ KPIs -- */}
      {tiles.length > 0 && (
        <section aria-label="Key figures">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {tiles.map((tile) => (
              <StatTile
                key={tile.key}
                label={tile.label}
                value={tile.value}
                hint={tile.hint}
                icon={tile.icon}
                tone={tile.tone}
                to={tile.to}
                isLoading={isLoading}
              />
            ))}
          </div>
        </section>
      )}

      {/* -------------------------------------------------------- approvals -- */}
      {showApprovals && (
        <Card>
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 space-y-0 pb-2">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2 text-sm">
                <ShieldCheck className="h-4 w-4 text-warning" aria-hidden />
                Needs your attention
              </CardTitle>
              <p className="text-xs text-muted-foreground">
                Documents waiting on a decision from you.
              </p>
            </div>
            {/* No "All approvals" link: nothing in the SPA lists the queue
                beyond this preview, and the audit trail it used to point at is
                gated on `audit_log.view`, which an approver need not hold. */}
          </CardHeader>
          <CardContent>
            {approvals.isLoading ? (
              <ul className="space-y-2">
                {Array.from({ length: 3 }).map((_, index) => (
                  <li key={index}>
                    <Skeleton className="h-12 w-full" />
                  </li>
                ))}
              </ul>
            ) : approvals.error ? (
              <Alert variant="warning">
                <AlertTitle>Approval queue unavailable</AlertTitle>
                <AlertDescription>{describeError(approvals.error)}</AlertDescription>
              </Alert>
            ) : (approvals.data?.data.length ?? 0) === 0 ? (
              <EmptyState
                icon={ClipboardCheck}
                title="Nothing waiting on you"
                description="Approvals raised for your roles will appear here."
              />
            ) : (
              <ul className="divide-y">
                {approvals.data?.data.map((request) => (
                  <li key={request.id}>
                    <Link
                      to={documentLink(request.entity_type, request.entity_id)}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-2.5 -mx-2 hover:bg-muted/50"
                    >
                      <span className="font-medium">
                        {request.entity_code ?? humanise(request.entity_type)}
                      </span>
                      <StatusBadge status={request.status} />
                      {/* The queue lists everything raised against this user's
                          roles, including requests they are not themselves
                          entitled to decide. Saying so here saves opening the
                          document to find the button missing. */}
                      {request.can_decide === false && (
                        <Badge variant="muted" title={request.blocked_reason ?? undefined}>
                          Not yours to decide
                        </Badge>
                      )}
                      <span className="text-sm text-muted-foreground">
                        {humanise(request.requested_action)} &middot;{' '}
                        {humanise(request.entity_type)}
                      </span>
                      {request.reason && (
                        <span className="w-full truncate text-xs text-muted-foreground">
                          {request.reason}
                        </span>
                      )}
                      <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                        {formatRelative(request.created_at)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {/* --------------------------------------------------------- activity -- */}
      {canSeeActivity && (
        <Card>
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 space-y-0 pb-2">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Activity className="h-4 w-4 text-muted-foreground" aria-hidden />
                Recent activity
              </CardTitle>
              <p className="text-xs text-muted-foreground">
                The last eight recorded actions across the company.
              </p>
            </div>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/audit">
                Audit trail
                <ArrowRight />
              </Link>
            </Button>
          </CardHeader>
          <CardContent>
            {activity.isLoading ? (
              <ul className="space-y-2">
                {Array.from({ length: 4 }).map((_, index) => (
                  <li key={index}>
                    <Skeleton className="h-10 w-full" />
                  </li>
                ))}
              </ul>
            ) : activity.error ? (
              <Alert variant="warning">
                <AlertTitle>Activity unavailable</AlertTitle>
                <AlertDescription>{describeError(activity.error)}</AlertDescription>
              </Alert>
            ) : (activity.data?.data.length ?? 0) === 0 ? (
              <EmptyState
                icon={Inbox}
                title="No activity yet"
                description="Every business action is recorded here as soon as work begins."
              />
            ) : (
              <ul className="divide-y">
                {activity.data?.data.map((log) => (
                  <li key={log.id}>
                    <Link
                      to={documentLink(log.entity_type, log.entity_id)}
                      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-md px-2 py-2 -mx-2 hover:bg-muted/50"
                    >
                      <span className="font-medium">{humanise(log.action)}</span>
                      <span className="text-sm text-muted-foreground">
                        {log.entity_code ?? humanise(log.entity_type)}
                      </span>
                      <span className="w-full truncate text-xs text-muted-foreground">
                        {log.summary}
                        {log.actor_label ? ` - ${log.actor_label}` : ''}
                      </span>
                      <time
                        dateTime={log.created_at}
                        title={formatDateTime(log.created_at)}
                        className="ml-auto shrink-0 text-xs text-muted-foreground"
                      >
                        {formatRelative(log.created_at)}
                      </time>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {tiles.length === 0 && !showApprovals && !canSeeActivity && (
        <EmptyState
          icon={AlertTriangle}
          title="Nothing to show on your dashboard"
          description="Your account does not hold the permissions these panels need. An administrator can grant them."
        />
      )}
    </div>
  )
}

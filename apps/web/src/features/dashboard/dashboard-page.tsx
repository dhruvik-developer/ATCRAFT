import { describeError } from '@/lib/messages'
/**
 * Management dashboard.
 *
 * Every number here comes from `GET /reports/dashboard`, which reads indexed
 * counts and the summary tables Celery refreshes (docs/database.md section 9).
 * The page never aggregates hot transactional tables itself, and it never
 * recomputes a tile in the browser.
 *
 * The 30-day sparklines come from `GET /reports/dashboard/trends`, a separate
 * request so the tiles paint without waiting for four aggregate series. Both
 * endpoints are gated on `dashboard.view`; the finance and operations tiles are
 * additionally scoped by the API to the permission that owns their documents,
 * and a withheld tile arrives as `null` rather than as a zero.
 */

import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Banknote,
  Boxes,
  CalendarClock,
  CircleDollarSign,
  ClipboardCheck,
  ClipboardList,
  Clock,
  Factory,
  Inbox,
  PackageSearch,
  PackageX,
  Receipt,
  RefreshCw,
  ShieldCheck,
  ShoppingCart,
  Truck,
  Wallet,
  Wrench,
} from 'lucide-react'
import { Link } from 'react-router-dom'

import { EmptyState, LoadError, PageHeader, StatTile, StatusBadge } from '@/components/shared'
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
import {
  formatDateTime,
  formatMoney,
  formatNumber,
  formatQuantity,
  formatRelative,
  humanise,
  toDecimal,
} from '@/lib/format'
import { APPROVAL_PERMISSIONS, P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString } from '@/types/api'

// ---------------------------------------------------------- wire shapes ----

/**
 * `GET /reports/dashboard` (`DashboardRead`), verbatim.
 *
 * The payload is flat. It carried a `metrics` object and two chart series in an
 * earlier shape; both are gone. The 30-day series now has its own endpoint,
 * `GET /reports/dashboard/trends`, so the counts and the sparklines can be
 * cached and permission-scoped independently.
 *
 * The finance and operations fields are `null` when this account lacks the
 * permission that owns the documents behind them. `null` is not zero: a zero is
 * a fact about the business, a null is "not shown to you", and the tiles below
 * are dropped rather than drawn as an empty queue.
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
  /** Exactly `PENDING_APPROVAL`: the queue that actually blocks buying. */
  purchase_orders_awaiting_approval?: number | null
  inbound_purchase_orders_late?: number | null
  payables_due_amount?: DecimalString | null
  payables_overdue_amount?: DecimalString | null
  receivables_amount?: DecimalString | null
  receivables_outstanding?: DecimalString | null
  receivables_overdue?: DecimalString | null
  follow_ups_due?: number | null
  invoiced_sales_mtd?: DecimalString | null
  collections_mtd?: DecimalString | null
  mrp_shortage_count?: number | null
  job_work_overdue?: number | null
  /** When these figures were computed. */
  generated_at: string
}

interface DashboardTrendPoint {
  day: string
  value: DecimalString
}

interface DashboardTrendSeries {
  series: string
  points: DashboardTrendPoint[]
}

/** `GET /reports/dashboard/trends` (`DashboardTrendsRead`). */
interface DashboardTrendsPayload {
  date_from: string
  date_to: string
  series: DashboardTrendSeries[]
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

// --------------------------------------------------------------- trends ----

/**
 * The four sparkline series, in display order, with the label and formatter each
 * one needs. The API returns the series it thinks this caller may read, so this
 * table is presentation only - a series missing from the payload is not drawn,
 * and never drawn as an empty line.
 */
const TREND_SERIES: {
  key: string
  label: string
  format: (value: DecimalString) => string
}[] = [
  {
    key: 'sales_invoiced',
    label: 'Sales invoiced',
    format: (value) => formatMoney(value, { compact: true }),
  },
  {
    key: 'collections',
    label: 'Collections',
    format: (value) => formatMoney(value, { compact: true }),
  },
  {
    key: 'dispatches',
    label: 'Dispatches',
    format: (value) => formatQuantity(value),
  },
  {
    key: 'production_output',
    label: 'Production output',
    format: (value) => formatQuantity(value),
  },
]

const SPARK_WIDTH = 132
const SPARK_HEIGHT = 34
const SPARK_PADDING = 2

/**
 * A small inline SVG line chart.
 *
 * Not `recharts`: this is thirty points and one path, and the chart library
 * costs a chunk boundary for a shape a dozen lines of SVG draw. It is also the
 * version that can be read by a test without a canvas or a resize observer.
 *
 * A flat series - thirty days of zero - is drawn along the middle rather than
 * the bottom, because a line pinned to the axis reads as missing data. The
 * `aria-label` carries the range and the total, which is what a reader who
 * cannot see the line needs from it.
 */
function Sparkline({
  points,
  label,
  total,
}: {
  points: DashboardTrendPoint[]
  label: string
  total: string
}) {
  const values = points.map((point) => toDecimal(point.value))
  const highest = values.reduce(
    (max, value) => (value.greaterThan(max) ? value : max),
    toDecimal(0),
  )
  const lowest = values.reduce((min, value) => (value.lessThan(min) ? value : min), toDecimal(0))
  const span = highest.minus(lowest)
  const step = values.length > 1 ? (SPARK_WIDTH - SPARK_PADDING * 2) / (values.length - 1) : 0

  const path = values
    .map((value, index) => {
      const ratio = span.isZero() ? 0.5 : value.minus(lowest).dividedBy(span).toNumber()
      const x = SPARK_PADDING + index * step
      const y = SPARK_HEIGHT - SPARK_PADDING - ratio * (SPARK_HEIGHT - SPARK_PADDING * 2)
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <svg
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      className="h-9 w-full text-primary"
      role="img"
      aria-label={`${label}: ${total} over the last ${points.length} days`}
      preserveAspectRatio="none"
    >
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}

function TrendCards({
  trends,
  isLoading,
}: {
  trends: DashboardTrendsPayload | undefined
  isLoading: boolean
}) {
  const series = trends?.series ?? []
  const bySeries = new Map(series.map((entry) => [entry.series, entry]))

  if (isLoading && series.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Last 30 days</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
            {TREND_SERIES.map((config) => (
              <div key={config.key}>
                <p className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {config.label}
                </p>
                <Skeleton className="mt-1 h-9 w-full" />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    )
  }

  // No series at all means this account may read none of the four. The card is
  // absent rather than a wall of four empty lines.
  if (series.length === 0) return null

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Last 30 days</CardTitle>
        <p className="text-xs text-muted-foreground">
          One figure per day, from the documents posted on that day. A day with no activity is zero,
          not missing.
        </p>
      </CardHeader>
      <CardContent>
        <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
          {TREND_SERIES.map((config) => {
            const entry = bySeries.get(config.key)
            if (!entry) return null
            const total = entry.points.reduce(
              (sum, point) => sum.plus(toDecimal(point.value)),
              toDecimal(0),
            )
            const latest =
              entry.points.length > 0 ? (entry.points[entry.points.length - 1]?.value ?? '0') : '0'
            const totalText = total.toDecimalPlaces(2).toFixed()
            return (
              <div key={config.key}>
                <div className="flex items-baseline justify-between gap-2">
                  <p className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    {config.label}
                  </p>
                  <p className="shrink-0 text-sm font-semibold tabular-nums">
                    {config.format(latest)}
                  </p>
                </div>
                <Sparkline
                  points={entry.points}
                  label={config.label}
                  total={config.format(totalText)}
                />
                <p className="text-2xs text-muted-foreground">
                  Latest day, {config.format(totalText)} in total
                </p>
              </div>
            )
          })}
        </div>
      </CardContent>
    </Card>
  )
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
    queryFn: ({ signal }) => api.get<DashboardPayload>('/reports/dashboard', { signal }),
    staleTime: staleTimes.dashboard,
    enabled: canSeeSummary,
  })

  /**
   * The 30-day series behind the sparklines.
   *
   * A separate request because it is a different question with a different
   * cache key and its own permission mask - and because the tiles must not wait
   * for thirty days of aggregates to be assembled. It is fetched under the same
   * `dashboard.view` right; the API decides which series this caller may read
   * and omits the rest.
   */
  const trends = useQuery({
    queryKey: queryKeys.reports.trends,
    queryFn: ({ signal }) =>
      api.get<DashboardTrendsPayload>('/reports/dashboard/trends', { signal }),
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
    queryFn: ({ signal }) =>
      api.getPage<ApprovalRequestRow>('/audit/approvals', { params: approvalsParams, signal }),
    staleTime: staleTimes.dashboard,
    enabled: showApprovals,
  })

  const activityParams = { limit: 8 }
  const activity = useQuery({
    queryKey: queryKeys.audit.logs(activityParams),
    queryFn: ({ signal }) =>
      api.getCursorPage<AuditLogRow>('/audit/logs', { params: activityParams, signal }),
    staleTime: staleTimes.dashboard,
    enabled: canSeeActivity,
  })

  const metrics = dashboard.data
  const isLoading = dashboard.isLoading

  // `refetch()` ignores `enabled`, so every gate is restated here.
  const refreshAll = () => {
    if (canSeeSummary) void dashboard.refetch()
    if (canSeeSummary) void trends.refetch()
    if (showApprovals) void approvals.refetch()
    if (canSeeActivity) void activity.refetch()
  }

  /**
   * Whether a gated tile has a figure to draw.
   *
   * Before the payload arrives the tile is a skeleton, which is honest: the
   * figure exists, it just has not been read yet. Once it has arrived, `null`
   * means the API withheld the figure because this account lacks the right that
   * owns the documents, so the tile is dropped. A cached payload that predates
   * the field also reads as absent, which drops the tile rather than inventing
   * a zero.
   */
  const shows = (value: unknown) => isLoading || (value !== null && value !== undefined)

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
      visible: true,
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
      visible: true,
    },
    {
      // This tile used to be labelled "POs awaiting approval" while counting
      // every open order, so a store keeper read a committed pipeline as a
      // decision queue. It now says what it counts; the queue has its own tile
      // below, fed by `purchase_orders_awaiting_approval`.
      key: 'open-purchase-orders',
      label: 'Open purchase orders',
      value: formatNumber(metrics?.pending_purchase_orders),
      // The committed value is the number that decides whether this pipeline is
      // urgent: six orders worth a lakh is not six worth a crore.
      hint: metrics
        ? `${formatMoney(metrics.pending_purchase_value, { compact: true })} committed`
        : 'Approved and not yet fully received',
      icon: ShoppingCart,
      tone: 'default' as const,
      to: '/procurement/purchase-orders',
      permissions: [P.PURCHASE_ORDER_VIEW],
      visible: true,
    },
    {
      key: 'po-approvals',
      label: 'POs awaiting approval',
      value: formatNumber(metrics?.purchase_orders_awaiting_approval ?? undefined),
      hint: 'Waiting on a decision before they can be sent',
      icon: ShieldCheck,
      tone: 'warning' as const,
      to: '/procurement/purchase-orders',
      permissions: [P.PURCHASE_ORDER_VIEW],
      visible: shows(metrics?.purchase_orders_awaiting_approval),
    },
    {
      key: 'inbound-purchase-orders-late',
      label: 'Inbound POs late',
      value: formatNumber(metrics?.inbound_purchase_orders_late ?? undefined),
      hint: 'Past the promised delivery date',
      icon: Clock,
      tone: 'destructive' as const,
      to: '/procurement/inbound-tracking',
      permissions: [P.PURCHASE_ORDER_VIEW],
      visible: shows(metrics?.inbound_purchase_orders_late),
    },
    {
      key: 'payables-due',
      label: 'Payables due',
      value: formatMoney(metrics?.payables_due_amount, { compact: true }),
      hint: 'Due in the next seven days',
      icon: Wallet,
      tone: 'warning' as const,
      to: '/finance/vendor-invoices',
      permissions: [P.VENDOR_INVOICE_VIEW],
      visible: shows(metrics?.payables_due_amount),
    },
    {
      key: 'payables-overdue',
      label: 'Payables overdue',
      value: formatMoney(metrics?.payables_overdue_amount, { compact: true }),
      hint: 'Past their due date',
      icon: AlertTriangle,
      tone: 'destructive' as const,
      to: '/finance/vendor-invoices',
      permissions: [P.VENDOR_INVOICE_VIEW],
      visible: shows(metrics?.payables_overdue_amount),
    },
    {
      key: 'receivables',
      label: 'Receivables',
      value: formatMoney(metrics?.receivables_outstanding ?? metrics?.receivables_amount, {
        compact: true,
      }),
      hint: 'Outstanding from customers',
      icon: CircleDollarSign,
      tone: 'default' as const,
      to: '/finance',
      permissions: [P.SALES_INVOICE_VIEW],
      visible: shows(metrics?.receivables_outstanding ?? metrics?.receivables_amount),
    },
    {
      key: 'receivables-overdue',
      label: 'Overdue receivables',
      value: formatMoney(metrics?.receivables_overdue, { compact: true }),
      hint: 'Past their invoice due dates',
      icon: CircleDollarSign,
      tone: 'destructive' as const,
      to: '/finance/collections',
      permissions: [P.COLLECTION_VIEW],
      visible: shows(metrics?.receivables_overdue),
    },
    {
      key: 'follow-ups-due',
      label: 'Follow-ups due',
      value: formatNumber(metrics?.follow_ups_due ?? undefined),
      hint: 'Next action due today or earlier',
      icon: CalendarClock,
      tone: 'warning' as const,
      to: '/finance/collections?view=follow-ups',
      permissions: [P.COLLECTION_VIEW],
      visible: shows(metrics?.follow_ups_due),
    },
    {
      key: 'invoiced-sales-mtd',
      label: 'Invoiced sales MTD',
      value: formatMoney(metrics?.invoiced_sales_mtd, { compact: true }),
      hint: 'Month to date, by invoice date',
      icon: Receipt,
      tone: 'default' as const,
      to: '/finance/sales-invoices',
      permissions: [P.SALES_INVOICE_VIEW],
      visible: shows(metrics?.invoiced_sales_mtd),
    },
    {
      key: 'collections-mtd',
      label: 'Collections MTD',
      value: formatMoney(metrics?.collections_mtd, { compact: true }),
      hint: 'Month to date, by receipt date',
      icon: Banknote,
      tone: 'success' as const,
      to: '/finance/payments',
      permissions: [P.PAYMENT_VIEW],
      visible: shows(metrics?.collections_mtd),
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
      visible: true,
    },
    {
      key: 'mrp-shortages',
      label: 'MRP shortages',
      value: formatNumber(metrics?.mrp_shortage_count ?? undefined),
      hint: 'Items short in the last MRP run',
      icon: PackageSearch,
      tone: 'warning' as const,
      to: '/mrp',
      permissions: [P.MRP_VIEW],
      visible: shows(metrics?.mrp_shortage_count),
    },
    {
      key: 'job-work-overdue',
      label: 'Job work overdue',
      value: formatNumber(metrics?.job_work_overdue ?? undefined),
      hint: 'Challans past their expected return',
      icon: Wrench,
      tone: 'destructive' as const,
      to: '/job-work',
      permissions: [P.JOB_WORK_VIEW],
      visible: shows(metrics?.job_work_overdue),
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
      visible: true,
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
      visible: true,
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
      visible: true,
    },
    {
      key: 'overdue-sales-orders',
      label: 'Overdue sales orders',
      value: formatNumber(metrics?.overdue_sales_orders),
      hint: 'Past their promised date',
      icon: PackageX,
      tone: 'destructive' as const,
      to: '/sales-orders',
      permissions: [P.SALES_ORDER_VIEW],
      visible: true,
    },
    // A tile linking to a screen the user cannot open is a dead end, so tiles
    // follow the same view permissions as the sidebar. UX, not security. Every
    // figure comes from the gated summary, so without `dashboard.view` a tile
    // could only ever show a dash.
  ].filter((tile) => canSeeSummary && tile.visible && canAny(...tile.permissions))

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
        <LoadError
          title="Could not load the dashboard"
          error={dashboard.error}
          onRetry={refreshAll}
        />
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

      {/* ---------------------------------------------------------- trends -- */}
      {canSeeSummary && <TrendCards trends={trends.data} isLoading={trends.isLoading} />}

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
            <Link
              to="/approvals"
              className="text-sm font-medium text-primary underline-offset-4 hover:underline"
            >
              View all
            </Link>
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

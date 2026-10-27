import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useState, useSyncExternalStore, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { EmptyState, PageHeader, PanelLoader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Badge,
  Checkbox,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { reportFieldErrors } from '@/lib/form-errors'
import { formatDate, formatDateTime, formatMoney, formatQuantity, humanise } from '@/lib/format'
import { describeError, empty, fileMessages } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { VendorActionDialog, VendorShipmentDialog, VendorUploadDialog } from './vendor-actions'
import { VendorInvoiceForm } from './vendor-invoice-form'
import {
  canShipOrder,
  orderTab,
  orderTabs,
  type VendorDashboard,
  type VendorDocument,
  type VendorInvoice,
  type VendorInvoiceRow,
  type VendorOrder,
  type VendorOrderResponse,
  type VendorPendingAmendment,
  type VendorOrderRow,
  type VendorPayment,
  type VendorPaymentSummary,
  type VendorProfile,
  type VendorRating,
  type VendorReceipt,
  type VendorShipment,
} from './vendor-model'

const desktopQuery = '(min-width: 768px)'
const subscribeViewport = (listener: () => void) => {
  const media = window.matchMedia(desktopQuery)
  media.addEventListener('change', listener)
  return () => media.removeEventListener('change', listener)
}
const desktopSnapshot = () => window.matchMedia(desktopQuery).matches

function QueryFailure({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-destructive/30 p-4">
      <p className="text-sm text-destructive">{describeError(error)}</p>
      {retry && (
        <Button className="mt-3 min-h-11" variant="outline" onClick={retry}>
          Try again
        </Button>
      )}
    </div>
  )
}
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">{children}</CardContent>
    </Card>
  )
}
function Value({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words text-sm">{children ?? '—'}</dd>
    </div>
  )
}
function CardRows<T>({ rows, render }: { rows: T[]; render: (row: T) => ReactNode }) {
  return (
    <div className="space-y-3">
      {rows.map((row, index) => (
        <div key={index} className="rounded-lg border bg-card p-4">
          {render(row)}
        </div>
      ))}
    </div>
  )
}

function VendorList<T>({
  query,
  columns,
  card,
  title,
  search = false,
  paginationLabel = 'List pages',
}: {
  query: ReturnType<typeof useListQuery<T>>
  columns: ColumnDef<T, unknown>[]
  card: (row: T) => ReactNode
  title: string
  search?: boolean
  paginationLabel?: string
}) {
  const desktop = useSyncExternalStore(subscribeViewport, desktopSnapshot, () => false)
  if (desktop)
    return (
      <DataTable
        columns={columns}
        data={query.data}
        pagination={query.pagination}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onPageChange={query.setPage}
        onLimitChange={query.setLimit}
        onRefresh={() => void query.refetch()}
        {...(search ? { onSearchChange: query.setSearch, searchValue: query.search } : {})}
        emptyTitle={title}
      />
    )
  return (
    <div className="space-y-4">
      {search && (
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            const value = new FormData(event.currentTarget).get('search')
            query.setSearch(String(value ?? ''))
          }}
        >
          <Input
            key={query.search}
            className="min-h-11 min-w-0"
            name="search"
            aria-label="Search orders"
            defaultValue={query.search}
            placeholder="Search orders…"
          />
          <Button className="min-h-11" type="submit">
            Search
          </Button>
        </form>
      )}
      {query.error ? (
        <QueryFailure error={query.error} retry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <PanelLoader />
      ) : query.data.length ? (
        <CardRows rows={query.data} render={card} />
      ) : (
        <EmptyState title={title} description={empty.filtered} />
      )}
      {query.pagination && (
        <nav
          aria-label={paginationLabel}
          className="flex flex-wrap items-center justify-between gap-2"
        >
          <Button
            className="min-h-11"
            variant="outline"
            disabled={!query.pagination.has_previous || query.isFetching}
            onClick={() => query.setPage(query.page - 1)}
          >
            Previous
          </Button>
          <span className="text-sm">Page {query.page}</span>
          <Button
            className="min-h-11"
            variant="outline"
            disabled={!query.pagination.has_next || query.isFetching}
            onClick={() => query.setPage(query.page + 1)}
          >
            Next
          </Button>
        </nav>
      )}
    </div>
  )
}

export function VendorHomePage() {
  const { user, can } = useAuth()
  const me = useQuery({
    queryKey: queryKeys.vendorPortal.me,
    queryFn: ({ signal }) => api.get<VendorProfile>('/vendor-portal/me', { signal }),
  })
  const dashboard = useQuery({
    queryKey: queryKeys.vendorPortal.dashboard,
    queryFn: ({ signal }) => api.get<VendorDashboard>('/vendor-portal/dashboard', { signal }),
  })
  const rating = useQuery({
    queryKey: queryKeys.vendorPortal.rating,
    queryFn: ({ signal }) => api.get<VendorRating>('/vendor-portal/rating', { signal }),
  })
  const tiles = [
    ['To accept', dashboard.data?.to_accept, '/vendor/orders?tab=to_accept'],
    ['Accepted', dashboard.data?.accepted, '/vendor/orders?tab=accepted'],
    ['Ready for dispatch', dashboard.data?.ready, '/vendor/orders?tab=ready'],
    ['In transit', dashboard.data?.in_transit, '/vendor/orders?tab=dispatched'],
    ['Delivered', dashboard.data?.delivered, '/vendor/orders?tab=delivered'],
    [
      'Payment pending',
      dashboard.data?.payment_pending,
      can(P.VENDOR_PORTAL_INVOICE) ? '/vendor/invoices?unpaid_only=1' : null,
    ],
    ['QC rejected', dashboard.data?.qc_rejected, '/vendor/receipts?rejected_only=1'],
  ] as const
  return (
    <div className="space-y-5">
      <PageHeader
        title={`Welcome${user?.full_name ? `, ${user.full_name}` : ''}`}
        description={me.data?.name ?? 'Your vendor workspace'}
      />
      {me.error && <QueryFailure error={me.error} retry={() => void me.refetch()} />}
      {me.data && (
        <p className="text-sm text-muted-foreground">
          Your categories:{' '}
          {me.data.categories.map((category) => category.name).join(', ') ||
            'No category restrictions'}
        </p>
      )}
      {dashboard.error ? (
        <QueryFailure error={dashboard.error} retry={() => void dashboard.refetch()} />
      ) : dashboard.isLoading ? (
        <PanelLoader />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {tiles.map(([label, count, href]) => (
            <Card key={label}>
              {href ? (
                <Link
                  className="block min-h-28 rounded-lg p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  to={href}
                >
                  <span className="block text-sm text-muted-foreground">{label}</span>
                  <span className="mt-2 block text-3xl font-semibold">{count ?? 0}</span>
                </Link>
              ) : (
                <div className="min-h-28 p-4">
                  <span className="block text-sm text-muted-foreground">{label}</span>
                  <span className="mt-2 block text-3xl font-semibold">{count ?? 0}</span>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
      <Section title="Your vendor rating">
        {rating.error ? (
          <QueryFailure error={rating.error} retry={() => void rating.refetch()} />
        ) : rating.isLoading ? (
          <p role="status">Loading rating…</p>
        ) : (
          rating.data && (
            <>
              <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Value label="Score">{rating.data.rating ?? 'Not enough evidence'}</Value>
                <Value label="On time">
                  {rating.data.on_time_percentage == null
                    ? '—'
                    : `${formatQuantity(rating.data.on_time_percentage)}%`}
                </Value>
                <Value label="Rejected">
                  {rating.data.rejection_percentage == null
                    ? '—'
                    : `${formatQuantity(rating.data.rejection_percentage)}%`}
                </Value>
                <Value label="Period and sample">
                  {rating.data.window_days} days · {rating.data.receipt_count} receipts
                </Value>
              </dl>
              <p className="text-sm text-muted-foreground">{rating.data.reason}</p>
            </>
          )
        )}
      </Section>
      {dashboard.data && (
        <p className="text-xs text-muted-foreground">
          QC rejection count covers the last {dashboard.data.qc_lookback_days} days.
        </p>
      )}
    </div>
  )
}

export function VendorOrdersPage() {
  const [params] = useSearchParams()
  const tab = orderTab(params.get('tab'))
  const query = useListQuery<VendorOrderRow>({
    path: '/vendor-portal/purchase-orders',
    queryKey: queryKeys.vendorPortal.orders,
    filters: { tab, assigned_to_me: params.get('assigned_to_me') === '1' },
  })
  const amendments = useListQuery<VendorPendingAmendment>({
    path: '/vendor-portal/purchase-orders/pending-amendments',
    queryKey: queryKeys.vendorPortal.pendingAmendments,
    namespace: 'amendments',
    staleTime: staleTimes.transactional,
  })
  const columns: ColumnDef<VendorOrderRow, unknown>[] = [
    {
      accessorKey: 'code',
      header: 'Order',
      cell: ({ row }) => (
        <Link
          className="font-medium text-primary underline-offset-4 hover:underline"
          to={`/vendor/orders/${row.original.id}`}
        >
          {row.original.code}
        </Link>
      ),
    },
    { accessorKey: 'item_category_name', header: 'Category' },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: ({ row }) => <StatusBadge status={row.original.status} />,
    },
    {
      accessorKey: 'tracking_status',
      header: 'Delivery',
      cell: ({ row }) => <StatusBadge status={row.original.tracking_status} />,
    },
    {
      accessorKey: 'delivery_expected_date',
      header: 'Expected delivery',
      cell: ({ row }) => formatDate(row.original.delivery_expected_date),
    },
    {
      accessorKey: 'total_amount',
      header: 'Total',
      cell: ({ row }) =>
        formatMoney(row.original.total_amount, { currency: row.original.currency }),
    },
  ]
  return (
    <>
      <PageHeader
        title="Purchase orders"
        description="Orders shared with your vendor account and categories."
      />
      {(amendments.data.length > 0 || amendments.isLoading || amendments.error) && (
        <Section title="Revised orders awaiting resend">
          <p className="text-sm text-muted-foreground">
            The buyer is revising these orders. Updated terms become available after approval and
            resend.
          </p>
          <VendorList
            query={amendments}
            title="No revised orders awaiting resend"
            paginationLabel="Revised order pages"
            columns={[
              {
                accessorKey: 'code',
                header: 'Order',
                cell: ({ row }) => (
                  <Link
                    className="font-medium text-primary underline"
                    to={`/vendor/orders/${row.original.id}`}
                  >
                    {row.original.code}
                  </Link>
                ),
              },
              {
                accessorKey: 'amendment_number',
                header: 'Revision',
                cell: ({ row }) => `Rev ${row.original.amendment_number}`,
              },
              {
                accessorKey: 'status',
                header: 'Status',
                cell: ({ row }) => <StatusBadge status={row.original.status} />,
              },
              {
                accessorKey: 'last_amended_at',
                header: 'Amended',
                cell: ({ row }) => formatDateTime(row.original.last_amended_at),
              },
            ]}
            card={(row) => (
              <>
                <Link
                  className="block min-h-11 font-semibold text-primary"
                  to={`/vendor/orders/${row.id}`}
                >
                  {row.code}
                </Link>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="warning">Amended (rev {row.amendment_number})</Badge>
                  <StatusBadge status={row.status} />
                </div>
                <p className="mt-2 text-sm text-muted-foreground">{row.message}</p>
              </>
            )}
          />
        </Section>
      )}
      <div className="mb-4 flex flex-wrap gap-2" aria-label="Order status">
        {orderTabs.map(([value, label]) => (
          <Button
            key={value}
            className="min-h-11"
            variant={tab === value ? 'default' : 'outline'}
            aria-pressed={tab === value}
            onClick={() => query.setParam('tab', value)}
          >
            {label}
          </Button>
        ))}
      </div>
      <Label className="mb-4 flex min-h-11 items-center gap-2">
        <Checkbox
          checked={params.get('assigned_to_me') === '1'}
          onCheckedChange={(checked) =>
            query.setParam('assigned_to_me', checked === true ? '1' : undefined)
          }
        />
        Assigned to me
      </Label>
      <VendorList
        query={query}
        columns={columns}
        title="No purchase orders"
        search
        card={(row) => (
          <>
            <Link
              className="block min-h-11 font-semibold text-primary"
              to={`/vendor/orders/${row.id}`}
            >
              {row.code}
            </Link>
            <div className="mb-3 flex flex-wrap gap-2">
              <StatusBadge status={row.status} />
              <StatusBadge status={row.tracking_status} />
            </div>
            <dl className="grid grid-cols-2 gap-3">
              <Value label="Category">{row.item_category_name}</Value>
              <Value label="Expected delivery">{formatDate(row.delivery_expected_date)}</Value>
              <Value label="Total">
                {formatMoney(row.total_amount, { currency: row.currency })}
              </Value>
              <Value label="Assigned to">{row.assignee_name}</Value>
            </dl>
          </>
        )}
      />
    </>
  )
}

function Shipments({ rows, canDeliver = false }: { rows: VendorShipment[]; canDeliver?: boolean }) {
  const client = useQueryClient()
  const delivered = useMutation({
    mutationFn: (row: VendorShipment) =>
      api.post(
        `/vendor-portal/purchase-orders/${row.purchase_order_id}/shipments/${row.id}/delivered`,
        {},
      ),
    onError: (error) => reportFieldErrors(error, 'Delivery could not be recorded'),
    onSuccess: async (_, row) => {
      await client.invalidateQueries({ queryKey: queryKeys.vendorPortal.all })
      toast.success(`Shipment ${row.shipment_number} marked delivered`)
    },
  })
  return (
    <CardRows
      rows={rows}
      render={(row) => (
        <>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <Link
              className="min-h-11 content-center font-medium text-primary"
              to={`/vendor/orders/${row.purchase_order_id}`}
            >
              Shipment {row.shipment_number}
            </Link>
            <StatusBadge status={row.delivered_at ? 'DELIVERED' : 'IN_TRANSIT'} />
          </div>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Value label="Courier">
              {row.courier_name ?? (row.transporter_id ? 'Registered transporter' : '—')}
            </Value>
            <Value label="Tracking / LR">
              {[row.tracking_reference, row.lr_number].filter(Boolean).join(' · ') || '—'}
            </Value>
            <Value label="Dispatched">{formatDateTime(row.shipped_at)}</Value>
            <Value label="Expected delivery">{formatDate(row.expected_delivery_date)}</Value>
            <Value label="Packages / weight">
              {row.packages ?? '—'} / {formatQuantity(row.weight_kg)} kg
            </Value>
            <Value label="Vehicle / driver">
              {[row.vehicle_number, row.driver_name, row.driver_mobile]
                .filter(Boolean)
                .join(' · ') || '—'}
            </Value>
          </dl>
          {row.notes && <p className="mt-3 break-words text-sm">{row.notes}</p>}
          {canDeliver && !row.delivered_at && (
            <Button
              className="mt-3 min-h-11"
              variant="outline"
              disabled={delivered.isPending}
              onClick={() => delivered.mutate(row)}
            >
              Mark shipment {row.shipment_number} delivered
            </Button>
          )}
        </>
      )}
    />
  )
}
function Receipts({ rows }: { rows: VendorReceipt[] }) {
  return (
    <CardRows
      rows={rows}
      render={(receipt) => (
        <>
          <Link
            className="block min-h-11 font-medium text-primary"
            to={`/vendor/orders/${receipt.purchase_order_id}`}
          >
            {receipt.code} · {receipt.purchase_order_code}
          </Link>
          <p className="mb-3 text-sm text-muted-foreground">{formatDate(receipt.receipt_date)}</p>
          <div className="space-y-3">
            {receipt.lines.map((line) => (
              <div key={line.id} className="rounded-md bg-muted/50 p-3">
                <p className="mb-2 text-sm font-medium">Line {line.line_number}</p>
                <dl className="grid grid-cols-3 gap-2">
                  <Value label="Received">{formatQuantity(line.received_quantity)}</Value>
                  <Value label="Accepted">{formatQuantity(line.accepted_quantity)}</Value>
                  <Value label="Rejected">{formatQuantity(line.rejected_quantity)}</Value>
                </dl>
                <p className="mt-2 text-sm">QC: {humanise(line.qc_status)}</p>
                {line.rejection_reason && (
                  <p className="mt-1 break-words text-sm text-destructive">
                    {line.rejection_reason}
                  </p>
                )}
              </div>
            ))}
          </div>
          {receipt.inspections.map((inspection) => (
            <p key={inspection.id} className="mt-3 text-sm">
              {inspection.code}: {humanise(inspection.result ?? inspection.status)} · Accepted{' '}
              {formatQuantity(inspection.accepted_quantity)}, rejected{' '}
              {formatQuantity(inspection.rejected_quantity)}
              {inspection.rejection_reason ? ` · ${inspection.rejection_reason}` : ''}
            </p>
          ))}
        </>
      )}
    />
  )
}

function OrderRelatedList({
  order,
  kind,
}: {
  order: VendorOrder
  kind: 'shipments' | 'documents' | 'receipts'
}) {
  const { can } = useAuth()
  const [params] = useSearchParams()
  const firstPage =
    (params.get(`${kind}_page`) ?? '1') === '1' &&
    Number(params.get(`${kind}_limit`) ?? order[`${kind}_pagination`].limit) ===
      order[`${kind}_pagination`].limit
  const paged = useListQuery<VendorShipment | VendorDocument | VendorReceipt>({
    path: `/vendor-portal/purchase-orders/${order.id}/${kind}`,
    queryKey: (params) => [...queryKeys.vendorPortal.order(order.id), kind, params],
    namespace: kind,
    defaultLimit: order[`${kind}_pagination`].limit,
    enabled: !firstPage,
  })
  // Page one comes with the order. Only older pages need another API request.
  const query = firstPage
    ? {
        ...paged,
        data: order[kind],
        pagination: order[`${kind}_pagination`],
        error: null,
        isLoading: false,
      }
    : paged
  const download = useMutation({
    mutationFn: (file: VendorDocument) =>
      api.download(`/vendor-portal/documents/${file.id}/download`, file.file_name),
    onSuccess: () => toast.success(fileMessages.downloadStarted),
  })
  return (
    <>
      {query.error ? (
        <QueryFailure error={query.error} retry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <PanelLoader />
      ) : query.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">No {kind} recorded.</p>
      ) : kind === 'shipments' ? (
        <Shipments
          rows={query.data as VendorShipment[]}
          canDeliver={can(P.VENDOR_PORTAL_SHIP) && !['CANCELLED', 'CLOSED'].includes(order.status)}
        />
      ) : kind === 'receipts' ? (
        <Receipts rows={query.data as VendorReceipt[]} />
      ) : (
        <ul className="space-y-2">
          {(query.data as VendorDocument[]).map((file) => (
            <li
              key={file.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3"
            >
              <div className="min-w-0 break-words text-sm">
                <p>{file.file_name}</p>
                <p className="text-xs text-muted-foreground">
                  {humanise(file.kind)}
                  {file.shipment_id ? ' · Shipment attachment' : ''}
                </p>
              </div>
              <Button
                className="min-h-11"
                variant="outline"
                disabled={download.isPending}
                onClick={() => download.mutate(file)}
              >
                Download {file.file_name}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {query.pagination && (query.pagination.has_next || query.pagination.has_previous) && (
        <div className="flex flex-wrap gap-2">
          <Button
            className="min-h-11"
            variant="outline"
            disabled={!query.pagination.has_previous || query.isFetching}
            onClick={() => query.setPage(query.page - 1)}
          >
            Previous {kind}
          </Button>
          <Button
            className="min-h-11"
            variant="outline"
            disabled={!query.pagination.has_next || query.isFetching}
            onClick={() => query.setPage(query.page + 1)}
          >
            More {kind}
          </Button>
        </div>
      )}
    </>
  )
}

export function VendorOrderPage() {
  const { orderId = '' } = useParams()
  const { can } = useAuth()
  const query = useQuery({
    queryKey: queryKeys.vendorPortal.order(orderId),
    queryFn: ({ signal }) =>
      api.get<VendorOrderResponse>(`/vendor-portal/purchase-orders/${orderId}`, { signal }),
    enabled: Boolean(orderId),
  })
  const [action, setAction] = useState<
    'accept' | 'decline' | 'delivery-date' | 'ready-for-dispatch' | 'shipment' | 'upload' | null
  >(null)
  const job = useReportJob()
  const pdf = useMutation({
    mutationFn: () => api.post<AcceptedJobData>(`/vendor-portal/purchase-orders/${orderId}/pdf`),
    onError: (error) => reportFieldErrors(error, 'Purchase order PDF could not be requested'),
    onSuccess: job.start,
  })
  if (query.error) return <QueryFailure error={query.error} retry={() => void query.refetch()} />
  if (!query.data) return <PanelLoader />
  const order = query.data
  if (order.pending_amendment)
    return (
      <div className="space-y-5">
        <PageHeader
          title={order.code}
          breadcrumbs={[{ label: 'Orders', to: '/vendor/orders' }, { label: order.code }]}
          status={
            <span className="flex flex-wrap items-center gap-2">
              <StatusBadge status={order.status} />
              <Badge variant="warning">Amended (rev {order.amendment_number})</Badge>
            </span>
          }
        />
        <Section title="Order being revised">
          <p className="text-sm">{order.message}</p>
          <p className="text-sm text-muted-foreground">
            Amended {formatDateTime(order.last_amended_at)}
          </p>
          <Button variant="outline" loading={query.isFetching} onClick={() => void query.refetch()}>
            Refresh order
          </Button>
          <Link
            className="inline-flex min-h-11 items-center text-sm text-primary underline"
            to="/vendor/orders"
          >
            Back to your orders
          </Link>
        </Section>
      </div>
    )
  const shipping = canShipOrder(order)
  return (
    <div className="space-y-5">
      <PageHeader
        title={order.code}
        status={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={order.status} />
            {(order.amendment_number ?? 0) > 0 && (
              <Badge variant="warning">Amended (rev {order.amendment_number})</Badge>
            )}
          </span>
        }
        breadcrumbs={[{ label: 'Orders', to: '/vendor/orders' }, { label: order.code }]}
        description={order.item_category_name ?? undefined}
      />
      {order.vendor_decline_reason && (
        <p className="rounded-lg border border-destructive/30 p-4 text-sm text-destructive">
          Declined: {order.vendor_decline_reason}
        </p>
      )}
      <Section title="Purchase order">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Value label="Order date">{formatDate(order.po_date)}</Value>
          <Value label="Buyer’s expected date">{formatDate(order.expected_delivery_date)}</Value>
          <Value label="Your committed date">{formatDate(order.vendor_committed_date)}</Value>
          <Value label="Total">
            {formatMoney(order.total_amount, { currency: order.currency })}
          </Value>
          <Value label="Payment terms">{order.payment_terms_days} days</Value>
          <Value label="Your reference">{order.vendor_reference}</Value>
        </dl>
        <p className="whitespace-pre-wrap break-words text-sm">
          <strong>Delivery address: </strong>
          {order.delivery_address ?? 'Ask the buyer for the delivery address.'}
        </p>
        {order.terms && (
          <p className="whitespace-pre-wrap break-words text-sm">
            <strong>Terms: </strong>
            {order.terms}
          </p>
        )}
      </Section>
      <Section title="Items">
        <CardRows
          rows={order.lines}
          render={(line) => (
            <>
              <p className="mb-2 break-words font-medium">
                {line.item_code} · {line.item_name ?? line.description}
              </p>
              <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Value label="Quantity">
                  {formatQuantity(line.quantity, line.uom_code ?? undefined)}
                </Value>
                <Value label="Rate">
                  {formatMoney(line.unit_price, { currency: order.currency })}
                </Value>
                <Value label="Tax">{formatQuantity(line.tax_rate)}%</Value>
                <Value label="Line total">
                  {formatMoney(line.line_total, { currency: order.currency })}
                </Value>
                <Value label="Received">{formatQuantity(line.received_quantity)}</Value>
                <Value label="Accepted">{formatQuantity(line.accepted_quantity)}</Value>
                <Value label="Rejected">{formatQuantity(line.rejected_quantity)}</Value>
              </dl>
            </>
          )}
        />
      </Section>
      <Section title="Timeline">
        {order.timeline.length ? (
          <ol className="space-y-3">
            {order.timeline.map((event) => (
              <li key={event.id} className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
                <span>{humanise(event.to_status)}</span>
                <time className="text-muted-foreground" dateTime={event.at}>
                  {formatDateTime(event.at)}
                </time>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-sm text-muted-foreground">No status events recorded.</p>
        )}
      </Section>
      <Section title="Shipments">
        <OrderRelatedList order={order} kind="shipments" />
      </Section>
      <Section title="Documents">
        <OrderRelatedList order={order} kind="documents" />
      </Section>
      <Section title="Receipts and quality">
        <OrderRelatedList order={order} kind="receipts" />
      </Section>
      {can(P.VENDOR_PORTAL_INVOICE) && (
        <Section title="Invoices and payments">
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Value label="Invoices">{order.invoice_summary.count}</Value>
            <Value label="Total invoiced">
              {formatMoney(order.invoice_summary.total, { currency: order.currency })}
            </Value>
            <Value label="Paid">
              {formatMoney(order.invoice_summary.paid, { currency: order.currency })}
            </Value>
            <Value label="Balance">
              {formatMoney(order.invoice_summary.balance, { currency: order.currency })}
            </Value>
          </dl>
          <Link
            className="inline-flex min-h-11 items-center text-sm text-primary underline"
            to="/vendor/invoices"
          >
            View your invoices
          </Link>
        </Section>
      )}
      <ReportJobPanel controller={job} label="Purchase order PDF" />
      <div
        className="sticky bottom-20 z-10 flex flex-wrap gap-2 rounded-lg border bg-background p-3 shadow-sm md:bottom-3"
        aria-label="Order actions"
      >
        {order.status === 'SENT' && can(P.VENDOR_PORTAL_RESPOND) && (
          <>
            <Button className="min-h-11" onClick={() => setAction('accept')}>
              Accept
            </Button>
            <Button className="min-h-11" variant="destructive" onClick={() => setAction('decline')}>
              Decline
            </Button>
          </>
        )}
        {shipping && can(P.VENDOR_PORTAL_RESPOND) && (
          <Button className="min-h-11" variant="outline" onClick={() => setAction('delivery-date')}>
            Change delivery date
          </Button>
        )}
        {shipping && can(P.VENDOR_PORTAL_SHIP) && (
          <>
            <Button
              className="min-h-11"
              variant="outline"
              onClick={() => setAction('ready-for-dispatch')}
            >
              Mark ready for dispatch
            </Button>
            <Button className="min-h-11" onClick={() => setAction('shipment')}>
              Add shipment
            </Button>
          </>
        )}
        {(can(P.VENDOR_PORTAL_SHIP) || can(P.VENDOR_PORTAL_INVOICE)) && (
          <Button className="min-h-11" variant="outline" onClick={() => setAction('upload')}>
            Upload document
          </Button>
        )}
        {can(P.VENDOR_PORTAL_ACCESS) && (
          <Button
            className="min-h-11"
            variant="outline"
            disabled={pdf.isPending || ['queued', 'running'].includes(job.phase)}
            onClick={() => pdf.mutate()}
          >
            Download PO PDF
          </Button>
        )}
      </div>
      {action && ['accept', 'decline', 'delivery-date', 'ready-for-dispatch'].includes(action) && (
        <VendorActionDialog
          key={action}
          order={order}
          kind={action as 'accept' | 'decline' | 'delivery-date' | 'ready-for-dispatch'}
          onClose={() => setAction(null)}
        />
      )}
      {action === 'shipment' && (
        <VendorShipmentDialog order={order} onClose={() => setAction(null)} />
      )}
      {action === 'upload' && (
        <VendorUploadDialog
          order={order}
          canShip={can(P.VENDOR_PORTAL_SHIP)}
          canInvoice={can(P.VENDOR_PORTAL_INVOICE)}
          onClose={() => setAction(null)}
        />
      )}
    </div>
  )
}

export function VendorShipmentsPage() {
  const [params] = useSearchParams()
  const { can } = useAuth()
  const raw = params.get('status')
  const status = raw === 'in_transit' || raw === 'delivered' ? raw : 'all'
  const query = useListQuery<VendorShipment>({
    path: '/vendor-portal/shipments',
    queryKey: queryKeys.vendorPortal.shipments,
    filters: { status },
  })
  return (
    <>
      <PageHeader title="Shipments" description="Consignments on your purchase orders." />
      <div className="mb-4 flex flex-wrap gap-2">
        {(['all', 'in_transit', 'delivered'] as const).map((value) => (
          <Button
            key={value}
            className="min-h-11"
            variant={status === value ? 'default' : 'outline'}
            aria-pressed={status === value}
            onClick={() => query.setParam('status', value)}
          >
            {humanise(value)}
          </Button>
        ))}
      </div>
      {query.error ? (
        <QueryFailure error={query.error} retry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <PanelLoader />
      ) : (
        <>
          <Shipments rows={query.data} canDeliver={can(P.VENDOR_PORTAL_SHIP)} />
          {!query.data.length && <EmptyState title="No shipments" description={empty.filtered} />}
          <SimplePages query={query} />
        </>
      )}
    </>
  )
}
function SimplePages({
  query,
}: {
  query: {
    page: number
    pagination?: { has_previous: boolean; has_next: boolean }
    isFetching: boolean
    setPage: (value: number) => void
  }
}) {
  return (
    <nav aria-label="List pages" className="mt-4 flex flex-wrap items-center justify-between gap-2">
      <Button
        className="min-h-11"
        variant="outline"
        disabled={!query.pagination?.has_previous || query.isFetching}
        onClick={() => query.setPage(query.page - 1)}
      >
        Previous
      </Button>
      <span className="text-sm">Page {query.page}</span>
      <Button
        className="min-h-11"
        variant="outline"
        disabled={!query.pagination?.has_next || query.isFetching}
        onClick={() => query.setPage(query.page + 1)}
      >
        Next
      </Button>
    </nav>
  )
}
export function VendorReceiptsPage() {
  const [params] = useSearchParams()
  const rejected = params.get('rejected_only') === '1'
  const query = useListQuery<VendorReceipt>({
    path: '/vendor-portal/receipts',
    queryKey: queryKeys.vendorPortal.receipts,
    filters: { rejected_only: rejected },
  })
  return (
    <>
      <PageHeader
        title="Receipts and quality"
        description="Goods received by the buyer and the recorded quality verdict."
      />
      <Label className="mb-4 flex min-h-11 items-center gap-2">
        <Checkbox
          checked={rejected}
          onCheckedChange={(value) =>
            query.setParam('rejected_only', value === true ? '1' : undefined)
          }
        />
        With rejected quantities
      </Label>
      {query.error ? (
        <QueryFailure error={query.error} retry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <PanelLoader />
      ) : (
        <>
          <Receipts rows={query.data} />
          {!query.data.length && (
            <EmptyState title="No goods receipts" description={empty.filtered} />
          )}
          <SimplePages query={query} />
        </>
      )}
    </>
  )
}

export function VendorInvoicesPage() {
  const { can } = useAuth()
  const [params] = useSearchParams()
  const [creating, setCreating] = useState(false)
  const unpaid = params.get('unpaid_only') === '1'
  const query = useListQuery<VendorInvoiceRow>({
    path: '/vendor-portal/invoices',
    queryKey: queryKeys.vendorPortal.invoices,
    filters: { unpaid_only: unpaid },
    enabled: can(P.VENDOR_PORTAL_INVOICE),
  })
  const columns: ColumnDef<VendorInvoiceRow, unknown>[] = [
    {
      accessorKey: 'vendor_invoice_number',
      header: 'Your invoice',
      cell: ({ row }) => (
        <Link className="text-primary underline" to={`/vendor/invoices/${row.original.id}`}>
          {row.original.vendor_invoice_number}
        </Link>
      ),
    },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: ({ row }) => <StatusBadge status={row.original.status} />,
    },
    {
      accessorKey: 'due_date',
      header: 'Due date',
      cell: ({ row }) => formatDate(row.original.due_date),
    },
    {
      accessorKey: 'outstanding_amount',
      header: 'Balance',
      cell: ({ row }) =>
        formatMoney(row.original.outstanding_amount, { currency: row.original.currency }),
    },
  ]
  return (
    <>
      <PageHeader
        title="Invoices"
        description="Your submitted bills, review status and payment balances."
        actions={
          can(P.VENDOR_PORTAL_INVOICE) && (
            <Button className="min-h-11" onClick={() => setCreating(true)}>
              New invoice against PO
            </Button>
          )
        }
      />
      <Label className="mb-4 flex min-h-11 items-center gap-2">
        <Checkbox
          checked={unpaid}
          onCheckedChange={(value) =>
            query.setParam('unpaid_only', value === true ? '1' : undefined)
          }
        />
        Payment pending
      </Label>
      <VendorList
        query={query}
        columns={columns}
        title="No invoices"
        card={(row) => (
          <>
            <Link
              className="block min-h-11 font-medium text-primary"
              to={`/vendor/invoices/${row.id}`}
            >
              {row.vendor_invoice_number}
            </Link>
            <StatusBadge status={row.status} />
            <dl className="mt-3 grid grid-cols-2 gap-3">
              <Value label="Due date">{formatDate(row.due_date)}</Value>
              <Value label="Total">
                {formatMoney(row.total_amount, { currency: row.currency })}
              </Value>
              <Value label="Paid">{formatMoney(row.paid_amount, { currency: row.currency })}</Value>
              <Value label="Balance">
                {formatMoney(row.outstanding_amount, { currency: row.currency })}
              </Value>
            </dl>
          </>
        )}
      />
      {creating && <VendorInvoiceForm onClose={() => setCreating(false)} />}
    </>
  )
}
export function VendorInvoicePage() {
  const { invoiceId = '' } = useParams()
  const { can } = useAuth()
  const query = useQuery({
    queryKey: queryKeys.vendorPortal.invoice(invoiceId),
    queryFn: ({ signal }) =>
      api.get<VendorInvoice>(`/vendor-portal/invoices/${invoiceId}`, { signal }),
    enabled: Boolean(invoiceId) && can(P.VENDOR_PORTAL_INVOICE),
  })
  if (query.error) return <QueryFailure error={query.error} retry={() => void query.refetch()} />
  if (!query.data) return <PanelLoader />
  const invoice = query.data
  return (
    <div className="space-y-5">
      <PageHeader
        title={invoice.vendor_invoice_number}
        status={<StatusBadge status={invoice.status} />}
        breadcrumbs={[{ label: 'Invoices', to: '/vendor/invoices' }, { label: invoice.code }]}
      />
      <Section title="Invoice">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Value label="Invoice date">{formatDate(invoice.invoice_date)}</Value>
          <Value label="Due date">{formatDate(invoice.due_date)}</Value>
          <Value label="Matching">{humanise(invoice.match_status)}</Value>
          <Value label="Total">
            {formatMoney(invoice.total_amount, { currency: invoice.currency })}
          </Value>
          <Value label="Paid">
            {formatMoney(invoice.paid_amount, { currency: invoice.currency })}
          </Value>
          <Value label="Balance">
            {formatMoney(invoice.outstanding_amount, { currency: invoice.currency })}
          </Value>
        </dl>
        {invoice.dispute_reason && (
          <p className="text-sm text-destructive">Buyer’s dispute: {invoice.dispute_reason}</p>
        )}
        {invoice.cancellation_reason && (
          <p className="text-sm">Cancellation: {invoice.cancellation_reason}</p>
        )}
        {invoice.notes && (
          <p className="whitespace-pre-wrap break-words text-sm">{invoice.notes}</p>
        )}
      </Section>
      <Section title="Invoice lines">
        <CardRows
          rows={invoice.lines ?? []}
          render={(line) => (
            <>
              <p className="mb-2 break-words font-medium">
                Line {line.line_number}: {line.description ?? 'Item'}
              </p>
              <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Value label="Quantity">{formatQuantity(line.quantity)}</Value>
                <Value label="Unit price">
                  {formatMoney(line.unit_price, { currency: invoice.currency })}
                </Value>
                <Value label="Tax">
                  {formatMoney(line.tax_amount, { currency: invoice.currency })}
                </Value>
                <Value label="Total">
                  {formatMoney(line.line_total, { currency: invoice.currency })}
                </Value>
              </dl>
            </>
          )}
        />
      </Section>
    </div>
  )
}
export function VendorPaymentsPage() {
  const { can } = useAuth()
  const allowed = can(P.VENDOR_PORTAL_INVOICE)
  const summary = useQuery({
    queryKey: queryKeys.vendorPortal.paymentSummary,
    queryFn: ({ signal }) =>
      api.get<VendorPaymentSummary[]>('/vendor-portal/payments/summary', { signal }),
    enabled: allowed,
  })
  const query = useListQuery<VendorPayment>({
    path: '/vendor-portal/payments',
    queryKey: queryKeys.vendorPortal.payments,
    enabled: allowed,
  })
  return (
    <div className="space-y-5">
      <PageHeader
        title="Payments"
        description="Approved payments and the invoices each payment settled."
      />
      {summary.error ? (
        <QueryFailure error={summary.error} retry={() => void summary.refetch()} />
      ) : (
        summary.data?.map((row) => (
          <Section key={row.currency} title={`${row.currency} payment summary`}>
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Value label="Outstanding">
                {formatMoney(row.outstanding, { currency: row.currency })}
              </Value>
              <Value label="Overdue">{formatMoney(row.overdue, { currency: row.currency })}</Value>
              <Value label="Due in the next 7 days">
                {formatMoney(row.due_next_seven_days, { currency: row.currency })}
              </Value>
              <Value label="Paid this month">
                {formatMoney(row.paid_this_month, { currency: row.currency })}
              </Value>
              <Value label="Last payment">
                {formatDate(row.last_payment_date)} ·{' '}
                {formatMoney(row.last_payment_amount, { currency: row.currency })}
              </Value>
            </dl>
          </Section>
        ))
      )}
      {query.error ? (
        <QueryFailure error={query.error} retry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <PanelLoader />
      ) : (
        <>
          <CardRows
            rows={query.data}
            render={(payment) => (
              <>
                <p className="mb-2 font-medium">{payment.code}</p>
                <dl className="grid grid-cols-2 gap-3">
                  <Value label="Payment date">{formatDate(payment.payment_date)}</Value>
                  <Value label="Amount">
                    {formatMoney(payment.amount, { currency: payment.currency })}
                  </Value>
                  <Value label="Method">{humanise(payment.payment_method)}</Value>
                  <Value label="Bank reference">{payment.reference_number}</Value>
                  <Value label="Unallocated">
                    {formatMoney(payment.unallocated_amount, { currency: payment.currency })}
                  </Value>
                </dl>
                <ul className="mt-3 space-y-2 text-sm">
                  {(payment.allocations ?? []).map((allocation, index) => (
                    <li key={`${allocation.vendor_invoice_id}-${index}`}>
                      <Link
                        className="inline-flex min-h-11 items-center text-primary underline"
                        to={`/vendor/invoices/${allocation.vendor_invoice_id}`}
                      >
                        {allocation.vendor_invoice_number ?? allocation.invoice_code ?? 'Invoice'}
                      </Link>
                      : {formatMoney(allocation.amount, { currency: payment.currency })}
                    </li>
                  ))}
                </ul>
              </>
            )}
          />
          {!query.data.length && <EmptyState title="No payments" description={empty.filtered} />}
          <SimplePages query={query} />
        </>
      )}
    </div>
  )
}

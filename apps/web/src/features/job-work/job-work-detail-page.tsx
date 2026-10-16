/**
 * One job-work order, whole: its lines, its timeline, its reconciliation, its
 * documents and its money.
 *
 * The list shows one row per order with a receipt count; everything the order
 * *means* lives here. Material at a vendor is company stock on somebody else's
 * floor, so the page answers the four questions that matter about it: what
 * went out, what came back, what was lost, and what is still out - with the
 * ledger's own job-work pool beside the order's books, because a divergence
 * between the two is visible no other way.
 */

import { useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { ArrowLeft, FlaskConical, PackageCheck, PackageMinus, Repeat } from 'lucide-react'
import { Suspense, lazy, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, PanelLoader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle, Badge } from '@/components/ui/primitives'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { ChallanDownload } from './job-work-challan-download'
import { JobWorkIssueDialog, JobWorkQcResultDialog, JobWorkReceiveDialog } from './job-work-dialogs'
import {
  acceptsIssue,
  acceptsReceipt,
  awaitsInspection,
  jobWorkKeys,
  linesOf,
  useOrderChallans,
  useOrderReceipts,
  useReconciliation,
  useJobWorkOrder,
  type ChallanRow,
  type JobWorkOrderDetail,
  type JobWorkOrderLine,
  type ReceiptRow,
  type ReconciliationReport,
} from './job-work-model'
import type { ReworkPrefill } from './job-work-order-form-dialog'

/** The rework form is the heaviest dialog on this page; it loads on demand. */
const JobWorkOrderFormDialog = lazy(() =>
  import('./job-work-order-form-dialog').then((m) => ({ default: m.JobWorkOrderFormDialog })),
)

interface TimelineEvent {
  at: string | null
  title: string
  detail?: string
}

/** Approved, issued, every challan, every receipt, QC, closed - in order. */
function useTimeline(
  order: JobWorkOrderDetail | undefined,
  challans: ChallanRow[] | undefined,
  receipts: ReceiptRow[] | undefined,
): TimelineEvent[] {
  return useMemo(() => {
    if (!order) return []
    const events: TimelineEvent[] = []
    if (order.approved_at) events.push({ at: order.approved_at, title: 'Approved' })
    if (order.issued_at) events.push({ at: order.issued_at, title: 'Material issued to vendor' })
    for (const challan of challans ?? [])
      events.push({
        at: challan.challan_date,
        title: `Challan ${challan.code} issued`,
        detail: challan.vehicle_number != null ? `Vehicle ${challan.vehicle_number}` : undefined,
      })
    for (const receipt of receipts ?? []) {
      events.push({
        at: receipt.receipt_date,
        title: `Receipt ${receipt.code}`,
        detail: `${formatQuantity(receipt.total_output_quantity)} good, ${formatQuantity(receipt.total_rejected_quantity)} rejected`,
      })
      if (receipt.qc_held_at)
        events.push({ at: receipt.qc_held_at, title: `Held for inspection (${receipt.code})` })
      if (receipt.qc_released_at)
        events.push({ at: receipt.qc_released_at, title: `QC released (${receipt.code})` })
    }
    if (order.closed_at) events.push({ at: order.closed_at, title: 'Closed' })
    return events.sort((a, b) => String(a.at ?? '').localeCompare(String(b.at ?? '')))
  }, [order, challans, receipts])
}

function atVendor(line: JobWorkOrderLine): string {
  const outstanding = toDecimal(line.issued_quantity).minus(toDecimal(line.received_quantity))
  return outstanding.greaterThan(0) ? outstanding.toFixed() : '0'
}

function LinesCard({ order }: { order: JobWorkOrderDetail }) {
  const columns = useMemo<ColumnDef<JobWorkOrderLine, unknown>[]>(
    () => [
      {
        accessorKey: 'line_number',
        header: 'Line',
        enableSorting: false,
        cell: ({ row }) => <span className="tabular">{String(row.original.line_number)}</span>,
      },
      {
        id: 'issued',
        header: 'Issued',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.issued_quantity)}</span>
        ),
      },
      {
        id: 'received_output',
        header: 'Good output',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.received_output_quantity)}</span>
        ),
      },
      {
        id: 'rejected',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          Number(row.original.rejected_quantity) > 0 ? (
            <span className="tabular font-medium text-destructive">
              {formatQuantity(row.original.rejected_quantity)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'scrap',
        header: 'Scrap',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          Number(row.original.scrap_quantity) > 0 ? (
            <span className="tabular">{formatQuantity(row.original.scrap_quantity)}</span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'at_vendor',
        header: 'Still at vendor',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular font-medium">{formatQuantity(atVendor(row.original))}</span>
        ),
      },
      {
        id: 'allowance',
        header: 'Loss allowance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.expected_loss_percentage)}%</span>
        ),
      },
    ],
    [],
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>Lines</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={linesOf(order)}
          getRowId={(row) => row.id}
          emptyTitle="No lines"
          emptyDescription="This order carries no material lines."
        />
      </CardContent>
    </Card>
  )
}

function ReconciliationCard({ report }: { report: ReconciliationReport }) {
  type ReportLine = NonNullable<ReconciliationReport['lines']>[number]
  const columns = useMemo<ColumnDef<ReportLine, unknown>[]>(
    () => [
      {
        accessorKey: 'line_number',
        header: 'Line',
        enableSorting: false,
        cell: ({ row }) => <span className="tabular">{String(row.original.line_number)}</span>,
      },
      {
        id: 'issued',
        header: 'Issued',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.issued_quantity)}</span>
        ),
      },
      {
        id: 'received',
        header: 'Accounted back',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.received_quantity)}</span>
        ),
      },
      {
        id: 'outstanding',
        header: 'Outstanding',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          Number(row.original.outstanding_quantity) > 0 ? (
            <span className="tabular font-medium">
              {formatQuantity(row.original.outstanding_quantity)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'loss',
        header: 'Loss vs allowance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">
            {formatQuantity(row.original.loss_percentage)}% /{' '}
            {formatQuantity(row.original.expected_loss_percentage)}%
          </span>
        ),
      },
      {
        id: 'excess',
        header: 'Excess loss',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          Number(row.original.excess_loss_quantity) > 0 ? (
            <span className="tabular font-medium text-destructive">
              {formatQuantity(row.original.excess_loss_quantity)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ],
    [],
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reconciliation</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <DataTable
          columns={columns}
          data={report.lines ?? []}
          getRowId={(row) => String(row.line_number)}
          emptyTitle="Nothing issued yet"
          emptyDescription="The reconciliation appears once material has left for the vendor."
        />
        <dl className="grid gap-2 text-sm sm:grid-cols-2">
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">Outstanding (order books)</dt>
            <dd className="tabular font-medium">{formatQuantity(report.total_outstanding)}</dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">Job-work pool (live ledger)</dt>
            <dd className="tabular font-medium">{formatQuantity(report.stock_in_job_work_pool)}</dd>
          </div>
        </dl>
        {!toDecimal(report.stock_in_job_work_pool).gte(toDecimal(report.total_outstanding)) && (
          <Alert variant="warning">
            <AlertTitle>The pool cannot cover the books</AlertTitle>
            <AlertDescription>
              The order&apos;s outstanding exceeds the ledger&apos;s live job-work pool for the same
              items and warehouse. Either a movement was posted outside job work or a receipt was
              recorded without one.
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}

function ReceiptsCard({
  receipts,
  onRework,
}: {
  receipts: ReceiptRow[] | undefined
  onRework: (prefill: ReworkPrefill) => void
}) {
  const { can } = useAuth()
  const canRework = can(P.JOB_WORK_CREATE)
  const columns = useMemo<ColumnDef<ReceiptRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Receipt',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'receipt_date',
        header: 'Date',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.receipt_date),
      },
      {
        id: 'good',
        header: 'Good',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.total_output_quantity)}</span>
        ),
      },
      {
        id: 'rejected',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          Number(row.original.total_rejected_quantity) > 0 ? (
            <span className="tabular font-medium text-destructive">
              {formatQuantity(row.original.total_rejected_quantity)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'qc',
        header: 'QC',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.qc_inspection_id ? (
            <Link to="/quality" className="hover:underline" title="Open the QC register">
              <Badge variant="warning">Inspected</Badge>
            </Link>
          ) : row.original.qc_held_at ? (
            <Badge variant="muted">On hold</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'rework',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const rejectedLines = (row.original.lines ?? []).filter(
            (line) => Number(line.rejected_quantity) > 0,
          )
          // Rework the worst line first: the prefill names one line's output,
          // and the planner adjusts from there.
          const worst = rejectedLines
            .slice()
            .sort((a, b) => Number(b.rejected_quantity) - Number(a.rejected_quantity))[0]
          if (!canRework || !worst) return <span className="text-muted-foreground">-</span>
          return (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                onRework({
                  sourceId: row.original.id,
                  vendorId: row.original.vendor_id,
                  lines: [
                    {
                      item_id: worst.output_item_id,
                      quantity_to_issue: String(worst.rejected_quantity),
                    },
                  ],
                })
              }
              title={`Send ${worst.rejected_quantity} rejected unit(s) back for rework`}
            >
              <Repeat />
              Rework
            </Button>
          )
        },
      },
    ],
    [canRework, onRework],
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>Receipts</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={receipts ?? []}
          getRowId={(row) => row.id}
          emptyTitle="Nothing received yet"
          emptyDescription="Lots returning from the vendor appear here with their QC outcome."
        />
      </CardContent>
    </Card>
  )
}

function ChallansCard({
  challans,
  isLoading,
  isFetching,
  error,
  onRefresh,
}: {
  challans: ChallanRow[] | undefined
  isLoading: boolean
  isFetching: boolean
  error: Error | null
  onRefresh: () => void
}) {
  const columns = useMemo<ColumnDef<ChallanRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Challan',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'challan_date',
        header: 'Despatched',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.challan_date),
      },
      {
        id: 'quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.total_quantity)}</span>
        ),
      },
      {
        id: 'value',
        header: 'Declared value',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{formatMoney(row.original.total_value)}</span>,
      },
      {
        id: 'pdf',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <ChallanDownload challanId={row.original.id} challanCode={row.original.code} />
        ),
      },
    ],
    [],
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>Challans</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={challans ?? []}
          isLoading={isLoading}
          isFetching={isFetching}
          error={error}
          onRefresh={onRefresh}
          getRowId={(row) => row.id}
          emptyTitle="Nothing despatched yet"
          emptyDescription="The delivery challan appears here once material is issued."
        />
      </CardContent>
    </Card>
  )
}

export default function JobWorkDetailPage() {
  const { orderId = '' } = useParams<{ orderId: string }>()
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const detailQuery = useJobWorkOrder(orderId, true)
  const order = detailQuery.data
  const challansQuery = useOrderChallans(orderId, true)
  const receiptsQuery = useOrderReceipts(orderId, true)
  const reconciliationQuery = useReconciliation(orderId, true)

  const [issueOpen, setIssueOpen] = useState(false)
  const [receiveOpen, setReceiveOpen] = useState(false)
  const [verdictOpen, setVerdictOpen] = useState(false)
  const [rework, setRework] = useState<ReworkPrefill | null>(null)

  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const vendor = order ? vendorById.get(order.vendor_id) : undefined

  const timeline = useTimeline(order, challansQuery.data, receiptsQuery.data)

  if (detailQuery.isLoading) return <PanelLoader />
  if (detailQuery.error || !order) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load this order</AlertTitle>
        <AlertDescription>
          {detailQuery.error?.message ?? 'The order does not exist.'}
        </AlertDescription>
      </Alert>
    )
  }

  const showIssue = can(P.JOB_WORK_ISSUE) && acceptsIssue(order.status)
  const showReceive = can(P.JOB_WORK_RECEIVE) && acceptsReceipt(order.status)
  const showVerdict = can(P.QC_DECIDE) && awaitsInspection(order.status)
  // Approval and close stay on the orders list: both need an audit reason the
  // list already collects, and duplicating those confirmations here would be a
  // second, drifting copy of the same command.

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
  }

  return (
    <>
      <PageHeader
        title={order.code}
        description={`${humanise(order.process_type)} at ${vendor ? `${vendor.name} (${vendor.code})` : order.vendor_id.slice(0, 8)} - due back ${formatDate(order.expected_return_date)}`}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" asChild>
              <Link to="/job-work">
                <ArrowLeft />
                All orders
              </Link>
            </Button>
            {showIssue && (
              <Button size="sm" onClick={() => setIssueOpen(true)}>
                <PackageMinus />
                Issue
              </Button>
            )}
            {showReceive && (
              <Button size="sm" onClick={() => setReceiveOpen(true)}>
                <PackageCheck />
                Receive
              </Button>
            )}
            {showVerdict && (
              <Button size="sm" onClick={() => setVerdictOpen(true)}>
                <FlaskConical />
                Verdict
              </Button>
            )}
          </div>
        }
      />

      <div className="mb-4 flex items-center gap-2">
        <StatusBadge status={order.status} />
        {order.requires_qc && <Badge variant="muted">QC</Badge>}
        {order.source_type !== 'MANUAL' && (
          <Badge variant="secondary">From {humanise(order.source_type)}</Badge>
        )}
      </div>

      <div className="space-y-4">
        <div className="grid gap-4 lg:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Money</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="space-y-2 text-sm">
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Estimated charges</dt>
                  <dd className="tabular">{formatMoney(order.estimated_charges)}</dd>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Accrued charges</dt>
                  <dd className="tabular font-medium">{formatMoney(order.total_charges)}</dd>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Vendor debit</dt>
                  <dd className="tabular font-medium text-destructive">
                    {formatMoney(order.vendor_debit_amount)}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <dt className="text-muted-foreground">Rate</dt>
                  <dd className="tabular">
                    {formatMoney(order.rate_per_unit)} / {humanise(order.rate_basis)}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Timeline</CardTitle>
            </CardHeader>
            <CardContent>
              {timeline.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Nothing has happened yet - this order is still a draft.
                </p>
              ) : (
                <ol className="space-y-3">
                  {timeline.map((event, index) => (
                    <li key={`${event.at}-${index}`} className="flex gap-3 text-sm">
                      <span
                        className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-primary"
                        aria-hidden
                      />
                      <div className="min-w-0">
                        <p className="font-medium">{event.title}</p>
                        <p className="text-xs text-muted-foreground">
                          {event.at ? formatDateTime(event.at) : 'date unknown'}
                          {event.detail ? ` - ${event.detail}` : ''}
                        </p>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </CardContent>
          </Card>
        </div>

        <LinesCard order={order} />

        {reconciliationQuery.data && <ReconciliationCard report={reconciliationQuery.data} />}

        <ReceiptsCard receipts={receiptsQuery.data} onRework={setRework} />

        <ChallansCard
          challans={challansQuery.data}
          isLoading={challansQuery.isLoading}
          isFetching={challansQuery.isFetching}
          error={challansQuery.error as Error | null}
          onRefresh={() => void challansQuery.refetch()}
        />
      </div>

      {issueOpen && (
        <JobWorkIssueDialog
          open
          onOpenChange={(next) => {
            if (!next) {
              setIssueOpen(false)
              refresh()
            }
          }}
          orderId={order.id}
          orderCode={order.code}
        />
      )}
      {receiveOpen && (
        <JobWorkReceiveDialog
          open
          onOpenChange={(next) => {
            if (!next) {
              setReceiveOpen(false)
              refresh()
            }
          }}
          orderId={order.id}
          orderCode={order.code}
        />
      )}
      {verdictOpen && (
        <JobWorkQcResultDialog
          open
          onOpenChange={(next) => {
            if (!next) {
              setVerdictOpen(false)
              refresh()
            }
          }}
          orderId={order.id}
          orderCode={order.code}
        />
      )}
      {rework && (
        <Suspense fallback={null}>
          <JobWorkOrderFormDialog
            open
            onOpenChange={(next) => {
              if (!next) {
                setRework(null)
                refresh()
              }
            }}
            rework={rework}
          />
        </Suspense>
      )}
    </>
  )
}

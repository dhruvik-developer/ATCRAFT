import { describeError } from '@/lib/messages'
/**
 * One request for quotation: the header, the lines that were put out, and the
 * vendors they went to.
 *
 * The actions are derived from the status rather than shown and disabled. The
 * machine in `apps/api/src/modules/procurement/service_rules.py` has four edges
 * and no others - a draft can be sent or cancelled, a sent RFQ can be closed or
 * cancelled - so a button that could only ever be refused is simply not there.
 * Send has a second condition the API also enforces: an RFQ that names nobody
 * cannot be sent, and the fix for that is an edit, so the screen says what is
 * missing instead of offering a command that would bounce.
 *
 * What is deliberately absent is the comparison. Quotations arriving do not
 * move this document, nothing on either payload counts them, and reading them
 * is a screen of its own with its own permission.
 *
 * The RFQ carries ids, not names: `warehouse_id`, a `vendor_ids` array and a
 * `uom_id` per line, each resolved through the bounded reference lookup its
 * module publishes. The item is the exception - it has no bulk lookup, and one
 * request per line would be the N+1 the rules forbid - so a line is labelled by
 * the description recorded on it and links out to the item.
 */

import { useQuery } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { Ban, Check, CheckCircle2, Pencil, RefreshCw, Send, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DescriptionList, EmptyState, PageHeader, StatusBadge } from '@/components/shared'
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
import { useAuth } from '@/features/auth/auth-context'
import { useUomOptions } from '@/features/items/item-model'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { api } from '@/lib/api-client'
import { formatDate, formatDateTime, formatNumber, formatQuantity, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { cn, indexById } from '@/lib/utils'

import { RfqCommandDialog, RfqFormDialog } from './rfq-dialogs'
import {
  RFQ_LIFECYCLE,
  canSendRfq,
  daysAwaitingResponse,
  hasInvitedVendors,
  isRfqCancellable,
  isRfqClosable,
  isRfqEditable,
  isRfqSendable,
  rfqKeys,
  rfqStageIndex,
  type RfqCommand,
  type RfqDetail,
  type RfqLine,
  type RfqStatus,
} from './rfq-model'

// -------------------------------------------------------------- stepper -----

function StatusStepper({ status }: { status: RfqStatus }) {
  const stopped = status === 'CANCELLED'
  const current = rfqStageIndex(status)

  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2" aria-label="RFQ lifecycle">
      {RFQ_LIFECYCLE.map((stage, index) => {
        const done = !stopped && current > index
        const active = !stopped && current === index
        return (
          <li key={stage.label} className="flex items-center gap-1">
            <span
              // aria-current is what tells a screen-reader user where the
              // document actually is; colour alone would not.
              aria-current={active ? 'step' : undefined}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
                done && 'border-success/40 bg-success/10 text-success',
                active && 'border-primary bg-primary/10 text-primary',
                !done && !active && 'border-border text-muted-foreground',
              )}
            >
              {done && <Check className="h-3 w-3" aria-hidden />}
              {stage.label}
            </span>
            {index < RFQ_LIFECYCLE.length - 1 && (
              <span className={cn('h-px w-4', done ? 'bg-success/50' : 'bg-border')} aria-hidden />
            )}
          </li>
        )
      })}
    </ol>
  )
}

// -------------------------------------------------------------- vendors -----

/**
 * Who was asked.
 *
 * Names come from `/vendors/options`, the bounded projection published for
 * exactly this. A vendor suspended since the enquiry went out drops off that
 * list, so it is reported as no longer listed rather than rendered as a UUID -
 * it was still asked, and the enquiry is still a fact.
 *
 * Approval status is shown because it is not a condition of being asked: the
 * API refuses an unapproved vendor on a purchase *order*, so a quotation from
 * one is a price the buyer can compare but not yet buy against.
 */
function VendorsCard({
  vendorIds,
  canEdit,
  onEdit,
}: {
  vendorIds: string[]
  canEdit: boolean
  onEdit: () => void
}) {
  const { can } = useAuth()
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Vendors asked
          {vendorIds.length > 0 && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              {vendorIds.length}
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {vendorIds.length === 0 ? (
          <EmptyState
            title="Nobody has been asked yet"
            description="An RFQ cannot be sent until at least one vendor is named on it."
            action={
              canEdit ? (
                <Button variant="outline" onClick={onEdit}>
                  <Pencil />
                  Add vendors
                </Button>
              ) : undefined
            }
          />
        ) : (
          <ul className="divide-y">
            {vendorIds.map((vendorId) => {
              const vendor = vendorById.get(vendorId)
              return (
                <li key={vendorId} className="flex items-start justify-between gap-3 py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {vendor ? vendor.name : 'Vendor no longer listed'}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {vendor
                        ? `${vendor.code}${
                            vendor.lead_time_days > 0
                              ? ` - usually ${formatNumber(vendor.lead_time_days)} days`
                              : ''
                          }`
                        : 'Suspended since this RFQ went out, or not visible to your account.'}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {vendor && vendor.rating !== null && (
                      <span className="tabular text-xs text-muted-foreground">
                        {toDecimal(vendor.rating).toDecimalPlaces(1).toFixed(1)}
                      </span>
                    )}
                    {vendor &&
                      (vendor.is_approved ? (
                        <Badge variant="success">Approved</Badge>
                      ) : (
                        // Worth calling out: a price from this vendor cannot be
                        // turned into a purchase order until they are approved.
                        <Badge variant="warning">Not approved</Badge>
                      ))}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

// ----------------------------------------------------------------- page -----

export default function RfqDetailPage() {
  const { rfqId = '' } = useParams<{ rfqId: string }>()
  const { can } = useAuth()

  const [editOpen, setEditOpen] = useState(false)
  const [command, setCommand] = useState<RfqCommand | null>(null)

  const canManage = can(P.RFQ_MANAGE)

  const rfqQuery = useQuery({
    queryKey: rfqKeys.detail(rfqId),
    queryFn: () => api.get<RfqDetail>(`/procurement/rfqs/${rfqId}`),
    staleTime: staleTimes.transactional,
    enabled: Boolean(rfqId),
  })

  const rfq = rfqQuery.data

  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])
  const uoms = useUomOptions(can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  const lineColumns = useMemo<ColumnDef<RfqLine, unknown>[]>(
    () => [
      {
        id: 'line_number',
        header: '#',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.line_number}</span>,
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[24rem]">
            <Link
              to={`/items/${row.original.item_id}`}
              className="block truncate font-medium hover:underline"
            >
              {row.original.description ?? 'Open item'}
            </Link>
            {row.original.requisition_line_id && (
              <span className="block text-2xs text-muted-foreground">From a requisition line</span>
            )}
          </div>
        ),
      },
      {
        id: 'quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatQuantity(row.original.quantity, uomById.get(row.original.uom_id)?.code),
      },
      {
        id: 'required_date',
        header: 'Needed',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.required_date),
      },
      {
        id: 'specification',
        header: 'Specification',
        enableSorting: false,
        // The whole point of an enquiry: what the vendor is being asked to
        // quote against. Wrapped rather than truncated to one line.
        cell: ({ row }) =>
          row.original.specification ? (
            <span className="block max-w-[26rem] whitespace-pre-wrap text-xs">
              {row.original.specification}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'notes',
        header: 'Notes',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.notes ? (
            <span className="block max-w-[18rem] text-xs text-muted-foreground">
              {row.original.notes}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ],
    [uomById],
  )

  if (rfqQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <Skeleton className="h-10 w-full" />
        <div className="grid gap-4 lg:grid-cols-3">
          <Skeleton className="h-64 lg:col-span-2" />
          <Skeleton className="h-64" />
        </div>
        <Skeleton className="h-72 w-full" />
      </div>
    )
  }

  if (rfqQuery.error) {
    return (
      <>
        <PageHeader
          title="Request for quotation"
          breadcrumbs={[{ label: 'RFQs', to: '/procurement/rfqs' }]}
        />
        <Alert variant="destructive">
          <AlertTitle>Could not load this RFQ</AlertTitle>
          <AlertDescription>{describeError(rfqQuery.error)}</AlertDescription>
          <Button
            variant="outline"
            size="sm"
            className="mt-4"
            onClick={() => void rfqQuery.refetch()}
          >
            <RefreshCw />
            Try again
          </Button>
        </Alert>
      </>
    )
  }

  if (!rfq) {
    return (
      <>
        <PageHeader
          title="Request for quotation"
          breadcrumbs={[{ label: 'RFQs', to: '/procurement/rfqs' }]}
        />
        <EmptyState
          title="RFQ not found"
          description="It may have been removed, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/procurement/rfqs">Back to the RFQ list</Link>
            </Button>
          }
        />
      </>
    )
  }

  const overdueDays = daysAwaitingResponse(rfq)
  const warehouse = warehouseById.get(rfq.warehouse_id)
  const lines = rfq.lines ?? []

  return (
    <>
      <PageHeader
        title={rfq.code}
        description={`${lines.length} line${lines.length === 1 ? '' : 's'} out to ${rfq.vendor_ids.length} vendor${rfq.vendor_ids.length === 1 ? '' : 's'}`}
        breadcrumbs={[{ label: 'RFQs', to: '/procurement/rfqs' }, { label: rfq.code }]}
        status={
          <span className="flex items-center gap-2">
            <StatusBadge status={rfq.status} />
            {overdueDays !== null && (
              <Badge variant="destructive">
                <TriangleAlert className="h-3 w-3" aria-hidden />
                {overdueDays}d overdue
              </Badge>
            )}
          </span>
        }
        actions={
          canManage ? (
            <>
              {/* Each command appears only where the state machine has an edge
                  for it. The gate is UX: the API authorises `rfq.manage` on
                  every one of these endpoints independently. */}
              {isRfqEditable(rfq.status) && (
                <Button variant="outline" onClick={() => setEditOpen(true)}>
                  <Pencil />
                  Edit
                </Button>
              )}
              {canSendRfq(rfq) && (
                <Button onClick={() => setCommand('send')}>
                  <Send />
                  Send to vendors
                </Button>
              )}
              {isRfqClosable(rfq.status) && (
                <Button onClick={() => setCommand('close')}>
                  <CheckCircle2 />
                  Close RFQ
                </Button>
              )}
              {isRfqCancellable(rfq.status) && (
                <Button
                  variant="outline"
                  className="text-destructive"
                  onClick={() => setCommand('cancel')}
                >
                  <Ban />
                  Cancel
                </Button>
              )}
            </>
          ) : null
        }
      />

      <div className="space-y-4">
        <Card>
          <CardContent className="p-4">
            <StatusStepper status={rfq.status} />
            {rfq.status === 'CANCELLED' && (
              <p className="mt-3 text-sm text-destructive">
                This RFQ was cancelled
                {rfq.cancellation_reason ? `: ${rfq.cancellation_reason}` : '.'}
              </p>
            )}
          </CardContent>
        </Card>

        {/* Not an error - a draft with nobody on it is a normal half-finished
            document - but it is the one thing standing between this RFQ and
            being sent, so it is stated rather than left to be discovered when
            the Send button is missing. */}
        {isRfqSendable(rfq.status) && !hasInvitedVendors(rfq) && (
          <Alert variant="warning">
            <AlertTitle>This RFQ names no vendors</AlertTitle>
            <AlertDescription>
              It cannot be sent until at least one is added.{' '}
              {canManage ? 'Edit it to invite the suppliers you want a price from.' : ''}
            </AlertDescription>
          </Alert>
        )}

        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Enquiry</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList
                columns={3}
                items={[
                  { label: 'RFQ date', value: formatDate(rfq.rfq_date) },
                  {
                    label: 'Replies due',
                    value: rfq.response_due_date ? (
                      <span
                        className={
                          overdueDays !== null ? 'font-medium text-destructive' : undefined
                        }
                      >
                        {formatDate(rfq.response_due_date)}
                      </span>
                    ) : (
                      'No date set'
                    ),
                  },
                  { label: 'Material needed', value: formatDate(rfq.required_date) },
                  {
                    label: 'Deliver to',
                    value: warehouse ? `${warehouse.code} - ${warehouse.name}` : '-',
                  },
                  { label: 'Currency', value: <span className="font-mono">{rfq.currency}</span> },
                  {
                    label: 'Raised against',
                    // The detail carries the requisition's id only, and there
                    // is no per-requisition screen to open, so this states the
                    // provenance rather than pretending to be a link.
                    value: rfq.requisition_id ? 'A purchase requisition' : 'Raised directly',
                  },
                  { label: 'Sent to vendors', value: formatDateTime(rfq.sent_at) },
                  { label: 'Closed', value: formatDateTime(rfq.closed_at) },
                  { label: 'Created', value: formatDateTime(rfq.created_at) },
                ]}
              />
              {rfq.terms && (
                <div className="mt-5 border-t pt-4">
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Terms
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-sm">{rfq.terms}</p>
                </div>
              )}
              {rfq.notes && (
                <p className="mt-3 whitespace-pre-wrap text-sm text-muted-foreground">
                  {rfq.notes}
                </p>
              )}
            </CardContent>
          </Card>

          <VendorsCard
            vendorIds={rfq.vendor_ids}
            canEdit={canManage && isRfqEditable(rfq.status)}
            onEdit={() => setEditOpen(true)}
          />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Lines</CardTitle>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={lineColumns}
              // `lines` is optional on the wire, so an RFQ that arrives without
              // them renders the empty state rather than crashing the table.
              data={lines}
              isFetching={rfqQuery.isFetching}
              getRowId={(row) => row.id}
              emptyTitle="No lines on this RFQ"
              emptyDescription={
                canManage && isRfqEditable(rfq.status)
                  ? 'There is nothing here for a vendor to price. Edit the RFQ to add what you want quoted.'
                  : 'There is nothing here for a vendor to price.'
              }
            />
          </CardContent>
        </Card>
      </div>

      {canManage && <RfqFormDialog open={editOpen} onOpenChange={setEditOpen} rfqId={rfq.id} />}

      <RfqCommandDialog
        rfq={command ? rfq : null}
        command={command}
        onOpenChange={(open) => !open && setCommand(null)}
      />
    </>
  )
}

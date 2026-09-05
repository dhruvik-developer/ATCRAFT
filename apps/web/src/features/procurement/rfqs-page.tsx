/**
 * Requests for quotation.
 *
 * The board a buyer works from while prices are being gathered: what has gone
 * out, to whom, and who has not replied by the date they were given. Status,
 * vendor, item, warehouse and the reply-by cut-off are all query parameters the
 * API answers by those names - the enquiry book is never pulled into the
 * browser to be filtered there (master rules 31/36/78).
 *
 * Two things this screen deliberately does not show.
 *
 * There is no "3 of 5 replied" column. An RFQ's status does not move when a
 * vendor answers - `RfqStatus` says so in as many words - and neither payload
 * carries a response count, so any such column would be an invention. The
 * replies are read on the comparison screen.
 *
 * And there is no vendor or warehouse name on the row: `RfqSummary` carries
 * ids. Both are named through the bounded reference projections their modules
 * publish, which is one cached lookup per session rather than a request per row.
 *
 * The overdue flag is the reason most people open this screen. It is derived
 * only for an RFQ that has actually been sent, because a reply date that has
 * passed on a draft is a planning slip rather than a late vendor.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { MoreHorizontal, Plus, Send, TriangleAlert, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { clearSearchKeys } from './procurement-types'
import { RfqCommandDialog, RfqFormDialog } from './rfq-dialogs'
import {
  RFQ_STATUSES,
  RFQ_TERMINAL_STATUSES,
  canSendRfq,
  daysAwaitingResponse,
  isRfqCancellable,
  isRfqClosable,
  isRfqEditable,
  isRfqSendable,
  rfqKeys,
  type RfqCommand,
  type RfqRow,
} from './rfq-model'

/** Every filter this screen owns, cleared as one write rather than five. */
const FILTER_KEYS = ['status', 'vendor_id', 'item_id', 'warehouse_id', 'due_before'] as const

/** How many invited vendors are named on a row before the rest are counted. */
const VENDORS_SHOWN = 2

export default function RfqsPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [pending, setPending] = useState<{ rfq: RfqRow; command: RfqCommand } | null>(null)

  const canManage = can(P.RFQ_MANAGE)

  const status = searchParams.get('status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const itemId = searchParams.get('item_id') ?? ''
  const warehouseId = searchParams.get('warehouse_id') ?? ''
  const dueBefore = searchParams.get('due_before') ?? ''

  const list = useListQuery<RfqRow>({
    path: '/procurement/rfqs',
    queryKey: rfqKeys.list,
    defaultSortBy: 'rfq_date',
    defaultSortDir: 'desc',
    // Enquiries move all day while replies come in; a stale board sends a buyer
    // to chase a vendor who answered an hour ago.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(itemId ? { item_id: itemId } : {}),
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(dueBefore ? { due_before: dueBefore } : {}),
    },
  })

  // Names for the vendor and warehouse columns. Two bounded reference queries,
  // shared with every other screen that needs them, rather than one lookup per
  // row - and each asked for only where the principal may read that module.
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  const openForm = (rfqId: string | null) => {
    setEditingId(rfqId)
    setFormOpen(true)
  }

  const columns = useMemo<ColumnDef<RfqRow, unknown>[]>(() => {
    const defs: ColumnDef<RfqRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'RFQ',
        cell: ({ row }) => (
          <div>
            <span className="font-medium">{row.original.code}</span>
            {row.original.requisition_id && (
              // The summary carries the requisition's id and nothing else, and
              // there is no per-requisition screen to open, so this states the
              // provenance rather than pretending to be a link.
              <span className="block text-2xs text-muted-foreground">From a requisition</span>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'rfq_date',
        header: 'Raised',
        cell: ({ row }) => formatDate(row.original.rfq_date),
      },
      {
        accessorKey: 'response_due_date',
        header: 'Replies due',
        cell: ({ row }) => {
          const late = daysAwaitingResponse(row.original)
          if (!row.original.response_due_date) {
            // No date was set, so nobody can be chased against one. Said plainly
            // rather than left as a dash the reader has to interpret.
            return <span className="text-xs text-muted-foreground">No date set</span>
          }
          return (
            <span className="flex items-center gap-1.5">
              <span className={late === null ? undefined : 'font-medium text-destructive'}>
                {formatDate(row.original.response_due_date)}
              </span>
              {late !== null && (
                <Badge variant="destructive">
                  <TriangleAlert className="h-3 w-3" aria-hidden />
                  {late}d overdue
                </Badge>
              )}
            </span>
          )
        },
      },
      {
        id: 'required_date',
        header: 'Needed',
        // `required_date` is not one of the API's sortable fields (code,
        // rfq_date, response_due_date, status, created_at), so this header is
        // fixed rather than sending a sort the endpoint would reject.
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.required_date),
      },
      {
        id: 'warehouse',
        header: 'For',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs">
            {warehouseById.get(row.original.warehouse_id)?.code ?? '-'}
          </span>
        ),
      },
      {
        id: 'vendors',
        header: 'Vendors',
        enableSorting: false,
        cell: ({ row }) => {
          const ids = row.original.vendor_ids
          if (ids.length === 0) {
            // Not decoration: `transition_rfq` refuses to send an RFQ that
            // names nobody, so this row cannot move until somebody is added.
            return <Badge variant="warning">None invited</Badge>
          }
          const shown = ids.slice(0, VENDORS_SHOWN)
          return (
            <div className="flex max-w-[18rem] flex-wrap items-center gap-1">
              {shown.map((id) => (
                <Badge key={id} variant="secondary">
                  {vendorById.get(id)?.code ?? 'Not listed'}
                </Badge>
              ))}
              {ids.length > shown.length && (
                <span className="text-xs text-muted-foreground">
                  +{ids.length - shown.length} more
                </span>
              )}
            </div>
          )
        },
      },
      {
        id: 'currency',
        header: 'Currency',
        enableSorting: false,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.currency}</span>,
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ]

    // Gating is UX only - `rfq.manage` is enforced by the API on every one of
    // these endpoints, and each command is offered only from a status the state
    // machine actually has an edge out of.
    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const rfq = row.original
          // CLOSED and CANCELLED are the machine's terminal states - there is
          // no edge out of either - so a settled row offers nothing rather than
          // a menu of refusals.
          if (RFQ_TERMINAL_STATUSES.includes(rfq.status)) {
            return <span className="sr-only">This RFQ is settled</span>
          }
          const sendable = isRfqSendable(rfq.status)
          const closable = isRfqClosable(rfq.status)
          const cancellable = isRfqCancellable(rfq.status)

          return (
            <div
              className="flex items-center justify-end gap-1"
              onClick={(event) => event.stopPropagation()}
            >
              {canSendRfq(rfq) && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Send ${rfq.code} to its vendors`}
                  onClick={() => setPending({ rfq, command: 'send' })}
                >
                  <Send />
                </Button>
              )}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`More actions for ${rfq.code}`}
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {isRfqEditable(rfq.status) && (
                    <DropdownMenuItem onSelect={() => openForm(rfq.id)}>
                      Edit draft
                    </DropdownMenuItem>
                  )}
                  {/* Sending is offered only once somebody has been invited:
                      the API refuses an RFQ that names no vendors, and the fix
                      is an edit rather than a retry. */}
                  {sendable && !canSendRfq(rfq) && (
                    <DropdownMenuItem onSelect={() => openForm(rfq.id)}>
                      Add vendors before sending
                    </DropdownMenuItem>
                  )}
                  {closable && (
                    <DropdownMenuItem onSelect={() => setPending({ rfq, command: 'close' })}>
                      Close RFQ
                    </DropdownMenuItem>
                  )}
                  {cancellable && (
                    <DropdownMenuItem
                      destructive
                      onSelect={() => setPending({ rfq, command: 'cancel' })}
                    >
                      Cancel RFQ
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          )
        },
      })
    }

    return defs
  }, [canManage, vendorById, warehouseById])

  const newRfqButton = canManage ? (
    <Button onClick={() => openForm(null)}>
      <Plus />
      New RFQ
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Requests for quotation"
        description="What has been put out to the market. An RFQ is raised as a draft, sent to as many vendors as you like, and closed once one of them has been chosen."
        breadcrumbs={[{ label: 'Procurement' }, { label: 'Requests for quotation' }]}
        actions={newRfqButton}
      />

      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        // The API searches the RFQ number and nothing else, so the placeholder
        // does not promise a vendor or item search that would return nothing.
        searchPlaceholder="Search by RFQ number..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/procurement/rfqs/${row.id}`)}
        emptyTitle="No requests for quotation"
        emptyDescription="Nothing matches these filters. Raise an RFQ to ask several vendors to price the same lines, then compare what comes back."
        emptyAction={newRfqButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(next) => setParam('status', next === SELECT_ALL ? undefined : next)}
            >
              <SelectTrigger className="w-full sm:w-[11rem]" aria-label="Filter by status">
                <SelectValue placeholder="Any status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any status</SelectItem>
                {RFQ_STATUSES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* Each picker is offered only where its own module may be read:
                firing the lookup otherwise earns a 403 the buyer cannot act
                on, and the filter would sit there naming nothing. */}
            {can(P.VENDOR_VIEW) && (
              <VendorPicker
                id="rfqs-vendor"
                className="w-full sm:w-auto"
                label="Vendor"
                hideLabel
                value={vendorId || null}
                onChange={(next) => setParam('vendor_id', next ?? undefined)}
                placeholder="Any vendor"
              />
            )}

            {can(P.ITEM_VIEW) && (
              <ItemPicker
                id="rfqs-item"
                className="w-full sm:w-auto"
                label="Item"
                hideLabel
                value={itemId || null}
                onChange={(next) => setParam('item_id', next ?? undefined)}
                placeholder="Any item"
              />
            )}

            {can(P.WAREHOUSE_VIEW) && (
              <WarehouseSelect
                id="rfqs-warehouse"
                className="w-full sm:w-auto"
                label="Warehouse"
                hideLabel
                includeAll
                value={warehouseId}
                onChange={(next) => setParam('warehouse_id', next || undefined)}
              />
            )}

            <div className="flex items-center gap-1.5">
              <Label htmlFor="rfqs-due-before" className="whitespace-nowrap text-xs font-normal">
                Replies due before
              </Label>
              <Input
                id="rfqs-due-before"
                type="date"
                className="w-[10.5rem]"
                value={dueBefore}
                onChange={(event) => setParam('due_before', event.target.value || undefined)}
              />
            </div>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => clearSearchKeys(setSearchParams, FILTER_KEYS)}
              >
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />

      {canManage && (
        <RfqFormDialog
          open={formOpen}
          onOpenChange={setFormOpen}
          rfqId={editingId}
          // A new RFQ opens on its own screen: the lines and the vendor list are
          // what gets checked before it is sent, and neither fits on a row.
          onCreated={(rfq) => navigate(`/procurement/rfqs/${rfq.id}`)}
        />
      )}

      <RfqCommandDialog
        rfq={pending?.rfq ?? null}
        command={pending?.command ?? null}
        onOpenChange={(open) => !open && setPending(null)}
      />
    </>
  )
}

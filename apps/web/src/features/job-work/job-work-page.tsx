/**
 * Job work: orders sent out for processing, and what is currently sitting at a
 * vendor.
 *
 * The second tab is the one that earns the screen. Material issued for job work
 * leaves the factory but never leaves the balance sheet - it is company stock in
 * the JOB_WORK state, counted in valuation - and the only way anyone notices a
 * consignment that quietly never came back is by looking at how long it has been
 * out. That is what the ageing column is for.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  Ban,
  CheckCircle2,
  ClipboardCheck,
  Lock,
  MoreHorizontal,
  PackageCheck,
  PackageMinus,
  Pencil,
  Plus,
  Trash2,
  Truck,
  X,
} from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_ALL } from '@/lib/constants'
import {
  formatDate,
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import { JobWorkIssueDialog, JobWorkReceiveDialog } from './job-work-dialogs'
import {
  AT_VENDOR_STATUSES,
  JOB_WORK_STATUSES,
  acceptsIssue,
  acceptsReceipt,
  clearSearchKeys,
  daysOut,
  daysOverdue,
  isApprovable,
  isCancellable,
  isClosable,
  isDeletable,
  isEditable,
  jobWorkKeys,
  pendingRowKey,
  type JobWorkOrderRow,
  type JobWorkStatus,
  type PendingAtVendorRow,
} from './job-work-model'
import { JobWorkOrderFormDialog } from './job-work-order-form-dialog'

const reasonReady = (value: string): boolean => {
  const trimmed = value.trim()
  return trimmed.length >= REASON_MIN_LENGTH && trimmed.length <= REASON_MAX_LENGTH
}

/**
 * The audit reason the API requires on an approve, a close, a cancel or a
 * delete.
 *
 * Every one of those endpoints refuses a request without it, so it is collected
 * rather than invented - a hardcoded string would satisfy the validator and
 * tell whoever reads the audit trail nothing.
 */
function ReasonField({
  id,
  value,
  onChange,
  invalid = false,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  /** Set once a confirmation was attempted with a reason too short to send. */
  invalid?: boolean
}) {
  return (
    <div className="mt-3">
      <Label htmlFor={id} required>
        Reason
      </Label>
      <Input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
        maxLength={REASON_MAX_LENGTH}
        className="mt-1.5"
        placeholder="Recorded in the audit trail"
        aria-invalid={invalid}
      />
      {invalid && (
        <FieldError message={`At least ${REASON_MIN_LENGTH} characters - the API records this`} />
      )}
    </div>
  )
}

/** The order panel's filters, cleared as one write rather than three. */
const ORDER_FILTER_KEYS = ['status', 'vendor_id', 'overdue_only'] as const

/** The pending-at-vendor panel's filters, cleared as one write rather than two. */
const PENDING_FILTER_KEYS = ['vendor_id', 'overdue_only'] as const

/**
 * How long the material has been out, and whether it is late.
 *
 * Late only counts while the stock is actually at the vendor: an order received
 * three weeks after its promised date is history, not an exception to chase.
 */
function AgeingCell({
  issuedAt,
  expectedReturnDate,
  status,
}: {
  issuedAt: string | null
  expectedReturnDate: string | null
  status: JobWorkStatus | null
}) {
  const out = daysOut(issuedAt)
  const overdue = daysOverdue(expectedReturnDate)
  const stillOut = status === null || AT_VENDOR_STATUSES.includes(status)

  if (out === null) return <span className="text-muted-foreground">Not issued</span>

  return (
    <span className="flex items-center gap-1.5">
      <span className="tabular">{out}d</span>
      {overdue !== null && stillOut && <Badge variant="destructive">{overdue}d overdue</Badge>}
    </span>
  )
}

// ---------------------------------------------------------------- orders ----

function OrdersPanel({ newOrderButton }: { newOrderButton: ReactNode }) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [issueFor, setIssueFor] = useState<JobWorkOrderRow | null>(null)
  const [receiveFor, setReceiveFor] = useState<JobWorkOrderRow | null>(null)
  const [approveFor, setApproveFor] = useState<JobWorkOrderRow | null>(null)
  const [closeFor, setCloseFor] = useState<JobWorkOrderRow | null>(null)
  const [amendFor, setAmendFor] = useState<JobWorkOrderRow | null>(null)
  const [cancelFor, setCancelFor] = useState<JobWorkOrderRow | null>(null)
  const [deleteFor, setDeleteFor] = useState<JobWorkOrderRow | null>(null)
  /**
   * The audit reason, shared by the four confirmations that need one.
   *
   * Only one of them can be open at a time - each is opened from a row menu -
   * so a single box is the whole state, and `reasonTouched` is what turns a
   * click on a confirm button with nothing typed into a visible message rather
   * than a button that silently does nothing.
   */
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)

  const clearReason = () => {
    setReason('')
    setReasonTouched(false)
  }

  // Each action is gated on the right its own endpoint requires, read from
  // job_work/router.py rather than guessed: approve and close both sit behind
  // `job_work.close`. An earlier comment here claimed the catalogue had no
  // dedicated approve or close right and substituted issue/receive - it does
  // have `job_work.close`, so that gating showed both actions to people the API
  // would refuse, and hid them from the people who hold the right.
  const canApprove = can(P.JOB_WORK_CLOSE)
  const canIssue = can(P.JOB_WORK_ISSUE)
  const canReceive = can(P.JOB_WORK_RECEIVE)
  const canClose = can(P.JOB_WORK_CLOSE)
  // Raising an order, amending it, cancelling it and deleting it are one right:
  // `POST /job-work`, `PATCH /job-work/{id}`, `DELETE /job-work/{id}` and
  // `POST /job-work/{id}/cancel` all declare `job_work.create`. The API treats
  // raising a document and un-raising it as the same duty, so none of these is
  // gated on the senior `job_work.close` right the approve and close actions
  // above use.
  const canCreate = can(P.JOB_WORK_CREATE)

  // Names for the vendor column. One bounded reference query, shared with every
  // other screen that needs it, rather than a lookup per row.
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  const status = searchParams.get('status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const overdueOnly = searchParams.get('overdue_only') === 'true'

  const list = useListQuery<JobWorkOrderRow>({
    path: '/job-work',
    queryKey: jobWorkKeys.list,
    defaultSortBy: 'created_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      // `GET /job-work` takes `at_vendor_only`, not `overdue_only` - the latter
      // was silently ignored, so the "overdue" toggle changed nothing. The
      // closest supported filter is "material currently out at a vendor".
      ...(overdueOnly ? { at_vendor_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount = (status ? 1 : 0) + (vendorId ? 1 : 0) + (overdueOnly ? 1 : 0)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
  }

  // Both transitions are audited and the API requires the reason in the
  // body; without it the request is refused outright.
  const approve = useMutation({
    mutationFn: ({ order, why }: { order: JobWorkOrderRow; why: string }) =>
      api.post<JobWorkOrderRow>(`/job-work/${order.id}/approve`, { reason: why }),
    onSuccess: (updated) => {
      invalidate()
      toast.success(
        success.approved('Job work order'),
        `${updated.code} can now be issued to the vendor.`,
      )
      setApproveFor(null)
      clearReason()
    },
  })

  const close = useMutation({
    mutationFn: ({ order, why }: { order: JobWorkOrderRow; why: string }) =>
      api.post<JobWorkOrderRow>(`/job-work/${order.id}/close`, { reason: why }),
    onSuccess: (updated) => {
      invalidate()
      toast.success(success.closed('Job work order'), `${updated.code} is closed.`)
      setCloseFor(null)
      clearReason()
    },
  })

  const cancel = useMutation({
    mutationFn: ({ order, why }: { order: JobWorkOrderRow; why: string }) =>
      api.post<JobWorkOrderRow>(`/job-work/${order.id}/cancel`, { reason: why }),
    onSuccess: (updated) => {
      invalidate()
      // Cancelling an APPROVED order releases its reservations in the same
      // transaction, which changes what every other document can be promised.
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      toast.success(
        success.cancelled('Job work order'),
        `${updated.code} is cancelled and its material released.`,
      )
      setCancelFor(null)
      clearReason()
    },
  })

  // The one endpoint in this module that takes its reason in the query string
  // rather than the body: a DELETE body is legal but several clients and
  // proxies drop it, so the API declares the reason as a parameter. Sending it
  // in a body here would be a 422 at the moment of use.
  const remove = useMutation({
    mutationFn: ({ order, why }: { order: JobWorkOrderRow; why: string }) =>
      api.delete(`/job-work/${order.id}`, { params: { reason: why } }),
    onSuccess: (_result, { order }) => {
      // A draft holds no reservation and has moved no stock, so only the
      // job-work caches are stale.
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      toast.success(success.deleted('Draft'), `${order.code} is gone.`)
      setDeleteFor(null)
      clearReason()
    },
  })

  const columns = useMemo<ColumnDef<JobWorkOrderRow, unknown>[]>(() => {
    const defs: ColumnDef<JobWorkOrderRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Order',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        // The row carries `vendor_id` and no name. The bounded vendor options
        // list turns it into one; a vendor missing from that projection (a
        // suspended one) is a dash rather than eight hex characters.
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const vendor = vendorById.get(row.original.vendor_id)
          if (!vendor) return <span className="text-muted-foreground">-</span>
          return (
            <div className="max-w-[18rem]">
              <span className="block truncate">{vendor.name}</span>
              <span className="block truncate text-xs text-muted-foreground">{vendor.code}</span>
            </div>
          )
        },
      },
      {
        id: 'process_type',
        header: 'Process',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[14rem]">
            <Badge variant="secondary">{humanise(row.original.process_type)}</Badge>
            {row.original.process_description && (
              <span className="mt-0.5 block truncate text-2xs text-muted-foreground">
                {row.original.process_description}
              </span>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'expected_return_date',
        header: 'Expected return',
        cell: ({ row }) => formatDate(row.original.expected_return_date),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <StatusBadge status={row.original.status} />
            {row.original.requires_qc && <Badge variant="muted">QC</Badge>}
          </span>
        ),
      },
      {
        // Quantities live on the order's lines, which only the detail endpoint
        // returns. What the list does carry is how many lots have come back,
        // which is the progress signal this column existed to give.
        //
        // Not sortable: `receipt_count` is not one of the fields the API allows
        // in ORDER BY, and an unknown sort field is refused outright rather than
        // ignored - so a click on this header used to empty the table with a
        // validation error.
        accessorKey: 'receipt_count',
        header: 'Receipts',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatNumber(row.original.receipt_count)}</span>
        ),
      },
      {
        accessorKey: 'total_charges',
        header: 'Charges',
        meta: { align: 'right' },
        // Nothing has been billed until a receipt is priced, so an unbilled
        // order shows the estimate instead. "Unbilled" is decided with
        // decimal.js: the charge is a NUMERIC(18,4) and arrives as "0.0000",
        // which never equals the string '0' - that comparison read every
        // unbilled order as billed and showed a zero where the estimate belongs.
        cell: ({ row }) => (
          <span className="tabular">
            {formatMoney(
              toDecimal(row.original.total_charges).isZero()
                ? row.original.estimated_charges
                : row.original.total_charges,
            )}
          </span>
        ),
      },
      {
        // Material the vendor lost or spoiled and is being charged for. It is
        // money owed back to the company, so it belongs in the scan.
        id: 'vendor_debit_amount',
        header: 'Vendor debit',
        enableSorting: false,
        meta: { align: 'right' },
        // Also a decimal string, so also compared through decimal.js rather
        // than against a literal that the wire format never produces.
        cell: ({ row }) =>
          toDecimal(row.original.vendor_debit_amount).isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="tabular font-medium text-destructive">
              {formatMoney(row.original.vendor_debit_amount)}
            </span>
          ),
      },
      {
        id: 'ageing',
        header: 'Days out',
        enableSorting: false,
        cell: ({ row }) => (
          <AgeingCell
            issuedAt={row.original.issued_at}
            expectedReturnDate={row.original.expected_return_date}
            status={row.original.status}
          />
        ),
      },
    ]

    if (canApprove || canIssue || canReceive || canClose || canCreate) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const order = row.original
          const showAmend = canCreate && isEditable(order.status)
          const showApprove = canApprove && isApprovable(order.status)
          const showIssue = canIssue && acceptsIssue(order.status)
          const showReceive = canReceive && acceptsReceipt(order.status)
          const showClose = canClose && isClosable(order.status)
          const showCancel = canCreate && isCancellable(order.status)
          const showDelete = canCreate && isDeletable(order.status)

          if (
            !showAmend &&
            !showApprove &&
            !showIssue &&
            !showReceive &&
            !showClose &&
            !showCancel &&
            !showDelete
          ) {
            return <span className="text-muted-foreground">-</span>
          }

          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${order.code}`}
                  onClick={(event) => event.stopPropagation()}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>{order.code}</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {showAmend && (
                  <DropdownMenuItem onSelect={() => setAmendFor(order)}>
                    <Pencil />
                    Amend draft
                  </DropdownMenuItem>
                )}
                {showApprove && (
                  <DropdownMenuItem onSelect={() => setApproveFor(order)}>
                    <CheckCircle2 />
                    Approve
                  </DropdownMenuItem>
                )}
                {showIssue && (
                  <DropdownMenuItem onSelect={() => setIssueFor(order)}>
                    <PackageMinus />
                    Issue to vendor
                  </DropdownMenuItem>
                )}
                {showReceive && (
                  <DropdownMenuItem onSelect={() => setReceiveFor(order)}>
                    <PackageCheck />
                    Receive output
                  </DropdownMenuItem>
                )}
                {showClose && (
                  <DropdownMenuItem onSelect={() => setCloseFor(order)}>
                    <Lock />
                    Close order
                  </DropdownMenuItem>
                )}
                {(showCancel || showDelete) && <DropdownMenuSeparator />}
                {showCancel && (
                  <DropdownMenuItem destructive onSelect={() => setCancelFor(order)}>
                    <Ban />
                    Cancel order
                  </DropdownMenuItem>
                )}
                {showDelete && (
                  <DropdownMenuItem destructive onSelect={() => setDeleteFor(order)}>
                    <Trash2 />
                    Delete draft
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )
        },
      })
    }

    return defs
  }, [canApprove, canIssue, canReceive, canClose, canCreate, vendorById])

  return (
    <>
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
        // The API searches the order code and the process description; it does
        // not search the vendor, so the box does not offer to.
        searchPlaceholder="Search order code or process..."
        getRowId={(row) => row.id}
        emptyTitle="No job work orders found"
        emptyDescription="Nothing matches the current filters. Clear a filter to see the rest."
        emptyAction={newOrderButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[190px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {JOB_WORK_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <VendorPicker
              id="job-work-vendor-filter"
              label="Vendor"
              hideLabel
              value={vendorId || null}
              onChange={(value) => setParam('vendor_id', value ?? undefined)}
              placeholder="All vendors"
            />

            <Button
              variant={overdueOnly ? 'default' : 'outline'}
              size="sm"
              aria-pressed={overdueOnly}
              onClick={() => setParam('overdue_only', overdueOnly ? undefined : 'true')}
            >
              <Truck />
              At a vendor now
            </Button>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => clearSearchKeys(setSearchParams, ORDER_FILTER_KEYS)}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {issueFor && (
        <JobWorkIssueDialog
          open
          onOpenChange={(open) => !open && setIssueFor(null)}
          orderId={issueFor.id}
          orderCode={issueFor.code}
        />
      )}

      {receiveFor && (
        <JobWorkReceiveDialog
          open
          onOpenChange={(open) => !open && setReceiveFor(null)}
          orderId={receiveFor.id}
          orderCode={receiveFor.code}
        />
      )}

      {amendFor && (
        <JobWorkOrderFormDialog
          open
          onOpenChange={(open) => !open && setAmendFor(null)}
          order={amendFor}
        />
      )}

      <ConfirmDialog
        open={approveFor !== null}
        onOpenChange={(open) => {
          if (!open) {
            setApproveFor(null)
            clearReason()
          }
        }}
        title={approveFor ? `Approve ${approveFor.code}?` : ''}
        description="Approval confirms the vendor, the process and the rate. The order can then be issued against a challan."
        confirmLabel="Approve order"
        loading={approve.isPending}
        onConfirm={() => {
          if (!approveFor) return
          if (!reasonReady(reason)) {
            setReasonTouched(true)
            return
          }
          approve.mutate({ order: approveFor, why: reason.trim() })
        }}
      >
        <ReasonField
          value={reason}
          onChange={setReason}
          invalid={reasonTouched && !reasonReady(reason)}
          id="job-work-approve-reason"
        />
      </ConfirmDialog>

      <ConfirmDialog
        open={closeFor !== null}
        onOpenChange={(open) => {
          if (!open) {
            setCloseFor(null)
            clearReason()
          }
        }}
        title={closeFor ? `Close ${closeFor.code}?` : ''}
        description="Closing settles the order. Anything still recorded as at the vendor stays on the books as company stock, so close only once the balance has been received or written off as loss."
        confirmLabel="Close order"
        destructive
        loading={close.isPending}
        onConfirm={() => {
          if (!closeFor) return
          if (!reasonReady(reason)) {
            setReasonTouched(true)
            return
          }
          close.mutate({ order: closeFor, why: reason.trim() })
        }}
      >
        <ReasonField
          value={reason}
          onChange={setReason}
          invalid={reasonTouched && !reasonReady(reason)}
          id="job-work-close-reason"
        />
      </ConfirmDialog>

      <ConfirmDialog
        open={cancelFor !== null}
        onOpenChange={(open) => {
          if (!open) {
            setCancelFor(null)
            clearReason()
          }
        }}
        title={cancelFor ? `Cancel ${cancelFor.code}?` : ''}
        description="The order is closed out as cancelled and every reservation it holds is released in the same transaction, so the material becomes available to other documents immediately. It stays on the list as a cancelled order - the trail is kept. Cancellation is only offered before the material has left; once a challan exists the order is the record of where company stock went, and it is settled by a final receipt instead."
        confirmLabel="Cancel order"
        destructive
        loading={cancel.isPending}
        onConfirm={() => {
          if (!cancelFor) return
          if (!reasonReady(reason)) {
            setReasonTouched(true)
            return
          }
          cancel.mutate({ order: cancelFor, why: reason.trim() })
        }}
      >
        <ReasonField
          value={reason}
          onChange={setReason}
          invalid={reasonTouched && !reasonReady(reason)}
          id="job-work-cancel-reason"
        />
      </ConfirmDialog>

      <ConfirmDialog
        open={deleteFor !== null}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteFor(null)
            clearReason()
          }
        }}
        title={deleteFor ? `Delete draft ${deleteFor.code}?` : ''}
        description="This removes the draft outright - the order and its lines are gone, and the code is not reused. Nothing else references an unapproved order, so no history is lost, but the deletion itself is what the audit trail keeps. Cancel it instead if the order should stay visible."
        confirmLabel="Delete draft"
        destructive
        loading={remove.isPending}
        onConfirm={() => {
          if (!deleteFor) return
          if (!reasonReady(reason)) {
            setReasonTouched(true)
            return
          }
          remove.mutate({ order: deleteFor, why: reason.trim() })
        }}
      >
        <ReasonField
          value={reason}
          onChange={setReason}
          invalid={reasonTouched && !reasonReady(reason)}
          id="job-work-delete-reason"
        />
      </ConfirmDialog>
    </>
  )
}

// ----------------------------------------------------- pending at vendor ----

function PendingPanel() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const vendorId = searchParams.get('vendor_id') ?? ''
  const overdueOnly = searchParams.get('overdue_only') === 'true'

  // The rows carry ids only. Both lookups are bounded reference lists shared
  // with the rest of the app, and each fails soft to a dash rather than a uuid.
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const list = useListQuery<PendingAtVendorRow>({
    path: '/job-work/pending-at-vendor',
    queryKey: jobWorkKeys.pendingAtVendor,
    // This endpoint takes no sort parameters: it returns its own grouping,
    // by vendor and item, with the ageing already computed. The vendor label is
    // printed once per run rather than repeated down the page.
    withTotal: true,
    staleTime: staleTimes.live,
    filters: {
      ...(vendorId ? { vendor_id: vendorId } : {}),
      // This is where `overdue_only` is actually supported.
      ...(overdueOnly ? { overdue_only: true } : {}),
    },
  })

  const { setParam, data: rows } = list

  const columns = useMemo<ColumnDef<PendingAtVendorRow, unknown>[]>(() => {
    const defs: ColumnDef<PendingAtVendorRow, unknown>[] = [
      {
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const previous = rows[row.index - 1]
          // Only the first row of each vendor's run is labelled: the page reads
          // as groups instead of a wall of repeated text.
          if (previous && previous.vendor_id === row.original.vendor_id) return null
          const vendor = vendorById.get(row.original.vendor_id)
          if (!vendor) return <span className="text-muted-foreground">-</span>
          return (
            <div className="max-w-[18rem]">
              <span className="block truncate font-medium">{vendor.name}</span>
              <span className="block truncate text-xs text-muted-foreground">{vendor.code}</span>
            </div>
          )
        },
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.item_id}`}
            className="font-mono text-xs hover:underline"
            title="Open this item"
          >
            {row.original.item_id.slice(0, 8)}
          </Link>
        ),
      },
      {
        id: 'warehouse',
        header: 'From store',
        enableSorting: false,
        cell: ({ row }) => {
          const warehouse = warehouseById.get(row.original.warehouse_id)
          return warehouse ? (
            <span title={warehouse.name}>{warehouse.code}</span>
          ) : (
            <span className="text-muted-foreground">-</span>
          )
        },
      },
      {
        // The row is an aggregate over open orders, not one challan, so the
        // number of orders behind the figure is part of reading it.
        accessorKey: 'order_count',
        header: 'Orders',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatNumber(row.original.order_count)}</span>
        ),
      },
      {
        accessorKey: 'outstanding_quantity',
        header: 'At vendor',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.outstanding_quantity)}</span>
        ),
      },
      {
        accessorKey: 'oldest_issued_at',
        header: 'Oldest issue',
        cell: ({ row }) => formatDate(row.original.oldest_issued_at),
      },
      {
        accessorKey: 'earliest_expected_return',
        header: 'Earliest due back',
        cell: ({ row }) => formatDate(row.original.earliest_expected_return),
      },
      {
        // The API computes the ageing and decides whether it is late; those are
        // properties of the whole group, not of a date this screen can subtract.
        id: 'ageing',
        header: 'Days out',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <span className="tabular">{formatNumber(row.original.ageing_days)}d</span>
            {row.original.is_overdue && <Badge variant="destructive">Overdue</Badge>}
          </span>
        ),
      },
    ]

    // The valuation column is gone: `GET /job-work/pending-at-vendor` no longer
    // returns a value for the outstanding quantity, and there is no other
    // endpoint that values it per vendor and item. Valuing it here would mean
    // pricing every row in the browser, which is not this screen's job.

    return defs
  }, [rows, vendorById, warehouseById])

  return (
    <div className="space-y-4">
      <Alert variant="info">
        <AlertTitle>This stock is still yours</AlertTitle>
        <AlertDescription>
          Material issued for job work sits at the vendor but never leaves the company&apos;s books:
          it is held in the job-work stock state and is included in inventory valuation. Anything
          ageing here is working capital outside the factory, and a consignment nobody chases is a
          write-off waiting to be discovered at year end.
        </AlertDescription>
      </Alert>

      {/* No search box and no sortable headers: `GET /job-work/pending-at-vendor`
          accepts neither a search term nor a sort, and this list is paged on the
          server - so a control wired here would either narrow nothing or, worse,
          narrow only the page in front of the reader. The endpoint returns its
          own grouping with the ageing already computed. */}
      <DataTable
        columns={columns}
        data={rows}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onRefresh={() => void list.refetch()}
        getRowId={pendingRowKey}
        emptyTitle="Nothing is at a vendor"
        emptyDescription="Every job-work consignment has been received back. Nothing is outstanding."
        toolbar={
          <>
            <VendorPicker
              id="pending-vendor-filter"
              label="Vendor"
              hideLabel
              value={vendorId || null}
              onChange={(value) => setParam('vendor_id', value ?? undefined)}
              placeholder="All vendors"
            />
            <Button
              variant={overdueOnly ? 'default' : 'outline'}
              size="sm"
              aria-pressed={overdueOnly}
              onClick={() => setParam('overdue_only', overdueOnly ? undefined : 'true')}
            >
              <Truck />
              Overdue only
            </Button>

            {(vendorId || overdueOnly) && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => clearSearchKeys(setSearchParams, PENDING_FILTER_KEYS)}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />
    </div>
  )
}

// ------------------------------------------------------------------ page ----

export default function JobWorkPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  const requested = searchParams.get('tab') ?? ''
  const tab = requested === 'pending' ? 'pending' : 'orders'

  // Switching tab drops the list parameters: a status filter typed against the
  // orders means nothing against the pending-at-vendor view, and carrying it
  // over shows an empty table with no visible reason.
  const setTab = (value: string) =>
    setSearchParams(
      () => {
        const next = new URLSearchParams()
        next.set('tab', value)
        return next
      },
      { replace: true },
    )

  // `POST /job-work` declares `job_work.create` - the same right the amend,
  // cancel and delete actions in the row menu are gated on, and not the
  // `job_work.close` one that approving and closing an order need.
  const newOrderButton = can(P.JOB_WORK_CREATE) ? (
    <Button onClick={() => setCreateOpen(true)}>
      <Plus />
      New job work order
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Job work"
        description="Processing done outside the factory. The material goes out on a challan and stays company stock until it comes back."
        // Only on the orders tab. Nothing is raised from the pending-at-vendor
        // view: it is an aggregate over open orders, not a list of documents.
        actions={tab === 'orders' ? newOrderButton : null}
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="orders">
            <ClipboardCheck className="h-3.5 w-3.5" aria-hidden />
            Orders
          </TabsTrigger>
          <TabsTrigger value="pending">
            <Truck className="h-3.5 w-3.5" aria-hidden />
            Pending at vendor
          </TabsTrigger>
        </TabsList>

        <TabsContent value="orders">
          <OrdersPanel newOrderButton={newOrderButton} />
        </TabsContent>

        <TabsContent value="pending">
          <PendingPanel />
        </TabsContent>
      </Tabs>

      <JobWorkOrderFormDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  )
}

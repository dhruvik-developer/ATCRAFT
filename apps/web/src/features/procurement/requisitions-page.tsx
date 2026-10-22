/**
 * Purchase requisition list.
 *
 * A requisition is the demand signal that precedes a purchase order - raised by
 * hand or converted from an MRP suggestion - and this screen is where its whole
 * life before sourcing happens: raised, amended, submitted, decided, withdrawn
 * or thrown away.
 *
 * Two different rights meet here, and they are not interchangeable. Raising,
 * amending, submitting, cancelling and deleting are all
 * `purchase_requisition.create` - the requester's side of the document. Approve
 * and reject are `purchase_requisition.approve`, and the API additionally
 * refuses an approver who is the person that raised it. Each affordance is gated
 * on the right its own endpoint enforces, never on a neighbouring one.
 *
 * Every action is offered only from a status the state machine actually permits,
 * through the `isRequisition*` predicates in `procurement-types.ts`. Offering an
 * action the backend will refuse puts somebody through a dialog for nothing.
 *
 * Approved demand leaves this screen as purchase orders. "Raise purchase orders"
 * converts one requisition, and the header's batch action several at once, into
 * one draft order per vendor and item category - `purchase_order.create`, the
 * right both conversion endpoints enforce, offered only where the row still has
 * a line to order. Closing approved or converted demand that will never be
 * ordered is the approver's `purchase_requisition.approve`, the right the
 * `/close` route and both CLOSED edges of the machine declare.
 *
 * `RequisitionListRow` is a header plus `open_line_count`: it carries no
 * estimated value, because that lives on the lines. It also carries ids rather
 * than names for the warehouse and department, which are named here through the
 * same bounded reference lookups the rest of the app uses.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { Layers, MoreHorizontal, Plus, X } from 'lucide-react'
import { Suspense, lazy, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import {
  ConfirmDialog,
  DateRangeFilter,
  FieldError,
  PageHeader,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useDepartmentOptions } from '@/hooks/use-organisation-options'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_ALL } from '@/lib/constants'
import { daysUntil, formatDate, formatDateTime, humanise } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import { RequisitionFormDialog } from './requisition-form-dialog'
import {
  ACTIONABLE_REQUISITION_STATUSES,
  CONVERTIBLE_REQUISITION_STATUSES,
  REQUISITION_STATUSES,
  clearSearchKeys,
  isRequisitionAmendable,
  isRequisitionCancellable,
  isRequisitionClosable,
  isRequisitionConvertible,
  isRequisitionDecidable,
  isRequisitionDeletable,
  isRequisitionSubmittable,
  isUrgentPriority,
  requisitionKeys,
  type RequisitionConversionTarget,
  type RequisitionRow,
  type RequisitionSummary,
} from './procurement-types'
import { serverFieldMessage } from './server-field-errors'

/**
 * The conversion dialogs are the heaviest thing a buyer can open here, and most
 * visits to the queue never open them, so they arrive in their own chunk - one
 * module, both dialogs - on first use.
 */
const RequisitionConversionDialog = lazy(() => import('./requisition-conversion-dialog'))
const RequisitionBatchPicker = lazy(() =>
  import('./requisition-conversion-dialog').then((m) => ({ default: m.RequisitionBatchPicker })),
)

/** Every filter this screen owns, cleared as one write rather than four. */
const FILTER_KEYS = ['status', 'required_from', 'required_to', 'open_only'] as const

type Decision = { requisition: RequisitionRow; action: 'approve' | 'reject' }

/**
 * The commands that carry a recorded reason: the requester's three, and the
 * approver's close.
 */
type CommandKind = 'submit' | 'cancel' | 'delete' | 'close'
type Command = { requisition: RequisitionRow; kind: CommandKind }

/**
 * What each command does, and what the API needs recorded with it.
 *
 * `reasonRequired` is the contract rather than a house style. `submit` posts an
 * `ApproveRequest`, whose reason is optional - the API substitutes "Submitted
 * for approval" when it is null. `cancel` posts a `ReasonRequest`, which
 * requires 3-255 characters and lands on the document as its cancellation
 * reason. `delete` takes its reason as a *query parameter*, also required and
 * also 3-255, and it is the only trace the deleted draft leaves behind. `close`
 * posts a `ReasonRequest` too: whatever is still unordered will never be, and
 * the reason is what explains that to the next person who looks.
 */
const COMMANDS: Record<
  CommandKind,
  {
    title: (code: string) => string
    description: string
    confirmLabel: string
    destructive: boolean
    reasonRequired: boolean
    reasonLabel: string
    reasonHint: string
    toastTitle: string
    toastBody: (code: string) => string
  }
> = {
  submit: {
    title: (code) => `Submit ${code} for approval?`,
    description:
      'The requisition joins the approval queue. It can no longer be edited or deleted once it is there - only withdrawn.',
    confirmLabel: 'Submit for approval',
    destructive: false,
    reasonRequired: false,
    reasonLabel: 'Note',
    reasonHint: 'Optional. Recorded on the status change for whoever picks it up.',
    toastTitle: success.submitted('Requisition'),
    toastBody: (code) => `${code} is waiting for a decision.`,
  },
  cancel: {
    title: (code) => `Cancel ${code}?`,
    description:
      'The demand is called off and nothing more can be sourced against it. The requisition stays on the record with your reason on it, and cancelling is final.',
    confirmLabel: 'Cancel requisition',
    destructive: true,
    reasonRequired: true,
    reasonLabel: 'Reason',
    reasonHint: 'Recorded on the document as its cancellation reason.',
    toastTitle: success.cancelled('Requisition'),
    toastBody: (code) => `${code} is closed and nothing will be sourced against it.`,
  },
  delete: {
    title: (code) => `Delete draft ${code}?`,
    description:
      'The draft and its lines are removed outright. Nothing has been decided against it, so nothing else is affected - but the document number does not come back.',
    confirmLabel: 'Delete draft',
    destructive: true,
    reasonRequired: true,
    reasonLabel: 'Reason',
    reasonHint: 'The audit row is all that survives the draft, so it needs to say why.',
    toastTitle: success.deleted('Draft requisition'),
    toastBody: (code) => `${code} was deleted.`,
  },
  close: {
    title: (code) => `Close ${code}?`,
    description:
      'Whatever is still unordered on it will not be ordered, and it stops counting as open demand. Purchase orders already raised against it are not affected. Closing is final.',
    confirmLabel: 'Close requisition',
    destructive: false,
    reasonRequired: true,
    reasonLabel: 'Reason',
    reasonHint:
      'Recorded against the status change, for whoever wonders why the rest was never ordered.',
    toastTitle: success.closed('Requisition'),
    toastBody: (code) => `${code} no longer counts as open demand.`,
  },
}

export default function RequisitionsPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [decision, setDecision] = useState<Decision | null>(null)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  // What the API said about the reason, if it refused it. The global handler
  // stays quiet on a 422 that names a field, on the understanding that the
  // form shows it - and these dialogs are the form.
  const [serverReason, setServerReason] = useState<string | null>(null)

  const [command, setCommand] = useState<Command | null>(null)
  const [commandReason, setCommandReason] = useState('')
  const [commandTouched, setCommandTouched] = useState(false)
  const [commandServerReason, setCommandServerReason] = useState<string | null>(null)

  // `null` inside means "raise a new one"; an id amends that draft.
  const [form, setForm] = useState<{ requisitionId: string | null } | null>(null)

  // What is being converted into purchase orders, and whether the batch picker
  // is open. Each dialog is mounted only while it is in use, so its chunk is
  // fetched on first use and its state starts clean every time.
  const [conversion, setConversion] = useState<RequisitionConversionTarget | null>(null)
  const [picking, setPicking] = useState(false)

  const canDecide = can(P.PURCHASE_REQUISITION_APPROVE)
  // Both conversion endpoints - one requisition and the batch - are declared
  // `require_permission(Permission.PURCHASE_ORDER_CREATE)`: converting is
  // raising purchase orders, whoever raised the requisition.
  const canConvert = can(P.PURCHASE_ORDER_CREATE)
  // Raise, amend, submit, cancel and delete are one right on the API: every one
  // of those five endpoints is declared
  // `require_permission(Permission.PURCHASE_REQUISITION_CREATE)`. Cancel is the
  // one worth spelling out - the APPROVED -> CANCELLED edge is *labelled* with
  // the approve right in `REQUISITION_LIFECYCLE`, but the route enforces the
  // create right and the route is what refuses.
  const canRaise = can(P.PURCHASE_REQUISITION_CREATE)

  const status = searchParams.get('status') ?? ''
  const requiredFrom = searchParams.get('required_from') ?? ''
  const requiredTo = searchParams.get('required_to') ?? ''
  const openOnly = searchParams.get('open_only') === 'true'

  // The window is on the required-by date, which is the one the API filters on
  // and the one a buyer works the queue by. `open_only` is the API's own
  // definition of unmet demand - approved or partly converted - not "not yet
  // decided", which is why the label says what it selects.
  const list = useListQuery<RequisitionRow>({
    path: '/procurement/purchase-requisitions',
    queryKey: requisitionKeys.list,
    defaultSortBy: 'requisition_date',
    defaultSortDir: 'desc',
    // Requisitions move through approval all day; a stale queue sends a buyer
    // to a document somebody else has already decided.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(requiredFrom ? { required_from: requiredFrom } : {}),
      ...(requiredTo ? { required_to: requiredTo } : {}),
      ...(openOnly ? { open_only: true } : {}),
    },
  })

  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])
  // Departments resolve to an empty list without company.view, and a missing
  // department label is decoration on this queue rather than the point of it.
  const departments = useDepartmentOptions()
  const departmentById = useMemo(() => indexById(departments.data), [departments.data])

  const { setParam } = list
  const filterCount =
    (status ? 1 : 0) + (requiredFrom ? 1 : 0) + (requiredTo ? 1 : 0) + (openOnly ? 1 : 0)

  const decide = useMutation({
    mutationFn: ({ requisition, action, reason: text }: Decision & { reason: string }) =>
      api.post<RequisitionSummary>(
        `/procurement/purchase-requisitions/${requisition.id}/${action}`,
        // A rejection must carry its reason; an approval may add a note.
        { reason: text === '' ? null : text },
      ),
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      toast.success(
        variables.action === 'approve'
          ? success.approved('Requisition')
          : success.rejected('Requisition'),
        `${variables.requisition.code} is now ${variables.action === 'approve' ? 'ready to source' : 'closed'}.`,
      )
      closeDecision()
    },
    onError: (error) => setServerReason(serverFieldMessage(error, 'reason')),
  })

  const run = useMutation({
    mutationFn: async ({
      requisition,
      kind,
      reason: text,
    }: Command & { reason: string }): Promise<void> => {
      // Each path is written out rather than assembled from a `base` and the
      // command name. `test_frontend_permission_gates` recovers the route shape
      // from the literal it can see, and a path built out of two variables
      // resolves to nothing - so the guard would quietly stop covering these
      // three calls, which is precisely the class of defect it exists to catch.
      const { id } = requisition
      if (kind === 'delete') {
        // The reason is a query parameter on this endpoint, not a body. A DELETE
        // body would simply never arrive and the call would be a 422.
        await api.delete(`/procurement/purchase-requisitions/${id}`, { params: { reason: text } })
        return
      }
      if (kind === 'submit') {
        // `ApproveRequest` tolerates a null reason and the API writes its own
        // "Submitted for approval" in its place.
        await api.post<RequisitionSummary>(`/procurement/purchase-requisitions/${id}/submit`, {
          reason: text === '' ? null : text,
        })
        return
      }
      if (kind === 'close') {
        // `ReasonRequest`, recorded on the APPROVED/CONVERTED -> CLOSED change.
        await api.post<RequisitionSummary>(`/procurement/purchase-requisitions/${id}/close`, {
          reason: text,
        })
        return
      }
      // `ReasonRequest` - refused without a reason, and what is written to the
      // document as its cancellation reason.
      await api.post<RequisitionSummary>(`/procurement/purchase-requisitions/${id}/cancel`, {
        reason: text,
      })
    },
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      // A deleted draft has no detail left to serve; dropping its cached entry
      // stops a stale copy answering if the id is opened again.
      if (variables.kind === 'delete') {
        queryClient.removeQueries({ queryKey: requisitionKeys.detail(variables.requisition.id) })
      }
      const copy = COMMANDS[variables.kind]
      toast.success(copy.toastTitle, copy.toastBody(variables.requisition.code))
      closeCommand()
    },
    onError: (error) => setCommandServerReason(serverFieldMessage(error, 'reason')),
  })

  const columns = useMemo<ColumnDef<RequisitionRow, unknown>[]>(() => {
    const defs: ColumnDef<RequisitionRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Requisition',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        accessorKey: 'requisition_date',
        header: 'Raised',
        cell: ({ row }) => formatDate(row.original.requisition_date),
      },
      {
        accessorKey: 'required_date',
        header: 'Required',
        cell: ({ row }) => {
          const days = daysUntil(row.original.required_date)
          // Only an undecided or unsourced requisition can still be late in a
          // way anybody can act on.
          const open = (ACTIONABLE_REQUISITION_STATUSES as readonly string[]).includes(
            row.original.status,
          )
          const late = days !== null && days < 0 && open
          return (
            <span className={late ? 'font-medium text-destructive' : undefined}>
              {formatDate(row.original.required_date)}
              {late && <span className="ml-1 text-xs">({Math.abs(days)}d late)</span>}
            </span>
          )
        },
      },
      {
        accessorKey: 'priority',
        header: 'Priority',
        meta: { align: 'right' },
        // 1-9, lower is more urgent. It is what the queue is worked in, so it
        // is a column rather than a detail.
        cell: ({ row }) => (
          <span
            className={
              isUrgentPriority(row.original.priority)
                ? 'tabular font-medium text-warning'
                : 'tabular text-muted-foreground'
            }
          >
            {row.original.priority}
          </span>
        ),
      },
      {
        id: 'to_order',
        header: 'To order',
        enableSorting: false,
        meta: { align: 'right' },
        // Only approved demand is waiting on a buyer; before approval every
        // line is "unordered" and the count would say nothing.
        cell: ({ row }) => {
          const { status, open_line_count: open } = row.original
          if (!(CONVERTIBLE_REQUISITION_STATUSES as readonly string[]).includes(status)) {
            return <span className="text-muted-foreground">-</span>
          }
          return open === 0 ? (
            <span className="text-xs text-muted-foreground">All ordered</span>
          ) : (
            <span className="font-medium">{open === 1 ? '1 line' : `${open} lines`}</span>
          )
        },
      },
      {
        id: 'source',
        header: 'Raised from',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.source_type === 'MANUAL' ? (
            <span className="text-xs text-muted-foreground">Manual</span>
          ) : (
            <Badge variant="default">{humanise(row.original.source_type)}</Badge>
          ),
      },
      {
        id: 'purpose',
        header: 'Purpose',
        enableSorting: false,
        // What the requester says the material is for. It is the only free text
        // on the header and it is usually what decides the approval.
        cell: ({ row }) =>
          row.original.purpose ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="block max-w-[18rem] cursor-help truncate">
                  {row.original.purpose}
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">{row.original.purpose}</TooltipContent>
            </Tooltip>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'department',
        header: 'Department',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {row.original.department_id
              ? (departmentById.get(row.original.department_id)?.name ?? '-')
              : '-'}
          </span>
        ),
      },
      {
        id: 'warehouse',
        header: 'Deliver to',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {warehouseById.get(row.original.warehouse_id)?.code ?? '-'}
          </span>
        ),
      },
      {
        id: 'decision',
        header: 'Decided',
        enableSorting: false,
        // The summary carries the approval timestamp but not the approver's
        // name and not the rejection reason - both are on the detail payload.
        cell: ({ row }) =>
          row.original.approved_at ? (
            <span className="text-xs text-muted-foreground">
              {formatDateTime(row.original.approved_at)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ]

    // Gating is UX only - the API authorises every action independently. The
    // rights are read separately because they belong to different people: the
    // requester amends and withdraws, the approver decides and closes, and the
    // buyer turns approved demand into purchase orders.
    if (canDecide || canRaise || canConvert) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const requisition = row.original
          const decidable = canDecide && isRequisitionDecidable(requisition.status)
          const amendable = canRaise && isRequisitionAmendable(requisition.status)
          const submittable = canRaise && isRequisitionSubmittable(requisition.status)
          const cancellable = canRaise && isRequisitionCancellable(requisition.status)
          const deletable = canRaise && isRequisitionDeletable(requisition.status)
          const convertible = canConvert && isRequisitionConvertible(requisition)
          const closable = canDecide && isRequisitionClosable(requisition.status)

          // A requisition nothing can be done to offers nothing at all rather
          // than an empty menu.
          if (
            !decidable &&
            !amendable &&
            !submittable &&
            !cancellable &&
            !deletable &&
            !convertible &&
            !closable
          ) {
            return null
          }

          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${requisition.code}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {convertible && (
                  <DropdownMenuItem
                    onSelect={() =>
                      setConversion({
                        kind: 'single',
                        requisition: { id: requisition.id, code: requisition.code },
                      })
                    }
                  >
                    Raise purchase orders
                  </DropdownMenuItem>
                )}
                {amendable && (
                  <DropdownMenuItem onSelect={() => setForm({ requisitionId: requisition.id })}>
                    Edit draft
                  </DropdownMenuItem>
                )}
                {submittable && (
                  <DropdownMenuItem onSelect={() => setCommand({ requisition, kind: 'submit' })}>
                    Submit for approval
                  </DropdownMenuItem>
                )}
                {decidable && (
                  <DropdownMenuItem
                    onSelect={() => setDecision({ requisition, action: 'approve' })}
                  >
                    Approve
                  </DropdownMenuItem>
                )}
                {decidable && (
                  <DropdownMenuItem
                    destructive
                    onSelect={() => setDecision({ requisition, action: 'reject' })}
                  >
                    Reject
                  </DropdownMenuItem>
                )}
                {(cancellable || deletable || closable) &&
                  (decidable || amendable || submittable || convertible) && (
                    <DropdownMenuSeparator />
                  )}
                {closable && (
                  <DropdownMenuItem onSelect={() => setCommand({ requisition, kind: 'close' })}>
                    Close requisition
                  </DropdownMenuItem>
                )}
                {cancellable && (
                  <DropdownMenuItem
                    destructive
                    onSelect={() => setCommand({ requisition, kind: 'cancel' })}
                  >
                    Cancel requisition
                  </DropdownMenuItem>
                )}
                {deletable && (
                  <DropdownMenuItem
                    destructive
                    onSelect={() => setCommand({ requisition, kind: 'delete' })}
                  >
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
  }, [canConvert, canDecide, canRaise, departmentById, warehouseById])

  const rejecting = decision?.action === 'reject'
  const reasonRequired = rejecting
  const reasonTooShort = reason.trim().length < REASON_MIN_LENGTH
  const reasonTooLong = reason.trim().length > REASON_MAX_LENGTH
  const reasonInvalid = (reasonRequired && reasonTooShort) || reasonTooLong

  const commandCopy = command ? COMMANDS[command.kind] : null
  const commandTooShort = commandReason.trim().length < REASON_MIN_LENGTH
  const commandTooLong = commandReason.trim().length > REASON_MAX_LENGTH
  const commandInvalid = (Boolean(commandCopy?.reasonRequired) && commandTooShort) || commandTooLong

  function closeDecision() {
    setDecision(null)
    setReason('')
    setReasonTouched(false)
    setServerReason(null)
  }

  function closeCommand() {
    setCommand(null)
    setCommandReason('')
    setCommandTouched(false)
    setCommandServerReason(null)
  }

  return (
    <>
      <PageHeader
        title="Purchase requisitions"
        description="Demand waiting to be sourced, raised by hand or converted from an MRP run. An approved requisition is what a purchase order is raised against."
        actions={
          canRaise || canConvert ? (
            <>
              {canConvert && (
                <Button variant="outline" onClick={() => setPicking(true)}>
                  <Layers />
                  Raise POs from approved requisitions
                </Button>
              )}
              {canRaise && (
                <Button onClick={() => setForm({ requisitionId: null })}>
                  <Plus />
                  Raise requisition
                </Button>
              )}
            </>
          ) : undefined
        }
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
        searchPlaceholder="Search requisition code or purpose..."
        getRowId={(row) => row.id}
        emptyTitle="No requisitions found"
        emptyDescription="Nothing matches the current filters. Widen the date window or clear a filter."
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
                {REQUISITION_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <DateRangeFilter
              id="req-required"
              fromLabel="Required from"
              from={requiredFrom}
              to={requiredTo}
              onFromChange={(value) => setParam('required_from', value || undefined)}
              onToChange={(value) => setParam('required_to', value || undefined)}
            />

            <div className="flex items-center gap-2">
              <Checkbox
                id="req-open-only"
                checked={openOnly}
                onCheckedChange={(checked) =>
                  setParam('open_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="req-open-only" className="font-normal">
                Approved, still unmet
              </Label>
            </div>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => clearSearchKeys(setSearchParams, FILTER_KEYS)}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      <RequisitionFormDialog
        open={Boolean(form)}
        onOpenChange={(next) => {
          if (!next) setForm(null)
        }}
        requisitionId={form?.requisitionId ?? null}
      />

      {picking && (
        <Suspense fallback={null}>
          <RequisitionBatchPicker
            open
            onOpenChange={setPicking}
            onContinue={(requisitions) => {
              setPicking(false)
              setConversion({ kind: 'batch', requisitions })
            }}
          />
        </Suspense>
      )}

      {conversion && (
        <Suspense fallback={null}>
          <RequisitionConversionDialog
            target={conversion}
            open
            onOpenChange={(next) => {
              if (!next) setConversion(null)
            }}
          />
        </Suspense>
      )}

      <ConfirmDialog
        open={Boolean(decision)}
        onOpenChange={(open) => {
          if (!open) closeDecision()
        }}
        title={
          decision
            ? rejecting
              ? `Reject ${decision.requisition.code}?`
              : `Approve ${decision.requisition.code}?`
            : ''
        }
        description={
          rejecting
            ? 'The requisition closes and nothing is sourced against it. The requester sees the reason you record here.'
            : 'The requisition becomes available to source. Raising the purchase order is a separate step.'
        }
        confirmLabel={rejecting ? 'Reject requisition' : 'Approve requisition'}
        destructive={rejecting}
        loading={decide.isPending}
        onConfirm={() => {
          if (!decision) return
          if (reasonInvalid) {
            setReasonTouched(true)
            return
          }
          decide.mutate({ ...decision, reason: reason.trim() })
        }}
      >
        <div>
          <Label htmlFor="requisition-reason" required={reasonRequired}>
            {rejecting ? 'Reason' : 'Note'}
          </Label>
          <Textarea
            id="requisition-reason"
            value={reason}
            maxLength={REASON_MAX_LENGTH}
            onChange={(event) => {
              setReason(event.target.value)
              setServerReason(null)
            }}
            className="mt-1.5"
            aria-invalid={(reasonTouched && reasonInvalid) || serverReason !== null}
          />
          {reasonRequired && reasonTouched && reasonTooShort && (
            <FieldError message="A rejection needs a recorded reason of at least three characters" />
          )}
          {reasonTouched && reasonTooLong && (
            <FieldError message={`At most ${REASON_MAX_LENGTH} characters`} />
          )}
          {serverReason !== null && <FieldError message={serverReason} />}
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={Boolean(command)}
        onOpenChange={(open) => {
          if (!open) closeCommand()
        }}
        title={command && commandCopy ? commandCopy.title(command.requisition.code) : ''}
        description={commandCopy?.description}
        confirmLabel={commandCopy?.confirmLabel ?? 'Confirm'}
        destructive={commandCopy?.destructive ?? false}
        loading={run.isPending}
        onConfirm={() => {
          if (!command || !commandCopy) return
          if (commandInvalid) {
            setCommandTouched(true)
            return
          }
          run.mutate({ ...command, reason: commandReason.trim() })
        }}
      >
        <div>
          <Label htmlFor="requisition-command-reason" required={commandCopy?.reasonRequired}>
            {commandCopy?.reasonLabel ?? 'Reason'}
          </Label>
          <Textarea
            id="requisition-command-reason"
            value={commandReason}
            onChange={(event) => {
              setCommandReason(event.target.value)
              setCommandServerReason(null)
            }}
            // The API caps every one of these at 255 characters, so the field
            // stops there rather than letting the paste be refused.
            maxLength={REASON_MAX_LENGTH}
            className="mt-1.5"
            aria-invalid={(commandTouched && commandInvalid) || commandServerReason !== null}
          />
          {commandCopy?.reasonRequired && commandTouched && commandTooShort ? (
            <FieldError
              message={`The API records this - it needs a reason of at least ${REASON_MIN_LENGTH} characters`}
            />
          ) : commandTouched && commandTooLong ? (
            <FieldError message={`At most ${REASON_MAX_LENGTH} characters`} />
          ) : commandServerReason !== null ? (
            <FieldError message={commandServerReason} />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">{commandCopy?.reasonHint}</p>
          )}
        </div>
      </ConfirmDialog>
    </>
  )
}

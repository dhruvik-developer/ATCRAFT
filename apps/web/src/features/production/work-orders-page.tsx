/**
 * The shop-floor queue: every operation of every live job.
 *
 * Two views of the same work. The queue lists operations - what is next, who
 * has it, when it is planned for - and is where an operation is assigned,
 * started, completed or abandoned. The board groups the same rows by the cell
 * they sit in, which is how a bottleneck is found.
 *
 * Everything is decided by the API. Job, status, work centre, machine, operator
 * and "open only" are the filters `work_order_filters` offers and they are all
 * query parameters; the browser never receives more than one page (master rules
 * 31/36/78).
 *
 * Three things this screen is deliberate about.
 *
 * **No search box.** The list endpoint takes no `search` parameter, so a box
 * would look like it worked and quietly filter nothing.
 *
 * **Four sortable headers.** `sequence`, `status` and `planned_start_at` are
 * sortable because `WorkOrderRepository.sortable_fields` says so; every other
 * header is fixed rather than sending a `sort_by` the API answers with a 422.
 *
 * **No machine column.** A row names `machine_id` with no code beside it, and a
 * machine is only ever meaningful within its own work centre - so machines are
 * resolved one cell at a time, in the assignment dialog and in the machine
 * filter that appears once a work centre is chosen. Labelling the grid would
 * mean either a request per row or an unbounded lookup, and both are worse than
 * not showing it.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { ClipboardList, Factory, MoreHorizontal, Pencil, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { useOperatorOptions } from '@/hooks/use-operator-options'
import { ShiftSelect } from '@/features/shifts/shift-select'
import { SELECT_ALL } from '@/lib/constants'
import { formatDateTime, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { WipBoardPanel } from './wip-board-panel'
import { WipByOrderPanel } from './wip-by-order-panel'
import {
  WorkOrderAssignDialog,
  WorkOrderTransitionDialog,
  type PendingTransition,
} from './work-order-dialogs'
import {
  WORK_ORDER_STATUSES,
  bookedPercent,
  formatMinutes,
  indexById,
  isReschedulable,
  optionLabel,
  plannedMinutes,
  transitionsFrom,
  useWorkCentreMachineOptions,
  useWorkCentreOptions,
  workOrderKeys,
  type WorkOrder,
} from './work-order-model'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button, the count of what is applied and the tab switch all read
 * this list, so a filter added to the toolbar cannot end up counted but not
 * cleared.
 */
const FILTER_KEYS = [
  'shift_id',
  'status',
  'work_center_id',
  'machine_id',
  'operator_user_id',
  'open_only',
  'production_order_id',
] as const

const TABS = [
  { value: 'queue', label: 'Queue', icon: ClipboardList },
  { value: 'board', label: 'Load by work centre', icon: Factory },
  { value: 'wip', label: 'WIP by order', icon: Factory },
] as const

export default function WorkOrdersPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [assigning, setAssigning] = useState<WorkOrder | null>(null)
  const [pending, setPending] = useState<PendingTransition | null>(null)

  // Both write paths are gated on `work_order.manage` in the production router -
  // not on the view right this page is reached with, and not on any of the
  // production-order rights.
  const canManage = can(P.WORK_ORDER_MANAGE)
  const canViewWip = can(P.PRODUCTION_ORDER_VIEW) && can(P.PRODUCTION_COST_VIEW)

  const requestedTab = searchParams.get('tab') ?? 'queue'
  const tab = requestedTab === 'wip' && !canViewWip ? 'queue' : requestedTab
  const shiftId = searchParams.get('shift_id') ?? ''
  const status = searchParams.get('status') ?? ''
  const workCentreId = searchParams.get('work_center_id') ?? ''
  const machineId = searchParams.get('machine_id') ?? ''
  const operatorId = searchParams.get('operator_user_id') ?? ''
  const openOnly = searchParams.get('open_only') === 'true'
  const productionOrderId = searchParams.get('production_order_id') ?? ''

  const workCentres = useWorkCentreOptions()
  const workCentreById = useMemo(() => indexById(workCentres.data ?? []), [workCentres.data])

  const operators = useOperatorOptions()
  const operatorById = useMemo(() => indexById(operators.data ?? []), [operators.data])

  // Machines only make sense inside one cell - the API refuses a machine from
  // another - so the filter appears only once a work centre has been chosen.
  const machines = useWorkCentreMachineOptions(workCentreId || null)
  const machineRows = machines.data ?? []

  /**
   * One job's operations come from the job's own endpoint.
   *
   * `GET /production/{order_id}/work-orders` exists precisely for this and
   * overwrites any `production_order_id` in the query string with the path
   * segment, so the id is spent on the path and never sent twice.
   */
  const list = useListQuery<WorkOrder>({
    path: productionOrderId
      ? `/production/${productionOrderId}/work-orders`
      : '/production/work-orders',
    queryKey: productionOrderId
      ? (params: unknown) => workOrderKeys.ofOrder(productionOrderId, params)
      : workOrderKeys.list,
    // Within one job the traveller order is the sequence; across the whole
    // floor the queue is read in planned-start order. Rows with no planned
    // start sort last, which is where unscheduled work belongs.
    defaultSortBy: productionOrderId ? 'sequence' : 'planned_start_at',
    staleTime: staleTimes.transactional,
    // Coming back to a tab that has been open all shift must not show the queue
    // as it stood when it was opened - someone else has started half of it.
    refetchOnWindowFocus: true,
    // The board is a different endpoint entirely; nothing should be paged or
    // refetched for a list that is not on screen.
    enabled: tab === 'queue',
    filters: {
      ...(shiftId ? { shift_id: shiftId } : {}),
      ...(status ? { status } : {}),
      ...(workCentreId ? { work_center_id: workCentreId } : {}),
      ...(machineId ? { machine_id: machineId } : {}),
      ...(operatorId ? { operator_user_id: operatorId } : {}),
      ...(openOnly ? { open_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: it builds the next URL from
   * the `searchParams` captured when the callback was created and then
   * navigates, so a sequence of `setParam` calls all start from the same stale
   * base and only the last one survives.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        // The page belonged to the narrowed list, not to this one.
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  /**
   * Change the work centre - and drop the machine with it - in ONE URL write.
   *
   * Two `setParam` calls would not do it. `setSearchParams` passes the callback
   * the params memoised from *this render's* location, so a second call in the
   * same tick starts from the same base as the first and its navigation is the
   * only one that survives - which would leave the machine cleared and the work
   * centre exactly where it was.
   *
   * `showOnQueue` also brings the board's selection over to the queue, which is
   * what makes a cell on the board a way in to its own list.
   */
  const chooseWorkCentre = useCallback(
    (nextWorkCentreId: string | null, { showOnQueue = false } = {}) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          if (nextWorkCentreId) next.set('work_center_id', nextWorkCentreId)
          else next.delete('work_center_id')
          // A machine belongs to exactly one cell, so it cannot outlive a
          // change of work centre.
          next.delete('machine_id')
          next.delete('page')
          if (showOnQueue) next.set('tab', 'queue')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const showQueueForWorkCentre = useCallback(
    (nextWorkCentreId: string) => chooseWorkCentre(nextWorkCentreId, { showOnQueue: true }),
    [chooseWorkCentre],
  )

  const changeTab = useCallback(
    (next: string) => {
      setSearchParams(
        (current) => {
          const params = new URLSearchParams(current)
          params.set('tab', next)
          // Page 4 of the queue, and a sort on a field the board does not
          // offer, are both meaningless once the board is what is on screen.
          for (const key of ['page', 'sort', 'dir']) params.delete(key)
          return params
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const columns = useMemo<ColumnDef<WorkOrder, unknown>[]>(() => {
    /**
     * The job the operation belongs to.
     *
     * Dropped when every row on screen already belongs to the same job.
     * `WorkOrderRead` carries the job's id and not its code, so the link names
     * what it opens rather than pretending to show a document number.
     */
    const jobColumn: ColumnDef<WorkOrder, unknown> = {
      id: 'production_order',
      header: 'Job',
      enableSorting: false,
      cell: ({ row }) => (
        <Link
          to={`/production/${row.original.production_order_id}`}
          className="text-xs hover:underline"
          aria-label={`Open the production order behind ${row.original.code}`}
          onClick={(event) => event.stopPropagation()}
        >
          Production order
        </Link>
      ),
    }

    const defs: ColumnDef<WorkOrder, unknown>[] = [
      {
        accessorKey: 'sequence',
        header: 'Seq',
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.sequence}</span>,
      },
      {
        id: 'operation',
        header: 'Operation',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[18rem]">
            <span className="block truncate font-medium">{row.original.operation_name}</span>
            <span className="block truncate font-mono text-2xs text-muted-foreground">
              {row.original.operation_code} &middot; {row.original.code}
            </span>
          </div>
        ),
      },
      ...(productionOrderId ? [] : [jobColumn]),
      {
        id: 'work_centre',
        header: 'Work centre',
        enableSorting: false,
        cell: ({ row }) => {
          const centre = workCentreById.get(row.original.work_center_id)
          return centre ? (
            <span>{optionLabel(centre)}</span>
          ) : (
            <span className="text-muted-foreground">Name unavailable</span>
          )
        },
      },
      {
        id: 'operator',
        header: 'Operator',
        enableSorting: false,
        cell: ({ row }) => {
          if (!row.original.operator_user_id) {
            return <span className="text-muted-foreground">Unassigned</span>
          }
          const operator = operatorById.get(row.original.operator_user_id)
          return operator ? (
            <span className="block max-w-[12rem] truncate">{operator.full_name}</span>
          ) : (
            <span className="text-muted-foreground">Assigned</span>
          )
        },
      },
      {
        id: 'shift',
        header: 'Shift',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.shift ? (
            <Badge variant="secondary">{row.original.shift}</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        accessorKey: 'planned_start_at',
        header: 'Planned',
        // The window and the work it holds. Setup and run time are separate
        // columns on the wire because they scale differently; what the operator
        // wants from them is one number, so they are added for display only.
        cell: ({ row }) => (
          <div className="whitespace-nowrap">
            <span className="block text-xs">{formatDateTime(row.original.planned_start_at)}</span>
            <span className="block text-2xs text-muted-foreground">
              to {formatDateTime(row.original.planned_end_at)} &middot;{' '}
              {formatMinutes(plannedMinutes(row.original))}
            </span>
          </div>
        ),
      },
      {
        id: 'quantity',
        header: 'Booked / planned',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <div className="flex flex-col items-end gap-1">
            <span className="tabular">
              {formatQuantity(row.original.completed_quantity)} /{' '}
              {formatQuantity(row.original.planned_quantity)}
            </span>
            <span className="h-1 w-20 overflow-hidden rounded-full bg-muted" aria-hidden>
              <span
                className="block h-full bg-primary"
                style={{ width: `${bookedPercent(row.original)}%` }}
              />
            </span>
          </div>
        ),
      },
      {
        id: 'rejected_quantity',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        // Only badged when there is something to badge: a red zero says the
        // wrong thing. The comparison goes through decimal.js like every other
        // quantity - `Number("0.0001")` is not what decides this.
        cell: ({ row }) =>
          toDecimal(row.original.rejected_quantity).greaterThan(0) ? (
            <Badge variant="destructive">{formatQuantity(row.original.rejected_quantity)}</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ]

    // Gating is UX only - the API authorises each request independently.
    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const edges = transitionsFrom(row.original.status)
          const editable = isReschedulable(row.original.status)
          if (!editable && edges.length === 0) {
            return <span className="text-2xs text-muted-foreground">Closed</span>
          }
          return (
            <div className="flex items-center justify-end gap-1">
              {editable && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Assign ${row.original.code}`}
                  onClick={() => setAssigning(row.original)}
                >
                  <Pencil />
                </Button>
              )}
              {edges.length > 0 && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Change the status of ${row.original.code}`}
                    >
                      <MoreHorizontal />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    {edges.map((edge) => (
                      <DropdownMenuItem
                        key={edge.target}
                        destructive={edge.destructive}
                        onSelect={() => setPending({ workOrder: row.original, transition: edge })}
                      >
                        {edge.label}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          )
        },
      })
    }

    return defs
  }, [canManage, operatorById, productionOrderId, workCentreById])

  return (
    <div>
      <PageHeader
        title="Work orders"
        description="Every operation of every live job: what is queued, what is running, and who has it. Quantities are booked against the job, not typed in here."
        breadcrumbs={[{ label: 'Production', to: '/production' }, { label: 'Work orders' }]}
      />

      <Tabs value={tab} onValueChange={changeTab}>
        <TabsList>
          {TABS.filter(({ value }) => value !== 'wip' || canViewWip).map(
            ({ value, label, icon: Icon }) => (
              <TabsTrigger key={value} value={value}>
                <Icon className="h-4 w-4" aria-hidden />
                {label}
              </TabsTrigger>
            ),
          )}
        </TabsList>

        <TabsContent value="queue">
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
            onRefresh={() => void list.refetch()}
            getRowId={(row) => row.id}
            emptyTitle="No operations here"
            emptyDescription="Nothing matches these filters. Operations appear when a production order is released - the release turns its routing into one work order per step."
            toolbar={
              <>
                <div>
                  <Label htmlFor="work-order-shift-filter">Shift</Label>
                  <ShiftSelect
                    id="work-order-shift-filter"
                    value={shiftId || null}
                    onChange={(value) => setParam('shift_id', value ?? undefined)}
                  />
                </div>
                <Select
                  value={status || SELECT_ALL}
                  onValueChange={(next) =>
                    setParam('status', next === SELECT_ALL ? undefined : next)
                  }
                >
                  <SelectTrigger className="w-full sm:w-[11rem]" aria-label="Operation status">
                    <SelectValue placeholder="Any status" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>Any status</SelectItem>
                    {WORK_ORDER_STATUSES.map((entry) => (
                      <SelectItem key={entry} value={entry}>
                        {humanise(entry)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                <Select
                  value={workCentreId || SELECT_ALL}
                  onValueChange={(next) => chooseWorkCentre(next === SELECT_ALL ? null : next)}
                >
                  <SelectTrigger className="w-full sm:w-[14rem]" aria-label="Work centre">
                    <SelectValue placeholder="Any work centre" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>Any work centre</SelectItem>
                    {workCentreId && !workCentreById.has(workCentreId) && (
                      <SelectItem value={workCentreId}>Selected work centre</SelectItem>
                    )}
                    {(workCentres.data ?? []).map((centre) => (
                      <SelectItem key={centre.id} value={centre.id}>
                        {optionLabel(centre)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                {workCentreId && (
                  <Select
                    value={machineId || SELECT_ALL}
                    onValueChange={(next) =>
                      setParam('machine_id', next === SELECT_ALL ? undefined : next)
                    }
                  >
                    <SelectTrigger className="w-full sm:w-[13rem]" aria-label="Machine">
                      <SelectValue placeholder="Any machine" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_ALL}>Any machine</SelectItem>
                      {/* A narrowing that is in force has to stay visible and
                          clearable even when the register cannot be read. */}
                      {machineId && !machineRows.some((machine) => machine.id === machineId) && (
                        <SelectItem value={machineId}>Selected machine</SelectItem>
                      )}
                      {machineRows.map((machine) => (
                        <SelectItem key={machine.id} value={machine.id}>
                          {optionLabel(machine)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}

                <Select
                  value={operatorId || SELECT_ALL}
                  onValueChange={(next) =>
                    setParam('operator_user_id', next === SELECT_ALL ? undefined : next)
                  }
                >
                  <SelectTrigger className="w-full sm:w-[13rem]" aria-label="Operator">
                    <SelectValue placeholder="Any operator" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>Any operator</SelectItem>
                    {operatorId && !operatorById.has(operatorId) && (
                      <SelectItem value={operatorId}>Selected operator</SelectItem>
                    )}
                    {(operators.data ?? []).map((operator) => (
                      <SelectItem key={operator.id} value={operator.id}>
                        {operator.full_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
                  <Checkbox
                    id="work-orders-open-only"
                    checked={openOnly}
                    onCheckedChange={(checked) =>
                      setParam('open_only', checked === true ? 'true' : undefined)
                    }
                  />
                  <Label
                    htmlFor="work-orders-open-only"
                    className="cursor-pointer text-xs font-normal"
                  >
                    Still open
                  </Label>
                </div>

                {productionOrderId && (
                  <Badge variant="secondary">Scoped to one production order</Badge>
                )}

                {filterCount > 0 && (
                  <Button variant="ghost" size="sm" onClick={clearFilters}>
                    <X />
                    Clear {filterCount}
                  </Button>
                )}
              </>
            }
          />
        </TabsContent>

        <TabsContent value="board">
          {/* The queue's work-centre filter is deliberately not applied here.
              A board narrowed to one cell cannot show a bottleneck, and the
              filter's own control lives on the queue's toolbar, so a single-row
              board would come with nothing on screen to explain it. The flow
              runs the other way: pick a cell here and its queue opens. */}
          <WipBoardPanel onSelectWorkCentre={showQueueForWorkCentre} />
        </TabsContent>

        {canViewWip && (
          <TabsContent value="wip">
            <WipByOrderPanel />
          </TabsContent>
        )}
      </Tabs>

      {canManage && (
        <>
          <WorkOrderAssignDialog
            workOrder={assigning}
            onOpenChange={(open) => {
              if (!open) setAssigning(null)
            }}
          />
          <WorkOrderTransitionDialog
            pending={pending}
            onOpenChange={(open) => {
              if (!open) setPending(null)
            }}
          />
        </>
      )}
    </div>
  )
}

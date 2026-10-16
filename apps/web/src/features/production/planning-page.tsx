/**
 * The production planning board: one week of work per work centre and machine.
 *
 * Two views of the same week. The heat strip answers "does the plan fit?" -
 * remaining planned minutes per centre per day against rostered capacity where
 * a roster exists, standard hours elsewhere, with the days that do not fit
 * flagged. The timeline answers "what runs where?" - every open operation with
 * a planned window drawn on its centre or machine, reschedulable through the
 * same assignment dialog the queue uses, which re-validates the window and
 * warns about a double-booked machine on save.
 *
 * Everything is decided by the API. The board reads `GET /production/capacity`
 * and `GET /production/work-orders` over the visible week; the browser never
 * holds more than those rows. Operations whose planned window starts up to
 * thirty days before the week are fetched too, so a long run reaching into the
 * week still draws its touched days rather than vanishing at the edge.
 *
 * The board is a week wide on purpose, and it scrolls inside itself on a
 * phone: seven day columns do not fit a narrow screen any other way.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { EmptyState, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tabs,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { useAuth } from '@/features/auth/auth-context'
import { ChevronLeft, ChevronRight, TriangleAlert } from 'lucide-react'

import { WorkOrderAssignDialog } from './work-order-dialogs'
import {
  addDaysISO,
  boardWeek,
  clashingWorkOrders,
  columnLabel,
  groupCapacityByCentre,
  localTodayISO,
  touchedColumns,
  useBoardMachines,
  useBoardWorkOrders,
  useCapacityBoard,
} from './planning-model'
import {
  formatMinutes,
  isReschedulable,
  optionLabel,
  useWorkCentreOptions,
  type WorkOrder,
} from './work-order-model'
import { P } from '@/lib/permissions'
import { SELECT_ALL } from '@/lib/constants'

/** Operations starting this far before the week are still fetched, so a long run reaching in draws its touched days. */
const BOARD_LOOKBACK_DAYS = 30

type BoardView = 'centre' | 'machine'

function HeatStrip({
  days,
  groups,
}: {
  days: string[]
  groups: ReturnType<typeof groupCapacityByCentre>
}) {
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full min-w-[42rem] border-collapse text-xs">
        <thead>
          <tr className="bg-muted/50">
            <th className="sticky left-0 bg-muted/50 p-2 text-left font-medium">Work centre</th>
            {days.map((day) => (
              <th key={day} className="p-2 text-right font-medium">
                {columnLabel(day)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <tr key={group.centreId} className="border-t">
              <th className="sticky left-0 bg-background p-2 text-left font-medium">
                {group.code} - {group.name}
              </th>
              {group.days.map((cell) => (
                <td
                  key={cell.date}
                  className="p-2 text-right tabular"
                  title={
                    cell.capacity_source === 'ROSTER'
                      ? 'Capacity is the rostered minutes for this date'
                      : 'Capacity is the standard working day - nothing rostered'
                  }
                >
                  <span className={cell.over_capacity ? 'font-semibold text-destructive' : ''}>
                    {formatMinutes(cell.planned_minutes)} / {formatMinutes(cell.capacity_minutes)}
                  </span>
                  {cell.over_capacity && (
                    <Badge variant="destructive" className="ml-1">
                      Over
                    </Badge>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function OperationChip({
  workOrder,
  clashing,
  onReschedule,
}: {
  workOrder: WorkOrder
  clashing: boolean
  onReschedule: (() => void) | null
}) {
  const label = `${workOrder.operation_name} (${workOrder.code})`
  const chip = (
    <span className="block truncate font-medium" title={label}>
      {workOrder.operation_name}
    </span>
  )
  const classes = `block w-full rounded border px-1.5 py-1 text-left text-2xs ${
    clashing ? 'border-warning bg-warning/10' : 'border-border bg-muted/40 hover:bg-muted'
  }`
  if (!onReschedule) {
    return (
      <span className={classes} title={label}>
        {chip}
        <span className="block truncate tabular text-muted-foreground">{workOrder.code}</span>
      </span>
    )
  }
  return (
    <button
      type="button"
      className={classes}
      title={`${label} - reschedule`}
      aria-label={`Reschedule ${label}`}
      onClick={onReschedule}
    >
      {chip}
      <span className="block truncate tabular text-muted-foreground">{workOrder.code}</span>
      {clashing && (
        <span className="mt-0.5 flex items-center gap-1 text-warning">
          <TriangleAlert className="h-3 w-3" aria-hidden />
          Machine clash
        </span>
      )}
    </button>
  )
}

export default function PlanningPage() {
  const { can } = useAuth()
  const canManage = can(P.WORK_ORDER_MANAGE)

  const [weekStart, setWeekStart] = useState(localTodayISO)
  const [workCentreId, setWorkCentreId] = useState('')
  const [view, setView] = useState<BoardView>('centre')
  const [assigning, setAssigning] = useState<WorkOrder | null>(null)

  const week = useMemo(() => boardWeek(weekStart), [weekStart])
  const weekEnd = week[week.length - 1] ?? weekStart

  const capacityParams = useMemo(
    () => ({
      date_from: week[0],
      date_to: weekEnd,
      ...(workCentreId ? { work_center_id: workCentreId } : {}),
    }),
    [week, weekEnd, workCentreId],
  )
  const boardParams = useMemo(
    () => ({
      open_only: true,
      planned_from: addDaysISO(week[0] ?? weekStart, -BOARD_LOOKBACK_DAYS),
      planned_to: weekEnd,
      ...(workCentreId ? { work_center_id: workCentreId } : {}),
    }),
    [week, weekStart, weekEnd, workCentreId],
  )

  const capacity = useCapacityBoard(capacityParams)
  const board = useBoardWorkOrders(boardParams)
  const workCentres = useWorkCentreOptions()
  const machineRegister = useBoardMachines()

  const groups = useMemo(() => groupCapacityByCentre(capacity.data ?? []), [capacity.data])
  const operations = useMemo(() => board.data ?? [], [board.data])
  const clashing = useMemo(() => clashingWorkOrders(operations), [operations])

  const machineRows = useMemo(() => {
    const boardCentreIds = new Set(groups.map((group) => group.centreId))
    return (machineRegister.data ?? [])
      .filter(
        (machine) =>
          boardCentreIds.has(machine.work_center_id) &&
          (!workCentreId || machine.work_center_id === workCentreId),
      )
      .sort((a, b) => a.code.localeCompare(b.code))
  }, [workCentreId, machineRegister.data, groups])

  const unscheduled = useMemo(
    () => operations.filter((row) => touchedColumns(row, week).length === 0),
    [operations, week],
  )

  const reschedule = (row: WorkOrder) =>
    canManage && isReschedulable(row.status) ? () => setAssigning(row) : null

  return (
    <div>
      <PageHeader
        title="Production planning"
        description="One week of work per work centre and machine: whether the plan fits the capacity, what runs where, and a way to move it."
        breadcrumbs={[{ label: 'Production', to: '/production' }, { label: 'Planning' }]}
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            aria-label="Previous week"
            onClick={() => setWeekStart((start) => addDaysISO(start, -7))}
          >
            <ChevronLeft />
          </Button>
          <Button variant="outline" size="sm" onClick={() => setWeekStart(localTodayISO())}>
            This week
          </Button>
          <Button
            variant="outline"
            size="sm"
            aria-label="Next week"
            onClick={() => setWeekStart((start) => addDaysISO(start, 7))}
          >
            <ChevronRight />
          </Button>
        </div>

        <div>
          <Label htmlFor="planning-centre">Work centre</Label>
          <Select
            value={workCentreId || SELECT_ALL}
            onValueChange={(next) => setWorkCentreId(next === SELECT_ALL ? '' : next)}
          >
            <SelectTrigger id="planning-centre" className="mt-1 w-full sm:w-[14rem]">
              <SelectValue placeholder="All work centres" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All work centres</SelectItem>
              {(workCentres.data ?? []).map((centre) => (
                <SelectItem key={centre.id} value={centre.id}>
                  {optionLabel(centre)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Tabs value={view} onValueChange={(next) => setView(next as BoardView)}>
          <TabsList aria-label="Timeline rows">
            <TabsTrigger value="centre">By work centre</TabsTrigger>
            <TabsTrigger value="machine">By machine</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {capacity.isLoading || board.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : capacity.error ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load capacity</AlertTitle>
          <AlertDescription>{(capacity.error as Error).message}</AlertDescription>
        </Alert>
      ) : board.error ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load the week&apos;s operations</AlertTitle>
          <AlertDescription>{(board.error as Error).message}</AlertDescription>
        </Alert>
      ) : groups.length === 0 ? (
        <EmptyState
          title="No work centres to plan"
          description="Work centres are the rows this board plans against. Create one on the Routings page first."
        />
      ) : (
        <div className="space-y-6">
          <section aria-label="Capacity heat strip">
            <h2 className="mb-2 text-sm font-semibold">
              Load against capacity
              <span className="ml-2 font-normal text-muted-foreground">
                Planned / available minutes per day. Over marks the days the plan does not fit.
              </span>
            </h2>
            <HeatStrip days={week} groups={groups} />
          </section>

          <section aria-label="Timeline">
            <h2 className="mb-2 text-sm font-semibold">
              {view === 'centre' ? 'Operations by work centre' : 'Operations by machine'}
              <span className="ml-2 font-normal text-muted-foreground">
                {canManage
                  ? 'Select an operation to reschedule it.'
                  : 'Rescheduling needs the work-order management right.'}
              </span>
            </h2>
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full min-w-[42rem] border-collapse text-xs">
                <thead>
                  <tr className="bg-muted/50">
                    <th className="sticky left-0 min-w-[10rem] bg-muted/50 p-2 text-left font-medium">
                      {view === 'centre' ? 'Work centre' : 'Machine'}
                    </th>
                    {week.map((day) => (
                      <th key={day} className="min-w-[7rem] p-2 text-left font-medium">
                        {columnLabel(day)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {view === 'centre'
                    ? groups.map((group) => (
                        <tr key={group.centreId} className="border-t align-top">
                          <th className="sticky left-0 bg-background p-2 text-left font-medium">
                            {group.code}
                            <span className="block truncate text-2xs font-normal text-muted-foreground">
                              {group.name}
                            </span>
                          </th>
                          {week.map((day, column) => (
                            <td key={day} className="space-y-1 border-l p-1.5">
                              {operations
                                .filter(
                                  (row) =>
                                    row.work_center_id === group.centreId &&
                                    touchedColumns(row, week).includes(column),
                                )
                                .map((row) => (
                                  <OperationChip
                                    key={row.id}
                                    workOrder={row}
                                    clashing={clashing.has(row.id)}
                                    onReschedule={reschedule(row)}
                                  />
                                ))}
                            </td>
                          ))}
                        </tr>
                      ))
                    : machineRows.map((machine) => (
                        <tr key={machine.id} className="border-t align-top">
                          <th className="sticky left-0 bg-background p-2 text-left font-medium">
                            {machine.code}
                            <span className="block truncate text-2xs font-normal text-muted-foreground">
                              {machine.name}
                            </span>
                          </th>
                          {week.map((day, column) => (
                            <td key={day} className="space-y-1 border-l p-1.5">
                              {operations
                                .filter(
                                  (row) =>
                                    row.machine_id === machine.id &&
                                    touchedColumns(row, week).includes(column),
                                )
                                .map((row) => (
                                  <OperationChip
                                    key={row.id}
                                    workOrder={row}
                                    clashing={clashing.has(row.id)}
                                    onReschedule={reschedule(row)}
                                  />
                                ))}
                            </td>
                          ))}
                        </tr>
                      ))}
                  {view === 'machine' && (
                    <tr className="border-t align-top">
                      <th className="sticky left-0 bg-background p-2 text-left font-medium">
                        No machine
                        <span className="block text-2xs font-normal text-muted-foreground">
                          Planned at the centre, not yet on a seat
                        </span>
                      </th>
                      {week.map((day, column) => (
                        <td key={day} className="space-y-1 border-l p-1.5">
                          {operations
                            .filter(
                              (row) =>
                                (workCentreId ? row.work_center_id === workCentreId : true) &&
                                !row.machine_id &&
                                touchedColumns(row, week).includes(column),
                            )
                            .map((row) => (
                              <OperationChip
                                key={row.id}
                                workOrder={row}
                                clashing={false}
                                onReschedule={reschedule(row)}
                              />
                            ))}
                        </td>
                      ))}
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          {unscheduled.length > 0 && (
            <section aria-label="Unscheduled operations">
              <h2 className="mb-2 text-sm font-semibold">
                Without a planned window
                <span className="ml-2 font-normal text-muted-foreground">
                  Open operations the board cannot place. Give them a window to plan them.
                </span>
              </h2>
              <ul className="divide-y rounded-md border">
                {unscheduled.map((row) => {
                  const plan = reschedule(row)
                  return (
                    <li key={row.id} className="flex flex-wrap items-center gap-2 p-2 text-xs">
                      <StatusBadge status={row.status} />
                      <span className="font-medium">{row.operation_name}</span>
                      <span className="text-muted-foreground">{row.code}</span>
                      <Link
                        to={`/production/${row.production_order_id}`}
                        className="text-muted-foreground hover:underline"
                      >
                        Open the job
                      </Link>
                      {plan && (
                        <Button variant="outline" size="sm" className="ml-auto" onClick={plan}>
                          Plan it
                        </Button>
                      )}
                    </li>
                  )
                })}
              </ul>
            </section>
          )}

          <p className="text-xs text-muted-foreground">
            Day columns follow the date where this browser sits; the load figures above it are
            company dates from the API. Clash badges compare the operations loaded for this week -
            the assignment itself re-validates against everything.
          </p>
        </div>
      )}

      {canManage && (
        <WorkOrderAssignDialog
          workOrder={assigning}
          onOpenChange={(open) => {
            if (!open) setAssigning(null)
          }}
        />
      )}
    </div>
  )
}

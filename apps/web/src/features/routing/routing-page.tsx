/**
 * Routings, work centres and machines.
 *
 * Three registers that only make sense together - an operation is a step on a
 * work centre, a machine is a seat inside one - so they are three tabs of one
 * screen rather than three menu entries.
 *
 * Each tab drives its own server-side list. Only the visible tab is mounted, so
 * exactly one list query is ever in flight; switching tabs clears the list
 * parameters, because a search typed against machines means nothing against work
 * centres.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Cpu, Factory, Pencil, Plus, Route, Star, Wrench, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { EmptyState, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
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
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { useCostCentreOptions } from '@/hooks/use-organisation-options'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import {
  MachineFormDialog,
  MachineStatusDialog,
  RoutingDetailDialog,
  RoutingFormDialog,
  WorkCenterFormDialog,
} from './routing-dialogs'
import {
  MACHINE_STATUSES,
  MACHINE_STATUS_VARIANT,
  ROUTING_STATUSES,
  isEditable,
  type Machine,
  type RoutingRow,
  type WorkCenter,
} from './routing-model'

// ----------------------------------------------------------- routings ------

function RoutingsPanel({ canManage }: { canManage: boolean }) {
  const [searchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<RoutingRow | null>(null)
  const [openRoutingId, setOpenRoutingId] = useState<string | null>(null)

  const status = searchParams.get('status') ?? ''

  const list = useListQuery<RoutingRow>({
    // `/routing`, not `/routing/routings`: the nested path does not exist and
    // every load of this tab was a 404.
    path: '/routing',
    queryKey: queryKeys.routing.routings,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: status ? { status } : {},
  })
  const { setParam } = list

  const columns = useMemo<ColumnDef<RoutingRow, unknown>[]>(() => {
    const defs: ColumnDef<RoutingRow, unknown>[] = [
      {
        id: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'name',
        header: 'Name',
        // `name` is not one of the API's sortable fields, and an unknown
        // `sort_by` is a hard validation error rather than an ignored parameter.
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate">{row.original.name ?? '-'}</span>
        ),
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        // A routing always names an item, and never its code: `item_id` is all
        // the row carries.
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.item_id}`}
            className="text-muted-foreground hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            Open item
          </Link>
        ),
      },
      {
        id: 'version',
        header: 'Version',
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">v{row.original.version}</span>,
      },
      {
        id: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'is_default',
        header: 'Default',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_default ? (
            <Badge variant="success">
              <Star className="h-3 w-3" aria-hidden />
              Default
            </Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'effective_from',
        header: 'Effective from',
        cell: ({ row }) => formatDate(row.original.effective_from),
      },
      {
        id: 'effective_to',
        header: 'Effective to',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.effective_to ? (
            formatDate(row.original.effective_to)
          ) : (
            <span className="text-muted-foreground">Open ended</span>
          ),
      },
    ]

    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        // Only a draft accepts an edit - the API refuses a PATCH on anything
        // else outright - so an approved routing offers no pencil rather than a
        // pencil that produces a rejection.
        cell: ({ row }) =>
          isEditable(row.original.status) ? (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Edit routing ${row.original.code}`}
              onClick={(event) => {
                event.stopPropagation()
                setEditing(row.original)
              }}
            >
              <Pencil />
            </Button>
          ) : null,
      })
    }

    return defs
  }, [canManage])

  const newButton = canManage ? (
    <Button size="sm" onClick={() => setCreateOpen(true)}>
      <Plus />
      New routing
    </Button>
  ) : null

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
        searchPlaceholder="Search routing code or name..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setOpenRoutingId(row.id)}
        emptyTitle="No routings found"
        emptyDescription="A routing describes the operations a product passes through. Nothing matches the current filters."
        emptyAction={newButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {ROUTING_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {status && (
              <Button variant="ghost" size="sm" onClick={() => setParam('status', undefined)}>
                <X />
                Clear
              </Button>
            )}

            {newButton}
          </>
        }
      />

      <RoutingDetailDialog
        routingId={openRoutingId}
        canManage={canManage}
        onOpenChange={(open) => {
          if (!open) setOpenRoutingId(null)
        }}
      />

      {canManage && (
        <>
          <RoutingFormDialog open={createOpen} onOpenChange={setCreateOpen} />
          <RoutingFormDialog
            open={editing !== null}
            onOpenChange={(open) => {
              if (!open) setEditing(null)
            }}
            routing={editing}
          />
        </>
      )}
    </>
  )
}

// -------------------------------------------------------- work centres ------

function WorkCentresPanel({
  canManage,
  canSeeCostCentres,
}: {
  canManage: boolean
  canSeeCostCentres: boolean
}) {
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<WorkCenter | null>(null)

  const list = useListQuery<WorkCenter>({
    path: '/routing/work-centers',
    queryKey: queryKeys.routing.workCenters,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
  })

  // One bounded reference list names every row's cost pool; it is its own
  // right (`cost_centre.view`), so it is not asked for without it.
  const costCentres = useCostCentreOptions(canSeeCostCentres)
  const costCentreById = useMemo(() => indexById(costCentres.data), [costCentres.data])

  const columns = useMemo<ColumnDef<WorkCenter, unknown>[]>(() => {
    const defs: ColumnDef<WorkCenter, unknown>[] = [
      {
        id: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate">{row.original.name}</span>
        ),
      },
      {
        id: 'capacity_per_hour',
        header: 'Capacity / hour',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.capacity_per_hour),
      },
      {
        id: 'cost_per_hour',
        header: 'Rate / hour',
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.cost_per_hour),
      },
      {
        id: 'setup_cost',
        header: 'Setup charge',
        enableSorting: false,
        meta: { align: 'right' },
        // A flat charge per batch on top of the setup time, so a centre with a
        // low rate and an expensive changeover reads correctly.
        cell: ({ row }) => formatMoney(row.original.setup_cost),
      },
      {
        id: 'working_hours_per_day',
        header: 'Hours / day',
        enableSorting: false,
        meta: { align: 'right' },
        // What turns an operation's minutes into lead-time days.
        cell: ({ row }) => formatQuantity(row.original.working_hours_per_day),
      },
      ...(canSeeCostCentres
        ? [
            {
              id: 'cost_centre',
              header: 'Cost centre',
              enableSorting: false,
              cell: ({ row }) => {
                const centre = row.original.cost_centre_id
                  ? costCentreById.get(row.original.cost_centre_id)
                  : undefined
                return <span className="text-muted-foreground">{centre?.code ?? '-'}</span>
              },
            } satisfies ColumnDef<WorkCenter, unknown>,
          ]
        : []),
      {
        id: 'is_active',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
    ]

    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Edit work centre ${row.original.code}`}
            onClick={(event) => {
              event.stopPropagation()
              setEditing(row.original)
            }}
          >
            <Pencil />
          </Button>
        ),
      })
    }

    return defs
  }, [canManage, canSeeCostCentres, costCentreById])

  const newButton = canManage ? (
    <Button size="sm" onClick={() => setCreateOpen(true)}>
      <Plus />
      New work centre
    </Button>
  ) : null

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
        searchPlaceholder="Search work centre code or name..."
        getRowId={(row) => row.id}
        emptyTitle="No work centres"
        emptyDescription="Operations are costed at a work centre rate, so every routing needs at least one."
        emptyAction={newButton}
        toolbar={newButton}
      />

      {canManage && (
        <>
          <WorkCenterFormDialog open={createOpen} onOpenChange={setCreateOpen} />
          <WorkCenterFormDialog
            open={editing !== null}
            onOpenChange={(open) => {
              if (!open) setEditing(null)
            }}
            workCenter={editing}
          />
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------ machines ------

function MachinesPanel({ canManage }: { canManage: boolean }) {
  const [searchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<Machine | null>(null)
  const [changingStatus, setChangingStatus] = useState<Machine | null>(null)

  const machineStatus = searchParams.get('status') ?? ''

  const list = useListQuery<Machine>({
    path: '/routing/machines',
    queryKey: queryKeys.routing.machines,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: machineStatus ? { status: machineStatus } : {},
  })
  const { setParam } = list

  const columns = useMemo<ColumnDef<Machine, unknown>[]>(() => {
    const defs: ColumnDef<Machine, unknown>[] = [
      {
        id: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate">{row.original.name}</span>
        ),
      },
      {
        id: 'machine_type',
        header: 'Type',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.machine_type ?? '-'}</span>
        ),
      },
      {
        id: 'capacity_per_hour',
        header: 'Capacity / hour',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.capacity_per_hour),
      },
      {
        id: 'cost_per_hour',
        header: 'Rate / hour',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.cost_per_hour),
      },
      {
        id: 'status',
        header: 'Condition',
        cell: ({ row }) => (
          <Badge variant={MACHINE_STATUS_VARIANT[row.original.status] ?? 'secondary'}>
            {humanise(row.original.status)}
          </Badge>
        ),
      },
      {
        id: 'last_maintenance_date',
        header: 'Last service',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.last_maintenance_date),
      },
      {
        id: 'next_maintenance_date',
        header: 'Next service',
        // Due-date order is how a maintenance planner reads this grid, and it is
        // one of the API's sortable fields.
        cell: ({ row }) => formatDate(row.original.next_maintenance_date),
      },
      {
        id: 'is_active',
        header: 'Active',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
    ]

    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="flex items-center justify-end gap-1">
            {/* Condition is a state change with a recorded reason, not a field on
                the edit form: the API moves it through its own endpoint. */}
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Change the condition of machine ${row.original.code}`}
              onClick={(event) => {
                event.stopPropagation()
                setChangingStatus(row.original)
              }}
            >
              <Wrench />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Edit machine ${row.original.code}`}
              onClick={(event) => {
                event.stopPropagation()
                setEditing(row.original)
              }}
            >
              <Pencil />
            </Button>
          </span>
        ),
      })
    }

    return defs
  }, [canManage])

  const newButton = canManage ? (
    <Button size="sm" onClick={() => setCreateOpen(true)}>
      <Plus />
      New machine
    </Button>
  ) : null

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
        searchPlaceholder="Search machine code or name..."
        getRowId={(row) => row.id}
        emptyTitle="No machines"
        emptyDescription="Machines are optional: an operation can name a work centre without naming a seat inside it."
        emptyAction={newButton}
        toolbar={
          <>
            <Select
              value={machineStatus || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by condition">
                <SelectValue placeholder="Any condition" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any condition</SelectItem>
                {MACHINE_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {machineStatus && (
              <Button variant="ghost" size="sm" onClick={() => setParam('status', undefined)}>
                <X />
                Clear
              </Button>
            )}

            {newButton}
          </>
        }
      />

      {canManage && (
        <>
          <MachineFormDialog open={createOpen} onOpenChange={setCreateOpen} />
          <MachineFormDialog
            open={editing !== null}
            onOpenChange={(open) => {
              if (!open) setEditing(null)
            }}
            machine={editing}
          />
          <MachineStatusDialog
            machine={changingStatus}
            onOpenChange={(open) => {
              if (!open) setChangingStatus(null)
            }}
          />
        </>
      )}
    </>
  )
}

// ---------------------------------------------------------------- page ------

export default function RoutingPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canViewRoutings = can(P.ROUTING_VIEW)
  const canViewWorkCentres = can(P.WORK_CENTER_VIEW)
  const canViewMachines = can(P.MACHINE_VIEW)
  // Three registers, three write permissions - `routing.manage`,
  // `work_center.manage` and `machine.manage` - exactly as the router gates
  // them. One `canManage` for all three showed a machine fitter the routing and
  // work-centre forms they would be refused, and hid the machine form from the
  // maintenance planner who is entitled to it. This gates the affordance only;
  // the API authorises every write itself.
  const canManageRoutings = can(P.ROUTING_MANAGE)
  const canManageWorkCentres = can(P.WORK_CENTER_MANAGE)
  const canManageMachines = can(P.MACHINE_MANAGE)

  const available = [
    canViewRoutings ? 'routings' : null,
    canViewWorkCentres ? 'work-centres' : null,
    canViewMachines ? 'machines' : null,
  ].filter((value): value is string => value !== null)

  const requested = searchParams.get('tab') ?? ''
  const tab = available.includes(requested) ? requested : (available[0] ?? 'routings')

  // Switching tab drops the list parameters: a search typed against machines is
  // meaningless against work centres, and carrying it over shows an empty table
  // with no visible reason.
  const setTab = (value: string) =>
    setSearchParams(
      () => {
        const next = new URLSearchParams()
        next.set('tab', value)
        return next
      },
      { replace: true },
    )

  return (
    <>
      <PageHeader
        title="Routings"
        description="How a product is made: the operations it passes through, the work centres that perform them and the machines inside those centres."
      />

      {available.length === 0 ? (
        <EmptyState
          title="Nothing here is visible to your account"
          description="Your role does not include routing, work centre or machine visibility. Ask an administrator if you believe this is wrong."
        />
      ) : (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            {canViewRoutings && (
              <TabsTrigger value="routings">
                <Route className="h-3.5 w-3.5" aria-hidden />
                Routings
              </TabsTrigger>
            )}
            {canViewWorkCentres && (
              <TabsTrigger value="work-centres">
                <Factory className="h-3.5 w-3.5" aria-hidden />
                Work centres
              </TabsTrigger>
            )}
            {canViewMachines && (
              <TabsTrigger value="machines">
                <Cpu className="h-3.5 w-3.5" aria-hidden />
                Machines
              </TabsTrigger>
            )}
          </TabsList>

          {canViewRoutings && (
            <TabsContent value="routings">
              <RoutingsPanel canManage={canManageRoutings} />
            </TabsContent>
          )}

          {canViewWorkCentres && (
            <TabsContent value="work-centres">
              <WorkCentresPanel
                canManage={canManageWorkCentres}
                canSeeCostCentres={can(P.COST_CENTRE_VIEW)}
              />
            </TabsContent>
          )}

          {canViewMachines && (
            <TabsContent value="machines">
              <MachinesPanel canManage={canManageMachines} />
            </TabsContent>
          )}
        </Tabs>
      )}
    </>
  )
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { addDays, format, parseISO } from 'date-fns'
import { useEffect, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/overlays'
import { Alert, AlertDescription, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { useShiftOptions, type ShiftOption } from '@/hooks/use-shift-options'
import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { RosterDialog, type RosterCell } from './roster-dialogs'
import { ShiftFormDialog } from './shift-dialogs'

type Assignment = SchemaOf<'ShiftAssignmentRead'>
const keys = (params: unknown) => [...queryKeys.routing.all, 'roster', params] as const
const dateText = (date: Date) => format(date, 'yyyy-MM-dd')

export default function ShiftRosterPage() {
  const { can } = useAuth()
  const cache = useQueryClient()
  const [day, setDay] = useState(dateText(new Date()))
  const [tab, setTab] = useState('day')
  const [centre, setCentre] = useState('')
  const [dialog, setDialog] = useState<{
    mode: 'assign' | 'bulk' | 'copy'
    cell?: RosterCell
  } | null>(null)
  const [editing, setEditing] = useState<ShiftOption | null | undefined>()
  const [confirm, setConfirm] = useState<{ path: string; label: string } | null>(null)
  const [reason, setReason] = useState('')
  const rosterView = can(P.SHIFT_ROSTER_VIEW)
  const rosterManage = can(P.SHIFT_ROSTER_MANAGE)
  const shiftView = can(P.SHIFT_VIEW)
  const shiftManage = can(P.SHIFT_MANAGE)
  useEffect(() => {
    if (!rosterView && shiftView) setTab('shifts')
    else if (rosterView && !shiftView && tab === 'shifts') setTab('day')
  }, [rosterView, shiftView, tab])
  const shifts = useShiftOptions()
  const from = day
  const to = tab === 'week' ? dateText(addDays(parseISO(day), 6)) : day
  const list = useListQuery<Assignment>({
    path: '/routing/roster/assignments',
    queryKey: keys,
    filters: {
      date_from: from,
      date_to: to,
      status: 'PLANNED',
      ...(centre ? { work_center_id: centre } : {}),
    },
    defaultLimit: LOOKUP_LIST_LIMIT,
    enabled: rosterView && tab !== 'shifts',
  })
  const masters = useListQuery<ShiftOption>({
    path: '/routing/shifts',
    queryKey: (params) => [...queryKeys.routing.all, 'shifts', params],
    defaultSortBy: 'sort_order',
    enabled: tab === 'shifts' && shiftView,
  })
  const refs = useQuery({
    queryKey: [...queryKeys.routing.all, 'roster', 'assets'],
    queryFn: async () => {
      const read = async <T,>(path: string) => {
        try {
          return (
            await api.getPage<T>(path, {
              params: { limit: LOOKUP_LIST_LIMIT, is_active: true, with_total: false },
            })
          ).data
        } catch (error) {
          return emptyOnPermissionError<T>(error)
        }
      }
      return {
        centres: await read<SchemaOf<'WorkCenterRead'>>('/routing/work-centers'),
        machines: await read<SchemaOf<'MachineRead'>>('/routing/machines'),
      }
    },
    enabled: rosterView,
  })
  const action = useMutation({
    mutationFn: () => {
      if (!confirm) throw new Error('Choose an action')
      return confirm.label === 'Retire'
        ? api.delete(`${confirm.path}?reason=${encodeURIComponent(reason)}`)
        : api.post(confirm.path, { reason })
    },
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success('Roster updated')
      setConfirm(null)
      setReason('')
    },
  })
  const columns: ColumnDef<ShiftOption>[] = [
    { accessorKey: 'code', header: 'Code' },
    { accessorKey: 'name', header: 'Name' },
    {
      id: 'window',
      header: 'Local window',
      enableSorting: false,
      cell: ({ row }) =>
        `${row.original.start_time.slice(0, 5)}–${row.original.end_time.slice(0, 5)}${row.original.crosses_midnight ? ' +1 day' : ''}`,
    },
    { accessorKey: 'break_minutes', header: 'Break', enableSorting: false },
    {
      id: 'active',
      header: 'Status',
      enableSorting: false,
      cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
    },
    ...(shiftManage
      ? [
          {
            id: 'actions',
            header: 'Actions',
            enableSorting: false,
            cell: ({ row }: { row: { original: ShiftOption } }) => (
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={() => setEditing(row.original)}>
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    setConfirm({
                      path: `/routing/shifts/${row.original.id}/${row.original.is_active ? 'deactivate' : 'activate'}`,
                      label: row.original.is_active ? 'Deactivate' : 'Activate',
                    })
                  }
                >
                  {row.original.is_active ? 'Deactivate' : 'Activate'}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    setConfirm({ path: `/routing/shifts/${row.original.id}`, label: 'Retire' })
                  }
                >
                  Retire
                </Button>
              </div>
            ),
          },
        ]
      : []),
  ]
  const chip = (row: Assignment) => (
    <div key={row.id} className="rounded-md border bg-muted/30 p-2">
      <span className="text-sm">{row.operator_name}</span>
      {rosterManage && (
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Cancel ${row.operator_name}`}
          onClick={() =>
            setConfirm({
              path: `/routing/roster/assignments/${row.id}/cancel`,
              label: 'Cancel assignment',
            })
          }
        >
          Cancel
        </Button>
      )}
    </div>
  )
  // The roster projections remain readable for roles without directory rights.
  const centres = Array.from(
    new Map([
      ...list.data.map(
        (row) =>
          [
            row.work_center_id,
            { id: row.work_center_id, code: row.work_center_code, name: row.work_center_name },
          ] as const,
      ),
      ...(refs.data?.centres ?? []).map((row) => [row.id, row] as const),
    ]).values(),
  )
  const machines = Array.from(
    new Map([
      ...list.data.flatMap((row) =>
        row.machine_id
          ? [
              [
                row.machine_id,
                {
                  id: row.machine_id,
                  code: row.machine_code ?? '',
                  work_center_id: row.work_center_id,
                },
              ] as const,
            ]
          : [],
      ),
      ...(refs.data?.machines ?? []).map((row) => [row.id, row] as const),
    ]).values(),
  )
  const assets = centres.flatMap((cell) => [
    { key: cell.id, centre: cell, machine: null },
    ...machines
      .filter((machine) => machine.work_center_id === cell.id)
      .map((machine) => ({ key: machine.id, centre: cell, machine })),
  ])
  const days = Array.from({ length: 7 }, (_, index) => dateText(addDays(parseISO(day), index)))
  const people = Array.from(
    new Map(list.data.map((row) => [row.operator_user_id, row.operator_name])),
  )
  const boardShifts = Array.from(
    new Map([
      ...list.data.map(
        (row) => [row.shift_id, { id: row.shift_id, code: row.shift_code }] as const,
      ),
      ...(shifts.data ?? []).map((row) => [row.id, row] as const),
    ]).values(),
  )
  return (
    <>
      <PageHeader
        title="Shift roster"
        description="Plan people and machines in local shift windows. Production bookings can differ from the plan."
        actions={
          rosterManage && rosterView && tab !== 'shifts' ? (
            <>
              <Button onClick={() => setDialog({ mode: 'assign' })}>Assign</Button>
              <Button variant="outline" onClick={() => setDialog({ mode: 'bulk' })}>
                Bulk assign
              </Button>
              <Button variant="outline" onClick={() => setDialog({ mode: 'copy' })}>
                Copy day
              </Button>
            </>
          ) : shiftManage && tab === 'shifts' ? (
            <Button onClick={() => setEditing(null)}>New shift</Button>
          ) : null
        }
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          {rosterView && (
            <>
              <TabsTrigger value="day">Day board</TabsTrigger>
              <TabsTrigger value="week">Week</TabsTrigger>
            </>
          )}
          {shiftView && <TabsTrigger value="shifts">Shifts</TabsTrigger>}
        </TabsList>
        {rosterView && (
          <>
            <div className="my-4 flex flex-wrap items-end gap-4">
              <div>
                <Label htmlFor="board-date">Starting date</Label>
                <Input
                  id="board-date"
                  type="date"
                  value={day}
                  onChange={(event) => {
                    if (event.target.value) setDay(event.target.value)
                  }}
                />
              </div>
              <div>
                <Label htmlFor="board-centre">Work centre</Label>
                <select
                  id="board-centre"
                  value={centre}
                  onChange={(event) => setCentre(event.target.value)}
                  className="h-10 rounded-md border bg-background px-3"
                >
                  <option value="">All work centres</option>
                  {centres.map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.code} · {row.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {list.error && (
              <Alert variant="destructive">
                <AlertDescription>{list.error.message}</AlertDescription>
              </Alert>
            )}
            {list.pagination?.has_next && (
              <Alert>
                <AlertDescription>
                  More assignments exist. Narrow the work centre to view the full board.
                </AlertDescription>
              </Alert>
            )}
          </>
        )}
        <TabsContent value="day">
          <div className="grid gap-4 md:hidden">
            {list.data.map((row) => (
              <div key={row.id} className="rounded-lg border p-3">
                <p className="mb-2 text-sm text-muted-foreground">
                  {row.work_center_code} · {row.machine_code ?? 'Cell'} · {row.shift_code}
                </p>
                {chip(row)}
              </div>
            ))}
          </div>
          <div className="hidden overflow-auto rounded-lg border md:block">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className="p-3 text-left">Work centre / machine</th>
                  {boardShifts.map((shift) => (
                    <th key={shift.id} className="p-3 text-left">
                      {shift.code}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {assets
                  .filter((asset) => !centre || asset.centre.id === centre)
                  .map((asset) => (
                    <tr key={asset.key} className="border-t">
                      <th scope="row" className="p-3 text-left font-medium">
                        {asset.centre.code} · {asset.machine?.code ?? 'Cell level'}
                      </th>
                      {boardShifts.map((shift) => (
                        <td key={shift.id} className="min-w-40 space-y-2 p-3">
                          {list.data
                            .filter(
                              (row) =>
                                row.work_center_id === asset.centre.id &&
                                row.machine_id === (asset.machine?.id ?? null) &&
                                row.shift_id === shift.id,
                            )
                            .map(chip)}
                          {rosterManage && (
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() =>
                                setDialog({
                                  mode: 'assign',
                                  cell: {
                                    roster_date: day,
                                    shift_id: shift.id,
                                    work_center_id: asset.centre.id,
                                    machine_id: asset.machine?.id ?? null,
                                  },
                                })
                              }
                            >
                              Assign
                            </Button>
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {!list.isLoading && list.data.length === 0 && (
            <p className="py-6 text-muted-foreground">No planned assignments for this date.</p>
          )}
        </TabsContent>
        <TabsContent value="week">
          <div className="overflow-auto rounded-lg border">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className="p-3 text-left">Operator</th>
                  {days.map((day) => (
                    <th className="p-3" key={day}>
                      {day}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {people.map(([id, name]) => (
                  <tr key={id} className="border-t">
                    <th scope="row" className="p-3 text-left">
                      {name}
                    </th>
                    {days.map((day) => (
                      <td className="p-3" key={day}>
                        {list.data
                          .filter((row) => row.operator_user_id === id && row.roster_date === day)
                          .map((row) => (
                            <p key={row.id}>
                              {row.shift_code} · {row.work_center_code} {row.machine_code ?? ''}
                            </p>
                          ))}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </TabsContent>
        <TabsContent value="shifts">
          <DataTable
            columns={columns}
            data={masters.data}
            pagination={masters.pagination}
            isLoading={masters.isLoading}
            error={masters.error}
            onPageChange={masters.setPage}
            onLimitChange={masters.setLimit}
            onSortChange={masters.setSort}
            onSearchChange={masters.setSearch}
            searchValue={masters.search}
            getRowId={(row) => row.id}
            emptyTitle="No shifts"
            emptyDescription="Create shifts to plan staffing and select them on production bookings."
          />
        </TabsContent>
      </Tabs>
      {editing !== undefined && shiftManage && (
        <ShiftFormDialog shift={editing} onClose={() => setEditing(undefined)} />
      )}
      {dialog && rosterManage && (
        <RosterDialog
          {...dialog}
          day={day}
          centres={refs.data?.centres ?? []}
          machines={refs.data?.machines ?? []}
          onClose={() => setDialog(null)}
        />
      )}
      <ConfirmDialog
        open={Boolean(confirm)}
        onOpenChange={(open) => {
          if (!open) {
            setConfirm(null)
            setReason('')
          }
        }}
        title={confirm?.label ?? ''}
        description="This change is recorded in the audit trail. Future assignments must be cancelled before a shift or machine can leave use."
        confirmLabel={confirm?.label ?? 'Confirm'}
        loading={action.isPending}
        onConfirm={() => {
          if (reason.trim().length >= 3) action.mutate()
        }}
      >
        <Label htmlFor="shift-reason">Reason</Label>
        <Input
          id="shift-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          minLength={3}
          maxLength={255}
        />
        {action.error && <p className="text-sm text-destructive">{action.error.message}</p>}
      </ConfirmDialog>
    </>
  )
}

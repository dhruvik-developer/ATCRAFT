/**
 * The organisation structure: company, branches, plants, departments and the
 * accounting calendar.
 *
 * Everything below the company is ordinary master data with a soft delete, so
 * "retire" always carries a reason - a record that vanished for no stated
 * reason is exactly what makes an audit trail useless six months later.
 *
 * Closing a financial year is the one irreversible act on this screen. It goes
 * through a confirmation that says so, and only a platform administrator can
 * reopen one afterwards.
 *
 * Reads are gated per tab because the API gates them per register:
 * `branch.view`, `plant.view`, `department.view`, and `settings.view` for the
 * accounting calendar (the Permission enum has no financial-year member - see
 * the ASSUMPTION in `organization/router.py`). A tab whose read right is
 * missing is not rendered at all, because it could only ever return 403; a tab
 * whose write right is missing stays fully readable with no create, edit or
 * retire affordance. All of it is presentation - the API authorises every
 * request regardless (master rules 38).
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import {
  Building2,
  CalendarRange,
  Factory,
  Lock,
  LockOpen,
  MoreHorizontal,
  Network,
  Pencil,
  Plus,
  ShieldAlert,
  Trash2,
  type LucideIcon,
} from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, EmptyState, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { Badge, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDate } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import {
  orgKeys,
  useBranchOptions,
  usePlantOptions,
  useUserOptions,
  type BranchRow,
  type DepartmentRow,
  type FinancialYearRow,
  type PlantRow,
} from './admin-types'
import { CompanyTab } from './organisation-company'
import {
  BranchFormDialog,
  DepartmentFormDialog,
  FinancialYearFormDialog,
  PlantFormDialog,
} from './organisation-dialogs'

type TabKey = 'company' | 'branches' | 'plants' | 'departments' | 'financial-years'

interface Retirable {
  id: string
  code: string
  name: string
}

/**
 * The retire confirmation.
 *
 * The reason travels in the query string rather than a DELETE body: a body on
 * DELETE is legal but several proxies drop it, and the reason is not optional.
 */
function RetireDialog({
  target,
  noun,
  path,
  onDone,
}: {
  target: Retirable | null
  noun: string
  path: string
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: ({ id, why }: { id: string; why: string }) =>
      api.delete(`${path}/${id}`, { params: { reason: why } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(`${noun} retired`, target ? `${target.code} - ${target.name}` : undefined)
      onDone()
    },
  })

  return (
    <ConfirmDialog
      open={Boolean(target)}
      onOpenChange={(open) => {
        if (!open) onDone()
      }}
      title={`Retire ${target?.code ?? ''}?`}
      description={`The ${noun.toLowerCase()} stops being selectable on new documents. Records that already reference it keep their history and stay readable.`}
      confirmLabel="Retire"
      destructive
      loading={mutation.isPending}
      onConfirm={() => {
        if (!target) return
        if (reason.trim().length < 3) {
          setReasonError('Give a reason of at least 3 characters - it is kept in the audit trail.')
          return
        }
        mutation.mutate({ id: target.id, why: reason.trim() })
      }}
    >
      <div>
        <Label htmlFor="retire-reason" required>
          Reason
        </Label>
        <Input
          id="retire-reason"
          value={reason}
          onChange={(event) => {
            setReason(event.target.value)
            setReasonError(null)
          }}
          autoComplete="off"
          aria-invalid={Boolean(reasonError)}
          className="mt-1.5"
        />
        <FieldError message={reasonError ?? undefined} />
      </div>
    </ConfirmDialog>
  )
}

// --------------------------------------------------------------- branches ---

function BranchesTab({ canManage }: { canManage: boolean }) {
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<BranchRow | null>(null)
  const [retiring, setRetiring] = useState<BranchRow | null>(null)

  const list = useListQuery<BranchRow>({
    path: '/organization/branches',
    queryKey: orgKeys.branches,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
  })

  const openEdit = useCallback((row: BranchRow) => {
    setEditing(row)
    setFormOpen(true)
  }, [])

  const columns = useMemo<ColumnDef<BranchRow, unknown>[]>(() => {
    const defs: ColumnDef<BranchRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => (
          <span className="flex items-center gap-2 font-medium">
            {row.original.code}
            {row.original.is_head_office && <Badge variant="default">Head office</Badge>}
          </span>
        ),
      },
      { accessorKey: 'name', header: 'Name' },
      {
        id: 'gstin',
        header: 'GSTIN',
        enableSorting: false,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.gstin ?? '-'}</span>,
      },
      {
        id: 'location',
        header: 'Location',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {[row.original.city, row.original.state].filter(Boolean).join(', ') || '-'}
          </span>
        ),
      },
      {
        id: 'is_active',
        header: 'Status',
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
        size: 56,
        cell: ({ row }) => (
          <div onClick={(event) => event.stopPropagation()}>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${row.original.code}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => openEdit(row.original)}>
                  <Pencil />
                  Edit
                </DropdownMenuItem>
                <DropdownMenuItem destructive onSelect={() => setRetiring(row.original)}>
                  <Trash2 />
                  Retire
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [canManage, openEdit])

  const newButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New branch
    </Button>
  ) : null

  return (
    <>
      <div className="mb-3 flex justify-end">{newButton}</div>
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
        searchPlaceholder="Search code, name or city..."
        getRowId={(row) => row.id}
        emptyTitle="No branches"
        emptyDescription="A branch is a GST registration and a billing address."
        emptyAction={newButton}
      />

      {canManage && (
        <>
          <BranchFormDialog open={formOpen} onOpenChange={setFormOpen} branch={editing} />
          <RetireDialog
            key={retiring?.id ?? 'none'}
            target={retiring}
            noun="Branch"
            path="/organization/branches"
            onDone={() => setRetiring(null)}
          />
        </>
      )}
    </>
  )
}

// ----------------------------------------------------------------- plants ---

function PlantsTab({ canManage, canSeeBranches }: { canManage: boolean; canSeeBranches: boolean }) {
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<PlantRow | null>(null)
  const [retiring, setRetiring] = useState<PlantRow | null>(null)

  // The branch column is a label lookup against `/organization/branches`, which
  // enforces `branch.view` in its own right. An account with `plant.view` alone
  // is entitled to this tab and not to that list, so do not ask for it: the
  // hook swallows the failure, but a request that can only 403 is still noise.
  const branches = useBranchOptions(canSeeBranches)
  const branchById = useMemo(() => indexById(branches.data), [branches.data])

  const list = useListQuery<PlantRow>({
    path: '/organization/plants',
    queryKey: orgKeys.plants,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
  })

  const openEdit = useCallback((row: PlantRow) => {
    setEditing(row)
    setFormOpen(true)
  }, [])

  const columns = useMemo<ColumnDef<PlantRow, unknown>[]>(() => {
    const defs: ColumnDef<PlantRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      { accessorKey: 'name', header: 'Name' },
      {
        id: 'branch',
        header: 'Branch',
        enableSorting: false,
        cell: ({ row }) => {
          const branch = row.original.branch_id ? branchById.get(row.original.branch_id) : undefined
          return <span className="text-muted-foreground">{branch?.name ?? '-'}</span>
        },
      },
      {
        id: 'location',
        header: 'Location',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {[row.original.city, row.original.state].filter(Boolean).join(', ') || '-'}
          </span>
        ),
      },
      {
        id: 'is_active',
        header: 'Status',
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
        size: 56,
        cell: ({ row }) => (
          <div onClick={(event) => event.stopPropagation()}>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${row.original.code}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => openEdit(row.original)}>
                  <Pencil />
                  Edit
                </DropdownMenuItem>
                <DropdownMenuItem destructive onSelect={() => setRetiring(row.original)}>
                  <Trash2 />
                  Retire
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [branchById, canManage, openEdit])

  const newButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New plant
    </Button>
  ) : null

  return (
    <>
      <div className="mb-3 flex justify-end">{newButton}</div>
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
        searchPlaceholder="Search code, name or city..."
        getRowId={(row) => row.id}
        emptyTitle="No plants"
        emptyDescription="A plant is a manufacturing site that warehouses and production orders belong to."
        emptyAction={newButton}
      />

      {canManage && (
        <>
          <PlantFormDialog open={formOpen} onOpenChange={setFormOpen} plant={editing} />
          <RetireDialog
            key={retiring?.id ?? 'none'}
            target={retiring}
            noun="Plant"
            path="/organization/plants"
            onDone={() => setRetiring(null)}
          />
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------ departments ---

function DepartmentsTab({
  canManage,
  canSeePlants,
}: {
  canManage: boolean
  canSeePlants: boolean
}) {
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<DepartmentRow | null>(null)
  const [retiring, setRetiring] = useState<DepartmentRow | null>(null)

  // Same reasoning as the branch column on the plants tab: the plant names come
  // from `/organization/plants`, which enforces `plant.view` separately.
  // `useUserOptions` needs no flag - it already returns an empty list to a
  // caller without `user.view`, which is the documented behaviour of that hook.
  const plants = usePlantOptions(canSeePlants)
  const users = useUserOptions()
  const plantById = useMemo(() => indexById(plants.data), [plants.data])
  const userById = useMemo(() => indexById(users.data), [users.data])

  const list = useListQuery<DepartmentRow>({
    path: '/organization/departments',
    queryKey: orgKeys.departments,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
  })

  const departmentById = useMemo(() => indexById(list.data), [list.data])

  const openEdit = useCallback((row: DepartmentRow) => {
    setEditing(row)
    setFormOpen(true)
  }, [])

  const columns = useMemo<ColumnDef<DepartmentRow, unknown>[]>(() => {
    const defs: ColumnDef<DepartmentRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      { accessorKey: 'name', header: 'Name' },
      {
        id: 'parent',
        header: 'Parent',
        enableSorting: false,
        cell: ({ row }) => {
          const parent = row.original.parent_id
            ? departmentById.get(row.original.parent_id)
            : undefined
          return (
            <span className="text-muted-foreground">
              {parent ? parent.name : row.original.parent_id ? 'On another page' : 'Top level'}
            </span>
          )
        },
      },
      {
        id: 'plant',
        header: 'Plant',
        enableSorting: false,
        cell: ({ row }) => {
          const plant = row.original.plant_id ? plantById.get(row.original.plant_id) : undefined
          return <span className="text-muted-foreground">{plant?.name ?? '-'}</span>
        },
      },
      {
        id: 'head',
        header: 'Head',
        enableSorting: false,
        cell: ({ row }) => {
          const head = row.original.head_user_id
            ? userById.get(row.original.head_user_id)
            : undefined
          return <span className="text-muted-foreground">{head?.full_name ?? '-'}</span>
        },
      },
      {
        id: 'is_active',
        header: 'Status',
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
        size: 56,
        cell: ({ row }) => (
          <div onClick={(event) => event.stopPropagation()}>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${row.original.code}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => openEdit(row.original)}>
                  <Pencil />
                  Edit
                </DropdownMenuItem>
                <DropdownMenuItem destructive onSelect={() => setRetiring(row.original)}>
                  <Trash2 />
                  Retire
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [canManage, departmentById, openEdit, plantById, userById])

  const newButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New department
    </Button>
  ) : null

  return (
    <>
      <div className="mb-3 flex justify-end">{newButton}</div>
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
        searchPlaceholder="Search code or name..."
        getRowId={(row) => row.id}
        emptyTitle="No departments"
        emptyDescription="Departments group people and cost; they nest under one another."
        emptyAction={newButton}
      />

      {canManage && (
        <>
          <DepartmentFormDialog
            open={formOpen}
            onOpenChange={setFormOpen}
            department={editing}
            siblings={list.data}
          />
          <RetireDialog
            key={retiring?.id ?? 'none'}
            target={retiring}
            noun="Department"
            path="/organization/departments"
            onDone={() => setRetiring(null)}
          />
        </>
      )}
    </>
  )
}

// -------------------------------------------------------- financial years ---

function FinancialYearsTab({ canManage }: { canManage: boolean }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<FinancialYearRow | null>(null)
  const [closing, setClosing] = useState<FinancialYearRow | null>(null)
  const [reopening, setReopening] = useState<FinancialYearRow | null>(null)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)

  const canReopen = Boolean(user?.is_superuser)

  const list = useListQuery<FinancialYearRow>({
    path: '/organization/financial-years',
    queryKey: orgKeys.financialYears,
    defaultSortBy: 'start_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.master,
  })

  const transition = useMutation({
    mutationFn: ({ id, action, why }: { id: string; action: 'close' | 'reopen'; why: string }) =>
      api.post<FinancialYearRow>(`/organization/financial-years/${id}/${action}`, { reason: why }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(
        saved.is_closed ? 'Financial year closed' : 'Financial year reopened',
        saved.code,
      )
      setClosing(null)
      setReopening(null)
    },
  })

  const openTransition = (year: FinancialYearRow, kind: 'close' | 'reopen') => {
    setReason('')
    setReasonError(null)
    if (kind === 'close') setClosing(year)
    else setReopening(year)
  }

  const confirmTransition = (kind: 'close' | 'reopen') => {
    const target = kind === 'close' ? closing : reopening
    if (!target) return
    if (reason.trim().length < 3) {
      setReasonError('Give a reason of at least 3 characters - it is kept in the audit trail.')
      return
    }
    transition.mutate({ id: target.id, action: kind, why: reason.trim() })
  }

  const columns = useMemo<ColumnDef<FinancialYearRow, unknown>[]>(() => {
    const defs: ColumnDef<FinancialYearRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Year',
        cell: ({ row }) => (
          <span className="flex items-center gap-2 font-medium">
            {row.original.code}
            {row.original.is_active && <Badge variant="default">Current</Badge>}
          </span>
        ),
      },
      {
        accessorKey: 'start_date',
        header: 'Starts',
        cell: ({ row }) => formatDate(row.original.start_date),
      },
      {
        accessorKey: 'end_date',
        header: 'Ends',
        cell: ({ row }) => formatDate(row.original.end_date),
      },
      {
        id: 'is_closed',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_closed ? 'CLOSED' : 'OPEN'} />,
      },
      {
        id: 'notes',
        header: 'Notes',
        enableSorting: false,
        cell: ({ row }) => (
          <span
            className="block max-w-[24rem] truncate text-muted-foreground"
            title={row.original.notes ?? undefined}
          >
            {row.original.notes ?? '-'}
          </span>
        ),
      },
    ]

    if (canManage || canReopen) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        size: 56,
        cell: ({ row }) => (
          <div onClick={(event) => event.stopPropagation()}>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${row.original.code}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {canManage && !row.original.is_closed && (
                  <DropdownMenuItem
                    onSelect={() => {
                      setEditing(row.original)
                      setFormOpen(true)
                    }}
                  >
                    <Pencil />
                    Edit
                  </DropdownMenuItem>
                )}
                {canManage && !row.original.is_closed && (
                  <DropdownMenuItem
                    destructive
                    onSelect={() => openTransition(row.original, 'close')}
                  >
                    <Lock />
                    Close year
                  </DropdownMenuItem>
                )}
                {canReopen && row.original.is_closed && (
                  <DropdownMenuItem onSelect={() => openTransition(row.original, 'reopen')}>
                    <LockOpen />
                    Reopen year
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [canManage, canReopen])

  const newButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      Open a year
    </Button>
  ) : null

  const reasonControl = (
    <div>
      <Label htmlFor="fy-transition-reason" required>
        Reason
      </Label>
      <Input
        id="fy-transition-reason"
        value={reason}
        onChange={(event) => {
          setReason(event.target.value)
          setReasonError(null)
        }}
        autoComplete="off"
        aria-invalid={Boolean(reasonError)}
        className="mt-1.5"
      />
      <FieldError message={reasonError ?? undefined} />
    </div>
  )

  return (
    <>
      <div className="mb-3 flex justify-end">{newButton}</div>
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
        searchPlaceholder="Search year code..."
        getRowId={(row) => row.id}
        emptyTitle="No financial years"
        emptyDescription="Open the current year before any document is posted against it."
        emptyAction={newButton}
      />

      {canManage && (
        <FinancialYearFormDialog open={formOpen} onOpenChange={setFormOpen} year={editing} />
      )}

      <ConfirmDialog
        open={Boolean(closing)}
        onOpenChange={(open) => {
          if (!open) setClosing(null)
        }}
        title={`Close ${closing?.code}?`}
        description="This cannot be undone from this screen. Once the year is closed nothing further can be posted into it, and only a platform administrator can reopen it."
        confirmLabel="Close the year"
        destructive
        loading={transition.isPending}
        onConfirm={() => confirmTransition('close')}
      >
        {reasonControl}
      </ConfirmDialog>

      <ConfirmDialog
        open={Boolean(reopening)}
        onOpenChange={(open) => {
          if (!open) setReopening(null)
        }}
        title={`Reopen ${reopening?.code}?`}
        description="Reopening a filed period lets documents be posted into a year that has already been reported on. Do it only to correct a genuine error."
        confirmLabel="Reopen the year"
        destructive
        loading={transition.isPending}
        onConfirm={() => confirmTransition('reopen')}
      >
        {reasonControl}
      </ConfirmDialog>
    </>
  )
}

// ------------------------------------------------------------------ page ----

export default function OrganisationPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  // One read right per register, exactly as `organization/router.py` declares
  // them. The route that mounts this page asks only for `company.view`, and for
  // a while the tabs restated nothing: an account holding `company.view`
  // without `branch.view` was shown a Branches tab whose list could only 403.
  //
  // The mirror case - `branch.view` without `company.view` - is not fixable
  // from here. The route guard still keeps that account off the screen
  // entirely; widening it belongs to `app/router.tsx`, not to this file.
  const canViewCompany = can(P.COMPANY_VIEW)
  const canViewBranches = can(P.BRANCH_VIEW)
  const canViewPlants = can(P.PLANT_VIEW)
  const canViewDepartments = can(P.DEPARTMENT_VIEW)
  // The accounting calendar has no permission of its own; the API gates it on
  // `settings.view` / `settings.manage` (ASSUMPTION in the organisation
  // router). `SETTINGS_MANAGE` below is the write right and was already right.
  const canViewFinancialYears = can(P.SETTINGS_VIEW)

  const tabs: { key: TabKey; label: string; icon: LucideIcon }[] = [
    ...(canViewCompany ? [{ key: 'company' as const, label: 'Company', icon: Building2 }] : []),
    ...(canViewBranches ? [{ key: 'branches' as const, label: 'Branches', icon: Network }] : []),
    ...(canViewPlants ? [{ key: 'plants' as const, label: 'Plants', icon: Factory }] : []),
    ...(canViewDepartments
      ? [{ key: 'departments' as const, label: 'Departments', icon: Network }]
      : []),
    ...(canViewFinancialYears
      ? [{ key: 'financial-years' as const, label: 'Financial years', icon: CalendarRange }]
      : []),
  ]

  const requested = searchParams.get('tab') as TabKey | null
  // A bookmarked link to a tab this account cannot open falls back to the first
  // one it can, rather than rendering an empty shell.
  const tab = tabs.some((entry) => entry.key === requested) ? (requested as TabKey) : tabs[0]?.key

  // Each tab drives its own list; carrying `page` and `sort` across would land
  // on an empty page or a 422 from a sort column the next endpoint has never
  // heard of.
  const selectTab = (next: string) => {
    setSearchParams(new URLSearchParams({ tab: next }), { replace: true })
  }

  return (
    <div>
      <PageHeader
        title="Organisation"
        description="The company, where it operates from, how it is divided, and the accounting calendar every document is dated against."
      />

      {tab === undefined ? (
        <EmptyState
          icon={ShieldAlert}
          title="Nothing here for this account"
          description="Reading the organisation structure needs one of the company.view, branch.view, plant.view, department.view or settings.view rights. Ask an administrator for the one you need."
        />
      ) : (
        <Tabs value={tab} onValueChange={selectTab}>
          <TabsList className="h-auto flex-wrap">
            {tabs.map((entry) => (
              <TabsTrigger key={entry.key} value={entry.key}>
                <entry.icon className="h-4 w-4" aria-hidden />
                {entry.label}
              </TabsTrigger>
            ))}
          </TabsList>

          {/* Radix mounts only the open tab, and a tab the account cannot read
              is not rendered at all - so no list query here can 403. */}
          {canViewCompany && (
            <TabsContent value="company">
              <CompanyTab canManage={can(P.COMPANY_MANAGE)} />
            </TabsContent>
          )}

          {canViewBranches && (
            <TabsContent value="branches">
              <BranchesTab canManage={can(P.BRANCH_MANAGE)} />
            </TabsContent>
          )}

          {canViewPlants && (
            <TabsContent value="plants">
              <PlantsTab canManage={can(P.PLANT_MANAGE)} canSeeBranches={canViewBranches} />
            </TabsContent>
          )}

          {canViewDepartments && (
            <TabsContent value="departments">
              <DepartmentsTab canManage={can(P.DEPARTMENT_MANAGE)} canSeePlants={canViewPlants} />
            </TabsContent>
          )}

          {canViewFinancialYears && (
            <TabsContent value="financial-years">
              <FinancialYearsTab canManage={can(P.SETTINGS_MANAGE)} />
            </TabsContent>
          )}
        </Tabs>
      )}
    </div>
  )
}

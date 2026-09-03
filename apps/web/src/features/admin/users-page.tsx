/**
 * Users, roles and the permission matrix.
 *
 * One screen because the three questions are one question: who has access, what
 * does a role grant, and which of those does this person hold. Splitting them
 * across three routes turns an access review into three round trips.
 *
 * Everything here is an affordance. The API re-resolves the caller's permission
 * set on every request, so hiding a button changes what is easy to do, never
 * what is possible.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import {
  KeyRound,
  MoreHorizontal,
  Pencil,
  Plus,
  Power,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, PageHeader, StatusBadge } from '@/components/shared'
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
import { Badge, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDateTime, formatNumber, formatRelative } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import {
  useBranchOptions,
  usePlantOptions,
  useRoleOptions,
  type RoleRow,
  type UserRow,
} from './admin-types'
import { PermissionMatrix } from './permission-matrix'
import { RoleFormDialog } from './role-dialogs'
import { ResetPasswordDialog, RoleAssignmentDialog, UserFormDialog } from './user-dialogs'

const ALL = '__all'

type TabKey = 'users' | 'roles' | 'permissions'

// ------------------------------------------------------------ users tab -----

function UsersTab({ onOpenRole }: { onOpenRole: (roleId: string) => void }) {
  const { can, user: signedIn } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()

  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [rolesTarget, setRolesTarget] = useState<UserRow | null>(null)
  const [resetTarget, setResetTarget] = useState<UserRow | null>(null)
  const [statusTarget, setStatusTarget] = useState<UserRow | null>(null)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)

  const canCreate = can(P.USER_CREATE)
  const canUpdate = can(P.USER_UPDATE)
  const canDeactivate = can(P.USER_DEACTIVATE)
  // `POST /users/{id}/reset-password` enforces USER_RESET_PASSWORD, not
  // USER_UPDATE. Gating this on the edit right showed the action to people the
  // API refuses, and hid it from a helpdesk role that holds only the reset.
  const canResetPassword = can(P.USER_RESET_PASSWORD)
  const canAssign = can(P.PERMISSION_ASSIGN)

  const activeState = searchParams.get('is_active') ?? ''
  const roleId = searchParams.get('role_id') ?? ''
  const accountType = searchParams.get('is_vendor_user') ?? ''

  const branches = useBranchOptions()
  const plants = usePlantOptions()
  const roles = useRoleOptions()
  const branchById = useMemo(() => indexById(branches.data), [branches.data])
  const plantById = useMemo(() => indexById(plants.data), [plants.data])

  const list = useListQuery<UserRow>({
    path: '/users',
    queryKey: queryKeys.users.list,
    defaultSortBy: 'full_name',
    staleTime: staleTimes.master,
    filters: {
      ...(activeState ? { is_active: activeState === 'true' } : {}),
      ...(roleId ? { role_id: roleId } : {}),
      ...(accountType ? { is_vendor_user: accountType === 'true' } : {}),
    },
  })

  const { setParam, setParams } = list

  const statusMutation = useMutation({
    mutationFn: ({ id, activate, why }: { id: string; activate: boolean; why: string }) =>
      api.post<UserRow>(`/users/${id}/${activate ? 'activate' : 'deactivate'}`, { reason: why }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      toast.success(
        saved.is_active ? 'User activated' : 'User deactivated',
        saved.is_active ? saved.email : `${saved.email} - live sessions revoked`,
      )
      setStatusTarget(null)
    },
  })

  const openCreate = () => {
    setEditingId(null)
    setFormOpen(true)
  }

  const openEdit = useCallback((row: UserRow) => {
    setEditingId(row.id)
    setFormOpen(true)
  }, [])

  const openStatusChange = useCallback((row: UserRow) => {
    setReason('')
    setReasonError(null)
    setStatusTarget(row)
  }, [])

  const confirmStatusChange = () => {
    if (!statusTarget) return
    if (reason.trim().length < 3) {
      setReasonError('Give a reason of at least 3 characters - it is kept in the audit trail.')
      return
    }
    statusMutation.mutate({
      id: statusTarget.id,
      activate: !statusTarget.is_active,
      why: reason.trim(),
    })
  }

  const roleByCode = useMemo(
    () => new Map((roles.data ?? []).map((role) => [role.code, role])),
    [roles.data],
  )

  const columns = useMemo<ColumnDef<UserRow, unknown>[]>(() => {
    const defs: ColumnDef<UserRow, unknown>[] = [
      {
        accessorKey: 'full_name',
        header: 'Name',
        cell: ({ row }) => (
          <div className="min-w-0">
            <span className="block truncate font-medium">{row.original.full_name}</span>
            <span className="block truncate text-xs text-muted-foreground">
              {row.original.email}
            </span>
          </div>
        ),
      },
      {
        id: 'roles',
        header: 'Roles',
        enableSorting: false,
        // `role_codes` is omitted rather than emptied for a user with no role.
        cell: ({ row }) =>
          row.original.is_superuser ? (
            <Badge variant="warning">Platform administrator</Badge>
          ) : (row.original.role_codes?.length ?? 0) === 0 ? (
            <span className="text-muted-foreground">No roles</span>
          ) : (
            <span className="flex flex-wrap gap-1">
              {(row.original.role_codes ?? []).map((code) => {
                const role = roleByCode.get(code)
                return role ? (
                  <button
                    key={code}
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation()
                      onOpenRole(role.id)
                    }}
                    className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    title={`Show what ${role.name} grants`}
                  >
                    <Badge variant="secondary">{role.name}</Badge>
                  </button>
                ) : (
                  <Badge key={code} variant="secondary">
                    {code}
                  </Badge>
                )
              })}
            </span>
          ),
      },
      {
        id: 'placement',
        header: 'Branch / plant',
        enableSorting: false,
        cell: ({ row }) => {
          const branch = row.original.branch_id ? branchById.get(row.original.branch_id) : undefined
          const plant = row.original.plant_id ? plantById.get(row.original.plant_id) : undefined
          if (!branch && !plant) return <span className="text-muted-foreground">-</span>
          return (
            <span className="block text-xs">
              <span className="block">{branch?.name ?? '-'}</span>
              <span className="block text-muted-foreground">{plant?.name ?? ''}</span>
            </span>
          )
        },
      },
      {
        id: 'binding',
        header: 'Account',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.vendor_id ? (
            <Badge variant="muted">Vendor portal</Badge>
          ) : (
            <span className="text-xs text-muted-foreground">Internal</span>
          ),
      },
      {
        accessorKey: 'last_login_at',
        header: 'Last sign-in',
        cell: ({ row }) =>
          row.original.last_login_at ? (
            <time
              dateTime={row.original.last_login_at}
              title={formatDateTime(row.original.last_login_at)}
            >
              {formatRelative(row.original.last_login_at)}
            </time>
          ) : (
            <span className="text-muted-foreground">Never</span>
          ),
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex flex-wrap items-center gap-1.5">
            <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />
            {row.original.locked_until && <Badge variant="destructive">Locked</Badge>}
            {row.original.must_change_password && (
              <Badge variant="warning">Must change password</Badge>
            )}
          </span>
        ),
      },
    ]

    if (canUpdate || canDeactivate || canAssign) {
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
                  aria-label={`Actions for ${row.original.full_name}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {canUpdate && (
                  <DropdownMenuItem onSelect={() => openEdit(row.original)}>
                    <Pencil />
                    Edit details
                  </DropdownMenuItem>
                )}
                {canAssign && (
                  <DropdownMenuItem onSelect={() => setRolesTarget(row.original)}>
                    <ShieldCheck />
                    Assign roles
                  </DropdownMenuItem>
                )}
                {canResetPassword && (
                  <DropdownMenuItem onSelect={() => setResetTarget(row.original)}>
                    <KeyRound />
                    Reset password
                  </DropdownMenuItem>
                )}
                {canDeactivate && row.original.id !== signedIn?.id && (
                  <DropdownMenuItem
                    destructive={row.original.is_active}
                    onSelect={() => openStatusChange(row.original)}
                  >
                    <Power />
                    {row.original.is_active ? 'Deactivate' : 'Activate'}
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [
    branchById,
    canAssign,
    canDeactivate,
    canResetPassword,
    canUpdate,
    onOpenRole,
    openEdit,
    openStatusChange,
    plantById,
    roleByCode,
    signedIn?.id,
  ])

  const newUserButton = canCreate ? (
    <Button onClick={openCreate}>
      <Plus />
      New user
    </Button>
  ) : null

  const hasFilters = Boolean(activeState || roleId || accountType)

  return (
    <>
      <div className="mb-3 flex justify-end">{newUserButton}</div>

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
        searchPlaceholder="Search name, email or employee code..."
        getRowId={(row) => row.id}
        emptyTitle="No users"
        emptyDescription="Nothing matches the current filters."
        emptyAction={newUserButton}
        toolbar={
          <>
            <Select
              value={activeState || ALL}
              onValueChange={(value) => setParam('is_active', value === ALL ? undefined : value)}
            >
              <SelectTrigger className="w-[150px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All statuses</SelectItem>
                <SelectItem value="true">Active only</SelectItem>
                <SelectItem value="false">Inactive only</SelectItem>
              </SelectContent>
            </Select>

            <Select
              value={roleId || ALL}
              onValueChange={(value) => setParam('role_id', value === ALL ? undefined : value)}
            >
              <SelectTrigger className="w-[190px]" aria-label="Filter by role">
                <SelectValue placeholder="All roles" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All roles</SelectItem>
                {(roles.data ?? []).map((role) => (
                  <SelectItem key={role.id} value={role.id}>
                    {role.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={accountType || ALL}
              onValueChange={(value) =>
                setParam('is_vendor_user', value === ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-[160px]" aria-label="Filter by account type">
                <SelectValue placeholder="All accounts" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All accounts</SelectItem>
                <SelectItem value="false">Internal staff</SelectItem>
                <SelectItem value="true">Vendor portal</SelectItem>
              </SelectContent>
            </Select>

            {hasFilters && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  // One navigation, not one per filter: `setSearchParams` computes every
                  // functional update in a tick from the same captured value, so a loop of
                  // `setParam` calls clears exactly the last one and looks like it did nothing.
                  setParams({
                    is_active: undefined,
                    role_id: undefined,
                    is_vendor_user: undefined,
                  })
                }}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {(canCreate || canUpdate) && (
        <UserFormDialog open={formOpen} onOpenChange={setFormOpen} userId={editingId} />
      )}

      {canAssign && rolesTarget && (
        <RoleAssignmentDialog
          key={rolesTarget.id}
          open
          onOpenChange={(open) => {
            if (!open) setRolesTarget(null)
          }}
          userId={rolesTarget.id}
          userName={rolesTarget.full_name}
        />
      )}

      {canResetPassword && resetTarget && (
        <ResetPasswordDialog
          key={resetTarget.id}
          open
          onOpenChange={(open) => {
            if (!open) setResetTarget(null)
          }}
          user={resetTarget}
        />
      )}

      {canDeactivate && (
        <ConfirmDialog
          open={Boolean(statusTarget)}
          onOpenChange={(open) => {
            if (!open) setStatusTarget(null)
          }}
          title={
            statusTarget?.is_active
              ? `Deactivate ${statusTarget.full_name}?`
              : `Activate ${statusTarget?.full_name}?`
          }
          description={
            statusTarget?.is_active
              ? 'Every live session this account holds is revoked immediately. Documents it created are untouched and keep its name.'
              : 'The account can sign in again with its existing password.'
          }
          confirmLabel={statusTarget?.is_active ? 'Deactivate' : 'Activate'}
          destructive={Boolean(statusTarget?.is_active)}
          loading={statusMutation.isPending}
          onConfirm={confirmStatusChange}
        >
          <div>
            <Label htmlFor="user-status-reason" required>
              Reason
            </Label>
            <Input
              id="user-status-reason"
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
            <p className="mt-1 text-xs text-muted-foreground">
              Recorded in the audit trail. Six months from now this is the only answer to why the
              account was switched off.
            </p>
          </div>
        </ConfirmDialog>
      )}
    </>
  )
}

// ------------------------------------------------------------ roles tab -----

function RolesTab({ onOpenRole }: { onOpenRole: (roleId: string) => void }) {
  const { can } = useAuth()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<RoleRow | null>(null)

  const canManage = can(P.ROLE_MANAGE)

  const list = useListQuery<RoleRow>({
    path: '/roles',
    queryKey: (params) => [...queryKeys.users.roles, 'list', params] as const,
    defaultSortBy: 'name',
    staleTime: staleTimes.master,
  })

  const openEdit = useCallback((role: RoleRow) => {
    setEditing(role)
    setFormOpen(true)
  }, [])

  const columns = useMemo<ColumnDef<RoleRow, unknown>[]>(() => {
    const defs: ColumnDef<RoleRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.code}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        id: 'description',
        header: 'Description',
        enableSorting: false,
        cell: ({ row }) => (
          <span
            className="block max-w-[28rem] truncate text-muted-foreground"
            title={row.original.description ?? undefined}
          >
            {row.original.description ?? '-'}
          </span>
        ),
      },
      {
        id: 'permission_count',
        header: 'Permissions',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.permission_count),
      },
      {
        id: 'flags',
        header: 'Kind',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex flex-wrap items-center gap-1.5">
            {row.original.is_system ? (
              <Badge variant="secondary">System</Badge>
            ) : (
              <Badge variant="outline">Custom</Badge>
            )}
            {row.original.is_vendor_role && <Badge variant="muted">Vendor</Badge>}
            {!row.original.is_active && <StatusBadge status="INACTIVE" />}
          </span>
        ),
      },
      {
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
                <DropdownMenuItem onSelect={() => onOpenRole(row.original.id)}>
                  <SlidersHorizontal />
                  Permissions
                </DropdownMenuItem>
                {canManage && (
                  <DropdownMenuItem onSelect={() => openEdit(row.original)}>
                    <Pencil />
                    Edit
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      },
    ]
    return defs
  }, [canManage, onOpenRole, openEdit])

  const newRoleButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New role
    </Button>
  ) : null

  return (
    <>
      <div className="mb-3 flex justify-end">{newRoleButton}</div>

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
        searchPlaceholder="Search code, name or note..."
        getRowId={(row) => row.id}
        onRowClick={(row) => onOpenRole(row.id)}
        emptyTitle="No roles"
        emptyDescription="Nothing matches the current filters."
        emptyAction={newRoleButton}
      />

      {canManage && <RoleFormDialog open={formOpen} onOpenChange={setFormOpen} role={editing} />}
    </>
  )
}

// ------------------------------------------------------------------ page ----

export default function UsersPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canSeeRoles = can(P.ROLE_VIEW)

  const tabs: { key: TabKey; label: string; icon: LucideIcon }[] = [
    { key: 'users', label: 'Users', icon: Users },
    ...(canSeeRoles
      ? [
          { key: 'roles' as const, label: 'Roles', icon: ShieldCheck },
          { key: 'permissions' as const, label: 'Permission matrix', icon: SlidersHorizontal },
        ]
      : []),
  ]

  const requested = searchParams.get('tab') as TabKey | null
  const tab: TabKey = tabs.some((entry) => entry.key === requested)
    ? (requested as TabKey)
    : 'users'
  const selectedRoleId = searchParams.get('role')

  // Switching tab clears the list state in the URL: `page=4&sort=email` from
  // the users grid means nothing to the roles grid, and carrying it over would
  // land on an empty page or a 422 from an unknown sort column.
  const selectTab = (next: string) => {
    setSearchParams(
      new URLSearchParams({ tab: next, ...(selectedRoleId ? { role: selectedRoleId } : {}) }),
      { replace: true },
    )
  }

  const openRole = useCallback(
    (roleId: string) => {
      setSearchParams(new URLSearchParams({ tab: 'permissions', role: roleId }), { replace: false })
    },
    [setSearchParams],
  )

  return (
    <div>
      <PageHeader
        title="Users & roles"
        description="Who can sign in, what each role grants, and which of those each person holds."
      />

      <Tabs value={tab} onValueChange={selectTab}>
        <TabsList className="h-auto flex-wrap">
          {tabs.map((entry) => (
            <TabsTrigger key={entry.key} value={entry.key}>
              <entry.icon className="h-4 w-4" aria-hidden />
              {entry.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="users">
          <UsersTab onOpenRole={openRole} />
        </TabsContent>

        {canSeeRoles && (
          <TabsContent value="roles">
            <RolesTab onOpenRole={openRole} />
          </TabsContent>
        )}

        {canSeeRoles && (
          <TabsContent value="permissions">
            <PermissionMatrix roleId={selectedRoleId} canManage={can(P.ROLE_MANAGE)} />
          </TabsContent>
        )}
      </Tabs>
    </div>
  )
}

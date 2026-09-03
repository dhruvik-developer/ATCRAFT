/**
 * Warehouse master.
 *
 * The grid is server-paged like every other list. Expanding a row opens that
 * warehouse's storage locations in a panel below the table rather than as
 * nested rows: the table renders exactly the page the server returned, and
 * inventing client-side sub-rows inside it would break the row/pagination
 * contract the rest of the application relies on.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  ChevronDown,
  ChevronRight,
  MapPin,
  MoreHorizontal,
  Pencil,
  Plus,
  Power,
  Star,
  X,
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
} from '@/components/ui/overlays'
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { useBranchOptions } from '@/hooks/use-organisation-options'
import { api } from '@/lib/api-client'
import { formatNumber, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import { LocationFormDialog, WarehouseFormDialog } from './warehouse-dialogs'
import { WAREHOUSE_TYPES, type StorageLocation, type Warehouse } from './warehouse-model'

const ALL = '__all'
const LOCATION_PAGE_SIZE = 50

// ------------------------------------------------------ locations panel -----

function LocationsPanel({
  warehouse,
  canManageLocations,
}: {
  warehouse: Warehouse
  canManageLocations: boolean
}) {
  const [page, setPage] = useState(1)
  const [addOpen, setAddOpen] = useState(false)

  const query = useQuery({
    queryKey: [...queryKeys.warehouses.detail(warehouse.id), 'locations', page] as const,
    queryFn: () =>
      api.getPage<StorageLocation>(`/warehouses/${warehouse.id}/locations`, {
        params: { page, limit: LOCATION_PAGE_SIZE },
      }),
    placeholderData: keepPreviousData,
    staleTime: staleTimes.master,
  })

  const columns = useMemo<ColumnDef<StorageLocation, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      { accessorKey: 'name', header: 'Name' },
      {
        id: 'location_type',
        header: 'Type',
        cell: ({ row }) => (
          <Badge variant="secondary">{humanise(row.original.location_type)}</Badge>
        ),
      },
      {
        id: 'capacity',
        header: 'Capacity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.capacity_quantity === null ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatQuantity(row.original.capacity_quantity, row.original.capacity_uom ?? undefined)
          ),
      },
      {
        id: 'barcode',
        header: 'Barcode',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.barcode ?? '-'}</span>
        ),
      },
      {
        id: 'is_active',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
    ],
    [],
  )

  const addButton = canManageLocations ? (
    <Button size="sm" onClick={() => setAddOpen(true)}>
      <Plus />
      Add location
    </Button>
  ) : null

  return (
    <Card className="mt-4">
      <CardHeader className="flex-row items-center justify-between gap-3 space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2">
            <MapPin className="h-4 w-4 text-muted-foreground" aria-hidden />
            Storage locations
          </CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            {warehouse.code} - {warehouse.name}
          </p>
        </div>
        {addButton}
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={query.data?.data ?? []}
          pagination={query.data?.pagination}
          isLoading={query.isLoading}
          isFetching={query.isFetching}
          error={query.error as Error | null}
          onPageChange={setPage}
          onRefresh={() => void query.refetch()}
          getRowId={(row) => row.id}
          emptyTitle="No storage locations"
          emptyDescription="Stock can still be held at warehouse level. Add racks and bins when picking needs them."
          emptyAction={addButton}
        />
      </CardContent>

      {canManageLocations && (
        <LocationFormDialog open={addOpen} onOpenChange={setAddOpen} warehouse={warehouse} />
      )}
    </Card>
  )
}

// ------------------------------------------------------------------ page ----

export default function WarehousesPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()

  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Warehouse | null>(null)
  const [statusTarget, setStatusTarget] = useState<Warehouse | null>(null)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)

  const canManage = can(P.WAREHOUSE_MANAGE)
  const canManageLocations = can(P.LOCATION_MANAGE)

  const warehouseType = searchParams.get('warehouse_type') ?? ''
  const activeState = searchParams.get('is_active') ?? ''

  const branches = useBranchOptions(can(P.COMPANY_VIEW))
  const branchById = useMemo(() => indexById(branches.data), [branches.data])

  const list = useListQuery<Warehouse>({
    path: '/warehouses',
    queryKey: queryKeys.warehouses.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(warehouseType ? { warehouse_type: warehouseType } : {}),
      ...(activeState ? { is_active: activeState === 'true' } : {}),
    },
  })

  const { setParam, setParams } = list
  const expanded = list.data.find((row) => row.id === expandedId) ?? null

  const statusMutation = useMutation({
    mutationFn: ({ id, activate, why }: { id: string; activate: boolean; why: string }) =>
      api.post<Warehouse>(`/warehouses/${id}/${activate ? 'activate' : 'deactivate'}`, {
        reason: why,
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.warehouses.all })
      toast.success(
        saved.is_active ? 'Warehouse activated' : 'Warehouse deactivated',
        `${saved.code} - ${saved.name}`,
      )
      setStatusTarget(null)
    },
  })

  const { mutate: setDefaultWarehouse } = useMutation({
    mutationFn: (id: string) => api.post<Warehouse>(`/warehouses/${id}/set-default`),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.warehouses.all })
      toast.success('Default warehouse updated', `${saved.code} - ${saved.name}`)
    },
  })

  const openCreate = () => {
    setEditing(null)
    setFormOpen(true)
  }

  const openEdit = useCallback((warehouse: Warehouse) => {
    setEditing(warehouse)
    setFormOpen(true)
  }, [])

  const openStatusChange = useCallback((warehouse: Warehouse) => {
    setReason('')
    setReasonError(null)
    setStatusTarget(warehouse)
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

  const columns = useMemo<ColumnDef<Warehouse, unknown>[]>(() => {
    const defs: ColumnDef<Warehouse, unknown>[] = [
      {
        id: 'expander',
        header: '',
        enableSorting: false,
        enableHiding: false,
        size: 44,
        cell: ({ row }) => {
          const open = row.original.id === expandedId
          return (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-expanded={open}
              aria-label={
                open
                  ? `Hide storage locations of ${row.original.code}`
                  : `Show storage locations of ${row.original.code}`
              }
              onClick={(event) => {
                event.stopPropagation()
                setExpandedId(open ? null : row.original.id)
              }}
            >
              {open ? <ChevronDown /> : <ChevronRight />}
            </Button>
          )
        },
      },
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => (
          <span className="flex items-center gap-2 font-medium">
            {row.original.code}
            {row.original.is_default && (
              <Badge variant="default" className="gap-1">
                <Star className="h-3 w-3" aria-hidden />
                Default
              </Badge>
            )}
          </span>
        ),
      },
      { accessorKey: 'name', header: 'Name' },
      {
        id: 'warehouse_type',
        header: 'Type',
        cell: ({ row }) => (
          <Badge variant="secondary">{humanise(row.original.warehouse_type)}</Badge>
        ),
      },
      {
        id: 'branch',
        header: 'Branch',
        enableSorting: false,
        cell: ({ row }) => {
          const branch = row.original.branch_id ? branchById.get(row.original.branch_id) : undefined
          return <span className="text-muted-foreground">{branch ? branch.name : '-'}</span>
        },
      },
      {
        id: 'location_count',
        header: 'Locations',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.location_count),
      },
      {
        id: 'city',
        header: 'City',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.city ?? '-'}</span>
        ),
      },
      {
        id: 'is_active',
        header: 'Status',
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />
            {row.original.allow_negative_stock && (
              <Badge variant="warning" title="Availability pre-check relaxed for this warehouse">
                Negative allowed
              </Badge>
            )}
          </span>
        ),
      },
    ]

    // Affordance only - the API authorises every one of these calls itself.
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
                {!row.original.is_default && row.original.is_active && (
                  <DropdownMenuItem onSelect={() => setDefaultWarehouse(row.original.id)}>
                    <Star />
                    Set as default
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  destructive={row.original.is_active}
                  onSelect={() => openStatusChange(row.original)}
                >
                  <Power />
                  {row.original.is_active ? 'Deactivate' : 'Activate'}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [branchById, canManage, expandedId, openEdit, openStatusChange, setDefaultWarehouse])

  const newWarehouseButton = canManage ? (
    <Button onClick={openCreate}>
      <Plus />
      New warehouse
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Warehouses"
        description="Stock-holding entities and the racks, bins and staging areas inside them."
        actions={newWarehouseButton}
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
        searchPlaceholder="Search code, name or city..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setExpandedId(row.id === expandedId ? null : row.id)}
        emptyTitle="No warehouses"
        emptyDescription="Nothing matches the current filters."
        emptyAction={newWarehouseButton}
        toolbar={
          <>
            <Select
              value={warehouseType || ALL}
              onValueChange={(value) =>
                setParam('warehouse_type', value === ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-[190px]" aria-label="Filter by warehouse type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All types</SelectItem>
                {WAREHOUSE_TYPES.map((type) => (
                  <SelectItem key={type} value={type}>
                    {humanise(type)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

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

            {(warehouseType || activeState) && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  // One navigation, not one per filter: `setSearchParams` computes every
                  // functional update in a tick from the same captured value, so a loop of
                  // `setParam` calls clears exactly the last one and looks like it did nothing.
                  setParams({
                    warehouse_type: undefined,
                    is_active: undefined,
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

      {expanded && (
        <LocationsPanel
          key={expanded.id}
          warehouse={expanded}
          canManageLocations={canManageLocations}
        />
      )}

      {canManage && (
        <>
          <WarehouseFormDialog open={formOpen} onOpenChange={setFormOpen} warehouse={editing} />

          <ConfirmDialog
            open={Boolean(statusTarget)}
            onOpenChange={(open) => {
              if (!open) setStatusTarget(null)
            }}
            title={
              statusTarget?.is_active
                ? `Deactivate ${statusTarget.code}?`
                : `Activate ${statusTarget?.code}?`
            }
            description={
              statusTarget?.is_active
                ? 'Documents will stop proposing this warehouse. Stock already held in it is untouched and stays visible in reports.'
                : 'The warehouse becomes selectable on documents again.'
            }
            confirmLabel={statusTarget?.is_active ? 'Deactivate' : 'Activate'}
            destructive={Boolean(statusTarget?.is_active)}
            loading={statusMutation.isPending}
            onConfirm={confirmStatusChange}
          >
            <div>
              <Label htmlFor="warehouse-status-reason" required>
                Reason
              </Label>
              <Input
                id="warehouse-status-reason"
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
                warehouse was switched off.
              </p>
            </div>
          </ConfirmDialog>
        </>
      )}
    </>
  )
}

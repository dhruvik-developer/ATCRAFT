/**
 * Storage locations across every warehouse.
 *
 * The warehouses screen shows the bins inside one warehouse. This screen answers
 * the question that one cannot: find a location wherever it lives, or audit the
 * racks of six plants side by side. So the list is sorted by code across
 * warehouses on purpose - codes are unique per warehouse, not per company, and
 * seeing A-01 appear four times is precisely how an inconsistent labelling
 * standard becomes visible.
 *
 * Every filter is a query parameter the API answers. A company that racks its
 * warehouses properly has tens of thousands of bins, and the browser never
 * receives more than one page of them (master rules 31/36/78).
 *
 * This screen is read-only. Creating and editing a location are
 * `LOCATION_MANAGE` operations that already live in `warehouse-dialogs.tsx`,
 * next to the warehouse they are created into - which is where the server puts
 * them too, since `warehouse_id` comes from the create route's path and not from
 * a body field this page could supply.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { CornerDownRight, Layers, X } from 'lucide-react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DescriptionList, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Checkbox,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatDateTime, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import {
  describeCapacity,
  describeLimit,
  isRetired,
  LOCATION_TYPES,
  locationKeys,
  useLocationDetail,
  type StorageLocationRow,
} from './location-model'
import { useWarehouseOptions, type WarehouseOption } from './warehouse-model'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = [
  'warehouse_id',
  'location_type',
  'is_active',
  'parent_location_id',
  'include_deleted',
] as const

/**
 * Where a warehouse is addressable from here.
 *
 * There is no warehouse detail route: the warehouses screen opens its location
 * panel from component state, so no URL points at one warehouse. The list
 * narrowed to that code is the closest honest link, and `q` is the same search
 * parameter `useListQuery` reads there.
 */
const warehouseHref = (code: string) => `/warehouses?q=${encodeURIComponent(code)}`

// ------------------------------------------------------- detail dialog ------

/**
 * One location in full.
 *
 * Rendered from the row the grid already holds rather than re-fetched:
 * `StorageLocationRead` is the same schema on the list and on
 * `GET /warehouses/locations/{id}`, so a second call would buy nothing - and
 * would fail outright for a retired row, which the list can show under
 * `include_deleted` but the detail endpoint scopes out.
 *
 * The one thing the row cannot supply is the parent's code, so that single id is
 * resolved here, for the one location the user opened.
 */
function LocationDetailDialog({
  location,
  warehouse,
  canLinkWarehouse,
  onOpenChange,
  onFilterByParent,
}: {
  location: StorageLocationRow | null
  warehouse: WarehouseOption | undefined
  canLinkWarehouse: boolean
  onOpenChange: (open: boolean) => void
  onFilterByParent: (parentId: string) => void
}) {
  const retired = location ? isRetired(location) : false

  // Not asked for a retired location. Retiring is refused while live
  // sub-locations remain, so a live row's parent is guaranteed live and visible
  // here; a retired row's parent may have been retired after it, and that 404
  // surfaces as a "Could not load data" toast which explains nothing.
  const parent = useLocationDetail(location?.parent_location_id ?? null, !retired)

  let parentValue: ReactNode = 'Top level'
  if (location?.parent_location_id) {
    if (retired) {
      parentValue = (
        <span className="text-muted-foreground">Not resolved while this location is retired</span>
      )
    } else if (parent.isLoading) {
      parentValue = <Skeleton className="h-4 w-40" />
    } else if (parent.data) {
      const above = parent.data
      parentValue = (
        <button
          type="button"
          onClick={() => onFilterByParent(above.id)}
          aria-label={`Show every location inside ${above.code}`}
          className="rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {above.code} - {above.name}
        </button>
      )
    } else {
      parentValue = <span className="text-muted-foreground">Could not be resolved</span>
    }
  }

  const warehouseValue: ReactNode = warehouse ? (
    canLinkWarehouse ? (
      <Link to={warehouseHref(warehouse.code)} className="hover:underline">
        {warehouse.code} - {warehouse.name}
      </Link>
    ) : (
      `${warehouse.code} - ${warehouse.name}`
    )
  ) : (
    <span className="text-muted-foreground">Not in the active warehouse list</span>
  )

  return (
    <Dialog open={Boolean(location)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {location ? `${location.code} - ${location.name}` : 'Storage location'}
          </DialogTitle>
          {/* The ceilings are stated as advisory because they are: no module
              outside `warehouses` reads capacity_quantity, max_weight_kg or
              max_volume_m3, so a stock movement into a full bin is not refused
              and the screen must not imply otherwise. */}
          <DialogDescription>
            Where stock physically sits. Capacity and the weight and volume ceilings are recorded
            for planning; no movement is refused for exceeding one.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {location && retired && (
            <Alert variant="warning">
              <AlertTitle>Retired location</AlertTitle>
              <AlertDescription>
                Withdrawn on {formatDateTime(location.deleted_at)}. It held no stock and had no live
                sub-locations at the time, and it is shown only because retired locations were asked
                for.
              </AlertDescription>
            </Alert>
          )}

          {location && (
            <>
              <DescriptionList
                columns={2}
                items={[
                  { label: 'Warehouse', value: warehouseValue },
                  {
                    label: 'Type',
                    value: <Badge variant="secondary">{humanise(location.location_type)}</Badge>,
                  },
                  { label: 'Sits inside', value: parentValue },
                  {
                    label: 'Barcode',
                    value: location.barcode ?? (
                      <span className="text-muted-foreground">Not labelled</span>
                    ),
                  },
                  {
                    label: 'Capacity',
                    value: describeCapacity(location) ?? (
                      <span className="text-muted-foreground">Not recorded</span>
                    ),
                  },
                  {
                    label: 'Maximum weight',
                    value: describeLimit(location.max_weight_kg, 'kg') ?? (
                      <span className="text-muted-foreground">Not recorded</span>
                    ),
                  },
                  {
                    label: 'Maximum volume',
                    value: describeLimit(location.max_volume_m3, 'm3') ?? (
                      <span className="text-muted-foreground">Not recorded</span>
                    ),
                  },
                  {
                    label: 'Status',
                    value: <StatusBadge status={location.is_active ? 'ACTIVE' : 'INACTIVE'} />,
                  },
                  { label: 'Created', value: formatDateTime(location.created_at) },
                  { label: 'Last changed', value: formatDateTime(location.updated_at) },
                ]}
              />

              {location.notes && (
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Notes
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-sm">{location.notes}</p>
                </div>
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {location && (
            // Nothing on the row says whether this location has children, so the
            // action is always offered and an empty result is a real answer -
            // the empty state below says so in as many words.
            <Button variant="secondary" onClick={() => onFilterByParent(location.id)}>
              <Layers />
              Show sub-locations
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------ page ----

export default function LocationsPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [detailRow, setDetailRow] = useState<StorageLocationRow | null>(null)

  // Reading a location is LOCATION_VIEW; the route itself carries that right.
  // Warehouse names, the warehouse filter and every link out of this screen
  // depend on the warehouse master instead, which is a different right - so they
  // are gated on the one their own endpoint and route enforce.
  const canViewWarehouses = can(P.WAREHOUSE_VIEW)
  const canManageLocations = can(P.LOCATION_MANAGE)

  const warehouseId = searchParams.get('warehouse_id') ?? ''
  const locationType = searchParams.get('location_type') ?? ''
  const activeState = searchParams.get('is_active') ?? ''
  const parentLocationId = searchParams.get('parent_location_id') ?? ''
  const includeRetired = searchParams.get('include_deleted') === 'true'

  /**
   * Warehouse codes for the filter and for every row's warehouse cell.
   *
   * One request for the whole page, not one per row. The endpoint lists ACTIVE
   * warehouses only, so a location left behind in a deactivated warehouse
   * resolves to nothing here - which is stated rather than papered over with a
   * uuid, because in an audit that is itself the finding.
   */
  const warehouses = useWarehouseOptions(canViewWarehouses)
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])
  const warehousesResolved = warehouses.isSuccess

  const list = useListQuery<StorageLocationRow>({
    path: '/warehouses/locations',
    queryKey: locationKeys.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(locationType ? { location_type: locationType } : {}),
      ...(activeState ? { is_active: activeState === 'true' } : {}),
      ...(parentLocationId ? { parent_location_id: parentLocationId } : {}),
      // Sent only when asked for: the server defaults it to false, and an
      // explicit `include_deleted=false` on every request is noise in the log.
      ...(includeRetired ? { include_deleted: true } : {}),
    },
  })

  const { setParam } = list

  /** Names the location the `parent_location_id` filter is pinned to. */
  const parentFilter = useLocationDetail(parentLocationId || null)

  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` captured when the callback was made, then
   * navigates. Five `setParam` calls in a row would all start from the same
   * stale base and only the last would survive.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        // The page number belonged to the narrowed list, not to this one.
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  const filterByParent = useCallback(
    (parentId: string) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          next.set('parent_location_id', parentId)
          next.delete('page')
          return next
        },
        { replace: true },
      )
      setDetailRow(null)
    },
    [setSearchParams],
  )

  /**
   * Retiring a location sets `is_active = False` as well as stamping
   * `deleted_at`, so "Active only" and "Include retired" together can only ever
   * return nothing. Asking for retired rows therefore drops an active-only
   * narrowing in the same write, rather than leaving the user staring at an
   * empty grid they have no way to read.
   */
  const toggleRetired = useCallback(
    (next: boolean) => {
      setSearchParams(
        (current) => {
          const params = new URLSearchParams(current)
          if (next) {
            params.set('include_deleted', 'true')
            if (params.get('is_active') === 'true') params.delete('is_active')
          } else {
            params.delete('include_deleted')
          }
          params.delete('page')
          return params
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const columns = useMemo<ColumnDef<StorageLocationRow, unknown>[]>(() => {
    const defs: ColumnDef<StorageLocationRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        // A button rather than a row click: the warehouse cell holds a link, and
        // a handler on the whole row would swallow it and be unreachable from
        // the keyboard.
        cell: ({ row }) => (
          <button
            type="button"
            onClick={() => setDetailRow(row.original)}
            aria-label={`Show details of location ${row.original.code}`}
            className="max-w-[12rem] rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="block truncate font-medium">{row.original.code}</span>
            {/* Search matches code, name AND barcode server-side, so the barcode
                is shown to explain why a scan landed on this row. */}
            {row.original.barcode && (
              <span className="tabular block truncate text-xs text-muted-foreground">
                {row.original.barcode}
              </span>
            )}
          </button>
        ),
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate">{row.original.name}</span>
        ),
      },
    ]

    // Without the warehouse master there is no code to print and nowhere to
    // link, and `warehouse_id` on its own is a uuid nobody can act on. The
    // column is left out entirely rather than filled with dashes.
    if (canViewWarehouses) {
      defs.push({
        id: 'warehouse',
        header: 'Warehouse',
        // `StorageLocationRepository.sortable_fields` has no warehouse column,
        // so offering the sort would send a `sort_by` the API ignores.
        enableSorting: false,
        cell: ({ row }) => {
          const warehouse = warehouseById.get(row.original.warehouse_id)
          if (warehouse) {
            return (
              <Link
                to={warehouseHref(warehouse.code)}
                className="block max-w-[14rem] rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="block truncate font-medium">{warehouse.code}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {warehouse.name}
                </span>
              </Link>
            )
          }
          return warehousesResolved ? (
            <Badge variant="warning">Not an active warehouse</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          )
        },
      })
    }

    defs.push(
      {
        accessorKey: 'location_type',
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
        // An unrecorded capacity is shown as unset, never as zero: "nobody has
        // measured this bin" and "this bin takes nothing" are different claims.
        cell: ({ row }) => {
          const capacity = describeCapacity(row.original)
          return capacity ?? <span className="text-muted-foreground">Not set</span>
        },
      },
      {
        id: 'nesting',
        header: 'Nesting',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.parent_location_id ? (
            <Badge variant="outline" className="gap-1">
              <CornerDownRight className="h-3 w-3" aria-hidden />
              Sub-location
            </Badge>
          ) : (
            <span className="text-xs text-muted-foreground">Top level</span>
          ),
      },
      {
        accessorKey: 'is_active',
        header: 'Status',
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />
            {/* Retired is not deactivated: the row has been withdrawn and only
                appears because retired locations were asked for. */}
            {isRetired(row.original) && <Badge variant="destructive">Retired</Badge>}
          </span>
        ),
      },
      {
        accessorKey: 'updated_at',
        header: 'Last changed',
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {formatDate(row.original.updated_at)}
          </span>
        ),
      },
    )

    return defs
  }, [canViewWarehouses, warehouseById, warehousesResolved])

  // Navigation only - no endpoint behind it. It is offered to the people who
  // could act on it: `LOCATION_MANAGE` creates the location, and the warehouses
  // route itself is behind `WAREHOUSE_VIEW`.
  const addLocationHint =
    canViewWarehouses && canManageLocations ? (
      <Button asChild variant="outline">
        <Link to="/warehouses">Open the warehouses screen</Link>
      </Button>
    ) : null

  return (
    <>
      <PageHeader
        title="Storage locations"
        description="Every rack, bin, shelf, floor space and staging area in the company, across all warehouses. Codes are unique within a warehouse, so the same code can legitimately appear more than once here."
        breadcrumbs={[
          // The crumb only links where the user can actually land: the
          // warehouses route is behind WAREHOUSE_VIEW.
          { label: 'Warehouses', to: canViewWarehouses ? '/warehouses' : undefined },
          { label: 'Storage locations' },
        ]}
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
        searchPlaceholder="Search code, name or barcode..."
        getRowId={(row) => row.id}
        emptyTitle={parentLocationId ? 'Nothing sits inside this location' : 'No storage locations'}
        emptyDescription={
          parentLocationId
            ? 'This location has no sub-locations. Clear the nesting filter to go back to the full list.'
            : 'Nothing matches these filters. Stock can still be held at warehouse level - racks and bins are added inside a warehouse, on the warehouses screen.'
        }
        emptyAction={parentLocationId ? undefined : addLocationHint}
        toolbar={
          <>
            {canViewWarehouses && (
              <Select
                value={warehouseId || ALL}
                onValueChange={(value) =>
                  setParam('warehouse_id', value === ALL ? undefined : value)
                }
              >
                <SelectTrigger className="w-[13rem]" aria-label="Filter by warehouse">
                  <SelectValue placeholder="All warehouses" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All warehouses</SelectItem>
                  {(warehouses.data ?? []).map((warehouse) => (
                    <SelectItem key={warehouse.id} value={warehouse.id}>
                      {warehouse.code} - {warehouse.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            <Select
              value={locationType || ALL}
              onValueChange={(value) =>
                setParam('location_type', value === ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-[10rem]" aria-label="Filter by location type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All types</SelectItem>
                {LOCATION_TYPES.map((type) => (
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
              <SelectTrigger className="w-[11rem]" aria-label="Filter by status">
                <SelectValue placeholder="Any status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any status</SelectItem>
                <SelectItem value="true">Active only</SelectItem>
                <SelectItem value="false">Deactivated only</SelectItem>
              </SelectContent>
            </Select>

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="locations-include-retired"
                checked={includeRetired}
                onCheckedChange={(checked) => toggleRetired(checked === true)}
              />
              <Label
                htmlFor="locations-include-retired"
                className="cursor-pointer text-xs font-normal"
              >
                Include retired
              </Label>
            </div>

            {parentLocationId && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setParam('parent_location_id', undefined)}
                aria-label="Stop showing only sub-locations"
              >
                <CornerDownRight />
                Inside {parentFilter.data ? parentFilter.data.code : 'one location'}
                <X />
              </Button>
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

      <LocationDetailDialog
        location={detailRow}
        warehouse={detailRow ? warehouseById.get(detailRow.warehouse_id) : undefined}
        canLinkWarehouse={canViewWarehouses}
        onOpenChange={(open) => !open && setDetailRow(null)}
        onFilterByParent={filterByParent}
      />
    </>
  )
}

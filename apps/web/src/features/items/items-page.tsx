/**
 * Item master list.
 *
 * Every filter here is a query parameter the API understands, so the browser
 * never receives more than one page of a table that will hold tens of thousands
 * of rows (master rules 31/36).
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Plus, SlidersHorizontal, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useBrandOptions } from '@/hooks/use-brand-options'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { cn, indexById } from '@/lib/utils'

import { ItemFormDialog } from './item-form-dialog'
import {
  CAPABILITY_META,
  FILTERABLE_CAPABILITIES,
  GRID_CAPABILITIES,
  ITEM_GROUPS,
  useCategoryOptions,
  useUomOptions,
  type ItemRow,
} from './item-model'

/** Compact, scannable read-out of the six capabilities the list row carries. */
function CapabilityRow({ item }: { item: ItemRow }) {
  const enabled = GRID_CAPABILITIES.filter((key) => item[key])
  return (
    <div className="flex items-center gap-1">
      {/* The icons are decorative once the same information is available as
          text; a screen reader gets the list, not six ambiguous glyphs. */}
      <span className="sr-only">
        {enabled.length === 0
          ? 'No capabilities enabled'
          : enabled.map((key) => CAPABILITY_META[key].label).join(', ')}
      </span>
      {GRID_CAPABILITIES.map((key) => {
        const { label, icon: Icon } = CAPABILITY_META[key]
        const on = item[key]
        return (
          <span key={key} title={`${label}: ${on ? 'yes' : 'no'}`} aria-hidden>
            <Icon className={cn('h-3.5 w-3.5', on ? 'text-primary' : 'text-muted-foreground/25')} />
          </span>
        )
      })}
    </div>
  )
}

export default function ItemsPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  const canViewCost = can(P.ITEM_COST_VIEW)

  const categoryId = searchParams.get('category_id') ?? ''
  const itemGroup = searchParams.get('item_group') ?? ''
  const brandId = searchParams.get('brand_id') ?? ''
  // The API models `is_active` as a two-valued filter that always defaults to
  // active, so the control offers exactly the two states the server can answer
  // rather than an "all" option it would silently ignore.
  const activeState = searchParams.get('is_active') === 'false' ? 'false' : 'true'
  const activeCapabilities = FILTERABLE_CAPABILITIES.filter(
    (key) => searchParams.get(key) === 'true',
  )

  const categories = useCategoryOptions(can(P.CATEGORY_VIEW))
  const uoms = useUomOptions(can(P.UOM_VIEW))
  // The brand filter is offered only to an account that may read brands; a
  // shared link carrying `brand_id` still narrows the list for anyone, because
  // the filter is the server's, not this control's.
  const canFilterByBrand = can(P.BRAND_VIEW)
  const brands = useBrandOptions(canFilterByBrand)
  const categoryById = useMemo(() => indexById(categories.data), [categories.data])
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  const list = useListQuery<ItemRow>({
    path: '/products/items',
    queryKey: queryKeys.items.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(categoryId ? { category_id: categoryId } : {}),
      ...(itemGroup ? { item_group: itemGroup } : {}),
      ...(brandId ? { brand_id: brandId } : {}),
      is_active: activeState === 'false' ? false : true,
      ...Object.fromEntries(activeCapabilities.map((key) => [key, true])),
    },
  })

  const { setParam, setParams } = list
  const filterCount =
    activeCapabilities.length +
    (categoryId ? 1 : 0) +
    (itemGroup ? 1 : 0) +
    (brandId ? 1 : 0) +
    (activeState === 'false' ? 1 : 0)

  const columns = useMemo<ColumnDef<ItemRow, unknown>[]>(() => {
    const defs: ColumnDef<ItemRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        // A real link, not just a clickable row: the row-click affordance is for
        // the mouse, and this screen is driven from the keyboard all day.
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <span className="block max-w-[22rem] truncate">{row.original.name}</span>
        ),
      },
      {
        id: 'category',
        header: 'Category',
        enableSorting: false,
        cell: ({ row }) => {
          const category = row.original.category_id
            ? categoryById.get(row.original.category_id)
            : undefined
          return <span className="text-muted-foreground">{category ? category.name : '-'}</span>
        },
      },
      {
        id: 'item_group',
        header: 'Group',
        enableSorting: false,
        cell: ({ row }) => <Badge variant="secondary">{humanise(row.original.item_group)}</Badge>,
      },
      {
        id: 'base_uom',
        header: 'UoM',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {uomById.get(row.original.base_uom_id)?.code ?? '-'}
          </span>
        ),
      },
      {
        id: 'capabilities',
        header: 'Capabilities',
        enableSorting: false,
        cell: ({ row }) => <CapabilityRow item={row.original} />,
      },
      // No stock or reorder-level column. `ItemSummary` is the narrow list
      // projection and carries neither, so both rendered an empty cell on every
      // row; joining the balances endpoint in the browser to fill them would be
      // the fetch-everything-and-filter pattern master rules 31 forbids. Stock
      // for one item is answered by the Stock tab on the item detail page.
    ]

    // Cost is withheld by the API itself for a principal without item.cost_view;
    // hiding the column keeps the grid honest rather than showing empty cells.
    if (canViewCost) {
      defs.push({
        id: 'standard_cost',
        header: 'Standard cost',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.standard_cost === undefined ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatMoney(row.original.standard_cost)
          ),
      })
    }

    defs.push({
      id: 'status',
      header: 'Status',
      enableSorting: false,
      cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
    })

    return defs
  }, [canViewCost, categoryById, uomById])

  const newItemButton = can(P.ITEM_CREATE) ? (
    <Button onClick={() => setCreateOpen(true)}>
      <Plus />
      New item
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Items"
        description="The single item master. One item may be purchased, manufactured, sold, used in a BOM and sent for job work at the same time."
        actions={newItemButton}
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
        searchPlaceholder="Search code or name..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/items/${row.id}`)}
        emptyTitle="No items found"
        emptyDescription="Nothing matches the current filters. Widen the search or clear a filter."
        emptyAction={newItemButton}
        toolbar={
          <>
            <Select
              value={categoryId || SELECT_ALL}
              onValueChange={(value) =>
                setParam('category_id', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[190px]" aria-label="Filter by category">
                <SelectValue placeholder="All categories" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All categories</SelectItem>
                {(categories.data ?? []).map((category) => (
                  <SelectItem key={category.id} value={category.id}>
                    {category.code} - {category.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={itemGroup || SELECT_ALL}
              onValueChange={(value) =>
                setParam('item_group', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by item group">
                <SelectValue placeholder="All groups" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All groups</SelectItem>
                {ITEM_GROUPS.map((group) => (
                  <SelectItem key={group} value={group}>
                    {humanise(group)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {canFilterByBrand && (
              <Select
                value={brandId || SELECT_ALL}
                onValueChange={(value) =>
                  setParam('brand_id', value === SELECT_ALL ? undefined : value)
                }
              >
                <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by brand">
                  <SelectValue placeholder="All brands" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_ALL}>All brands</SelectItem>
                  {(brands.data ?? []).map((brand) => (
                    <SelectItem key={brand.id} value={brand.id}>
                      {brand.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            <Select value={activeState} onValueChange={(value) => setParam('is_active', value)}>
              <SelectTrigger className="w-full sm:w-[130px]" aria-label="Filter by status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="true">Active only</SelectItem>
                <SelectItem value="false">Inactive only</SelectItem>
              </SelectContent>
            </Select>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  <SlidersHorizontal />
                  Capabilities
                  {activeCapabilities.length > 0 && (
                    <Badge variant="default" className="ml-1 px-1.5">
                      {activeCapabilities.length}
                    </Badge>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuLabel>Must be able to</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {/* Several may be ticked at once: the flags are independent, so
                    "purchasable AND manufacturable" is a real question. */}
                {FILTERABLE_CAPABILITIES.map((key) => (
                  <DropdownMenuCheckboxItem
                    key={key}
                    checked={activeCapabilities.includes(key)}
                    onCheckedChange={(checked) => setParam(key, checked ? 'true' : undefined)}
                    onSelect={(event) => event.preventDefault()}
                  >
                    {CAPABILITY_META[key].label}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  // One navigation, not one per filter: `setSearchParams` computes every
                  // functional update in a tick from the same captured value, so a loop of
                  // `setParam` calls clears exactly the last one and looks like it did nothing.
                  setParams({
                    category_id: undefined,
                    item_group: undefined,
                    brand_id: undefined,
                    is_active: undefined,
                    ...Object.fromEntries(FILTERABLE_CAPABILITIES.map((key) => [key, undefined])),
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

      {can(P.ITEM_CREATE) && <ItemFormDialog open={createOpen} onOpenChange={setCreateOpen} />}
    </>
  )
}

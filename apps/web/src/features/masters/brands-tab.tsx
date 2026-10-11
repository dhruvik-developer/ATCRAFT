/**
 * The Brands tab: the names products are sold under.
 *
 * A tab of the item-masters screen, loaded on first open (see `masters-page`).
 * Reads need `brand.view` - the page renders this tab only for a holder - and
 * every write affordance is gated on `brand.manage`, the right each brand
 * endpoint declares.
 *
 * The list is server-driven throughout. `code`, `name` and `sort_order` are the
 * columns `BrandRepository.sortable_fields` whitelists, so exactly those three
 * headers sort; the owner column is a label resolved from the manufacturer
 * options and sorts nothing.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { MoreHorizontal, Pencil, Plus } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
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
import { useAuth } from '@/features/auth/auth-context'
import { manufacturerLabeller, useManufacturerOptions } from '@/hooks/use-manufacturer-options'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { BrandFormDialog, BrandRetireDialog } from './brand-dialogs'
import { mastersKeys, type BrandRow } from './masters-model'
import { ActiveBadge, StatusFilter, TabIntro } from './masters-shared'
import { useFilterWriter } from './use-filter-writer'

export function BrandsTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const setFilters = useFilterWriter()

  const canManage = can(P.BRAND_MANAGE)
  // The owner is a manufacturer, which is a right of its own: without it the
  // owner column falls back to a sentence and the owner filter is not offered.
  const canSeeMakers = can(P.MANUFACTURER_VIEW)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<BrandRow | null>(null)
  const [retiring, setRetiring] = useState<BrandRow | null>(null)

  const status = searchParams.get('status') ?? ''
  const ownerId = searchParams.get('manufacturer_id') ?? ''

  const list = useListQuery<BrandRow>({
    path: '/products/brands',
    queryKey: mastersKeys.brands,
    staleTime: staleTimes.reference,
    filters: {
      ...(status ? { is_active: status === 'active' } : {}),
      ...(ownerId ? { manufacturer_id: ownerId } : {}),
    },
  })

  const makers = useManufacturerOptions(canSeeMakers)
  const ownerLabel = useMemo(() => manufacturerLabeller(makers.data), [makers.data])

  const columns = useMemo<ColumnDef<BrandRow, unknown>[]>(() => {
    const defs: ColumnDef<BrandRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate">{row.original.name}</span>
        ),
      },
      {
        id: 'manufacturer',
        header: 'Owner',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{ownerLabel(row.original.manufacturer_id)}</span>
        ),
      },
      {
        accessorKey: 'sort_order',
        header: 'Position',
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.sort_order}</span>,
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <ActiveBadge isActive={row.original.is_active} />,
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
          <div className="flex items-center justify-end gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Edit ${row.original.code}`}
              onClick={() => {
                setEditing(row.original)
                setFormOpen(true)
              }}
            >
              <Pencil />
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`More actions for ${row.original.code}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem destructive onSelect={() => setRetiring(row.original)}>
                  Retire brand
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [canManage, ownerLabel])

  const newButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New brand
    </Button>
  ) : null

  return (
    <>
      <TabIntro actions={newButton}>
        The names products are sold under - the house brand, its premium line, partner brands. Items
        choose from this list, and a renamed brand reaches every item at once. A brand that items
        still carry cannot be retired.
      </TabIntro>

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
        emptyTitle="No brands yet"
        emptyDescription={
          canManage
            ? 'Add the brands this company sells under, and they appear on every item form.'
            : 'Nothing matches the current filters.'
        }
        emptyAction={newButton}
        toolbar={
          <>
            <StatusFilter
              value={status}
              onChange={(next) => setFilters({ status: next })}
              label="Filter brands by status"
            />

            {canSeeMakers && (
              <Select
                value={ownerId || SELECT_ALL}
                onValueChange={(value) =>
                  setFilters({ manufacturer_id: value === SELECT_ALL ? undefined : value })
                }
              >
                <SelectTrigger className="w-full sm:w-[14rem]" aria-label="Filter brands by owner">
                  <SelectValue placeholder="Any owner" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_ALL}>Any owner</SelectItem>
                  {(makers.data ?? []).map((maker) => (
                    <SelectItem key={maker.id} value={maker.id}>
                      {maker.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </>
        }
      />

      {canManage && <BrandFormDialog open={formOpen} onOpenChange={setFormOpen} brand={editing} />}
      {canManage && (
        <BrandRetireDialog brand={retiring} onOpenChange={(open) => !open && setRetiring(null)} />
      )}
    </>
  )
}

/**
 * The Manufacturers tab: who makes the parts.
 *
 * A tab of the item-masters screen, loaded on first open (see `masters-page`).
 * Reads need `manufacturer.view` - the page renders this tab only for a holder -
 * and every write affordance is gated on `manufacturer.manage`, the right each
 * manufacturer endpoint declares.
 *
 * `code` and `name` are the columns `ManufacturerRepository.sortable_fields`
 * whitelists, so exactly those two headers sort.
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
} from '@/components/ui/overlays'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { ManufacturerFormDialog, ManufacturerRetireDialog } from './manufacturer-dialogs'
import { mastersKeys, type ManufacturerRow } from './masters-model'
import { ActiveBadge, StatusFilter, TabIntro } from './masters-shared'
import { useFilterWriter } from './use-filter-writer'

export function ManufacturersTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const setFilters = useFilterWriter()

  const canManage = can(P.MANUFACTURER_MANAGE)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ManufacturerRow | null>(null)
  const [retiring, setRetiring] = useState<ManufacturerRow | null>(null)

  const status = searchParams.get('status') ?? ''

  const list = useListQuery<ManufacturerRow>({
    path: '/products/manufacturers',
    queryKey: mastersKeys.manufacturers,
    staleTime: staleTimes.reference,
    filters: status ? { is_active: status === 'active' } : {},
  })

  const columns = useMemo<ColumnDef<ManufacturerRow, unknown>[]>(() => {
    const defs: ColumnDef<ManufacturerRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <span className="block truncate">{row.original.name}</span>
            {row.original.legal_name && (
              <span className="block truncate text-xs text-muted-foreground">
                {row.original.legal_name}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'country',
        header: 'Country',
        enableSorting: false,
        cell: ({ row }) => <span className="text-muted-foreground">{row.original.country}</span>,
      },
      {
        id: 'website',
        header: 'Website',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.website ? (
            <span className="block max-w-[14rem] truncate text-xs text-muted-foreground">
              {row.original.website}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
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
                  Retire manufacturer
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [canManage])

  const newButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New manufacturer
    </Button>
  ) : null

  return (
    <>
      <TabIntro actions={newButton}>
        Who makes the parts - this company's own works and the outside makers whose parts are bought
        in, often through a distributor. Items, brands and vendor links name a maker from this list;
        one they still name cannot be retired.
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
        searchPlaceholder="Search code, name or legal name..."
        getRowId={(row) => row.id}
        emptyTitle="No manufacturers yet"
        emptyDescription={
          canManage
            ? 'Add the makers whose parts this company uses, starting with its own works.'
            : 'Nothing matches the current filters.'
        }
        emptyAction={newButton}
        toolbar={
          <StatusFilter
            value={status}
            onChange={(next) => setFilters({ status: next })}
            label="Filter manufacturers by status"
          />
        }
      />

      {canManage && (
        <ManufacturerFormDialog open={formOpen} onOpenChange={setFormOpen} manufacturer={editing} />
      )}
      {canManage && (
        <ManufacturerRetireDialog
          manufacturer={retiring}
          onOpenChange={(open) => !open && setRetiring(null)}
        />
      )}
    </>
  )
}

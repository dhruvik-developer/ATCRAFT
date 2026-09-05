/**
 * Bills of materials.
 *
 * One row per BOM *version*, not per item: an item legitimately carries a
 * superseded version 1, an approved version 2 and a draft version 3 at the same
 * time, and the list has to show that rather than hide it behind a "current"
 * projection. Filtering, sorting, paging and search are all query parameters the
 * API answers.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Plus, Star, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { BomFormDialog } from './bom-dialogs'
import { BOM_STATUSES, BOM_TYPES, type BomRow } from './bom-model'

export default function BomListPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  const status = searchParams.get('status') ?? ''
  const bomType = searchParams.get('bom_type') ?? ''

  const list = useListQuery<BomRow>({
    path: '/bom',
    queryKey: queryKeys.bom.list,
    // The API sorts on code, version, status, effective_from, created_at and
    // updated_at. Anything else is a hard validation error rather than an
    // ignored parameter, so `item_code` failed the whole list on arrival.
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(status ? { status } : {}),
      ...(bomType ? { bom_type: bomType } : {}),
    },
  })

  const { setParam, setParams } = list
  const filterCount = (status ? 1 : 0) + (bomType ? 1 : 0)

  const columns = useMemo<ColumnDef<BomRow, unknown>[]>(
    () => [
      {
        // The list row carries `code`, `item_id` and no item code or name, so the
        // bill's own code is the identity this grid can honestly show.
        id: 'code',
        header: 'BOM code',
        // A real link rather than only a clickable row: this screen is driven
        // from the keyboard all day.
        cell: ({ row }) => (
          <Link
            to={`/bom/${row.original.id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
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
        cell: ({ row }) => (
          <span className="tabular">
            v{row.original.version}
            {row.original.revision && (
              <span className="ml-1 text-muted-foreground">rev {row.original.revision}</span>
            )}
          </span>
        ),
      },
      {
        id: 'bom_type',
        header: 'Type',
        enableSorting: false,
        cell: ({ row }) => <Badge variant="secondary">{humanise(row.original.bom_type)}</Badge>,
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
        id: 'output_quantity',
        header: 'Output',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.output_quantity),
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
      {
        id: 'approved_at',
        header: 'Approved',
        enableSorting: false,
        // The row records who approved it as `approved_by_id` only - no name -
        // so the date is what this column can honestly say.
        cell: ({ row }) =>
          row.original.approved_at ? (
            formatDate(row.original.approved_at)
          ) : (
            <span className="text-muted-foreground">Not approved</span>
          ),
      },
    ],
    [],
  )

  const newBomButton = can(P.BOM_CREATE) ? (
    <Button onClick={() => setCreateOpen(true)}>
      <Plus />
      New BOM
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Bills of Material"
        description="Every version of every bill. A bill only drives planning and production once it is approved, and only one approved version per item can be the default."
        actions={newBomButton}
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
        searchPlaceholder="Search BOM code..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/bom/${row.id}`)}
        emptyTitle="No bills of material found"
        emptyDescription="Nothing matches the current filters. Widen the search or clear a filter."
        emptyAction={newBomButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {BOM_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={bomType || SELECT_ALL}
              onValueChange={(value) =>
                setParam('bom_type', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by BOM type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All types</SelectItem>
                {BOM_TYPES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  // One navigation, not one per filter: `setSearchParams` computes every
                  // functional update in a tick from the same captured value, so a loop of
                  // `setParam` calls clears exactly the last one and looks like it did nothing.
                  setParams({
                    status: undefined,
                    bom_type: undefined,
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

      {can(P.BOM_CREATE) && <BomFormDialog open={createOpen} onOpenChange={setCreateOpen} />}
    </>
  )
}

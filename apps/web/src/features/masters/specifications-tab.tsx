import type { ColumnDef } from '@tanstack/react-table'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/auth-context'
import type { SpecificationAttribute } from '@/features/product-descriptions/specification-model'
import { useListQuery } from '@/hooks/use-list-query'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { CategorySpecificationsPanel } from './category-specifications-panel'
import { ActiveBadge, StatusFilter, TabIntro } from './masters-shared'
import { SpecificationFormDialog, SpecificationRetireDialog } from './specification-dialogs'
import { useFilterWriter } from './use-filter-writer'

export function SpecificationsTab() {
  const { can } = useAuth()
  const [params] = useSearchParams()
  const setFilters = useFilterWriter()
  const templates = params.get('view') === 'templates'
  const canManage = can(P.ITEM_ATTRIBUTE_MANAGE)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<SpecificationAttribute | null>(null)
  const [retiring, setRetiring] = useState<SpecificationAttribute | null>(null)
  const status = params.get('status') ?? ''
  const list = useListQuery<SpecificationAttribute>({
    path: '/products/specification-attributes',
    queryKey: queryKeys.specifications.attributes,
    staleTime: staleTimes.reference,
    filters: status ? { is_active: status === 'active' } : {},
    enabled: !templates,
    defaultSortBy: 'sort_order',
  })
  const columns = useMemo<ColumnDef<SpecificationAttribute, unknown>[]>(
    () => [
      { accessorKey: 'code', header: 'Code' },
      { accessorKey: 'name', header: 'Name' },
      { accessorKey: 'value_type', header: 'Type', enableSorting: false },
      {
        accessorKey: 'unit',
        header: 'Unit',
        enableSorting: false,
        cell: ({ row }) => row.original.unit || '-',
      },
      {
        accessorKey: 'is_filterable',
        header: 'Catalog filter',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_filterable && ['OPTION', 'BOOLEAN'].includes(row.original.value_type)
            ? 'Available'
            : '-',
      },
      { accessorKey: 'sort_order', header: 'Position' },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <ActiveBadge isActive={row.original.is_active} />,
      },
      ...(canManage
        ? [
            {
              id: 'actions',
              header: '',
              enableSorting: false,
              cell: ({ row }) => (
                <div className="flex justify-end gap-1">
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
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Retire ${row.original.code}`}
                    onClick={() => setRetiring(row.original)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              ),
            } satisfies ColumnDef<SpecificationAttribute, unknown>,
          ]
        : []),
    ],
    [canManage],
  )
  const createButton = canManage ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      New specification
    </Button>
  ) : null
  return (
    <>
      <TabIntro actions={!templates ? createButton : undefined}>
        Govern technical item descriptions and inherited category completeness warnings. These
        values describe products; they do not change stock, pricing, BOM or planning.
      </TabIntro>
      <nav aria-label="Specification views" className="mb-4 flex flex-wrap gap-2">
        <Button
          variant={templates ? 'outline' : 'secondary'}
          onClick={() => setFilters({ view: undefined })}
        >
          Specification attributes
        </Button>
        <Button
          variant={templates ? 'secondary' : 'outline'}
          onClick={() => setFilters({ view: 'templates' })}
        >
          Category templates
        </Button>
      </nav>
      {templates ? (
        <CategorySpecificationsPanel />
      ) : (
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
          searchPlaceholder="Search specifications"
          emptyTitle="No specifications"
          emptyDescription="Add technical attributes such as material, backset and fire rating."
          emptyAction={createButton}
          toolbar={
            <StatusFilter
              value={status}
              onChange={(next) => setFilters({ status: next })}
              label="Filter specifications by status"
            />
          }
        />
      )}
      {canManage && (
        <>
          <SpecificationFormDialog open={formOpen} onOpenChange={setFormOpen} attribute={editing} />
          <SpecificationRetireDialog
            attribute={retiring}
            onOpenChange={(open) => {
              if (!open) setRetiring(null)
            }}
          />
        </>
      )}
    </>
  )
}

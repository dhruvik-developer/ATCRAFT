/**
 * Inspection specifications - the check sheets quality measures against.
 *
 * This is a master list, but it is not a flat one: every row is a *version*.
 * `POST /quality/templates/{id}/versions` writes a new row carrying the same
 * `code` and `version + 1` and deactivates the one it forked from, because an
 * inspection names the exact version it was judged against and that record has
 * to stay reproducible. So the list shows what is in force by default, marks
 * the rest as superseded, and hands the full revision history to the detail
 * dialog.
 *
 * A specification that has been *retired* (`DELETE`) is a soft delete and drops
 * out of every list; only a version superseded by a fork, or held back with the
 * active flag, is still listed as inactive. The two states are different and
 * the copy on this screen keeps them apart.
 *
 * Every filter is a query parameter the API answers. Nothing is narrowed in the
 * browser (master rules 31/36/78).
 *
 * Reading is `P.QC_VIEW`, which is what `GET /quality/templates` declares.
 * Creating, amending, forking and retiring are all `P.QC_TEMPLATE_MANAGE` - a
 * separate right from `qc.inspect` and `qc.decide` because the person who
 * measures a part is not usually the person authorised to write the
 * specification it is measured against (section 38). The gating here is UX
 * only; the API authorises each request independently.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { ExternalLink, GitBranch, MoreHorizontal, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader } from '@/components/shared'
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
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { useCategoryOptions } from '@/features/items/item-model'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  QcTemplateDetailDialog,
  QcTemplateFormDialog,
  QcTemplateRetireDialog,
  QcTemplateVersionDialog,
} from './qc-template-dialogs'
import {
  QC_SAMPLING_PLANS,
  SAMPLING_PLAN_LABEL,
  qcTemplateKeys,
  samplingLabel,
  scopeOf,
  type QcTemplateRow,
} from './qc-template-model'
import { QC_STAGES } from './quality-types'

/**
 * Every filter key this screen owns, named once.
 *
 * Clear and the count of what is applied read the same list, so a filter added
 * to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = ['qc_stage', 'sampling_plan', 'item_id', 'item_category_id', 'active'] as const

/** What the retire dialog needs to name the row it is about to take away. */
type RetireTarget = Pick<QcTemplateRow, 'id' | 'code' | 'version' | 'name'>

/**
 * Where a specification applies.
 *
 * The contract sends `item_id` and `item_category_id` and no accompanying code
 * or name, so an item links out to the item master rather than showing a label
 * the row does not carry - resolving one per line would be the N+1 the codebase
 * forbids. Categories are different: they are a bounded reference set already
 * fetched once for the filter, so the name is available without another
 * request.
 */
function ScopeCell({
  template,
  categoryNames,
}: {
  template: QcTemplateRow
  categoryNames: Map<string, string>
}) {
  const scope = scopeOf(template)

  if (scope === 'ITEM' && template.item_id) {
    return (
      <Link
        to={`/items/${template.item_id}`}
        onClick={(event) => event.stopPropagation()}
        className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
        title="Open this item"
      >
        {template.item_id.slice(0, 8)}
        <ExternalLink className="h-3 w-3 shrink-0 opacity-60" aria-hidden />
      </Link>
    )
  }

  if (scope === 'CATEGORY' && template.item_category_id) {
    const name = categoryNames.get(template.item_category_id)
    return name ? (
      <span className="block max-w-[14rem] truncate">{name}</span>
    ) : (
      <span className="font-mono text-xs">{template.item_category_id.slice(0, 8)}</span>
    )
  }

  return <span className="text-muted-foreground">Stage default</span>
}

export default function QcTemplatesPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canManage = can(P.QC_TEMPLATE_MANAGE)

  const [detailId, setDetailId] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [formTemplateId, setFormTemplateId] = useState<string | null>(null)
  const [versionOpen, setVersionOpen] = useState(false)
  const [versionTemplateId, setVersionTemplateId] = useState<string | null>(null)
  const [retireTarget, setRetireTarget] = useState<RetireTarget | null>(null)

  const stage = searchParams.get('qc_stage') ?? ''
  const plan = searchParams.get('sampling_plan') ?? ''
  const itemId = searchParams.get('item_id') ?? ''
  const categoryId = searchParams.get('item_category_id') ?? ''
  // Default to what is in force. A superseded version is history, and history
  // belongs in the detail dialog rather than three quarters of the working list.
  const activeState = searchParams.get('active') ?? 'true'

  const categories = useCategoryOptions(can(P.CATEGORY_VIEW))
  const categoryNames = useMemo(
    () => new Map((categories.data ?? []).map((category) => [category.id, category.name])),
    [categories.data],
  )

  const list = useListQuery<QcTemplateRow>({
    path: '/quality/templates',
    queryKey: qcTemplateKeys.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(stage ? { qc_stage: stage } : {}),
      ...(plan ? { sampling_plan: plan } : {}),
      ...(itemId ? { item_id: itemId } : {}),
      ...(categoryId ? { item_category_id: categoryId } : {}),
      ...(activeState === 'any' ? {} : { is_active: activeState === 'true' }),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: each call builds the next URL
   * from the `searchParams` captured when the callback was created, so five
   * `setParam` calls in a row would all start from the same stale base and only
   * the last would survive.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        // The page belonged to the narrowed list, not to this one.
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  const openCreate = () => {
    setFormTemplateId(null)
    setFormOpen(true)
  }

  const openAmend = (templateId: string) => {
    setDetailId(null)
    setFormTemplateId(templateId)
    setFormOpen(true)
  }

  const openFork = (templateId: string) => {
    setDetailId(null)
    setVersionTemplateId(templateId)
    setVersionOpen(true)
  }

  const columns = useMemo<ColumnDef<QcTemplateRow, unknown>[]>(() => {
    const defs: ColumnDef<QcTemplateRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-mono font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'version',
        header: 'Version',
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">v{row.original.version}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <span className="block max-w-[20rem] truncate">{row.original.name}</span>
        ),
      },
      {
        accessorKey: 'qc_stage',
        header: 'Stage',
        cell: ({ row }) => <Badge variant="secondary">{humanise(row.original.qc_stage)}</Badge>,
      },
      {
        id: 'scope',
        header: 'Applies to',
        // Not sortable: the API sorts on code, name, qc_stage, version,
        // created_at and updated_at only, and anything else is a 422.
        enableSorting: false,
        cell: ({ row }) => <ScopeCell template={row.original} categoryNames={categoryNames} />,
      },
      {
        id: 'sampling',
        header: 'Sampling',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">{samplingLabel(row.original)}</span>
        ),
      },
      {
        id: 'is_active',
        header: 'State',
        enableSorting: false,
        // "In force" rather than "Active", because the flag decides whether this
        // version is the one an incoming lot resolves to - not whether the row
        // still exists.
        cell: ({ row }) =>
          row.original.is_active ? (
            <Badge variant="success">In force</Badge>
          ) : (
            <Badge variant="muted">Superseded</Badge>
          ),
      },
      {
        accessorKey: 'updated_at',
        header: 'Updated',
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {formatDate(row.original.updated_at)}
          </span>
        ),
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
          <div
            className="flex items-center justify-end gap-1"
            onClick={(event) => event.stopPropagation()}
          >
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Amend ${row.original.code} v${row.original.version}`}
              onClick={() => openAmend(row.original.id)}
            >
              <Pencil />
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`More actions for ${row.original.code} v${row.original.version}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => openFork(row.original.id)}>
                  <GitBranch aria-hidden />
                  Raise version {row.original.version + 1}
                </DropdownMenuItem>
                <DropdownMenuItem destructive onSelect={() => setRetireTarget(row.original)}>
                  <Trash2 aria-hidden />
                  Retire this version
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      })
    }

    return defs
  }, [canManage, categoryNames])

  const newTemplateButton = canManage ? (
    <Button onClick={openCreate}>
      <Plus />
      New specification
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Inspection specifications"
        description="What quality measures, and what counts as a pass. Each row is one version: an inspection is judged against the version in force when it was raised, so a specification is amended while it is unused and re-versioned once it is not."
        breadcrumbs={[{ label: 'Quality', to: '/quality' }, { label: 'Specifications' }]}
        actions={newTemplateButton}
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
        onRowClick={(row) => setDetailId(row.id)}
        emptyTitle={
          filterCount > 0 ? 'No specification matches' : 'No specification is in force yet'
        }
        emptyDescription={
          filterCount > 0
            ? 'Nothing matches these filters. Superseded versions are hidden until you ask for them, and a retired specification is gone from every list.'
            : 'Until one exists, an inspection has no check sheet and can only be dispositioned on judgement. Write the specification for an item, a category, or a whole stage.'
        }
        emptyAction={newTemplateButton}
        toolbar={
          <>
            <Select
              value={stage || SELECT_ALL}
              onValueChange={(next) => setParam('qc_stage', next === SELECT_ALL ? undefined : next)}
            >
              <SelectTrigger
                className="w-full sm:w-[11rem]"
                aria-label="Filter by inspection stage"
              >
                <SelectValue placeholder="Any stage" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any stage</SelectItem>
                {QC_STAGES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={plan || SELECT_ALL}
              onValueChange={(next) =>
                setParam('sampling_plan', next === SELECT_ALL ? undefined : next)
              }
            >
              <SelectTrigger className="w-full sm:w-[11rem]" aria-label="Filter by sampling plan">
                <SelectValue placeholder="Any sampling" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any sampling</SelectItem>
                {QC_SAMPLING_PLANS.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {SAMPLING_PLAN_LABEL[entry]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <ItemPicker
              id="qc-templates-item"
              label="Item"
              hideLabel
              value={itemId || null}
              onChange={(next) => setParam('item_id', next ?? undefined)}
              placeholder="Any item"
            />

            {can(P.CATEGORY_VIEW) && (
              <Select
                value={categoryId || SELECT_ALL}
                onValueChange={(next) =>
                  setParam('item_category_id', next === SELECT_ALL ? undefined : next)
                }
              >
                <SelectTrigger className="w-full sm:w-[13rem]" aria-label="Filter by item category">
                  <SelectValue placeholder="Any category" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_ALL}>Any category</SelectItem>
                  {(categories.data ?? []).map((category) => (
                    <SelectItem key={category.id} value={category.id}>
                      {category.code} - {category.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            <Select
              value={activeState}
              onValueChange={(next) => setParam('active', next === 'true' ? undefined : next)}
            >
              <SelectTrigger className="w-full sm:w-[12rem]" aria-label="Filter by state">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="true">In force</SelectItem>
                <SelectItem value="false">Superseded only</SelectItem>
                <SelectItem value="any">Every version</SelectItem>
              </SelectContent>
            </Select>

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />

      <QcTemplateDetailDialog
        templateId={detailId}
        onOpenChange={(open) => !open && setDetailId(null)}
        onOpenVersion={(id) => setDetailId(id)}
        onAmend={openAmend}
        onFork={openFork}
        onRetire={(template) => {
          setDetailId(null)
          setRetireTarget(template)
        }}
      />

      {canManage && (
        <>
          <QcTemplateFormDialog
            open={formOpen}
            onOpenChange={setFormOpen}
            templateId={formTemplateId}
            onForkInstead={openFork}
          />

          <QcTemplateVersionDialog
            open={versionOpen}
            onOpenChange={setVersionOpen}
            templateId={versionTemplateId}
          />

          <QcTemplateRetireDialog
            template={retireTarget}
            onOpenChange={(open) => !open && setRetireTarget(null)}
          />
        </>
      )}
    </>
  )
}

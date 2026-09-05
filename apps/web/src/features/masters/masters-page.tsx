/**
 * Item master maintenance: categories, units of measure, the conversions
 * between units, and the dated price list.
 *
 * Four small masters share one screen because they are maintained together and
 * separately from anything transactional - and because three of them are
 * meaningless alone. A category is only useful once items hang off it, a unit
 * only once something is measured in it, a conversion only once two units
 * exist.
 *
 * The tab lives in the URL so a colleague can be sent straight to the unit
 * conversions rather than "open Masters, then click the third tab". Switching
 * tabs clears the other query parameters: each tab drives its own paging,
 * search and filters through the same URL keys, and carrying page 4 of the
 * category tree into the price list would show the wrong slice of a different
 * dataset.
 *
 * Reads and writes are gated separately, because the API gates them separately:
 * `category.view` / `category.manage`, `uom.view` / `uom.manage`,
 * `price.view` / `price.manage`. A tab whose *read* right is missing is not
 * rendered at all; a tab whose write right is missing is fully readable with no
 * create, edit or delete affordance. All of it is presentation - the API
 * authorises every request regardless (master rules 38).
 *
 * None of these four list endpoints publishes `sort_by`, so every column here
 * sets `enableSorting: false`. Paging, searching and filtering are all
 * server-side; nothing on this screen fetches a table to filter it in the
 * browser.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeftRight,
  Calculator,
  CornerDownRight,
  FolderTree,
  MoreHorizontal,
  Pencil,
  Plus,
  Ruler,
  ShieldAlert,
  Tags,
  Trash2,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { EmptyState, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
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
import { Alert, AlertDescription, AlertTitle, Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { CategoryFormDialog, CategoryRetireDialog } from './category-dialogs'
import {
  conversionStatement,
  formatFactor,
  indexById,
  inverseFactor,
  levelLabel,
  mastersKeys,
  PRICE_TYPES,
  useUomReference,
  useVariantOptions,
  type CategoryRow,
  type ConversionRow,
  type PriceRow,
  type UomRow,
} from './masters-model'
import { PriceDeleteDialog, PriceFormDialog } from './price-dialogs'
import {
  ConversionDeleteDialog,
  ConversionFormDialog,
  UomConversionCheckDialog,
  UomFormDialog,
} from './uom-dialogs'

type TabKey = 'categories' | 'units' | 'conversions' | 'prices'

/**
 * Change several filters in ONE URL write.
 *
 * `setSearchParams` is not a React state setter: two calls in the same handler
 * both start from the URL captured at render, and only the last navigation
 * survives. That cost the finance screen its Clear button, and this screen has
 * several controls that must move two keys at once - drilling into a branch
 * sets the parent and drops the "top level only" scope together.
 */
function useFilterWriter() {
  const [, setSearchParams] = useSearchParams()

  return useCallback(
    (next: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (value === undefined || value === '') updated.delete(key)
            else updated.set(key, value)
          }
          // The page number belonged to the previous narrowing, not this one.
          updated.delete('page')
          return updated
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )
}

/** Active / inactive, the same three-way control on three of the four tabs. */
function StatusFilter({
  value,
  onChange,
  label,
}: {
  value: string
  onChange: (next: string | undefined) => void
  label: string
}) {
  return (
    <Select
      value={value || SELECT_ALL}
      onValueChange={(next) => onChange(next === SELECT_ALL ? undefined : next)}
    >
      <SelectTrigger className="w-full sm:w-[150px]" aria-label={label}>
        <SelectValue placeholder="Any status" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={SELECT_ALL}>Any status</SelectItem>
        <SelectItem value="active">Active</SelectItem>
        <SelectItem value="inactive">Inactive</SelectItem>
      </SelectContent>
    </Select>
  )
}

function ActiveBadge({ isActive }: { isActive: boolean }) {
  return isActive ? (
    <Badge variant="secondary">Active</Badge>
  ) : (
    <Badge variant="muted">Inactive</Badge>
  )
}

/** The sentence that introduces a tab, above its table. */
function TabIntro({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 mt-4 flex flex-wrap items-start justify-between gap-3">
      <p className="max-w-3xl text-sm text-muted-foreground">{children}</p>
      {actions && (
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
  )
}

// ------------------------------------------------------------ categories ----

function CategoriesTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const setFilters = useFilterWriter()

  const canManage = can(P.CATEGORY_MANAGE)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<CategoryRow | null>(null)
  const [createParentId, setCreateParentId] = useState<string | null>(null)
  const [retiring, setRetiring] = useState<CategoryRow | null>(null)

  const scope = searchParams.get('scope') === 'roots' ? 'roots' : 'tree'
  const parentId = searchParams.get('parent_id') ?? ''
  const status = searchParams.get('status') ?? ''

  const list = useListQuery<CategoryRow>({
    path: '/products/categories',
    queryKey: mastersKeys.categories,
    staleTime: staleTimes.reference,
    filters: {
      // `only_roots` wins over `parent_id` server-side, so the two are never
      // sent together - the toolbar clears one when it sets the other.
      ...(scope === 'roots' ? { only_roots: true } : {}),
      ...(scope === 'tree' && parentId ? { parent_id: parentId } : {}),
      ...(status ? { is_active: status === 'active' } : {}),
    },
  })

  // Named rather than shown as a UUID: the drill-down survives a bookmark, and
  // "children of 0192f3c1-..." tells the user nothing.
  const parent = useQuery({
    queryKey: mastersKeys.category(parentId),
    queryFn: () => api.get<CategoryRow>(`/products/categories/${parentId}`),
    enabled: Boolean(parentId),
    staleTime: staleTimes.reference,
  })

  const openCreate = useCallback((underParentId: string | null) => {
    setEditing(null)
    setCreateParentId(underParentId)
    setFormOpen(true)
  }, [])

  const openEdit = useCallback((category: CategoryRow) => {
    setEditing(category)
    setCreateParentId(null)
    setFormOpen(true)
  }, [])

  const columns = useMemo<ColumnDef<CategoryRow, unknown>[]>(() => {
    const defs: ColumnDef<CategoryRow, unknown>[] = [
      {
        id: 'code',
        header: 'Code',
        enableSorting: false,
        // The API orders on the materialised path, which is depth-first tree
        // order, so indenting by `depth` reproduces the tree exactly. The
        // indentation is decorative - the Level column carries the same fact
        // for anyone not reading the layout.
        cell: ({ row }) => (
          <div
            className="flex items-center"
            style={{ paddingLeft: `${row.original.depth * 1.1}rem` }}
          >
            {row.original.depth > 0 && (
              <CornerDownRight
                className="mr-1.5 h-3.5 w-3.5 shrink-0 text-muted-foreground/50"
                aria-hidden
              />
            )}
            <span className="font-medium">{row.original.code}</span>
          </div>
        ),
      },
      {
        accessorKey: 'name',
        header: 'Name',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate">{row.original.name}</span>
        ),
      },
      {
        id: 'description',
        header: 'Description',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.description ? (
            <span className="block max-w-[22rem] truncate text-xs text-muted-foreground">
              {row.original.description}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'depth',
        header: 'Level',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">{levelLabel(row.original.depth)}</span>
        ),
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <ActiveBadge isActive={row.original.is_active} />,
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <div className="flex items-center justify-end gap-1">
            {canManage && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Edit ${row.original.code}`}
                onClick={() => openEdit(row.original)}
              >
                <Pencil />
              </Button>
            )}
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
                <DropdownMenuItem
                  onSelect={() => setFilters({ parent_id: row.original.id, scope: undefined })}
                >
                  Show sub-categories
                </DropdownMenuItem>
                {canManage && (
                  <>
                    <DropdownMenuItem onSelect={() => openCreate(row.original.id)}>
                      Add a sub-category
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem destructive onSelect={() => setRetiring(row.original)}>
                      Retire category
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      },
    ]

    return defs
  }, [canManage, setFilters, openCreate, openEdit])

  const newButton = canManage ? (
    <Button onClick={() => openCreate(parentId || null)}>
      <Plus />
      New category
    </Button>
  ) : null

  return (
    <>
      <TabIntro actions={newButton}>
        Categories nest, and rows arrive in tree order with each level indented under its parent.
        Searching or filtering narrows to the matching nodes only, so an indented row may then sit
        below something that is not its parent.
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
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search code or name..."
        getRowId={(row) => row.id}
        emptyTitle="No categories here"
        emptyDescription={
          parentId
            ? 'This branch has no sub-categories yet. Add one, or clear the drill-down to see the whole tree.'
            : canManage
              ? 'Nothing matches the current filters. Clear a filter, or create the first category.'
              : 'Nothing matches the current filters. Clear a filter to see the whole tree.'
        }
        emptyAction={newButton}
        toolbar={
          <>
            <Select
              value={scope}
              onValueChange={(next) =>
                // Scope and drill-down are mutually exclusive: the API ignores
                // `parent_id` whenever `only_roots` is set, so choosing one
                // clears the other in the same write.
                setFilters({
                  scope: next === 'roots' ? 'roots' : undefined,
                  parent_id: undefined,
                })
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Which part of the tree">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="tree">Whole tree</SelectItem>
                <SelectItem value="roots">Top level only</SelectItem>
              </SelectContent>
            </Select>

            <StatusFilter
              value={status}
              onChange={(next) => setFilters({ status: next })}
              label="Filter categories by status"
            />

            {parentId && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setFilters({ parent_id: undefined })}
              >
                <X />
                {parent.data ? `Inside ${parent.data.code}` : 'Inside one branch'}
              </Button>
            )}
          </>
        }
      />

      {canManage && (
        <CategoryFormDialog
          open={formOpen}
          onOpenChange={setFormOpen}
          category={editing}
          defaultParentId={createParentId}
        />
      )}

      {canManage && (
        <CategoryRetireDialog
          category={retiring}
          onOpenChange={(open) => !open && setRetiring(null)}
        />
      )}
    </>
  )
}

// ----------------------------------------------------------------- units ----

function UnitsTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const setFilters = useFilterWriter()

  const canManage = can(P.UOM_MANAGE)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<UomRow | null>(null)

  const status = searchParams.get('status') ?? ''

  const list = useListQuery<UomRow>({
    path: '/products/uoms',
    queryKey: mastersKeys.uoms,
    staleTime: staleTimes.reference,
    filters: { ...(status ? { is_active: status === 'active' } : {}) },
  })

  const columns = useMemo<ColumnDef<UomRow, unknown>[]>(() => {
    const defs: ColumnDef<UomRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      { accessorKey: 'name', header: 'Name', enableSorting: false },
      {
        id: 'uom_category',
        header: 'Measurement family',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex items-center gap-2">
            <span>{humanise(row.original.uom_category)}</span>
            {/* Every conversion in a family resolves through its base unit, so
                which unit that is belongs on the row rather than buried in the
                edit form. */}
            {row.original.is_base && <Badge variant="default">Base</Badge>}
          </div>
        ),
      },
      {
        accessorKey: 'decimal_places',
        header: 'Decimals',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.decimal_places}</span>,
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <ActiveBadge isActive={row.original.is_active} />,
      },
    ]

    // There is no DELETE on a unit, and correctly so: removing one would strand
    // every quantity ever recorded in it. Retirement is the `is_active` flag on
    // the edit form.
    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <div className="flex justify-end">
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
      New unit
    </Button>
  ) : null

  return (
    <>
      <TabIntro actions={newButton}>
        Each unit belongs to one measurement family, and each family has exactly one base unit that
        every conversion in it resolves through. The decimal places decide how finely a quantity may
        be stated on a document.
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
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search code or name..."
        getRowId={(row) => row.id}
        emptyTitle="No units of measure"
        emptyDescription={
          canManage
            ? 'Nothing matches the current filters. Every item needs a base unit, so start with one per measurement family you use.'
            : 'Nothing matches the current filters. Clear a filter to see every unit.'
        }
        emptyAction={newButton}
        toolbar={
          <StatusFilter
            value={status}
            onChange={(next) => setFilters({ status: next })}
            label="Filter units by status"
          />
        }
      />

      {canManage && <UomFormDialog open={formOpen} onOpenChange={setFormOpen} uom={editing} />}
    </>
  )
}

// ----------------------------------------------------------- conversions ----

function ConversionsTab() {
  const { can } = useAuth()

  const canManage = can(P.UOM_MANAGE)

  const [formOpen, setFormOpen] = useState(false)
  const [checkOpen, setCheckOpen] = useState(false)
  const [deleting, setDeleting] = useState<ConversionRow | null>(null)

  const list = useListQuery<ConversionRow>({
    path: '/products/uom-conversions',
    queryKey: mastersKeys.conversions,
    staleTime: staleTimes.reference,
  })

  // The rows carry bare unit ids and nothing else, so the unit master is loaded
  // once and joined here. It is reference data of a few dozen rows, gated on
  // the same `uom.view` right as the list itself - resolving a code per row
  // would be exactly the N+1 the codebase forbids.
  const units = useUomReference(true)
  const unitById = useMemo(() => indexById(units.data?.rows), [units.data])

  const codeOf = (uomId: string): string => unitById.get(uomId)?.code ?? 'Unknown unit'

  const statementFor = (row: ConversionRow): string =>
    conversionStatement(codeOf(row.from_uom_id), codeOf(row.to_uom_id), row.factor)

  const unitCell = (uomId: string) => {
    const unit = unitById.get(uomId)
    if (!unit) {
      // A conversion pointing at a unit outside the loaded page, or at one that
      // has been removed. Saying so beats printing a UUID or an empty cell.
      return (
        <span className="text-muted-foreground" title={uomId}>
          Unknown unit
        </span>
      )
    }
    return (
      <div>
        <span className="font-medium">{unit.code}</span>
        <span className="block text-xs text-muted-foreground">{unit.name}</span>
      </div>
    )
  }

  const columns = useMemo<ColumnDef<ConversionRow, unknown>[]>(() => {
    const defs: ColumnDef<ConversionRow, unknown>[] = [
      {
        id: 'from_uom_id',
        header: 'From',
        enableSorting: false,
        cell: ({ row }) => unitCell(row.original.from_uom_id),
      },
      {
        id: 'to_uom_id',
        header: 'To',
        enableSorting: false,
        cell: ({ row }) => unitCell(row.original.to_uom_id),
      },
      {
        accessorKey: 'factor',
        header: 'Factor',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.factor}</span>,
      },
      {
        id: 'statement',
        header: 'Reads as',
        enableSorting: false,
        // The factor alone is ambiguous and a factor read backwards corrupts
        // every quantity derived from it. Both directions are spelled out
        // because the resolver uses both: a pair stored once is inverted on
        // demand rather than entered twice.
        cell: ({ row }) => {
          const inverse = inverseFactor(row.original.factor)
          return (
            <div>
              <span className="block font-medium">{statementFor(row.original)}</span>
              {inverse && (
                <span className="block text-xs text-muted-foreground">
                  1 {codeOf(row.original.to_uom_id)} = {formatFactor(inverse)}{' '}
                  {codeOf(row.original.from_uom_id)}
                </span>
              )}
            </div>
          )
        },
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
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Remove the conversion ${statementFor(row.original)}`}
              onClick={() => setDeleting(row.original)}
            >
              <Trash2 />
            </Button>
          </div>
        ),
      })
    }

    return defs
    // `unitById` is what turns an id into a code, so the cells must be rebuilt
    // when the unit master resolves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage, unitById])

  const newButton = canManage ? (
    <Button onClick={() => setFormOpen(true)}>
      <Plus />
      New conversion
    </Button>
  ) : null

  return (
    <>
      <TabIntro
        actions={
          <>
            <Button variant="outline" onClick={() => setCheckOpen(true)}>
              <Calculator />
              Check a conversion
            </Button>
            {newButton}
          </>
        }
      >
        A conversion is one factor between two units of the same measurement family, and it is
        directional: the factor says how many of the second unit make one of the first. It is stored
        once and resolves both ways, so there is no reverse row to enter.
      </TabIntro>

      {units.data?.truncated && (
        <Alert variant="warning" className="mb-4">
          <AlertTitle>Some units could not be named</AlertTitle>
          <AlertDescription>
            This company has more units than one page can carry, so a conversion pointing at one of
            the later ones shows as &ldquo;Unknown unit&rdquo;. The factor and its direction are
            still correct.
          </AlertDescription>
        </Alert>
      )}

      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading || units.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onRefresh={() => void list.refetch()}
        getRowId={(row) => row.id}
        emptyTitle="No unit conversions"
        emptyDescription={
          canManage
            ? 'Nothing is defined yet. Add one before a document quotes an item in anything other than its own base unit - an undefined pair is refused rather than guessed at.'
            : 'Nothing is defined yet. A document quoting a unit with no conversion is refused rather than guessed at.'
        }
        emptyAction={newButton}
      />

      {canManage && (
        <ConversionFormDialog
          open={formOpen}
          onOpenChange={setFormOpen}
          units={units.data?.rows ?? []}
        />
      )}

      {canManage && (
        <ConversionDeleteDialog
          conversion={deleting}
          statement={deleting ? statementFor(deleting) : ''}
          onOpenChange={(open) => !open && setDeleting(null)}
        />
      )}

      <UomConversionCheckDialog
        open={checkOpen}
        onOpenChange={setCheckOpen}
        units={units.data?.rows ?? []}
      />
    </>
  )
}

// ---------------------------------------------------------------- prices ----

function PricesTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const setFilters = useFilterWriter()

  const canManage = can(P.PRICE_MANAGE)
  // The price list is addressed under an item, and choosing one means searching
  // the item master - which is `item.view`, a different right from the
  // `price.view` that reads this list. Without it the tab is readable only from
  // a link that already names an item.
  const canPickItem = can(P.ITEM_VIEW)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<PriceRow | null>(null)
  const [deleting, setDeleting] = useState<PriceRow | null>(null)

  const itemId = searchParams.get('item_id') ?? ''
  const priceType = searchParams.get('price_type') ?? ''
  const variantId = searchParams.get('variant_id') ?? ''
  const status = searchParams.get('status') ?? ''

  const variants = useVariantOptions(itemId, canPickItem)
  const variantById = useMemo(() => indexById(variants.data), [variants.data])

  const list = useListQuery<PriceRow>({
    path: `/products/items/${itemId}/prices`,
    // The item id is part of the key, not just the path: without it two items
    // would share one cache entry and the second one opened would show the
    // first one's prices.
    queryKey: (params) => mastersKeys.prices(itemId, params),
    enabled: Boolean(itemId),
    staleTime: staleTimes.master,
    filters: {
      ...(priceType ? { price_type: priceType } : {}),
      ...(variantId ? { variant_id: variantId } : {}),
      ...(status ? { is_active: status === 'active' } : {}),
    },
  })

  const variantLabel = (row: PriceRow) => {
    if (row.variant_id === null) {
      return <span className="text-xs text-muted-foreground">Every variant</span>
    }
    const variant = variantById.get(row.variant_id)
    return variant ? (
      <span className="text-xs">
        {variant.code} - {variant.name}
      </span>
    ) : (
      // Reached when the account holds `price.view` but not `item.view`, so the
      // variant list was never fetched. The row is still a variant price and
      // must not read as an item-wide one.
      <span className="text-xs text-muted-foreground">One variant</span>
    )
  }

  const columns = useMemo<ColumnDef<PriceRow, unknown>[]>(() => {
    const defs: ColumnDef<PriceRow, unknown>[] = [
      {
        id: 'price_type',
        header: 'Channel',
        enableSorting: false,
        cell: ({ row }) => <Badge variant="secondary">{humanise(row.original.price_type)}</Badge>,
      },
      {
        id: 'variant_id',
        header: 'Applies to',
        enableSorting: false,
        cell: ({ row }) => variantLabel(row.original),
      },
      {
        id: 'min_quantity',
        header: 'From quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.min_quantity)}</span>
        ),
      },
      {
        id: 'currency',
        header: 'Currency',
        enableSorting: false,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.currency}</span>,
      },
      {
        accessorKey: 'price',
        header: 'Price',
        enableSorting: false,
        meta: { align: 'right' },
        // Formatted in the row's own currency. Nothing on this screen adds two
        // denominations together.
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.price, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        accessorKey: 'valid_from',
        header: 'Valid from',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.valid_from),
      },
      {
        id: 'valid_to',
        header: 'Valid to',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.valid_to ? (
            formatDate(row.original.valid_to)
          ) : (
            <span className="text-xs text-muted-foreground">Until superseded</span>
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
              aria-label={`Edit the ${humanise(row.original.price_type)} price valid from ${row.original.valid_from}`}
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
              aria-label={`Remove the ${humanise(row.original.price_type)} price valid from ${row.original.valid_from}`}
              onClick={() => setDeleting(row.original)}
            >
              <Trash2 />
            </Button>
          </div>
        ),
      })
    }

    return defs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage, variantById])

  const newButton =
    canManage && itemId ? (
      <Button
        onClick={() => {
          setEditing(null)
          setFormOpen(true)
        }}
      >
        <Plus />
        New price
      </Button>
    ) : null

  return (
    <>
      <TabIntro actions={newButton}>
        A price is a channel, an optional variant, a quantity slab and a validity window. Two active
        rows may not overlap on all four at once, which is what keeps &ldquo;the price today&rdquo;
        a single answer.
      </TabIntro>

      {canPickItem ? (
        <div className="mb-4 max-w-md">
          <ItemPicker
            id="prices-item"
            label="Item"
            value={itemId || null}
            onChange={(next) =>
              // The variant filter belongs to the previous item, so it is
              // dropped in the same write that changes the item.
              setFilters({ item_id: next ?? undefined, variant_id: undefined })
            }
            placeholder="Choose an item to price"
            emptyMessage="No item matches that search."
          />
        </div>
      ) : (
        <Alert variant="warning" className="mb-4">
          <AlertTitle>An item cannot be chosen here</AlertTitle>
          <AlertDescription>
            Searching the item master needs the item.view right, which this account does not hold.
            Prices can still be opened from a link that already names an item.
          </AlertDescription>
        </Alert>
      )}

      {itemId ? (
        <DataTable
          columns={columns}
          data={list.data}
          pagination={list.pagination}
          isLoading={list.isLoading}
          isFetching={list.isFetching}
          error={list.error}
          onPageChange={list.setPage}
          onLimitChange={list.setLimit}
          onRefresh={() => void list.refetch()}
          getRowId={(row) => row.id}
          emptyTitle="No prices for this item"
          emptyDescription={
            canManage
              ? 'Nothing matches the current filters. Add the first price for a channel, and leave the end date empty for one that runs until it is superseded.'
              : 'Nothing matches the current filters. Clear a filter to see every price on this item.'
          }
          emptyAction={newButton}
          toolbar={
            <>
              <Select
                value={priceType || SELECT_ALL}
                onValueChange={(next) =>
                  setFilters({ price_type: next === SELECT_ALL ? undefined : next })
                }
              >
                <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by channel">
                  <SelectValue placeholder="Any channel" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_ALL}>Any channel</SelectItem>
                  {PRICE_TYPES.map((entry) => (
                    <SelectItem key={entry} value={entry}>
                      {humanise(entry)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Offered only when the variants could actually be named; a
                  filter listing bare UUIDs would be worse than none. */}
              {canPickItem && (variants.data?.length ?? 0) > 0 && (
                <Select
                  value={variantId || SELECT_ALL}
                  onValueChange={(next) =>
                    setFilters({ variant_id: next === SELECT_ALL ? undefined : next })
                  }
                >
                  <SelectTrigger className="w-full sm:w-[200px]" aria-label="Filter by variant">
                    <SelectValue placeholder="Any variant" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>Any variant</SelectItem>
                    {(variants.data ?? []).map((variant) => (
                      <SelectItem key={variant.id} value={variant.id}>
                        {variant.code} - {variant.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}

              <StatusFilter
                value={status}
                onChange={(next) => setFilters({ status: next })}
                label="Filter prices by status"
              />
            </>
          }
        />
      ) : (
        <EmptyState
          icon={Tags}
          title="Choose an item"
          description={
            canPickItem
              ? 'A price list belongs to one item. Pick one above to read and maintain its channels, slabs and validity windows.'
              : 'A price list belongs to one item, and this account cannot search the item master. Open this tab from an item to see its prices.'
          }
        />
      )}

      {canManage && itemId && (
        <PriceFormDialog
          open={formOpen}
          onOpenChange={setFormOpen}
          itemId={itemId}
          price={editing}
          variants={variants.data ?? []}
        />
      )}

      {canManage && itemId && (
        <PriceDeleteDialog
          price={deleting}
          itemId={itemId}
          onOpenChange={(open) => !open && setDeleting(null)}
        />
      )}
    </>
  )
}

// ------------------------------------------------------------------ page ----

export default function MastersPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  // One tab per read right the account actually holds. Units and conversions
  // are both `uom.view`, so they appear and disappear together.
  const tabs: { key: TabKey; label: string; icon: LucideIcon }[] = [
    ...(can(P.CATEGORY_VIEW)
      ? [{ key: 'categories' as const, label: 'Categories', icon: FolderTree }]
      : []),
    ...(can(P.UOM_VIEW)
      ? [
          { key: 'units' as const, label: 'Units', icon: Ruler },
          { key: 'conversions' as const, label: 'Conversions', icon: ArrowLeftRight },
        ]
      : []),
    ...(can(P.PRICE_VIEW) ? [{ key: 'prices' as const, label: 'Prices', icon: Tags }] : []),
  ]

  const requested = searchParams.get('tab') as TabKey | null
  // A bookmarked link to a tab this account cannot open falls back to the first
  // one it can, rather than rendering an empty shell.
  const active = tabs.some((entry) => entry.key === requested)
    ? (requested as TabKey)
    : tabs[0]?.key

  const selectTab = (next: string) => {
    // One write, and deliberately a fresh parameter set: every tab drives its
    // paging, search and filters through the same URL keys, so carrying them
    // across would apply one tab's narrowing to another tab's data.
    setSearchParams(new URLSearchParams({ tab: next }), { replace: true })
  }

  return (
    <div>
      <PageHeader
        title="Item master data"
        description="The vocabulary every other screen depends on: how items are classified, what they are measured in, how those measures relate, and what they sell for."
      />

      {active === undefined ? (
        <EmptyState
          icon={ShieldAlert}
          title="Nothing here for this account"
          description="Maintaining item master data needs one of the category.view, uom.view or price.view rights. Ask an administrator for the one you need."
        />
      ) : (
        <Tabs value={active} onValueChange={selectTab}>
          <TabsList>
            {tabs.map((entry) => (
              <TabsTrigger key={entry.key} value={entry.key}>
                <entry.icon className="h-4 w-4" aria-hidden />
                {entry.label}
              </TabsTrigger>
            ))}
          </TabsList>

          {/* Radix mounts only the open tab, so exactly one list query is ever
              live - which matters because all four read the same URL keys. */}
          <TabsContent value="categories">
            <CategoriesTab />
          </TabsContent>

          <TabsContent value="units">
            <UnitsTab />
          </TabsContent>

          <TabsContent value="conversions">
            <ConversionsTab />
          </TabsContent>

          <TabsContent value="prices">
            <PricesTab />
          </TabsContent>
        </Tabs>
      )}
    </div>
  )
}

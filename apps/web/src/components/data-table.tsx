/**
 * The enterprise data table (master rules 36, 78).
 *
 * Everything expensive happens on the server: pagination, filtering, sorting and
 * search. The browser receives one page of rows and never the whole table.
 *
 * The virtualiser is opt-in rather than always-on. For a 50-row page it costs
 * more than it saves; it earns its keep on a wide report page (200 rows x 20
 * columns) where the DOM node count starts to hurt scrolling.
 */

import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
  type VisibilityState,
} from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Columns3,
  Download,
  Inbox,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/overlays'
import { Input, Skeleton } from '@/components/ui/primitives'
import type { OffsetPagination } from '@/types/api'
import { cn } from '@/lib/utils'

export interface DataTableProps<T> {
  columns: ColumnDef<T, unknown>[]
  data: T[]
  pagination?: OffsetPagination
  isLoading?: boolean
  isFetching?: boolean
  error?: Error | null

  // --- server-side control (the table never sorts or filters locally) --------
  onPageChange?: (page: number) => void
  onLimitChange?: (limit: number) => void
  onSortChange?: (sortBy: string | undefined, sortDir: 'asc' | 'desc') => void
  onSearchChange?: (term: string) => void
  onRefresh?: () => void
  onExport?: () => void

  searchPlaceholder?: string
  searchValue?: string
  /** Filter controls rendered in the toolbar. */
  toolbar?: ReactNode
  onRowClick?: (row: T) => void
  getRowId?: (row: T) => string
  emptyTitle?: string
  emptyDescription?: string
  emptyAction?: ReactNode
  /** Turn on row virtualisation. Worth it above roughly 150 visible rows. */
  virtualise?: boolean
  estimatedRowHeight?: number
  maxHeight?: string
  stickyHeader?: boolean
  className?: string
}

const PAGE_SIZES = [25, 50, 100, 200]
const SEARCH_DEBOUNCE_MS = 350

/**
 * A readable name for a column that renders no header of its own.
 *
 * `row-actions` becomes "Row actions". Derived from the id rather than required
 * as a prop, so a column added without thinking about this still gets a name -
 * a required prop would just be filled in with an empty string.
 */
function humaniseColumnId(id: string): string {
  const words = id.replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim()
  if (!words) return 'Column'
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase()
}

export function DataTable<T>({
  columns,
  data,
  pagination,
  isLoading = false,
  isFetching = false,
  error = null,
  onPageChange,
  onLimitChange,
  onSortChange,
  onSearchChange,
  onRefresh,
  onExport,
  searchPlaceholder = 'Search...',
  searchValue = '',
  toolbar,
  onRowClick,
  getRowId,
  emptyTitle = 'Nothing here yet',
  emptyDescription = 'No records match the current filters.',
  emptyAction,
  virtualise = false,
  estimatedRowHeight = 41,
  maxHeight = '60vh',
  stickyHeader = true,
  className,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>({})
  const [searchInput, setSearchInput] = useState(searchValue)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Debounced search: a request per keystroke would put a filtered scan of a
  // large table behind every character the user types.
  useEffect(() => {
    if (!onSearchChange) return
    if (searchInput === searchValue) return
    const timer = setTimeout(() => onSearchChange(searchInput), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [searchInput, searchValue, onSearchChange])

  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnVisibility },
    onSortingChange: (updater) => {
      const next = typeof updater === 'function' ? updater(sorting) : updater
      setSorting(next)
      const first = next[0]
      onSortChange?.(first?.id, first?.desc ? 'desc' : 'asc')
    },
    onColumnVisibilityChange: setColumnVisibility,
    getCoreRowModel: getCoreRowModel(),
    // Sorting and filtering are server-side; enabling the client models here
    // would silently re-sort only the current page and mislead the user.
    manualPagination: true,
    manualSorting: true,
    manualFiltering: true,
    getRowId: getRowId ? (row) => getRowId(row) : undefined,
  })

  const rows = table.getRowModel().rows

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estimatedRowHeight,
    overscan: 12,
    enabled: virtualise,
  })
  const virtualRows = virtualise ? virtualizer.getVirtualItems() : []
  const paddingTop = virtualRows.length > 0 ? (virtualRows[0]?.start ?? 0) : 0
  const paddingBottom =
    virtualRows.length > 0
      ? virtualizer.getTotalSize() - (virtualRows[virtualRows.length - 1]?.end ?? 0)
      : 0

  const visibleColumnCount = table.getVisibleLeafColumns().length
  const hideableColumns = useMemo(
    () => table.getAllColumns().filter((c) => c.getCanHide()),
    [table],
  )

  const page = pagination?.page ?? 1
  const limit = pagination?.limit ?? 50
  const total = pagination?.total ?? null
  const rangeStart = total === 0 ? 0 : (page - 1) * limit + 1
  const rangeEnd = (page - 1) * limit + data.length

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {/* ------------------------------------------------------- toolbar -- */}
      <div className="flex flex-wrap items-center gap-2">
        {onSearchChange && (
          <div className="relative min-w-[240px] flex-1 sm:max-w-xs">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder={searchPlaceholder}
              className="pl-8 pr-8"
              aria-label={searchPlaceholder}
            />
            {searchInput && (
              <button
                type="button"
                onClick={() => setSearchInput('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm text-muted-foreground hover:text-foreground"
                aria-label="Clear search"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        )}

        {toolbar}

        <div className="ml-auto flex items-center gap-1.5">
          {isFetching && !isLoading && (
            <span className="mr-1 text-xs text-muted-foreground" role="status">
              Updating...
            </span>
          )}
          {onRefresh && (
            <Button variant="outline" size="icon-sm" onClick={onRefresh} aria-label="Refresh">
              <RefreshCw className={cn(isFetching && 'animate-spin')} />
            </Button>
          )}
          {hideableColumns.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  <Columns3 />
                  Columns
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
                <DropdownMenuLabel>Visible columns</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {hideableColumns.map((column) => (
                  <DropdownMenuCheckboxItem
                    key={column.id}
                    checked={column.getIsVisible()}
                    onCheckedChange={(value) => column.toggleVisibility(Boolean(value))}
                    onSelect={(e) => e.preventDefault()}
                  >
                    {typeof column.columnDef.header === 'string'
                      ? column.columnDef.header
                      : column.id}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {onExport && (
            <Button variant="outline" size="sm" onClick={onExport}>
              <Download />
              Export
            </Button>
          )}
        </div>
      </div>

      {/* --------------------------------------------------------- table -- */}
      <div className="rounded-lg border">
        <div
          ref={scrollRef}
          className="relative overflow-auto"
          style={{ maxHeight: virtualise ? maxHeight : undefined }}
        >
          <table className="w-full caption-bottom text-sm">
            <thead
              className={cn(
                'bg-muted/50 [&_tr]:border-b',
                stickyHeader && 'sticky top-0 z-10 backdrop-blur-sm',
              )}
            >
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header) => {
                    const canSort = header.column.getCanSort() && Boolean(onSortChange)
                    const sorted = header.column.getIsSorted()
                    const align = (header.column.columnDef.meta as { align?: string } | undefined)
                      ?.align
                    // A column with no visible header - the row-actions column,
                    // a selection checkbox - still needs an accessible name, or
                    // a screen reader announces every cell in it as belonging to
                    // a nameless column and the table loses its shape. The label
                    // is hidden visually, never removed.
                    const headerDef = header.column.columnDef.header
                    const hasVisibleHeader =
                      !header.isPlaceholder &&
                      headerDef !== undefined &&
                      headerDef !== '' &&
                      headerDef !== null
                    return (
                      <th
                        key={header.id}
                        style={{ width: header.getSize() !== 150 ? header.getSize() : undefined }}
                        className={cn(
                          'h-10 px-3 text-left align-middle text-xs font-semibold uppercase tracking-wide text-muted-foreground',
                          align === 'right' && 'text-right',
                          align === 'center' && 'text-center',
                        )}
                        aria-sort={
                          sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : 'none'
                        }
                      >
                        {!hasVisibleHeader ? (
                          <span className="sr-only">{humaniseColumnId(header.column.id)}</span>
                        ) : canSort ? (
                          <button
                            type="button"
                            onClick={header.column.getToggleSortingHandler()}
                            className={cn(
                              'inline-flex items-center gap-1 hover:text-foreground',
                              align === 'right' && 'flex-row-reverse',
                            )}
                          >
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            {sorted === 'asc' ? (
                              <ArrowUp className="h-3 w-3" />
                            ) : sorted === 'desc' ? (
                              <ArrowDown className="h-3 w-3" />
                            ) : (
                              <ArrowUpDown className="h-3 w-3 opacity-40" />
                            )}
                          </button>
                        ) : (
                          flexRender(header.column.columnDef.header, header.getContext())
                        )}
                      </th>
                    )
                  })}
                </tr>
              ))}
            </thead>

            <tbody className="[&_tr:last-child]:border-0">
              {/* Loading: skeleton rows matching the real row height, so the
                  layout does not jump when data arrives. */}
              {isLoading &&
                Array.from({ length: 8 }).map((_, rowIndex) => (
                  <tr key={`skeleton-${rowIndex}`} className="border-b">
                    {Array.from({ length: visibleColumnCount }).map((__, cellIndex) => (
                      <td key={cellIndex} className="px-3 py-2.5">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))}

              {!isLoading && error && (
                <tr>
                  <td colSpan={visibleColumnCount} className="px-3 py-12 text-center">
                    <p className="font-medium text-destructive">Could not load this list</p>
                    <p className="mt-1 text-sm text-muted-foreground">{error.message}</p>
                    {onRefresh && (
                      <Button variant="outline" size="sm" onClick={onRefresh} className="mt-4">
                        <RefreshCw />
                        Try again
                      </Button>
                    )}
                  </td>
                </tr>
              )}

              {!isLoading && !error && rows.length === 0 && (
                <tr>
                  <td colSpan={visibleColumnCount} className="px-3 py-16 text-center">
                    <Inbox className="mx-auto mb-3 h-8 w-8 text-muted-foreground/50" aria-hidden />
                    <p className="font-medium">{emptyTitle}</p>
                    <p className="mt-1 text-sm text-muted-foreground">{emptyDescription}</p>
                    {emptyAction && <div className="mt-4">{emptyAction}</div>}
                  </td>
                </tr>
              )}

              {!isLoading && !error && paddingTop > 0 && (
                <tr style={{ height: paddingTop }} aria-hidden />
              )}

              {!isLoading &&
                !error &&
                (virtualise ? virtualRows.map((v) => rows[v.index]!) : rows).map((row) => (
                  <tr
                    key={row.id}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                    className={cn(
                      'border-b transition-colors hover:bg-muted/40',
                      onRowClick && 'cursor-pointer',
                    )}
                  >
                    {row.getVisibleCells().map((cell) => {
                      const align = (cell.column.columnDef.meta as { align?: string } | undefined)
                        ?.align
                      return (
                        <td
                          key={cell.id}
                          className={cn(
                            'px-3 py-2 align-middle',
                            align === 'right' && 'text-right tabular',
                            align === 'center' && 'text-center',
                          )}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      )
                    })}
                  </tr>
                ))}

              {!isLoading && !error && paddingBottom > 0 && (
                <tr style={{ height: paddingBottom }} aria-hidden />
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ---------------------------------------------------- pagination -- */}
      {pagination && (
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <p className="text-muted-foreground" role="status">
            {data.length === 0
              ? 'No records'
              : total !== null
                ? `${rangeStart}-${rangeEnd} of ${total.toLocaleString()}`
                : `${rangeStart}-${rangeEnd}`}
          </p>

          <div className="flex items-center gap-3">
            {onLimitChange && (
              <label className="flex items-center gap-2 text-muted-foreground">
                Rows
                <select
                  value={limit}
                  onChange={(e) => onLimitChange(Number(e.target.value))}
                  className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                >
                  {PAGE_SIZES.map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {onPageChange && (
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="icon-sm"
                  disabled={!pagination.has_previous}
                  onClick={() => onPageChange(page - 1)}
                  aria-label="Previous page"
                >
                  <ChevronLeft />
                </Button>
                <span className="min-w-[5rem] px-1 text-center text-muted-foreground">
                  {pagination.total_pages
                    ? `Page ${page} of ${pagination.total_pages}`
                    : `Page ${page}`}
                </span>
                <Button
                  variant="outline"
                  size="icon-sm"
                  disabled={!pagination.has_next}
                  onClick={() => onPageChange(page + 1)}
                  aria-label="Next page"
                >
                  <ChevronRight />
                </Button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

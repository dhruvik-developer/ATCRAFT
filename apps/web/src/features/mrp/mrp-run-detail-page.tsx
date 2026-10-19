/**
 * One MRP run: what it found, and what to do about it.
 *
 * The screen is built around two things the engine does that a planner has to be
 * able to audit before turning advice into a purchase order.
 *
 * The first is the merge. A part several sales orders and several BOM branches
 * all need becomes ONE suggestion (docs/database.md sections 5 and 6), so every
 * row expands in place to show its `source_demands`: which orders asked for it,
 * and through which parent each contribution arrived.
 *
 * The second is the time phasing (docs/assumptions.md 21). Supply that lands
 * inside the horizon but *after* it is needed is not netted, so a planner can be
 * looking at a shortage for a part that has a purchase order against it. The run
 * says so in `warnings` - naming the item, the quantity, the date it arrives and
 * the date it was needed - and counts the items in
 * `result_summary.late_supply_item_count`. Both are shown at the top of this
 * page; hiding them recreates exactly the confusion the phasing removed.
 *
 * Conversion is a bulk action because that is how it is used - a planner works
 * through a shortage list, ticks the rows they accept, and converts them in one
 * go. The confirmation states exactly what documents that will create before
 * anything is written.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  Ban,
  CalendarClock,
  ChevronDown,
  ChevronRight,
  Factory,
  GitMerge,
  Hammer,
  Layers,
  PackageSearch,
  RefreshCw,
  ShoppingCart,
  Timer,
  TriangleAlert,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  PageHeader,
  StatTile,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
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
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { WAREHOUSE_LOOKUP_PARAMS } from '@/features/inventory/types'
import { ShortageReportTab } from '@/features/mrp/shortage-report-tab'

import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_ALL } from '@/lib/constants'
import { reportFieldErrors } from '@/lib/form-errors'
import {
  daysUntil,
  formatDate,
  formatDateTime,
  formatNumber,
  formatQuantity,
  humanise,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  CONVERSION_TARGET,
  SUGGESTION_STATUSES,
  SUGGESTION_TYPES,
  SUGGESTION_TYPE_VARIANT,
  formatDuration,
  isActionable,
  isBomSourceDemand,
  isConvertible,
  isRunInFlight,
  runDurationMs,
  runStat,
  suggestionsOfType,
  MAX_BULK_IGNORE,
  type MrpBulkIgnoreResult,
  type MrpConversionResult,
  type MrpRunDetail,
  type MrpSourceDemand,
  type MrpSuggestionRow,
  type SuggestionType,
  type WarehouseOption,
} from './mrp-types'

const POLL_INTERVAL_MS = 3000

/**
 * How many conversions run at once.
 *
 * Each one writes a document, and MRP endpoints sit under a tighter rate limit
 * (docs/api.md, "Rate limits"). Firing two hundred requests in parallel would
 * earn a 429 for the tail of the batch rather than finishing faster.
 */
const BULK_CONCURRENCY = 5

interface BulkOutcome {
  total: number
  failed: number
  /**
   * Conversions that actually wrote a document.
   *
   * The API closes a suggestion even when the module that would own the
   * document is absent, and says so with `handled: false` and a null
   * `created_document_id`. Reporting "became live documents" in that case sends
   * a buyer looking for a requisition that was never created.
   */
  handled: number
}

async function runBulk(
  ids: string[],
  call: (id: string) => Promise<MrpConversionResult>,
): Promise<BulkOutcome> {
  let failed = 0
  let handled = 0
  let firstError: unknown = null

  for (let index = 0; index < ids.length; index += BULK_CONCURRENCY) {
    const chunk = ids.slice(index, index + BULK_CONCURRENCY)
    const settled = await Promise.allSettled(chunk.map((id) => call(id)))
    for (const outcome of settled) {
      if (outcome.status === 'rejected') {
        failed += 1
        firstError ??= outcome.reason
      } else if (outcome.value?.handled) {
        handled += 1
      }
    }
  }

  // Nothing succeeded, so the whole action failed: rethrow and let the global
  // mutation-cache handler report the API's own message. A partial failure is a
  // different story and is summarised by the caller, because "3 of 12 could not
  // be converted" is information no generic handler can produce.
  if (failed === ids.length && firstError !== null) throw firstError

  return { total: ids.length, failed, handled }
}

/** A count the run did not report reads as unknown, never as zero. */
const tileValue = (value: number | null): string =>
  value === null ? 'Not reported' : formatNumber(value)

// ------------------------------------------------------------ traceability --

function SourceDemandList({ demands }: { demands: MrpSourceDemand[] }) {
  return (
    <ol className="mt-2 space-y-2 border-l pl-3">
      {demands.map((demand, index) => (
        <li key={index} className="text-2xs leading-relaxed">
          <span className="flex flex-wrap items-baseline gap-x-2">
            {isBomSourceDemand(demand) ? (
              <Link
                to={`/items/${demand.via_item_id}`}
                className="font-medium hover:underline"
                onClick={(event) => event.stopPropagation()}
              >
                via {demand.via_item_code}
              </Link>
            ) : demand.code && demand.source_id ? (
              <Link
                to={`/sales-orders/${demand.source_id}`}
                className="font-medium hover:underline"
                onClick={(event) => event.stopPropagation()}
              >
                {demand.code}
              </Link>
            ) : (
              <span className="font-medium">{demand.code ?? humanise(demand.source)}</span>
            )}
            <span className="tabular">{formatQuantity(demand.quantity)}</span>
            <span className="text-muted-foreground">due {formatDate(demand.required_date)}</span>
          </span>

          <span className="block text-muted-foreground">
            {isBomSourceDemand(demand)
              ? `Exploded from a bill of materials at level ${demand.level}`
              : humanise(demand.source)}
          </span>
        </li>
      ))}
    </ol>
  )
}

// -------------------------------------------------------- action summary ----

/** What the confirmation shows before anything is written. */
function SelectionSummary({
  rows,
  action,
}: {
  rows: MrpSuggestionRow[]
  action: 'convert' | 'ignore'
}) {
  const byType = new Map<SuggestionType, MrpSuggestionRow[]>()
  for (const row of rows) {
    const bucket = byType.get(row.suggestion_type)
    if (bucket) bucket.push(row)
    else byType.set(row.suggestion_type, [row])
  }

  // The API's own verdict, not a date subtraction done in the browser.
  const lateCount = rows.filter((row) => row.is_late).length

  const codes = rows.map((row) => row.item_code)
  const shown = codes.slice(0, 8)

  return (
    <div className="rounded-md border bg-muted/40 p-3">
      <ul className="space-y-1 text-sm">
        {[...byType.entries()].map(([type, list]) => (
          <li key={type} className="flex items-center gap-2">
            <Badge variant={SUGGESTION_TYPE_VARIANT[type]}>{humanise(type)}</Badge>
            <span>
              {formatNumber(list.length)}{' '}
              {action === 'convert'
                ? `to become ${CONVERSION_TARGET[type]}`
                : 'to be closed unactioned'}
            </span>
          </li>
        ))}
      </ul>

      <p className="mt-2.5 break-words text-xs text-muted-foreground">
        {shown.join(', ')}
        {codes.length > shown.length && ` and ${formatNumber(codes.length - shown.length)} more`}
      </p>

      {action === 'convert' && lateCount > 0 && (
        <p className="mt-2 text-xs font-medium text-warning">
          {formatNumber(lateCount)} of these are already past their suggested order date, so the
          required date will slip unless the lead time is expedited.
        </p>
      )}
    </div>
  )
}

// ------------------------------------------------------------------ page ----

export default function MrpRunDetailPage() {
  const { runId = '' } = useParams<{ runId: string }>()
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()

  const [selected, setSelected] = useState<Record<string, MrpSuggestionRow>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [convertOpen, setConvertOpen] = useState(false)
  const [ignoreOpen, setIgnoreOpen] = useState(false)
  const [ignoreReason, setIgnoreReason] = useState('')
  const [ignoreReasonTouched, setIgnoreReasonTouched] = useState(false)

  const typeFilter = searchParams.get('suggestion_type') ?? ''
  const statusFilter = searchParams.get('status') ?? ''
  const lateOnly = searchParams.get('late_only') === 'true'

  const runQuery = useQuery({
    queryKey: queryKeys.mrp.run(runId),
    queryFn: () => api.get<MrpRunDetail>(`/mrp/runs/${runId}`),
    enabled: Boolean(runId),
    staleTime: staleTimes.transactional,
    // Opening a run that is still calculating must show it finishing, not a
    // frozen snapshot the planner has to reload by hand.
    refetchInterval: (query) =>
      isRunInFlight(query.state.data?.status) ? POLL_INTERVAL_MS : false,
  })

  const run = runQuery.data
  const isFinished = Boolean(run) && !isRunInFlight(run?.status)

  /**
   * Tile counts come off the run itself.
   *
   * They used to be four extra `with_total` calls against the suggestions
   * endpoint, which no longer works: above `COUNT_CEILING` the API declines to
   * count and `pagination.total` is null (docs/assumptions.md 15), so exactly the
   * runs big enough to need a summary were the ones that lost it. The run's own
   * `result_summary` and `suggestion_counts_by_status` are exact at any size and
   * cost no request at all.
   */
  const summary = run?.result_summary ?? null
  const openCount = run?.suggestion_counts_by_status?.OPEN ?? null
  const lateSupplyItems = runStat(summary, 'late_supply_item_count')
  // Both fields default server-side and are therefore optional on the wire; a
  // payload without them must read as "nothing to report", not throw.
  const warnings = run?.warnings ?? []

  const suggestions = useListQuerySuggestions(runId, isFinished, typeFilter, statusFilter, lateOnly)

  /**
   * The run stores warehouse ids; a planner reads warehouse codes. The list is
   * small, cached master data and is already in the cache from the run form.
   *
   * `GET /warehouses` is behind `warehouse.view`, which the MRP route does not
   * demand, so the lookup is only fired for a principal who holds it; anyone
   * else sees the count `warehouseLabel` already falls back to.
   */
  const scopedWarehouseIds = useMemo(
    () => run?.warehouse_ids ?? run?.parameters?.warehouse_ids ?? [],
    [run],
  )
  // The one spelling of the lookup, so this and `WarehouseSelect` share a
  // cache entry rather than fetching the same bounded list twice.
  const warehouseParams = WAREHOUSE_LOOKUP_PARAMS
  const warehouses = useQuery({
    queryKey: queryKeys.warehouses.list(warehouseParams),
    queryFn: () => api.getPage<WarehouseOption>('/warehouses', { params: warehouseParams }),
    enabled: scopedWarehouseIds.length > 0 && can(P.WAREHOUSE_VIEW),
    staleTime: staleTimes.master,
  })

  const warehouseLabel = useMemo(() => {
    if (scopedWarehouseIds.length === 0) return 'All warehouses'
    const codeById = new Map((warehouses.data?.data ?? []).map((row) => [row.id, row.code]))
    const codes = scopedWarehouseIds
      .map((id) => codeById.get(id))
      .filter((code): code is string => Boolean(code))
    // Falls back to a count rather than showing a partial list that reads as if
    // the run were scoped more narrowly than it was.
    return codes.length === scopedWarehouseIds.length
      ? codes.join(', ')
      : `${formatNumber(scopedWarehouseIds.length)} selected`
  }, [scopedWarehouseIds, warehouses.data])

  // Moving to another run must not carry a stale tick list with it.
  useEffect(() => {
    setSelected({})
    setExpanded({})
  }, [runId])

  const selectedRows = useMemo(() => Object.values(selected), [selected])

  const convert = useMutation({
    mutationFn: (ids: string[]) =>
      // The API requires a body even when nothing is being overridden: quantity,
      // date and target all default from the suggestion itself.
      runBulk(ids, (id) => api.post<MrpConversionResult>(`/mrp/suggestions/${id}/convert`, {})),
    onSuccess: ({ total, failed, handled }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.mrp.all })
      // Conversion creates documents in the modules that own them.
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      setSelected({})
      setConvertOpen(false)

      const closed = total - failed
      if (failed > 0) {
        toast.warning(
          `Converted ${formatNumber(closed)} of ${formatNumber(total)}`,
          `${formatNumber(failed)} could not be converted and are still open.`,
        )
      } else if (handled < closed) {
        // Closed, but the owning module raised nothing. The suggestion will not
        // come back, and there is no document to work from either.
        toast.warning(
          `Closed ${formatNumber(closed)} suggestion${closed === 1 ? '' : 's'}`,
          `${formatNumber(closed - handled)} produced no document because the module that would own it is unavailable. They will not be planned again from this run.`,
        )
      } else {
        toast.success(
          'Suggestions converted',
          `${formatNumber(handled)} suggestion${handled === 1 ? '' : 's'} became live documents.`,
        )
      }
    },
    // Fired from a confirm box, not a form: a 422 has no control to land on,
    // and the mutation cache stays quiet for it.
    onError: (error) => reportFieldErrors(error, 'Could not convert these suggestions'),
  })

  /**
   * Ignoring is one call, not one per row: the API takes the whole set and a
   * single recorded reason (`POST /mrp/suggestions/bulk-ignore`). It reports back
   * which ids it skipped, which is how a planner learns that somebody else
   * converted a row while this list was on screen.
   */
  const ignore = useMutation({
    mutationFn: ({ ids, reason }: { ids: string[]; reason: string }) =>
      api.post<MrpBulkIgnoreResult>('/mrp/suggestions/bulk-ignore', {
        suggestion_ids: ids,
        reason,
      }),
    onSuccess: (result, { ids }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.mrp.all })
      // Only the ids actually sent leave the tick list; anything past
      // MAX_BULK_IGNORE was never submitted and stays selected for the next call.
      const sent = new Set(ids)
      setSelected((current) =>
        Object.fromEntries(Object.entries(current).filter(([id]) => !sent.has(id))),
      )
      setIgnoreOpen(false)
      setIgnoreReason('')
      setIgnoreReasonTouched(false)

      const skipped = result.skipped_ids?.length ?? 0
      if (skipped > 0) {
        toast.warning(
          `Ignored ${formatNumber(result.ignored_count)}`,
          `${formatNumber(skipped)} had already been converted or ignored and were left alone.`,
        )
      } else {
        toast.success(
          'Suggestions ignored',
          `${formatNumber(result.ignored_count)} suggestion${result.ignored_count === 1 ? '' : 's'} will not be planned again from this run.`,
        )
      }
    },
    onError: (error) => reportFieldErrors(error, 'Could not ignore these suggestions'),
  })

  const isMutating = convert.isPending || ignore.isPending
  const canConvert = can(P.MRP_CONVERT_SUGGESTION)

  const pageActionable = useMemo(
    () => suggestions.data.filter((row) => isActionable(row.status)),
    [suggestions.data],
  )

  // A transfer can be ignored but not converted, so the two buttons look at
  // different halves of the tick list.
  const convertibleIds = useMemo(
    () => selectedRows.filter((row) => isConvertible(row.suggestion_type)).map((row) => row.id),
    [selectedRows],
  )
  const convertibleRows = useMemo(
    () => selectedRows.filter((row) => isConvertible(row.suggestion_type)),
    [selectedRows],
  )
  const transferCount = selectedRows.length - convertibleRows.length
  const allPageSelected =
    pageActionable.length > 0 && pageActionable.every((row) => Boolean(selected[row.id]))

  // The API caps one bulk ignore at `MAX_BULK_SUGGESTIONS` and rejects the
  // whole request past it, so one call takes the first batch and the rest stay
  // ticked for the next. The dialog states exactly that split.
  const ignoreBatch = useMemo(() => selectedRows.slice(0, MAX_BULK_IGNORE), [selectedRows])
  const ignoreRemainder = selectedRows.length - ignoreBatch.length
  const ignoreReasonTooShort = ignoreReason.trim().length < REASON_MIN_LENGTH

  const toggleRow = (row: MrpSuggestionRow) => {
    setSelected((current) => {
      const next = { ...current }
      if (next[row.id]) delete next[row.id]
      else next[row.id] = row
      return next
    })
  }

  const togglePage = (checked: boolean) => {
    setSelected((current) => {
      const next = { ...current }
      for (const row of pageActionable) {
        if (checked) next[row.id] = row
        else delete next[row.id]
      }
      return next
    })
  }

  const toggleExpanded = (id: string) =>
    setExpanded((current) => ({ ...current, [id]: !current[id] }))

  const columns = useMemo<ColumnDef<MrpSuggestionRow, unknown>[]>(
    () => [
      {
        id: 'select',
        header: () => (
          <Checkbox
            checked={allPageSelected}
            onCheckedChange={(checked) => togglePage(checked === true)}
            disabled={!canConvert || pageActionable.length === 0}
            aria-label="Select every open suggestion on this page"
          />
        ),
        enableSorting: false,
        enableHiding: false,
        size: 40,
        cell: ({ row }) => {
          const actionable = isActionable(row.original.status)
          return (
            <span onClick={(event) => event.stopPropagation()}>
              <Checkbox
                checked={Boolean(selected[row.original.id])}
                onCheckedChange={() => toggleRow(row.original)}
                disabled={!canConvert || !actionable}
                aria-label={`Select ${row.original.item_code}`}
              />
            </span>
          )
        },
      },
      {
        // `item_code` is one of the API's sortable fields; the id has to match
        // the field name because it is what the table sends as `sort_by`.
        id: 'item_code',
        header: 'Item',
        cell: ({ row }) => (
          <div className="max-w-[16rem]">
            <Link
              to={`/items/${row.original.item_id}`}
              className="block truncate font-medium hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              {row.original.item_code}
            </Link>
            <span className="block text-2xs text-muted-foreground">
              lead time {formatNumber(row.original.lead_time_days)}d
            </span>
          </div>
        ),
      },
      {
        id: 'suggestion_type',
        header: 'Type',
        cell: ({ row }) => (
          <Badge variant={SUGGESTION_TYPE_VARIANT[row.original.suggestion_type] ?? 'secondary'}>
            {humanise(row.original.suggestion_type)}
          </Badge>
        ),
      },
      {
        id: 'required_quantity',
        header: 'Required',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.required_quantity),
      },
      {
        id: 'available_quantity',
        header: 'Available',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.available_quantity),
      },
      {
        id: 'incoming_quantity',
        header: 'Incoming in time',
        enableSorting: false,
        meta: { align: 'right' },
        // Time-phased: supply that lands after the required date is deliberately
        // NOT in this figure (docs/assumptions.md 21). The run warnings name what
        // was left out.
        cell: ({ row }) => formatQuantity(row.original.incoming_quantity),
      },
      {
        id: 'shortage_quantity',
        header: 'Shortage',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium text-destructive">
            {formatQuantity(row.original.shortage_quantity)}
          </span>
        ),
      },
      {
        id: 'suggested_quantity',
        header: 'Suggested',
        meta: { align: 'right' },
        // Rounded up to the item's minimum order quantity and order multiple,
        // so it is normally larger than the raw shortage.
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.suggested_quantity)}</span>
        ),
      },
      {
        id: 'required_date',
        header: 'Required by',
        cell: ({ row }) => formatDate(row.original.required_date),
      },
      {
        id: 'suggested_order_date',
        header: 'Order by',
        cell: ({ row }) => {
          // `is_late` is the API's verdict, computed against the run's own as-of
          // date. The day count is only the elaboration on it.
          const late = row.original.is_late && isActionable(row.original.status)
          const days = daysUntil(row.original.suggested_order_date)
          return (
            <span className="flex flex-wrap items-center gap-1.5">
              <span className={late ? 'font-medium text-destructive' : undefined}>
                {formatDate(row.original.suggested_order_date)}
              </span>
              {late && (
                <Badge variant="destructive">
                  {days !== null && days < 0 ? `Late ${Math.abs(days)}d` : 'Late'}
                </Badge>
              )}
            </span>
          )
        },
      },
      {
        id: 'status',
        header: 'Status',
        cell: ({ row }) => (
          <div className="flex flex-col gap-0.5">
            <StatusBadge status={row.original.status} />
            {row.original.converted_to_type && (
              <span className="text-2xs text-muted-foreground">
                {humanise(row.original.converted_to_type)}
                {row.original.converted_at && ` ${formatDate(row.original.converted_at)}`}
              </span>
            )}
            {row.original.ignored_reason && (
              <span
                className="max-w-[12rem] truncate text-2xs text-muted-foreground"
                title={row.original.ignored_reason}
              >
                {row.original.ignored_reason}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'source_demands',
        header: 'Source demands',
        enableSorting: false,
        size: 320,
        cell: ({ row }) => {
          const demands = row.original.source_demands ?? []
          if (demands.length === 0) {
            return <span className="text-xs text-muted-foreground">Not recorded</span>
          }

          // Distinct origins: a sales order code, or the parent a BOM branch
          // came through. More than one is the merge doing its job.
          const origins = new Set(
            demands.map((demand) =>
              isBomSourceDemand(demand) ? demand.via_item_code : (demand.code ?? demand.source),
            ),
          )
          const isOpen = Boolean(expanded[row.original.id])

          return (
            <div className="w-[19rem]">
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation()
                  toggleExpanded(row.original.id)
                }}
                aria-expanded={isOpen}
                className="flex w-full items-center gap-1.5 rounded-sm text-left text-xs hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {isOpen ? (
                  <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
                )}
                {origins.size > 1 && (
                  <GitMerge className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
                )}
                <span
                  className={cn(
                    'truncate',
                    origins.size > 1 ? 'font-medium text-primary' : 'text-muted-foreground',
                  )}
                >
                  {formatNumber(demands.length)} demand{demands.length === 1 ? '' : 's'}
                  {origins.size > 0 &&
                    ` from ${formatNumber(origins.size)} source${origins.size === 1 ? '' : 's'}`}
                </span>
              </button>

              {isOpen && <SourceDemandList demands={demands} />}
            </div>
          )
        },
      },
    ],
    // The header checkbox and every row checkbox depend on the current tick
    // list and on which rows this page can act on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allPageSelected, canConvert, expanded, pageActionable, selected],
  )

  // ------------------------------------------------------------- states ----

  if (runQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-[5.5rem] w-full" />
          ))}
        </div>
        <div className="space-y-2 rounded-lg border p-3">
          {Array.from({ length: 8 }).map((_, index) => (
            <Skeleton key={index} className="h-8 w-full" />
          ))}
        </div>
      </div>
    )
  }

  if (runQuery.error) {
    return (
      <>
        <PageHeader title="MRP run" breadcrumbs={[{ label: 'MRP', to: '/mrp' }]} />
        <Alert variant="destructive">
          <AlertTitle>Could not load this run</AlertTitle>
          <AlertDescription>{(runQuery.error as Error).message}</AlertDescription>
          <Button
            variant="outline"
            size="sm"
            className="mt-4"
            onClick={() => void runQuery.refetch()}
          >
            <RefreshCw />
            Try again
          </Button>
        </Alert>
      </>
    )
  }

  if (!run) {
    return (
      <>
        <PageHeader title="MRP run" breadcrumbs={[{ label: 'MRP', to: '/mrp' }]} />
        <EmptyState
          title="Run not found"
          description="It may have been removed, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/mrp">Back to MRP</Link>
            </Button>
          }
        />
      </>
    )
  }

  const parameters = run.parameters

  return (
    <>
      <PageHeader
        title={run.code}
        description="Requirements after netting demand against stock and against the supply that arrives in time to meet it."
        breadcrumbs={[{ label: 'MRP', to: '/mrp' }, { label: run.code }]}
        status={
          <span className="flex items-center gap-2">
            <StatusBadge status={run.status} />
            <Badge variant={run.mode === 'ASYNC' ? 'secondary' : 'muted'}>
              {run.mode === 'ASYNC' ? 'Background' : 'Immediate'}
            </Badge>
          </span>
        }
        actions={
          <Button variant="outline" size="sm" onClick={() => void runQuery.refetch()}>
            <RefreshCw className={cn(runQuery.isFetching && 'animate-spin')} />
            Refresh
          </Button>
        }
      />

      {isRunInFlight(run.status) && (
        <Alert variant="info" className="mb-5">
          <AlertTitle className="flex items-center gap-2">
            <Timer className="h-4 w-4 animate-pulse" aria-hidden />
            This run is still calculating
          </AlertTitle>
          <AlertDescription>
            Suggestions appear when the engine finishes. The page refreshes itself every three
            seconds, so there is nothing to reload.
          </AlertDescription>
        </Alert>
      )}

      {(run.status === 'FAILED' || run.status === 'CANCELLED') && (
        <Alert variant="destructive" className="mb-5">
          <AlertTitle className="flex items-center gap-2">
            <TriangleAlert className="h-4 w-4" aria-hidden />
            {run.status === 'CANCELLED' ? 'This run was cancelled' : 'This run failed'}
          </AlertTitle>
          <AlertDescription>
            {run.error_message ??
              'The engine stopped before producing suggestions. Nothing was created.'}
          </AlertDescription>
        </Alert>
      )}

      {/*
        The caveats the engine already knows about its own answer. The most
        important is the time phasing: supply that exists but lands too late is
        not netted, so the shortage below is real even though a purchase order
        for the part is open. Each warning names the item, the quantity, the date
        it arrives and the date it was needed.
      */}
      {warnings.length > 0 && (
        <Alert variant="warning" className="mb-5">
          <AlertTitle className="flex items-center gap-2">
            <CalendarClock className="h-4 w-4" aria-hidden />
            Read these before acting on the suggestions
          </AlertTitle>
          <AlertDescription>
            <ul className="list-disc space-y-1.5 pl-4">
              {warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <StatTile
          label="Demand lines"
          value={formatNumber(run.demand_line_count)}
          hint="Order lines processed"
          icon={Layers}
        />
        <StatTile
          label="Items short"
          value={tileValue(runStat(summary, 'shortage_item_count'))}
          hint="One row per merged part"
          icon={PackageSearch}
          tone="warning"
        />
        <StatTile
          label="Purchase"
          value={tileValue(suggestionsOfType(summary, 'PURCHASE'))}
          hint="Buy from a vendor"
          icon={ShoppingCart}
        />
        <StatTile
          label="Manufacture"
          value={tileValue(suggestionsOfType(summary, 'MANUFACTURE'))}
          hint="Make in house"
          icon={Factory}
        />
        <StatTile
          label="Job work"
          value={tileValue(suggestionsOfType(summary, 'JOB_WORK'))}
          hint="Send out to a job worker"
          icon={Hammer}
        />
        <StatTile
          label="Supply arriving late"
          // The whole point of the time phasing: these items have inbound supply
          // inside the horizon that lands after it is needed, so the shortage
          // stands and a buyer has an expedite to chase.
          value={tileValue(lateSupplyItems)}
          hint="Items with supply that misses its date"
          icon={CalendarClock}
          tone={lateSupplyItems !== null && lateSupplyItems > 0 ? 'warning' : undefined}
        />
      </div>

      <div className="mb-6 rounded-lg border p-4">
        <DescriptionList
          columns={4}
          items={[
            { label: 'Horizon', value: `${formatNumber(run.horizon_days)} days` },
            { label: 'Warehouses', value: warehouseLabel },
            {
              label: 'Safety stock',
              value:
                parameters?.respect_safety_stock === false
                  ? 'Planned away - safety stock was available to net'
                  : 'Held back from the netting',
            },
            {
              label: 'Open suggestions',
              value: openCount === null ? 'Not reported' : formatNumber(openCount),
            },
            { label: 'Started', value: formatDateTime(run.started_at ?? run.created_at) },
            { label: 'Completed', value: formatDateTime(run.completed_at) },
            { label: 'Duration', value: formatDuration(runDurationMs(run)) },
            {
              label: 'Manual demand',
              value:
                parameters?.manual_demand_lines === undefined ||
                parameters.manual_demand_lines === null
                  ? 'Not reported'
                  : `${formatNumber(parameters.manual_demand_lines)} line(s)`,
            },
          ]}
        />
      </div>

      {selectedRows.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-primary/40 bg-primary/5 px-3 py-2">
          <span className="text-sm font-medium" role="status">
            {formatNumber(selectedRows.length)} suggestion{selectedRows.length === 1 ? '' : 's'}{' '}
            selected
            {transferCount > 0 && (
              <span className="ml-2 font-normal text-muted-foreground">
                ({formatNumber(transferCount)} transfer
                {transferCount === 1 ? '' : 's'} cannot be converted - raise a stock transfer by
                hand)
              </span>
            )}
            {selectedRows.length > MAX_BULK_IGNORE && (
              <span className="ml-2 font-normal text-warning">
                (the API ignores at most {formatNumber(MAX_BULK_IGNORE)} in one call)
              </span>
            )}
          </span>
          {/* Affordances only - the API authorises each conversion itself. */}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setSelected({})}>
              Clear selection
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setIgnoreOpen(true)}
              disabled={isMutating}
            >
              <Ban />
              Ignore selected
            </Button>
            <Button
              size="sm"
              onClick={() => setConvertOpen(true)}
              disabled={isMutating || convertibleIds.length === 0}
            >
              <Factory />
              Convert selected
            </Button>
          </div>
        </div>
      )}

      <DataTable
        columns={columns}
        data={suggestions.data}
        pagination={suggestions.pagination}
        isLoading={suggestions.isLoading}
        isFetching={suggestions.isFetching}
        error={suggestions.error}
        onPageChange={suggestions.setPage}
        onLimitChange={suggestions.setLimit}
        onSortChange={suggestions.setSort}
        onSearchChange={suggestions.setSearch}
        onRefresh={() => void suggestions.refetch()}
        searchValue={suggestions.search}
        searchPlaceholder="Search item code..."
        getRowId={(row) => row.id}
        onRowClick={(row) => toggleExpanded(row.id)}
        emptyTitle={isFinished ? 'No suggestions match' : 'No suggestions yet'}
        emptyDescription={
          isFinished
            ? 'Either the run found no shortage in this slice, or a filter is hiding it.'
            : 'The engine has not finished this run. Suggestions appear as soon as it does.'
        }
        toolbar={
          <>
            <Select
              value={typeFilter || SELECT_ALL}
              onValueChange={(value) =>
                suggestions.setParam('suggestion_type', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by suggestion type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All types</SelectItem>
                {SUGGESTION_TYPES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={statusFilter || SELECT_ALL}
              onValueChange={(value) =>
                suggestions.setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger
                className="w-full sm:w-[170px]"
                aria-label="Filter by suggestion status"
              >
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {SUGGESTION_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* Server-side, like every other filter here: `late_only` is a query
                parameter the suggestions endpoint answers. */}
            <div className="flex items-center gap-2">
              <Checkbox
                id="mrp-late-only"
                checked={lateOnly}
                onCheckedChange={(checked) =>
                  suggestions.setParam('late_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="mrp-late-only" className="font-normal">
                Already past the order date
              </Label>
            </div>
          </>
        }
      />

      {/*
        The requirements behind those suggestions, and specifically what supply
        the run refused to net because it lands too late. The suggestion list
        above says "order 50"; this says whether 100 are already coming and
        simply arrive a month after they are needed - which is a call to a
        supplier rather than a new purchase order.
      */}
      {isFinished && (
        <section className="space-y-3">
          <div>
            <h2 className="text-base font-semibold">Shortages and late supply</h2>
            <p className="text-sm text-muted-foreground">
              Every requirement this run could not cover, with the committed supply that arrives
              after it is needed.
            </p>
          </div>
          <ShortageReportTab runId={runId} warehouseIds={run?.warehouse_ids ?? []} />
        </section>
      )}

      <ConfirmDialog
        open={convertOpen}
        onOpenChange={setConvertOpen}
        title={`Convert ${formatNumber(convertibleRows.length)} suggestion${convertibleRows.length === 1 ? '' : 's'}?`}
        description="This creates real documents that other people will act on. Each suggestion is converted separately, so a rejected one does not undo the rest."
        confirmLabel="Convert"
        loading={convert.isPending}
        onConfirm={() => convert.mutate(convertibleIds)}
      >
        <SelectionSummary rows={convertibleRows} action="convert" />
        {transferCount > 0 && (
          <p className="mt-2 text-xs text-muted-foreground">
            {formatNumber(transferCount)} transfer suggestion
            {transferCount === 1 ? '' : 's'} in the selection will be left untouched: the API has no
            conversion target for a transfer.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={ignoreOpen}
        onOpenChange={(open) => {
          setIgnoreOpen(open)
          if (!open) setIgnoreReasonTouched(false)
        }}
        title={`Ignore ${formatNumber(ignoreBatch.length)} suggestion${ignoreBatch.length === 1 ? '' : 's'}?`}
        description="Ignored suggestions stay on the run for the record but are never converted. The shortage itself does not go away - the next run will raise it again."
        confirmLabel="Ignore"
        destructive
        loading={ignore.isPending}
        onConfirm={() => {
          // The API's own 3..255 bound on `reason`. Checked here because this is
          // a confirmation, not a form: a 422 against `reason` is a field error
          // the mutation cache stays quiet about, with nothing to render it.
          if (ignoreReasonTooShort) {
            setIgnoreReasonTouched(true)
            return
          }
          ignore.mutate({ ids: ignoreBatch.map((row) => row.id), reason: ignoreReason.trim() })
        }}
      >
        <SelectionSummary rows={ignoreBatch} action="ignore" />
        {ignoreRemainder > 0 && (
          <p className="mt-2 text-xs text-warning">
            The API ignores at most {formatNumber(MAX_BULK_IGNORE)} in one call. The remaining{' '}
            {formatNumber(ignoreRemainder)} stay selected for a further call.
          </p>
        )}
        <div className="mt-3">
          <Label htmlFor="mrp-ignore-reason" required>
            Reason
          </Label>
          <Input
            id="mrp-ignore-reason"
            value={ignoreReason}
            onChange={(event) => setIgnoreReason(event.target.value)}
            maxLength={REASON_MAX_LENGTH}
            autoComplete="off"
            className="mt-1.5"
            placeholder="Covered by an order the plan cannot see"
            aria-invalid={ignoreReasonTouched && ignoreReasonTooShort}
          />
          {ignoreReasonTouched && ignoreReasonTooShort && (
            <FieldError
              message={
                ignoreReason.trim() === ''
                  ? 'The API records a reason against every ignored suggestion'
                  : `Record at least ${REASON_MIN_LENGTH} characters of reason`
              }
            />
          )}
        </div>
      </ConfirmDialog>
    </>
  )
}

// ---------------------------------------------------------------- helpers ---

/**
 * The suggestions list.
 *
 * Wrapped so the page body reads as one flow: page, sort, search and all three
 * filters are query parameters the API answers, and the browser never holds more
 * than the page it is showing.
 */
function useListQuerySuggestions(
  runId: string,
  isFinished: boolean,
  typeFilter: string,
  statusFilter: string,
  lateOnly: boolean,
) {
  return useListQuery<MrpSuggestionRow>({
    path: `/mrp/runs/${runId}/suggestions`,
    queryKey: (params) => queryKeys.mrp.suggestions(runId, params),
    defaultSortBy: 'suggested_order_date',
    defaultSortDir: 'asc',
    enabled: Boolean(runId) && isFinished,
    staleTime: staleTimes.transactional,
    filters: {
      ...(typeFilter ? { suggestion_type: typeFilter } : {}),
      ...(statusFilter ? { status: statusFilter } : {}),
      ...(lateOnly ? { late_only: true } : {}),
    },
  })
}

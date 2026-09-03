/**
 * Material requirements planning: start a run, watch it, and read the history.
 *
 * The one thing this screen must get right is that an MRP run is not a request.
 * A small run is answered inline; a run over
 * `settings.mrp_sync_threshold_demand_lines` is dispatched to Celery and
 * answered with 202 and a job id (docs/database.md section 6). Both outcomes
 * arrive at the same POST, so the screen handles both: the async case starts a
 * 3-second poll of the run itself and keeps the rest of the page - history,
 * search, navigation - fully usable while the engine works.
 *
 * The elapsed timer matters more than it looks. A planner who has just kicked
 * off a 40,000-line explosion needs to see that something is still happening;
 * a spinner with no clock is indistinguishable from a hung page.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  Check,
  ChevronsUpDown,
  Play,
  Timer,
  TriangleAlert,
  Warehouse as WarehouseIcon,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useForm, type FieldPath } from 'react-hook-form'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
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
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Checkbox,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { ApiError, api } from '@/lib/api-client'
import { formatDateTime, formatNumber, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  MRP_RUN_STATUSES,
  formatDuration,
  isAcceptedRun,
  isRunInFlight,
  runDurationMs,
  runIdFromStatusUrl,
  type MrpRunCreated,
  type MrpRunDetail,
  type MrpRunRow,
  type WarehouseOption,
} from './mrp-types'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/** docs/api.md: poll the status URL, do not hold a connection open. */
const POLL_INTERVAL_MS = 3000

/** `MIN_HORIZON_DAYS` / `MAX_HORIZON_DAYS` in `apps/api/src/modules/mrp/models.py`. */
const MAX_HORIZON_DAYS = 1095

/** `MAX_RUN_WAREHOUSES` in `apps/api/src/modules/mrp/schemas.py`. */
const MAX_RUN_WAREHOUSES = 50

// ------------------------------------------------- warehouse multi-select ---

/**
 * Restricting a run to a few warehouses is normal (a plant planner does not
 * want the other plant's shortages), so the control is multi-select. Choosing
 * nothing means every warehouse, which is what the API does with an omitted
 * `warehouse_ids`.
 */
function WarehouseMultiSelect({
  id,
  value,
  onChange,
  disabled = false,
}: {
  id: string
  value: string[]
  onChange: (next: string[]) => void
  disabled?: boolean
}) {
  const params = { limit: 200, sort_by: 'code', sort_dir: 'asc', with_total: false }
  const warehouses = useQuery({
    queryKey: queryKeys.warehouses.list(params),
    queryFn: () => api.getPage<WarehouseOption>('/warehouses', { params }),
    staleTime: staleTimes.master,
  })

  const rows = useMemo(() => warehouses.data?.data ?? [], [warehouses.data])
  const chosen = useMemo(() => new Set(value), [value])

  const label = useMemo(() => {
    if (value.length === 0) return 'All warehouses'
    if (value.length === 1) {
      const match = rows.find((warehouse) => warehouse.id === value[0])
      return match ? `${match.code} - ${match.name}` : '1 warehouse'
    }
    return `${value.length} warehouses`
  }, [rows, value])

  const toggle = (warehouseId: string) => {
    onChange(
      chosen.has(warehouseId)
        ? value.filter((existing) => existing !== warehouseId)
        : [...value, warehouseId],
    )
  }

  return (
    <div className="flex items-center gap-1">
      <Popover>
        <PopoverTrigger asChild>
          <Button
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            disabled={disabled}
            className={cn(
              'h-9 w-full justify-between font-normal',
              value.length === 0 && 'text-muted-foreground',
            )}
          >
            <span className="truncate">{warehouses.isLoading ? 'Loading...' : label}</span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-80 p-0">
          <p className="border-b px-3 py-2 text-xs text-muted-foreground">
            Leave everything unticked to plan across all warehouses.
          </p>
          <div className="max-h-64 overflow-y-auto p-1">
            {warehouses.isLoading && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                Loading warehouses...
              </p>
            )}
            {!warehouses.isLoading && warehouses.error && (
              <p className="px-2 py-6 text-center text-sm text-destructive">
                {(warehouses.error as Error).message}
              </p>
            )}
            {!warehouses.isLoading && !warehouses.error && rows.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                No warehouses are configured yet.
              </p>
            )}
            {rows.map((warehouse) => (
              <button
                key={warehouse.id}
                type="button"
                role="checkbox"
                aria-checked={chosen.has(warehouse.id)}
                onClick={() => toggle(warehouse.id)}
                className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              >
                <Check
                  className={cn(
                    'mt-0.5 h-4 w-4 shrink-0',
                    !chosen.has(warehouse.id) && 'invisible',
                  )}
                  aria-hidden
                />
                <span className="min-w-0">
                  <span className="block truncate font-medium">{warehouse.code}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {warehouse.name}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </PopoverContent>
      </Popover>

      {value.length > 0 && !disabled && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Clear warehouse selection"
          onClick={() => onChange([])}
        >
          <X />
        </Button>
      )}
    </div>
  )
}

// -------------------------------------------------------------- run form ----

const runSchema = z.object({
  horizon_days: z
    .string()
    .regex(/^\d{1,4}$/, 'Enter a whole number of days.')
    .refine((value) => {
      const days = Number.parseInt(value, 10)
      return days >= 1 && days <= MAX_HORIZON_DAYS
    }, `The planning horizon must be between 1 and ${MAX_HORIZON_DAYS} days.`),
  warehouse_ids: z
    .array(z.string())
    .max(
      MAX_RUN_WAREHOUSES,
      `A run may be scoped to at most ${MAX_RUN_WAREHOUSES} warehouses; leave the list empty to plan across all of them.`,
    ),
  // The engine's own toggle: hold safety stock back from the netting, or let the
  // run plan it away (`engine.planning_available`).
  respect_safety_stock: z.boolean(),
})

type RunForm = z.infer<typeof runSchema>

// ------------------------------------------------------------------ page ----

export default function MrpPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()

  /**
   * The run being watched after a 202. Kept in component state rather than the
   * URL because the run history below is already URL-driven; a planner who
   * reloads mid-run picks the run back up from the history table, which shows
   * it as RUNNING.
   */
  const [watchedRunId, setWatchedRunId] = useState<string | null>(null)
  const [watchStartedAt, setWatchStartedAt] = useState<number | null>(null)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [failedRun, setFailedRun] = useState<MrpRunDetail | null>(null)

  const form = useForm<RunForm>({
    resolver: zodResolver(runSchema),
    defaultValues: { horizon_days: '90', warehouse_ids: [], respect_safety_stock: true },
  })

  const startRun = useMutation({
    mutationFn: (values: RunForm) =>
      api.post<MrpRunCreated>('/mrp/runs', {
        horizon_days: Number.parseInt(values.horizon_days, 10),
        // Omitted rather than empty: an empty array would read as "no warehouse".
        ...(values.warehouse_ids.length > 0 ? { warehouse_ids: values.warehouse_ids } : {}),
        respect_safety_stock: values.respect_safety_stock,
      }),
    onSuccess: (result) => {
      setFailedRun(null)
      void queryClient.invalidateQueries({ queryKey: queryKeys.mrp.all })

      if (isAcceptedRun(result)) {
        const runId = runIdFromStatusUrl(result.status_url) ?? result.job_id
        setWatchedRunId(runId)
        setWatchStartedAt(Date.now())
        toast.show('MRP queued', 'The run is large enough to calculate in the background.')
        return
      }

      if (result.status === 'COMPLETED') {
        toast.success(
          'MRP finished',
          `${result.code} produced ${formatNumber(result.suggestion_count)} suggestions.`,
        )
        navigate(`/mrp/runs/${result.id}`)
        return
      }

      // A run that came back still moving is watched exactly like a 202 one.
      setWatchedRunId(result.id)
      setWatchStartedAt(Date.now())
    },
    onError: (error) => {
      // Only field-level messages are handled here; everything else is already
      // surfaced by the global mutation cache.
      if (!(error instanceof ApiError)) return
      for (const [field, messages] of Object.entries(error.fieldErrors)) {
        form.setError(field as FieldPath<RunForm>, { type: 'server', message: messages.join('. ') })
      }
    },
  })

  const watched = useQuery({
    queryKey: queryKeys.mrp.run(watchedRunId ?? ''),
    queryFn: () => api.get<MrpRunDetail>(`/mrp/runs/${watchedRunId}`),
    enabled: Boolean(watchedRunId),
    staleTime: 0,
    // Poll until the engine reaches a terminal state, then stop dead.
    refetchInterval: (query) =>
      isRunInFlight(query.state.data?.status) ? POLL_INTERVAL_MS : false,
  })

  const watchedRun = watched.data
  useEffect(() => {
    if (!watchedRun || isRunInFlight(watchedRun.status)) return

    setWatchedRunId(null)
    setWatchStartedAt(null)
    void queryClient.invalidateQueries({ queryKey: queryKeys.mrp.all })

    if (watchedRun.status === 'COMPLETED') {
      toast.success(
        'MRP finished',
        `${watchedRun.code} produced ${formatNumber(watchedRun.suggestion_count)} suggestions.`,
      )
      navigate(`/mrp/runs/${watchedRun.id}`)
      return
    }

    setFailedRun(watchedRun)
    toast.error(
      watchedRun.status === 'CANCELLED' ? 'MRP run cancelled' : 'MRP run failed',
      watchedRun.error_message ?? `${watchedRun.code} did not finish. Nothing was created.`,
    )
  }, [watchedRun, navigate, queryClient])

  useEffect(() => {
    if (watchStartedAt === null) {
      setElapsedMs(0)
      return
    }
    setElapsedMs(Date.now() - watchStartedAt)
    const timer = setInterval(() => setElapsedMs(Date.now() - watchStartedAt), 1000)
    return () => clearInterval(timer)
  }, [watchStartedAt])

  const isCalculating = Boolean(watchedRunId)

  // --------------------------------------------------------- run history ---

  const statusFilter = searchParams.get('status') ?? ''

  const runs = useListQuery<MrpRunRow>({
    path: '/mrp/runs',
    queryKey: queryKeys.mrp.runs,
    defaultSortBy: 'started_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    // While a run is in flight the history is the other place its progress
    // shows, so it keeps step with the watcher.
    refetchInterval: isCalculating ? POLL_INTERVAL_MS : false,
    filters: statusFilter ? { status: statusFilter } : {},
  })

  const columns = useMemo<ColumnDef<MrpRunRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Run',
        // A real link, not only a clickable row: this screen is driven from the
        // keyboard all day.
        cell: ({ row }) => (
          <Link
            to={`/mrp/runs/${row.original.id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        accessorKey: 'started_at',
        header: 'Started',
        cell: ({ row }) => formatDateTime(row.original.started_at ?? row.original.created_at),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        // The list row carries no `error_message` - only the run detail does -
        // so a failed run is a badge here and a message when it is opened.
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'mode',
        header: 'Mode',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={row.original.mode === 'ASYNC' ? 'secondary' : 'muted'}>
            {row.original.mode === 'ASYNC' ? 'Background' : 'Immediate'}
          </Badge>
        ),
      },
      {
        accessorKey: 'horizon_days',
        header: 'Horizon',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => `${formatNumber(row.original.horizon_days)}d`,
      },
      {
        accessorKey: 'demand_line_count',
        header: 'Demand lines',
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.demand_line_count),
      },
      {
        accessorKey: 'suggestion_count',
        header: 'Suggestions',
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.suggestion_count),
      },
      {
        id: 'duration',
        header: 'Duration',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          isRunInFlight(row.original.status) ? (
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <Timer className="h-3.5 w-3.5" aria-hidden />
              running
            </span>
          ) : (
            formatDuration(runDurationMs(row.original))
          ),
      },
    ],
    [],
  )

  const errors = form.formState.errors

  return (
    <>
      <PageHeader
        title="Material requirements planning"
        description="Net confirmed demand against stock, incoming purchase orders and work in progress, then propose what to buy, make and send out for job work."
      />

      {can(P.MRP_RUN) && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Run MRP</CardTitle>
            <CardDescription>
              Common parts needed by several orders are merged into one requirement, so the run
              proposes one order for a shared component rather than one per sales order.
            </CardDescription>
          </CardHeader>

          <CardContent>
            <form
              onSubmit={form.handleSubmit((values) => startRun.mutate(values))}
              className="grid gap-4 lg:grid-cols-[10rem_minmax(0,22rem)_minmax(0,1fr)_auto] lg:items-start"
            >
              <div className="space-y-1.5">
                <Label htmlFor="mrp-horizon" required>
                  Horizon (days)
                </Label>
                <Input
                  id="mrp-horizon"
                  inputMode="numeric"
                  autoComplete="off"
                  disabled={isCalculating}
                  aria-invalid={Boolean(errors.horizon_days)}
                  aria-describedby="mrp-horizon-hint"
                  {...form.register('horizon_days')}
                />
                <p id="mrp-horizon-hint" className="text-xs text-muted-foreground">
                  Demand due within this window.
                </p>
                <FieldError message={errors.horizon_days?.message} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="mrp-warehouses">Warehouses</Label>
                <Controller
                  control={form.control}
                  name="warehouse_ids"
                  render={({ field }) => (
                    <WarehouseMultiSelect
                      id="mrp-warehouses"
                      value={field.value}
                      onChange={field.onChange}
                      disabled={isCalculating}
                    />
                  )}
                />
                <p className="text-xs text-muted-foreground">
                  Stock and incoming quantities are netted from these warehouses only.
                </p>
                <FieldError message={errors.warehouse_ids?.message} />
              </div>

              <div className="flex items-start gap-2 lg:pt-7">
                <Controller
                  control={form.control}
                  name="respect_safety_stock"
                  render={({ field }) => (
                    <Checkbox
                      id="mrp-safety-stock"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                      disabled={isCalculating}
                    />
                  )}
                />
                <div>
                  <Label htmlFor="mrp-safety-stock" className="font-normal">
                    Protect safety stock
                  </Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Held back from the netting, so the run does not plan the buffer away.
                  </p>
                </div>
              </div>

              {/* Affordance only - the API authorises the run independently. */}
              <Button
                type="submit"
                loading={startRun.isPending}
                disabled={isCalculating}
                className="lg:mt-6"
              >
                <Play />
                Run MRP
              </Button>
            </form>

            {isCalculating && (
              <Alert variant="info" className="mt-4">
                <AlertTitle className="flex items-center gap-2">
                  <Timer className="h-4 w-4 animate-pulse" aria-hidden />
                  Calculating...
                </AlertTitle>
                <AlertDescription>
                  <span role="status">
                    {watchedRun?.code ? `${watchedRun.code} is ` : 'The run is '}
                    {watchedRun?.status
                      ? humanise(watchedRun.status).toLowerCase()
                      : 'queued'} -{' '}
                    <span className="tabular font-medium text-foreground">
                      {formatDuration(elapsedMs)}
                    </span>{' '}
                    elapsed.
                  </span>
                  <span className="mt-1 block">
                    This runs on the server; you can keep working and come back. The run opens
                    automatically when it finishes.
                  </span>
                </AlertDescription>
              </Alert>
            )}

            {failedRun && !isCalculating && (
              <Alert variant="destructive" className="mt-4">
                <AlertTitle className="flex items-center gap-2">
                  <TriangleAlert className="h-4 w-4" aria-hidden />
                  {failedRun.code} {failedRun.status === 'CANCELLED' ? 'was cancelled' : 'failed'}
                </AlertTitle>
                <AlertDescription>
                  {failedRun.error_message ??
                    'The engine stopped before producing suggestions. Nothing was created.'}
                  <span className="mt-3 flex items-center gap-2">
                    <Button variant="outline" size="sm" asChild>
                      <Link to={`/mrp/runs/${failedRun.id}`}>Open run</Link>
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setFailedRun(null)}>
                      Dismiss
                    </Button>
                  </span>
                </AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      )}

      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Run history
      </h2>

      <DataTable
        columns={columns}
        data={runs.data}
        pagination={runs.pagination}
        isLoading={runs.isLoading}
        isFetching={runs.isFetching}
        error={runs.error}
        onPageChange={runs.setPage}
        onLimitChange={runs.setLimit}
        onSortChange={runs.setSort}
        onSearchChange={runs.setSearch}
        onRefresh={() => void runs.refetch()}
        searchValue={runs.search}
        searchPlaceholder="Search run code..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/mrp/runs/${row.id}`)}
        emptyTitle="No MRP runs yet"
        emptyDescription={
          can(P.MRP_RUN)
            ? 'Set a horizon above and run MRP to see what the order book needs.'
            : 'Nothing matches the current filters.'
        }
        toolbar={
          <>
            <Select
              value={statusFilter || ALL}
              onValueChange={(value) => runs.setParam('status', value === ALL ? undefined : value)}
            >
              <SelectTrigger className="w-[180px]" aria-label="Filter runs by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All statuses</SelectItem>
                {MRP_RUN_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {statusFilter && (
              <Button variant="ghost" size="sm" onClick={() => runs.setParam('status', undefined)}>
                <X />
                Clear
              </Button>
            )}

            <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:inline-flex">
              <WarehouseIcon className="h-3.5 w-3.5" aria-hidden />
              Open a run to see and convert its suggestions.
            </span>
          </>
        }
      />
    </>
  )
}

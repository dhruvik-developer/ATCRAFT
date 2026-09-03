/**
 * Report catalogue.
 *
 * The catalogue is served by the API, filtered to what this user may run, and it
 * carries each report's title, whether it needs a period, and its result
 * columns. That is what keeps this screen from being a hardcoded list that
 * drifts out of step with the server - a report added in the backend appears
 * here on the next page load, and its grid is built from the columns the API
 * declared rather than from anything typed out here.
 *
 * Three contract facts shape the runner, and none of them is a choice this
 * screen gets to make:
 *
 * 1. **The filter set is uniform.** Every report route binds the same
 *    `report_filters` dependency, and the descriptor declares no per-report
 *    parameters. So the same controls are offered for every report, and the
 *    ones a given report ignores simply change nothing.
 * 2. **A period is required only where the descriptor says so.** `date_from`
 *    and `date_to` are validated as a pair and bounded at 366 days; a position
 *    report takes no dates at all and is not sent any.
 * 3. **The server decides inline or background.** A result inside the row limit
 *    comes back paginated; one past it comes back 202 with a job to poll. There
 *    is no parameter to force either and no flag to read in advance, so the
 *    screen watches for the job envelope and switches to the download strip
 *    when it arrives.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { differenceInCalendarDays, subDays } from 'date-fns'
import { ArrowLeft, FileBarChart, Play } from 'lucide-react'
import { useEffect, useMemo } from 'react'
import { useForm } from 'react-hook-form'
import { useSearchParams } from 'react-router-dom'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { EmptyState, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatQuantity,
  toDateInput,
} from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { ReportJobPanel } from './job-panel'
import { useReportJob, type AcceptedJobData } from './report-jobs'
import {
  PERIOD_GROUP,
  POSITION_GROUP,
  groupOf,
  reportPath,
  type ReportColumn,
  type ReportDescriptor,
  type ReportRow,
} from './reports-types'

const DEFAULT_RANGE_DAYS = 30
/** `REPORT_MAX_RANGE_DAYS` in the API. Checked here so the trip is not wasted. */
const MAX_RANGE_DAYS = 366

/**
 * The filters every report route accepts.
 *
 * Taken from `report_filters` in the API rather than from the descriptor, which
 * declares none: the dependency is shared by every route, so this list is the
 * contract. `category_id` is accepted too but has no picker in this app yet, so
 * it is not offered rather than being offered as a UUID box.
 */
type FilterKind = 'WAREHOUSE' | 'ITEM' | 'VENDOR' | 'CUSTOMER' | 'STATUS' | 'MIN_DAYS'

const FILTERS: { name: string; label: string; kind: FilterKind; help?: string }[] = [
  { name: 'warehouse_id', label: 'Warehouse', kind: 'WAREHOUSE' },
  { name: 'item_id', label: 'Item', kind: 'ITEM' },
  { name: 'vendor_id', label: 'Vendor', kind: 'VENDOR' },
  { name: 'customer_id', label: 'Customer', kind: 'CUSTOMER' },
  { name: 'status', label: 'Status', kind: 'STATUS', help: 'Exact status code, where the report has one' },
  {
    name: 'min_days',
    label: 'Age threshold (days)',
    kind: 'MIN_DAYS',
    help: 'Where the report ages something',
  },
]

export default function ReportsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedKey = searchParams.get('report') ?? ''

  /**
   * The catalogue is a bounded, permission-filtered reference list - a few dozen
   * descriptors - so it is read whole rather than paged. It is not a data list.
   */
  const catalogue = useQuery({
    queryKey: ['reports', 'catalogue'],
    queryFn: () => api.get<ReportDescriptor[]>('/reports/catalogue'),
    staleTime: staleTimes.reference,
  })

  const definitions = useMemo(() => catalogue.data ?? [], [catalogue.data])
  const definition = definitions.find((entry) => entry.key === selectedKey) ?? null

  const grouped = useMemo(() => {
    const order = [PERIOD_GROUP, POSITION_GROUP]
    const byGroup = new Map<string, ReportDescriptor[]>()
    for (const entry of definitions) {
      const group = groupOf(entry)
      const bucket = byGroup.get(group)
      if (bucket) bucket.push(entry)
      else byGroup.set(group, [entry])
    }
    return [...byGroup.entries()]
      .sort(([a], [b]) => order.indexOf(a) - order.indexOf(b))
      .map(([group, reports]) => ({
        group,
        reports: [...reports].sort((a, b) => a.title.localeCompare(b.title)),
      }))
  }, [definitions])

  const openReport = (key: string) => {
    setSearchParams(new URLSearchParams({ report: key }))
  }

  const backToCatalogue = () => {
    setSearchParams(new URLSearchParams())
  }

  return (
    <div>
      <PageHeader
        title={definition ? definition.title : 'Reports'}
        description={
          definition
            ? definition.description
            : 'Every report the system can produce, split by whether it covers a period or the position as it stands.'
        }
        breadcrumbs={
          definition
            ? [{ label: 'Reports', to: '/reports' }, { label: definition.title }]
            : undefined
        }
        actions={
          definition && (
            <Button variant="outline" onClick={backToCatalogue}>
              <ArrowLeft />
              All reports
            </Button>
          )
        }
      />

      {catalogue.isLoading && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-28 w-full" />
          ))}
        </div>
      )}

      {catalogue.error && !catalogue.isLoading && (
        <Alert variant="destructive">
          <AlertTitle>The report catalogue could not be loaded</AlertTitle>
          <AlertDescription>{catalogue.error.message}</AlertDescription>
        </Alert>
      )}

      {!catalogue.isLoading && !catalogue.error && selectedKey && !definition && (
        <Alert variant="warning">
          <AlertTitle>That report is not available</AlertTitle>
          <AlertDescription>
            The catalogue has no report called &quot;{selectedKey}&quot;, or your account may not
            run it.
          </AlertDescription>
        </Alert>
      )}

      {definition && <ReportRunner key={definition.key} definition={definition} />}

      {!definition && !catalogue.isLoading && !catalogue.error && (
        <>
          {definitions.length === 0 ? (
            <EmptyState
              icon={FileBarChart}
              title="No reports are available to you"
              description="Your account does not have access to any report in the catalogue."
            />
          ) : (
            <div className="space-y-7">
              {grouped.map((group) => (
                <section key={group.group} aria-labelledby={`group-${group.group}`}>
                  <h2
                    id={`group-${group.group}`}
                    className="mb-2.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    {group.group}
                  </h2>
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {group.reports.map((report) => (
                      <ReportCard
                        key={report.key}
                        definition={report}
                        onOpen={() => openReport(report.key)}
                      />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ------------------------------------------------------------ catalogue -----

function ReportCard({ definition, onOpen }: { definition: ReportDescriptor; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="rounded-lg border bg-card p-4 text-left shadow-sm transition-colors hover:border-primary/40 hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="flex items-start justify-between gap-2">
        <span className="font-medium">{definition.title}</span>
        {definition.requires_period && (
          <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-2xs text-muted-foreground">
            Needs dates
          </span>
        )}
      </span>
      <span className="mt-1 block text-sm text-muted-foreground">{definition.description}</span>
    </button>
  )
}

// ---------------------------------------------------------------- runner ----

interface RunFormValues {
  date_from: string
  date_to: string
  /** The uniform filter set, by parameter name. */
  values: Record<string, string>
}

function buildSchema(requiresPeriod: boolean): z.ZodType<RunFormValues> {
  return z
    .object({
      date_from: z.string(),
      date_to: z.string(),
      values: z.record(z.string()),
    })
    .superRefine((data, ctx) => {
      if (!requiresPeriod) return
      if (!data.date_from) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['date_from'],
          message: 'A start date is required',
        })
      }
      if (!data.date_to) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['date_to'],
          message: 'An end date is required',
        })
      }
      if (!data.date_from || !data.date_to) return
      // ISO dates compare correctly as strings, which is why the API takes them
      // in that shape.
      if (data.date_from > data.date_to) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['date_to'],
          message: 'The end date cannot be before the start date',
        })
      } else if (
        differenceInCalendarDays(new Date(data.date_to), new Date(data.date_from)) + 1 >
        MAX_RANGE_DAYS
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['date_to'],
          message: `A report period may cover at most ${MAX_RANGE_DAYS} days`,
        })
      }
    })
}

function ReportRunner({ definition }: { definition: ReportDescriptor }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const job = useReportJob()

  const requiresPeriod = definition.requires_period
  const schema = useMemo(() => buildSchema(requiresPeriod), [requiresPeriod])

  const form = useForm<RunFormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      date_from: requiresPeriod
        ? searchParams.get('date_from') || toDateInput(subDays(new Date(), DEFAULT_RANGE_DAYS))
        : '',
      date_to: requiresPeriod ? searchParams.get('date_to') || toDateInput(new Date()) : '',
      values: Object.fromEntries(
        FILTERS.map((filter) => [filter.name, searchParams.get(filter.name) ?? '']),
      ),
    },
  })

  const hasRun = searchParams.get('run') === '1'

  const applied = useMemo(() => {
    const filters: Record<string, unknown> = {}
    if (requiresPeriod) {
      filters.date_from = searchParams.get('date_from') ?? ''
      filters.date_to = searchParams.get('date_to') ?? ''
    }
    for (const filter of FILTERS) {
      const value = searchParams.get(filter.name)
      if (value) filters[filter.name] = value
    }
    return filters
  }, [searchParams, requiresPeriod])

  const list = useListQuery<ReportRow>({
    path: reportPath(definition.key),
    queryKey: (params) => queryKeys.reports.report(definition.key, params),
    filters: applied,
    // A report is only fetched once it has been asked for, and a period report
    // never without both ends of its range - half a range is a 422.
    enabled:
      hasRun && (!requiresPeriod || Boolean(applied.date_from)) && (!requiresPeriod || Boolean(applied.date_to)),
    staleTime: staleTimes.transactional,
  })

  /**
   * The same route answers 200 with a page of rows or 202 with a job. The
   * envelope is the only tell: a paginated answer carries an array, an accepted
   * one carries the job description. Reading it this way keeps the escalation
   * working without asking the client to guess in advance which it will get.
   */
  const payload = list.data as unknown
  const rows = Array.isArray(payload) ? (payload as ReportRow[]) : []
  const accepted =
    !Array.isArray(payload) && payload !== null && typeof payload === 'object' &&
    'status_url' in (payload as Record<string, unknown>)
      ? (payload as AcceptedJobData)
      : null

  const { start } = job
  useEffect(() => {
    if (accepted) start(accepted)
  }, [accepted, start])

  const runInline = form.handleSubmit((values) => {
    const next = new URLSearchParams({ report: definition.key, run: '1' })
    if (requiresPeriod) {
      next.set('date_from', values.date_from)
      next.set('date_to', values.date_to)
    }
    for (const filter of FILTERS) {
      const value = (values.values[filter.name] ?? '').trim()
      if (value) next.set(filter.name, value)
    }
    // Replacing the whole query string resets paging and the search box, which
    // belong to the previous run rather than this one.
    setSearchParams(next, { replace: true })
  })

  const errors = form.formState.errors
  const valueErrors = errors.values as unknown as
    | Record<string, { message?: string } | undefined>
    | undefined

  const columns = useMemo<ColumnDef<ReportRow, unknown>[]>(
    // Identifier columns are declared by the API but are UUIDs; they identify a
    // row rather than telling a reader anything, so they key the row instead of
    // filling a column with them.
    () => definition.columns.filter((column) => column.kind !== 'id').map(toColumn),
    [definition],
  )

  const idKeys = useMemo(
    () => definition.columns.filter((column) => column.kind === 'id').map((column) => column.name),
    [definition],
  )

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Parameters</CardTitle>
          <p className="text-xs text-muted-foreground">
            {requiresPeriod
              ? `This report covers a period, so both dates are required and the range may span at most ${MAX_RANGE_DAYS} days. The default is the last ${DEFAULT_RANGE_DAYS} days.`
              : 'This report reads the position as it stands and takes no dates. The filters below narrow it where it uses them.'}
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={runInline} className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {requiresPeriod && (
                <>
                  <div className="space-y-1.5">
                    <Label htmlFor="report-date-from" required>
                      From
                    </Label>
                    <Input
                      id="report-date-from"
                      type="date"
                      aria-invalid={Boolean(errors.date_from)}
                      {...form.register('date_from')}
                    />
                    <FieldError message={errors.date_from?.message} />
                  </div>

                  <div className="space-y-1.5">
                    <Label htmlFor="report-date-to" required>
                      To
                    </Label>
                    <Input
                      id="report-date-to"
                      type="date"
                      aria-invalid={Boolean(errors.date_to)}
                      {...form.register('date_to')}
                    />
                    <FieldError message={errors.date_to?.message} />
                  </div>
                </>
              )}

              {FILTERS.map((filter) => (
                <FilterField
                  key={filter.name}
                  filter={filter}
                  value={form.watch(`values.${filter.name}` as `values.${string}`) ?? ''}
                  onChange={(next) =>
                    form.setValue(`values.${filter.name}` as `values.${string}`, next, {
                      shouldValidate: form.formState.isSubmitted,
                    })
                  }
                  error={valueErrors?.[filter.name]?.message}
                />
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit" loading={list.isFetching && hasRun}>
                <Play />
                Run report
              </Button>
              <span className="text-2xs text-muted-foreground">
                A result too large to answer inline is generated as a file instead - the download
                appears below when it is ready.
              </span>
            </div>
          </form>
        </CardContent>
      </Card>

      <ReportJobPanel controller={job} label={definition.title} />

      {!hasRun ? (
        <EmptyState
          icon={Play}
          title="Set the parameters and run the report"
          description="Nothing is fetched until you ask for it, so an unbounded query never reaches the database."
        />
      ) : accepted ? null : (
        <DataTable
          columns={columns}
          data={rows}
          pagination={list.pagination}
          isLoading={list.isLoading}
          isFetching={list.isFetching}
          error={list.error}
          onPageChange={list.setPage}
          onLimitChange={list.setLimit}
          onSearchChange={list.setSearch}
          onRefresh={() => void list.refetch()}
          searchValue={list.search}
          searchPlaceholder="Search results"
          getRowId={(row) =>
            idKeys.length > 0
              ? idKeys.map((key) => String(row[key] ?? '')).join(':')
              : JSON.stringify(row)
          }
          virtualise={rows.length > 150}
          maxHeight="60vh"
          emptyTitle="No rows for these parameters"
          emptyDescription="Widen the date range or clear a filter and run it again."
        />
      )}
    </div>
  )
}

// ------------------------------------------------------------ parameters ----

function FilterField({
  filter,
  value,
  onChange,
  error,
}: {
  filter: (typeof FILTERS)[number]
  value: string
  onChange: (next: string) => void
  error?: string
}) {
  const id = `report-filter-${filter.name}`

  if (filter.kind === 'WAREHOUSE') {
    return (
      <div className="space-y-1.5">
        <WarehouseSelect
          id={id}
          label={filter.label}
          includeAll
          allLabel="All warehouses"
          value={value}
          onChange={onChange}
          invalid={Boolean(error)}
        />
        <FieldError message={error} />
      </div>
    )
  }

  if (filter.kind === 'ITEM') {
    return (
      <div className="space-y-1.5">
        <ItemPicker
          id={id}
          label={filter.label}
          value={value || null}
          onChange={(next) => onChange(next ?? '')}
          invalid={Boolean(error)}
        />
        <FieldError message={error} />
      </div>
    )
  }

  if (filter.kind === 'VENDOR') {
    return (
      <div className="space-y-1.5">
        <VendorPicker
          id={id}
          label={filter.label}
          value={value || null}
          onChange={(next) => onChange(next ?? '')}
          invalid={Boolean(error)}
        />
        <FieldError message={error} />
      </div>
    )
  }

  if (filter.kind === 'CUSTOMER') {
    return (
      <div className="space-y-1.5">
        <CustomerPicker
          id={id}
          label={filter.label}
          value={value || null}
          onChange={(next) => onChange(next ?? '')}
          invalid={Boolean(error)}
        />
        <FieldError message={error} />
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{filter.label}</Label>
      <Input
        id={id}
        type={filter.kind === 'MIN_DAYS' ? 'number' : 'text'}
        min={filter.kind === 'MIN_DAYS' ? 0 : undefined}
        max={filter.kind === 'MIN_DAYS' ? 3650 : undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={Boolean(error)}
      />
      {filter.help && <p className="text-xs text-muted-foreground">{filter.help}</p>}
      <FieldError message={error} />
    </div>
  )
}

// --------------------------------------------------------------- results ----

/**
 * Money, quantity and percentage all arrive as `number` in the descriptor - the
 * API says so explicitly, because the value is an exact Decimal either way and
 * the distinction is presentational. The column name carries it, which is the
 * same convention the server used to name the field.
 */
function numericFormat(name: string): 'money' | 'quantity' | 'percent' | 'plain' {
  // The API names every proportion `*_percent` and already scales it to 0-100.
  if (/_percent$/.test(name)) return 'percent'
  if (/(_quantity|_qty)$/.test(name)) return 'quantity'
  if (/(_amount|_value|_cost|_price|_total)$/.test(name)) return 'money'
  return 'plain'
}

function toColumn(column: ReportColumn): ColumnDef<ReportRow, unknown> {
  const numeric = column.kind === 'number' || column.kind === 'int'

  return {
    id: column.name,
    accessorKey: column.name,
    header: column.label,
    // The report routes take no sort parameter, so the header never offers one:
    // a sort the server ignores would silently reorder nothing.
    enableSorting: false,
    meta: { align: numeric ? 'right' : undefined },
    cell: ({ row }) => renderValue(column, row.original[column.name]),
  }
}

function renderValue(column: ReportColumn, value: ReportRow[string] | undefined) {
  if (value === null || value === undefined || value === '') {
    return <span className="text-muted-foreground">-</span>
  }

  switch (column.kind) {
    case 'number': {
      // Decimal strings. Never parsed to a float for display or arithmetic.
      const decimal = typeof value === 'boolean' ? null : value
      switch (numericFormat(column.name)) {
        case 'money':
          return formatMoney(decimal)
        case 'quantity':
          return formatQuantity(decimal)
        case 'percent':
          return formatPercent(decimal)
        default:
          return formatQuantity(decimal)
      }
    }
    case 'int':
      return typeof value === 'number' ? formatNumber(value) : String(value)
    case 'bool':
      return value ? 'Yes' : 'No'
    case 'date':
      return formatDate(String(value))
    case 'datetime':
      return formatDateTime(String(value))
    case 'id':
      return <span className="font-mono text-2xs text-muted-foreground">{String(value)}</span>
    default: {
      const text = String(value)
      if (/(^|_)(status|state)$/.test(column.name)) return <StatusBadge status={text} />
      if (/_code$/.test(column.name)) return <span className="font-mono text-xs">{text}</span>
      return <span className="block max-w-[24rem] truncate">{text}</span>
    }
  }
}

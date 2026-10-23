import { describeError } from '@/lib/messages'
/**
 * Audit trail.
 *
 * Two ways of reading the same append-only table:
 *
 * * **Activity** - a bounded window of everything that happened, filtered by
 *   entity, action and actor. Keyset-paginated, because `audit_logs` grows
 *   without bound and an offset page deep into it would make PostgreSQL walk
 *   and discard every row before it.
 * * **Entity history** - the full ordered trail of one record, which is the
 *   question a traceability review actually asks ("what happened to this
 *   purchase order?").
 *
 * The date range is mandatory in both directions and defaults to the last
 * thirty days. An unbounded audit query is a full-history scan, and the one
 * screen whose job is accountability must not be the one that takes the
 * database down.
 */

import { useInfiniteQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { subDays } from 'date-fns'
import { ChevronDown, ChevronRight, History, ScrollText, Search, X } from 'lucide-react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, EmptyState, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
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
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useUserOptions } from '@/hooks/use-user-options'
import { DEFAULT_PAGE_SIZE, SELECT_ALL } from '@/lib/constants'
import { formatDateTime, humanise, toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import {
  actionTone,
  AUDIT_ACTIONS,
  AUDIT_ENTITY_TYPES,
  changedFields,
  entityLink,
  fetchAuditPage,
  formatAuditValue,
  useAuditLogDetail,
  type AuditActorOption,
  type AuditLogRow,
} from './audit-types'

/** Rows per cursor page - the same size a list screen shows by default. */
const PAGE_SIZE = DEFAULT_PAGE_SIZE
const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/

type Mode = 'activity' | 'entity'

// --------------------------------------------------------------- detail -----

/**
 * The before/after panel.
 *
 * Only changed fields are shown, because that is all the API stores: whole-row
 * snapshots on every update would bloat the table without making a single
 * investigation easier.
 */
function AuditDetail({ row }: { row: AuditLogRow }) {
  // The list row carries no before/after values; they are on the full entry.
  const detail = useAuditLogDetail(row.id)
  const full = detail.data
  const changes = full ? changedFields(full) : []
  const link = entityLink(row.entity_type, row.entity_id)

  return (
    <Card className="mt-4">
      <CardHeader className="space-y-1.5">
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Badge variant={actionTone(row.action)}>{humanise(row.action)}</Badge>
          <span>{humanise(row.entity_type)}</span>
          {row.entity_code && (
            <span className="font-mono text-sm font-normal text-muted-foreground">
              {row.entity_code}
            </span>
          )}
        </CardTitle>
        <p className="text-sm text-muted-foreground">{row.summary}</p>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 pt-1 text-xs text-muted-foreground">
          <div className="flex gap-1.5">
            <dt>When</dt>
            <dd className="text-foreground">{formatDateTime(row.created_at)}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt>Who</dt>
            <dd className="text-foreground">{row.actor_label ?? 'System'}</dd>
          </div>
          {full?.ip_address && (
            <div className="flex min-w-0 gap-1.5">
              <dt className="shrink-0">From</dt>
              <dd className="break-all font-mono text-foreground">{full.ip_address}</dd>
            </div>
          )}
          {row.request_id && (
            <div className="flex min-w-0 gap-1.5">
              <dt className="shrink-0">Request</dt>
              <dd className="break-all font-mono text-foreground">{row.request_id}</dd>
            </div>
          )}
          {link && (
            <div className="flex gap-1.5">
              <dt>Record</dt>
              <dd>
                <Link to={link} className="text-primary hover:underline">
                  Open
                </Link>
              </dd>
            </div>
          )}
        </dl>
      </CardHeader>

      <CardContent>
        {detail.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : detail.error ? (
          <Alert variant="warning">
            <AlertTitle>Could not load what changed</AlertTitle>
            <AlertDescription>{describeError(detail.error)}</AlertDescription>
          </Alert>
        ) : changes.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            This action recorded no field-level change - it is an event rather than an edit.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-sm">
              <caption className="sr-only">Fields changed by this action</caption>
              <thead className="bg-muted/50">
                <tr className="border-b">
                  <th
                    scope="col"
                    className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    Field
                  </th>
                  <th
                    scope="col"
                    className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    Before
                  </th>
                  <th
                    scope="col"
                    className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    After
                  </th>
                </tr>
              </thead>
              <tbody>
                {changes.map((change) => (
                  <tr key={change.field} className="border-b last:border-0 align-top">
                    <td className="px-3 py-2 font-medium">{humanise(change.field)}</td>
                    <td className="px-3 py-2 text-muted-foreground line-through decoration-destructive/50">
                      {formatAuditValue(change.before)}
                    </td>
                    <td className="px-3 py-2 font-medium text-success">
                      {formatAuditValue(change.after)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {full?.context && Object.keys(full.context).length > 0 && (
          <div className="mt-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Context
            </p>
            <dl className="mt-1.5 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
              {Object.entries(full.context).map(([key, value]) => (
                <div key={key} className="flex min-w-0 gap-1.5">
                  <dt className="shrink-0 text-muted-foreground">{humanise(key)}</dt>
                  <dd className="truncate">{formatAuditValue(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------- trail -----

/**
 * Shared table for both modes.
 *
 * The trail and the activity feed carry identical rows, so they get identical
 * columns; only the query behind them differs.
 */
function AuditTable({
  rows,
  isLoading,
  isFetching,
  error,
  onRefresh,
  expandedId,
  onToggleExpand,
  onShowHistory,
  toolbar,
  searchValue,
  onSearchChange,
  emptyTitle,
  emptyDescription,
}: {
  rows: AuditLogRow[]
  isLoading: boolean
  isFetching: boolean
  error: Error | null
  onRefresh: () => void
  expandedId: string | null
  onToggleExpand: (id: string) => void
  onShowHistory?: (row: AuditLogRow) => void
  toolbar?: ReactNode
  searchValue?: string
  onSearchChange?: (term: string) => void
  emptyTitle: string
  emptyDescription: string
}) {
  const columns = useMemo<ColumnDef<AuditLogRow, unknown>[]>(() => {
    const defs: ColumnDef<AuditLogRow, unknown>[] = [
      {
        id: 'expander',
        header: '',
        enableSorting: false,
        enableHiding: false,
        size: 44,
        cell: ({ row }) => {
          const open = row.original.id === expandedId
          return (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-expanded={open}
              aria-label={open ? 'Hide changed values' : 'Show changed values'}
              onClick={(event) => {
                event.stopPropagation()
                onToggleExpand(row.original.id)
              }}
            >
              {open ? <ChevronDown /> : <ChevronRight />}
            </Button>
          )
        },
      },
      {
        id: 'created_at',
        header: 'When',
        enableSorting: false,
        cell: ({ row }) => (
          <time dateTime={row.original.created_at} className="whitespace-nowrap tabular">
            {formatDateTime(row.original.created_at)}
          </time>
        ),
      },
      {
        id: 'actor',
        header: 'User',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[12rem] truncate">
            {row.original.actor_label ?? <span className="text-muted-foreground">System</span>}
          </span>
        ),
      },
      {
        id: 'action',
        header: 'Action',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={actionTone(row.original.action)}>{humanise(row.original.action)}</Badge>
        ),
      },
      {
        id: 'entity_type',
        header: 'Entity',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{humanise(row.original.entity_type)}</span>
        ),
      },
      {
        id: 'entity_code',
        header: 'Code',
        enableSorting: false,
        cell: ({ row }) => {
          const { entity_code, entity_type, entity_id } = row.original
          if (!entity_code) return <span className="text-muted-foreground">-</span>
          const href = entityLink(entity_type, entity_id)
          return href ? (
            <Link to={href} className="font-mono text-xs hover:underline">
              {entity_code}
            </Link>
          ) : (
            <span className="font-mono text-xs">{entity_code}</span>
          )
        },
      },
      {
        id: 'summary',
        header: 'Summary',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[26rem] truncate" title={row.original.summary}>
            {row.original.summary}
          </span>
        ),
      },
      {
        id: 'request_id',
        header: 'Request id',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.request_id ? (
            <span
              className="font-mono text-xs text-muted-foreground"
              title={row.original.request_id}
            >
              {row.original.request_id.slice(0, 8)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ]

    if (onShowHistory) {
      defs.push({
        id: 'history',
        header: '',
        enableSorting: false,
        enableHiding: false,
        size: 56,
        cell: ({ row }) =>
          row.original.entity_id ? (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Full history of ${row.original.entity_code ?? humanise(row.original.entity_type)}`}
              onClick={(event) => {
                event.stopPropagation()
                onShowHistory(row.original)
              }}
            >
              <History />
            </Button>
          ) : null,
      })
    }

    return defs
  }, [expandedId, onShowHistory, onToggleExpand])

  const expanded = rows.find((row) => row.id === expandedId) ?? null

  return (
    <>
      <DataTable
        columns={columns}
        data={rows}
        isLoading={isLoading}
        isFetching={isFetching}
        error={error}
        onRefresh={onRefresh}
        onRowClick={(row) => onToggleExpand(row.id)}
        getRowId={(row) => row.id}
        searchValue={searchValue}
        onSearchChange={onSearchChange}
        searchPlaceholder="Find by request id..."
        toolbar={toolbar}
        virtualise={rows.length > 150}
        maxHeight="65svh"
        emptyTitle={emptyTitle}
        emptyDescription={emptyDescription}
      />

      {expanded && <AuditDetail row={expanded} />}
    </>
  )
}

function LoadMore({
  count,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  noun,
}: {
  count: number
  hasNextPage: boolean
  isFetchingNextPage: boolean
  onLoadMore: () => void
  noun: string
}) {
  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
      <p className="text-muted-foreground" role="status">
        {count === 0 ? `No ${noun}` : `${count} ${noun} loaded`}
      </p>
      {hasNextPage ? (
        <Button variant="outline" size="sm" onClick={onLoadMore} loading={isFetchingNextPage}>
          Load more
        </Button>
      ) : (
        count > 0 && <p className="text-xs text-muted-foreground">End of the trail.</p>
      )}
    </div>
  )
}

// ------------------------------------------------------------ entity mode ---

function EntityHistory({
  entityType,
  entityId,
  onEntityTypeChange,
  onEntityIdChange,
}: {
  entityType: string
  entityId: string
  onEntityTypeChange: (value: string) => void
  onEntityIdChange: (value: string) => void
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [draftId, setDraftId] = useState(entityId)

  const looksLikeId = UUID_PATTERN.test(entityId.trim())

  const trail = useInfiniteQuery({
    queryKey: queryKeys.audit.entity(entityType, entityId),
    queryFn: ({ pageParam }) =>
      fetchAuditPage(`/audit/entity/${entityType}/${entityId}`, {
        limit: PAGE_SIZE,
        ...(pageParam ? { cursor: pageParam } : {}),
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    // Audit rows are immutable, so a fetched page can never change; only the
    // head of the trail moves.
    staleTime: staleTimes.transactional,
    enabled: Boolean(entityType) && looksLikeId,
  })

  const rows = useMemo(() => trail.data?.pages.flatMap((page) => page.data) ?? [], [trail.data])

  const toggleExpand = useCallback(
    (id: string) => setExpandedId((current) => (current === id ? null : id)),
    [],
  )

  return (
    <div>
      <form
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          onEntityIdChange(draftId.trim())
        }}
      >
        <div className="w-full space-y-1.5 sm:w-auto">
          <Label htmlFor="history-entity-type">Record type</Label>
          <Select value={entityType} onValueChange={onEntityTypeChange}>
            <SelectTrigger id="history-entity-type" className="w-full sm:w-[220px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {AUDIT_ENTITY_TYPES.map((type) => (
                <SelectItem key={type} value={type}>
                  {humanise(type)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="w-full space-y-1.5 sm:w-auto">
          <Label htmlFor="history-entity-id">Record id</Label>
          <Input
            id="history-entity-id"
            value={draftId}
            onChange={(event) => setDraftId(event.target.value)}
            placeholder="0193f2a1-0000-7000-8000-000000000000"
            autoComplete="off"
            spellCheck={false}
            className="w-full max-w-full font-mono text-xs sm:w-[24rem]"
          />
        </div>

        <Button type="submit" variant="outline">
          <Search />
          Show trail
        </Button>
      </form>

      {!looksLikeId ? (
        <EmptyState
          icon={History}
          title="Pick a record"
          description="Choose a record type and paste its id. Every screen that lists a record can hand you one, and the history icon in the activity feed fills both fields for you."
        />
      ) : (
        <>
          <AuditTable
            rows={rows}
            isLoading={trail.isLoading}
            isFetching={trail.isFetching && !trail.isFetchingNextPage}
            error={trail.error as Error | null}
            onRefresh={() => void trail.refetch()}
            expandedId={expandedId}
            onToggleExpand={toggleExpand}
            emptyTitle="Nothing recorded for this record"
            emptyDescription="Either the id belongs to another company, or nothing has happened to it yet."
          />
          <LoadMore
            count={rows.length}
            hasNextPage={Boolean(trail.hasNextPage)}
            isFetchingNextPage={trail.isFetchingNextPage}
            onLoadMore={() => void trail.fetchNextPage()}
            noun="entries"
          />
        </>
      )}
    </div>
  )
}

// ------------------------------------------------------------------ page ----

export default function AuditPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [expandedId, setExpandedId] = useState<string | null>(null)

  /**
   * Change several parameters in ONE navigation.
   *
   * `setSearchParams` is not a React state setter: every functional update in a
   * tick is computed from the value captured at the last render, so calling the
   * single-key helper in a loop writes the last key and discards the rest. The
   * "Clear" button did exactly that and cleared one filter of three.
   */
  const setParams = useCallback(
    (next: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (!value) updated.delete(key)
            else updated.set(key, value)
          }
          return updated
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const setParam = useCallback(
    (key: string, value: string | undefined) => setParams({ [key]: value }),
    [setParams],
  )

  // Thirty days is the window an investigation almost always wants. It is
  // widened by hand, never by leaving a field blank.
  const defaultFrom = useMemo(() => toDateInput(subDays(new Date(), 30)), [])
  const today = useMemo(() => toDateInput(new Date()), [])

  const mode: Mode = searchParams.get('mode') === 'entity' ? 'entity' : 'activity'
  const entityType = searchParams.get('entity_type') ?? ''
  const entityId = searchParams.get('entity_id') ?? ''
  const action = searchParams.get('action') ?? ''
  const userId = searchParams.get('user_id') ?? ''
  // `GET /audit/logs` takes no free-text search. It does take an exact
  // `request_id`, which is what an investigator holding a correlation id from a
  // support ticket actually needs, so the box searches that instead of
  // pretending to grep summaries the endpoint never filtered on.
  const requestId = searchParams.get('request_id') ?? ''
  const dateFrom = searchParams.get('from') || defaultFrom
  const dateTo = searchParams.get('to') || today

  // `activeOnly: false` on purpose: a log entry written by someone who has
  // since left the company still needs their name against it, so the actor
  // filter is the one picker that must offer deactivated staff.
  const actors = useUserOptions<AuditActorOption>({
    activeOnly: false,
    enabled: mode === 'activity' && can(P.USER_VIEW),
  })

  const filters = useMemo(
    () => ({
      date_from: dateFrom,
      date_to: dateTo,
      ...(entityType ? { entity_type: entityType } : {}),
      ...(action ? { action } : {}),
      ...(userId ? { user_id: userId } : {}),
      ...(requestId ? { request_id: requestId } : {}),
      limit: PAGE_SIZE,
    }),
    [dateFrom, dateTo, entityType, action, userId, requestId],
  )

  const logs = useInfiniteQuery({
    queryKey: queryKeys.audit.logs(filters),
    queryFn: ({ pageParam }) =>
      fetchAuditPage('/audit/logs', { ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    staleTime: staleTimes.transactional,
    enabled: mode === 'activity',
  })

  const rows = useMemo(() => logs.data?.pages.flatMap((page) => page.data) ?? [], [logs.data])

  const toggleExpand = useCallback(
    (id: string) => setExpandedId((current) => (current === id ? null : id)),
    [],
  )

  const showHistory = useCallback(
    (row: AuditLogRow) => {
      if (!row.entity_id) return
      setSearchParams(
        new URLSearchParams({
          mode: 'entity',
          entity_type: row.entity_type,
          entity_id: row.entity_id,
        }),
        { replace: false },
      )
    },
    [setSearchParams],
  )

  const selectMode = (next: string) => {
    setSearchParams(
      new URLSearchParams(
        next === 'entity'
          ? {
              mode: 'entity',
              ...(entityType ? { entity_type: entityType } : {}),
              ...(entityId ? { entity_id: entityId } : {}),
            }
          : {},
      ),
      { replace: true },
    )
  }

  const hasFilters = Boolean(entityType || action || userId)

  return (
    <div>
      <PageHeader
        title="Audit trail"
        description="Who did what, to which record, from where, and on which request. Entries are append-only - the database refuses updates and deletes on this table."
      />

      <Tabs value={mode} onValueChange={selectMode}>
        <TabsList>
          <TabsTrigger value="activity">
            <ScrollText className="h-4 w-4" aria-hidden />
            Activity
          </TabsTrigger>
          <TabsTrigger value="entity">
            <History className="h-4 w-4" aria-hidden />
            Entity history
          </TabsTrigger>
        </TabsList>

        <TabsContent value="activity">
          <AuditTable
            rows={rows}
            isLoading={logs.isLoading}
            isFetching={logs.isFetching && !logs.isFetchingNextPage}
            error={logs.error as Error | null}
            onRefresh={() => void logs.refetch()}
            expandedId={expandedId}
            onToggleExpand={toggleExpand}
            onShowHistory={showHistory}
            searchValue={requestId}
            onSearchChange={(term) => setParam('request_id', term || undefined)}
            emptyTitle="Nothing recorded in this window"
            emptyDescription="Widen the date range or clear the entity, action and user filters."
            toolbar={
              <div className="flex flex-1 flex-wrap items-end gap-2">
                <Select
                  value={entityType || SELECT_ALL}
                  onValueChange={(value) =>
                    setParam('entity_type', value === SELECT_ALL ? undefined : value)
                  }
                >
                  <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by record type">
                    <SelectValue placeholder="All record types" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>All record types</SelectItem>
                    {AUDIT_ENTITY_TYPES.map((type) => (
                      <SelectItem key={type} value={type}>
                        {humanise(type)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                <Select
                  value={action || SELECT_ALL}
                  onValueChange={(value) =>
                    setParam('action', value === SELECT_ALL ? undefined : value)
                  }
                >
                  <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by action">
                    <SelectValue placeholder="All actions" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>All actions</SelectItem>
                    {AUDIT_ACTIONS.map((value) => (
                      <SelectItem key={value} value={value}>
                        {humanise(value)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                {can(P.USER_VIEW) && (
                  <Select
                    value={userId || SELECT_ALL}
                    onValueChange={(value) =>
                      setParam('user_id', value === SELECT_ALL ? undefined : value)
                    }
                  >
                    <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by user">
                      <SelectValue placeholder="All users" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_ALL}>All users</SelectItem>
                      {(actors.data ?? []).map((actor) => (
                        <SelectItem key={actor.id} value={actor.id}>
                          {actor.full_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}

                {/* Clearing a field falls back to the default window rather
                    than sending an open-ended query. */}
                <DateRangeFilter
                  id="audit"
                  from={dateFrom}
                  to={dateTo}
                  onFromChange={(value) => setParam('from', value || defaultFrom)}
                  onToChange={(value) => setParam('to', value || today)}
                  fromInputProps={{ required: true, max: dateTo }}
                  toInputProps={{ required: true, min: dateFrom, max: today }}
                />

                {hasFilters && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      // One navigation, not one per filter: `setSearchParams` computes every
                      // functional update in a tick from the same captured value, so a loop of
                      // `setParam` calls clears exactly the last one and looks like it did nothing.
                      setParams({
                        entity_type: undefined,
                        action: undefined,
                        user_id: undefined,
                      })
                    }}
                  >
                    <X />
                    Clear
                  </Button>
                )}
              </div>
            }
          />

          <p className="mt-2 text-xs text-muted-foreground">
            The date range is required: the audit log keeps every action ever taken, and an
            unbounded query would read years of it to answer a question about last week.
          </p>

          <LoadMore
            count={rows.length}
            hasNextPage={Boolean(logs.hasNextPage)}
            isFetchingNextPage={logs.isFetchingNextPage}
            onLoadMore={() => void logs.fetchNextPage()}
            noun="entries"
          />
        </TabsContent>

        <TabsContent value="entity">
          <EntityHistory
            key={entityId}
            entityType={entityType || 'purchase_order'}
            entityId={entityId}
            onEntityTypeChange={(value) => setParam('entity_type', value)}
            onEntityIdChange={(value) => setParam('entity_id', value || undefined)}
          />
        </TabsContent>
      </Tabs>
    </div>
  )
}

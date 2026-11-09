import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { CalendarClock, CirclePlus, ExternalLink } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { LoadError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
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
import { Card, CardContent, Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useUserOptions } from '@/hooks/use-user-options'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { reportFieldErrors } from '@/lib/form-errors'
import { success } from '@/lib/messages'
import { toast } from '@/lib/toast'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  humanise,
  toDateInput,
} from '@/lib/format'
import { CURSOR_PAGE_SIZE, SELECT_ALL, SELECT_NONE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { P } from '@/lib/permissions'
import type { SchemaOf } from '@/types/api'

type WorklistRow = SchemaOf<'WorklistRow'>
type FollowUpSummary = SchemaOf<'FollowUpSummary'>
type FollowUpDetail = SchemaOf<'FollowUpDetail'>
type FollowUpNote = SchemaOf<'FollowUpNoteRead'>
type FollowUpStatus = SchemaOf<'FollowUpStatus'>
type FollowUpChannel = SchemaOf<'FollowUpChannel'>
type FollowUpOutcome = SchemaOf<'FollowUpOutcome'>

const FOLLOW_UP_STATUSES: FollowUpStatus[] = [
  'OPEN',
  'PROMISED',
  'ESCALATED',
  'RESOLVED',
  'CANCELLED',
]
const CONTACT_CHANNELS: Exclude<FollowUpChannel, 'SYSTEM'>[] = ['CALL', 'EMAIL', 'VISIT', 'MESSAGE']
const NOTE_OUTCOMES: FollowUpOutcome[] = [
  'PROMISED',
  'DISPUTED',
  'NO_RESPONSE',
  'PARTIAL_PAYMENT',
  'OTHER',
]
const OPEN_FOLLOW_UP_FAILED = 'Could not open a payment follow-up'
const UPDATE_FOLLOW_UP_FAILED = 'Could not update the payment follow-up'

export default function CollectionsPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [mobileSearch, setMobileSearch] = useState(() => searchParams.get('q') ?? '')
  const queryClient = useQueryClient()
  const canManage = can(P.COLLECTION_MANAGE)

  const view = searchParams.get('view') === 'follow-ups' ? 'follow-ups' : 'worklist'
  const statusFilter = searchParams.get('status') ?? ''
  const customerId = searchParams.get('customer_id') ?? ''
  const assignedToMe = searchParams.get('assigned_to_me') === 'true'
  const dealerOnly = searchParams.get('dealer_only') === 'true'
  const minOverdueDays = searchParams.get('min_overdue_days') ?? ''
  const registerStatus = searchParams.get('followup_status') ?? ''
  const registerLiveOnly = searchParams.get('live_only') !== 'false'
  const registerAssignedToMe = searchParams.get('followup_assigned_to_me') === 'true'
  const settledMobileSearch = useDebouncedValue(mobileSearch)

  useEffect(() => {
    setMobileSearch(searchParams.get('q') ?? '')
  }, [searchParams])

  useEffect(() => {
    const currentSearch = searchParams.get('q') ?? ''
    if (settledMobileSearch === currentSearch) return
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        if (settledMobileSearch) next.set('q', settledMobileSearch)
        else next.delete('q')
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [searchParams, settledMobileSearch, setSearchParams])

  const worklist = useListQuery<WorklistRow>({
    path: '/collections/worklist',
    queryKey: queryKeys.collections.worklist,
    defaultSortBy: 'oldest_overdue_days',
    defaultSortDir: 'desc',
    filters: {
      customer_id: customerId || undefined,
      status: statusFilter || undefined,
      assigned_to_me: assignedToMe,
      dealer_only: dealerOnly,
      min_overdue_days: minOverdueDays || undefined,
    },
    enabled: can(P.COLLECTION_VIEW),
  })

  const followUps = useListQuery<FollowUpSummary>({
    path: '/collections/follow-ups',
    queryKey: queryKeys.collections.followUps,
    namespace: 'followups',
    filters: {
      status: registerStatus || undefined,
      live_only: registerLiveOnly,
      assigned_to_me: registerAssignedToMe,
    },
    enabled: can(P.COLLECTION_VIEW) && view === 'follow-ups',
  })

  const setFilter = (key: string, value: string | undefined) => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        if (value) next.set(key, value)
        else next.delete(key)
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }

  const openFollowUp = useMutation({
    mutationFn: (row: WorklistRow) =>
      api.post<FollowUpDetail>('/collections/follow-ups', {
        customer_id: row.customer_id,
        currency: row.currency,
      }),
    onSuccess: async (created) => {
      toast.success(success.opened('Payment follow-up'), created.code)
      setSelectedId(created.id)
      await queryClient.invalidateQueries({ queryKey: queryKeys.collections.all })
    },
    // The global net toasts every other refusal under this title; a 422's
    // field detail is the one thing it leaves to the screen.
    meta: { errorTitle: OPEN_FOLLOW_UP_FAILED },
    onError: (error) => reportFieldErrors(error, OPEN_FOLLOW_UP_FAILED),
  })

  const worklistColumns = useMemo<ColumnDef<WorklistRow, unknown>[]>(
    () => [
      {
        accessorKey: 'customer_code',
        header: 'Customer',
        cell: ({ row }) => (
          <div className="min-w-36">
            {row.original.follow_up_id ? (
              <button
                type="button"
                className="font-medium hover:underline"
                onClick={() => setSelectedId(row.original.follow_up_id ?? null)}
              >
                {row.original.customer_code} · {row.original.customer_name}
              </button>
            ) : canManage ? (
              <button
                type="button"
                className="inline-flex items-center gap-1 font-medium hover:underline"
                disabled={openFollowUp.isPending}
                onClick={() => openFollowUp.mutate(row.original)}
              >
                <CirclePlus className="h-3.5 w-3.5" />
                {row.original.customer_code} · {row.original.customer_name}
              </button>
            ) : (
              <span className="font-medium">
                {row.original.customer_code} · {row.original.customer_name}
              </span>
            )}
            <div className="text-xs text-muted-foreground">
              {humanise(row.original.customer_type)}
            </div>
          </div>
        ),
      },
      {
        accessorKey: 'assigned_to_name',
        header: 'Assigned to',
        enableSorting: false,
        cell: ({ row }) => row.original.assigned_to_name ?? 'Unassigned',
      },
      {
        accessorKey: 'overdue_amount',
        header: 'Overdue',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.overdue_amount, { currency: row.original.currency }),
      },
      {
        accessorKey: 'overdue_invoice_count',
        header: 'Invoices',
        meta: { align: 'right' },
        enableSorting: false,
        cell: ({ row }) => formatNumber(row.original.overdue_invoice_count),
      },
      {
        accessorKey: 'oldest_overdue_days',
        header: 'Oldest overdue',
        meta: { align: 'right' },
        cell: ({ row }) => `${formatNumber(row.original.oldest_overdue_days)} days`,
      },
      {
        id: 'ageing',
        header: 'Ageing (0-30 / 31-60 / 61-90 / 90+)',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs tabular-nums">
            {[
              row.original.bucket_0_30_amount,
              row.original.bucket_31_60_amount,
              row.original.bucket_61_90_amount,
              row.original.bucket_90_plus_amount,
            ]
              .map((amount) =>
                formatMoney(amount, { currency: row.original.currency, compact: true }),
              )
              .join(' / ')}
          </span>
        ),
      },
      {
        accessorKey: 'next_action_date',
        header: 'Next action',
        cell: ({ row }) => formatDate(row.original.next_action_date),
      },
      {
        id: 'promise',
        header: 'Promise',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.promised_date && row.original.promised_amount
            ? `${formatMoney(row.original.promised_amount, { currency: row.original.currency })} · ${formatDate(row.original.promised_date)}`
            : '-',
      },
      {
        accessorKey: 'status',
        header: 'Follow-up',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [canManage, openFollowUp],
  )

  const followUpColumns = useMemo<ColumnDef<FollowUpSummary, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Follow-up',
        enableSorting: false,
        cell: ({ row }) => (
          <button
            type="button"
            className="font-medium hover:underline"
            onClick={() => setSelectedId(row.original.id)}
          >
            {row.original.code}
          </button>
        ),
      },
      {
        accessorKey: 'customer_code',
        header: 'Customer',
        enableSorting: false,
        cell: ({ row }) => `${row.original.customer_code} · ${row.original.customer_name}`,
      },
      {
        accessorKey: 'currency',
        header: 'Currency',
        enableSorting: false,
      },
      {
        accessorKey: 'current_overdue_amount',
        header: 'Overdue',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.current_overdue_amount, { currency: row.original.currency }),
      },
      {
        accessorKey: 'assigned_to_name',
        header: 'Assigned to',
        enableSorting: false,
        cell: ({ row }) => row.original.assigned_to_name ?? 'Unassigned',
      },
      {
        accessorKey: 'next_action_date',
        header: 'Next action',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.next_action_date),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [],
  )

  return (
    <>
      <PageHeader
        title="Collections"
        description="Review overdue customer balances, record contact attempts and manage payment promises."
        breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Collections' }]}
        actions={
          <Button variant="outline" asChild>
            <Link to="/reports?report=DEALER_PERFORMANCE">
              <ExternalLink />
              Dealer performance
            </Link>
          </Button>
        }
      />
      <Tabs
        value={view}
        onValueChange={(next) => setFilter('view', next === 'worklist' ? undefined : next)}
      >
        <TabsList aria-label="Collections views">
          <TabsTrigger value="worklist">Overdue worklist</TabsTrigger>
          <TabsTrigger value="follow-ups">Follow-up register</TabsTrigger>
        </TabsList>
        <TabsContent value="worklist">
          <div className="md:hidden">
            <Input
              aria-label="Search customer code or name"
              type="search"
              placeholder="Search customer code or name"
              value={mobileSearch}
              onChange={(event) => setMobileSearch(event.target.value)}
            />
            <MobileWorklistFilters
              status={statusFilter}
              assignedToMe={assignedToMe}
              dealerOnly={dealerOnly}
              minOverdueDays={minOverdueDays}
              customerId={customerId}
              setFilter={setFilter}
            />
            {worklist.isLoading ? (
              <p className="py-8 text-center text-sm text-muted-foreground" role="status">
                Loading overdue receivables…
              </p>
            ) : worklist.error ? (
              <LoadError error={worklist.error} onRetry={() => void worklist.refetch()} />
            ) : worklist.data.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                No overdue receivables match these filters.
              </p>
            ) : (
              <div className="mt-3 space-y-3">
                {worklist.data.map((row) => (
                  <Card key={`${row.customer_id}:${row.currency}`}>
                    <CardContent className="space-y-3 pt-4">
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <p className="font-medium">{row.customer_code}</p>
                          <p className="text-sm">{row.customer_name}</p>
                          <p className="text-xs text-muted-foreground">
                            {humanise(row.customer_type)} · {row.currency}
                          </p>
                        </div>
                        <StatusBadge status={row.status} />
                      </div>
                      <div className="grid grid-cols-2 gap-3 text-sm">
                        <div>
                          <p className="text-xs text-muted-foreground">Overdue</p>
                          <p className="font-medium tabular-nums">
                            {formatMoney(row.overdue_amount, { currency: row.currency })}
                          </p>
                        </div>
                        <div>
                          <p className="text-xs text-muted-foreground">Invoices · oldest</p>
                          <p className="tabular-nums">
                            {row.overdue_invoice_count} · {row.oldest_overdue_days} days
                          </p>
                        </div>
                        <div>
                          <p className="text-xs text-muted-foreground">Assigned to</p>
                          <p>{row.assigned_to_name ?? 'Unassigned'}</p>
                        </div>
                        <div>
                          <p className="text-xs text-muted-foreground">Next action</p>
                          <p>{formatDate(row.next_action_date)}</p>
                        </div>
                      </div>
                      {row.promised_amount && row.promised_date && (
                        <p className="text-xs text-muted-foreground">
                          Promise {formatMoney(row.promised_amount, { currency: row.currency })} by{' '}
                          {formatDate(row.promised_date)}
                        </p>
                      )}
                      {row.follow_up_id ? (
                        <Button
                          className="w-full"
                          variant="outline"
                          onClick={() => setSelectedId(row.follow_up_id ?? null)}
                        >
                          View follow-up
                        </Button>
                      ) : canManage ? (
                        <Button
                          className="w-full"
                          loading={openFollowUp.isPending}
                          onClick={() => openFollowUp.mutate(row)}
                        >
                          <CirclePlus />
                          Open follow-up
                        </Button>
                      ) : null}
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
            {worklist.pagination && (
              <div className="mt-4 flex items-center justify-between">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!worklist.pagination.has_previous || worklist.isFetching}
                  onClick={() => worklist.setPage(worklist.page - 1)}
                >
                  Previous
                </Button>
                <span className="text-xs text-muted-foreground">
                  Page {worklist.pagination.page}
                  {worklist.pagination.total_pages ? ` of ${worklist.pagination.total_pages}` : ''}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!worklist.pagination.has_next || worklist.isFetching}
                  onClick={() => worklist.setPage(worklist.page + 1)}
                >
                  Next
                </Button>
              </div>
            )}
          </div>
          <div className="hidden md:block">
            <DataTable
              columns={worklistColumns}
              data={worklist.data}
              pagination={worklist.pagination}
              isLoading={worklist.isLoading}
              isFetching={worklist.isFetching}
              error={worklist.error}
              onPageChange={worklist.setPage}
              onLimitChange={worklist.setLimit}
              onSortChange={worklist.setSort}
              onSearchChange={worklist.setSearch}
              onRefresh={() => void worklist.refetch()}
              searchValue={worklist.search}
              searchPlaceholder="Search customer code or name"
              getRowId={(row) => `${row.customer_id}:${row.currency}`}
              emptyTitle="No overdue receivables"
              emptyDescription="Invoices appear here when an issued balance passes its due date."
              toolbar={
                <>
                  <Select
                    value={statusFilter || SELECT_ALL}
                    onValueChange={(next) =>
                      setFilter('status', next === SELECT_ALL ? undefined : next)
                    }
                  >
                    <SelectTrigger aria-label="Follow-up status" className="w-full sm:w-44">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_ALL}>Any follow-up</SelectItem>
                      {FOLLOW_UP_STATUSES.map((status) => (
                        <SelectItem key={status} value={status}>
                          {humanise(status)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Label className="flex items-center gap-2 whitespace-nowrap text-sm">
                    <Checkbox
                      checked={assignedToMe}
                      onCheckedChange={(checked) =>
                        setFilter('assigned_to_me', checked === true ? 'true' : undefined)
                      }
                    />
                    My follow-ups
                  </Label>
                  <Label className="flex items-center gap-2 whitespace-nowrap text-sm">
                    <Checkbox
                      checked={dealerOnly}
                      onCheckedChange={(checked) =>
                        setFilter('dealer_only', checked === true ? 'true' : undefined)
                      }
                    />
                    Dealer channels
                  </Label>
                  <Input
                    aria-label="Minimum overdue days"
                    type="number"
                    min={0}
                    max={3650}
                    placeholder="Min overdue days"
                    value={minOverdueDays}
                    className="w-full sm:w-40"
                    onChange={(event) =>
                      setFilter('min_overdue_days', event.target.value || undefined)
                    }
                  />
                  {customerId && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setFilter('customer_id', undefined)}
                    >
                      Clear customer filter
                    </Button>
                  )}
                </>
              }
            />
          </div>
        </TabsContent>
        <TabsContent value="follow-ups">
          <DataTable
            columns={followUpColumns}
            data={followUps.data}
            pagination={followUps.pagination}
            isLoading={followUps.isLoading}
            isFetching={followUps.isFetching}
            error={followUps.error}
            onPageChange={followUps.setPage}
            onLimitChange={followUps.setLimit}
            onSearchChange={followUps.setSearch}
            onRefresh={() => void followUps.refetch()}
            searchValue={followUps.search}
            searchPlaceholder="Search follow-up or customer"
            getRowId={(row) => row.id}
            onRowClick={(row) => setSelectedId(row.id)}
            emptyTitle="No payment follow-ups"
            emptyDescription="Open a follow-up from the overdue worklist."
            toolbar={
              <>
                <Select
                  value={registerStatus || SELECT_ALL}
                  onValueChange={(next) =>
                    setFilter('followup_status', next === SELECT_ALL ? undefined : next)
                  }
                >
                  <SelectTrigger aria-label="Register status" className="w-full sm:w-44">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELECT_ALL}>Any status</SelectItem>
                    {FOLLOW_UP_STATUSES.map((status) => (
                      <SelectItem key={status} value={status}>
                        {humanise(status)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Label className="flex items-center gap-2 whitespace-nowrap text-sm">
                  <Checkbox
                    checked={registerLiveOnly}
                    onCheckedChange={(checked) =>
                      setFilter('live_only', checked === true ? undefined : 'false')
                    }
                  />
                  Live only
                </Label>
                <Label className="flex items-center gap-2 whitespace-nowrap text-sm">
                  <Checkbox
                    checked={registerAssignedToMe}
                    onCheckedChange={(checked) =>
                      setFilter('followup_assigned_to_me', checked === true ? 'true' : undefined)
                    }
                  />
                  Assigned to me
                </Label>
              </>
            }
          />
        </TabsContent>
      </Tabs>
      {selectedId && (
        <FollowUpDialog
          id={selectedId}
          canManage={canManage}
          canBlockCustomer={can(P.CUSTOMER_UPDATE)}
          onClose={() => setSelectedId(null)}
        />
      )}
    </>
  )
}

function MobileWorklistFilters({
  status,
  assignedToMe,
  dealerOnly,
  minOverdueDays,
  customerId,
  setFilter,
}: {
  status: string
  assignedToMe: boolean
  dealerOnly: boolean
  minOverdueDays: string
  customerId: string
  setFilter: (key: string, value: string | undefined) => void
}) {
  return (
    <div className="my-3 grid grid-cols-2 gap-2">
      <Select
        value={status || SELECT_ALL}
        onValueChange={(next) => setFilter('status', next === SELECT_ALL ? undefined : next)}
      >
        <SelectTrigger aria-label="Follow-up status">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SELECT_ALL}>Any follow-up</SelectItem>
          {FOLLOW_UP_STATUSES.map((entry) => (
            <SelectItem key={entry} value={entry}>
              {humanise(entry)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        aria-label="Minimum overdue days"
        type="number"
        min={0}
        max={3650}
        placeholder="Min overdue days"
        value={minOverdueDays}
        onChange={(event) => setFilter('min_overdue_days', event.target.value || undefined)}
      />
      <Label className="flex items-center gap-2 text-sm">
        <Checkbox
          checked={assignedToMe}
          onCheckedChange={(checked) =>
            setFilter('assigned_to_me', checked === true ? 'true' : undefined)
          }
        />
        My follow-ups
      </Label>
      <Label className="flex items-center gap-2 text-sm">
        <Checkbox
          checked={dealerOnly}
          onCheckedChange={(checked) =>
            setFilter('dealer_only', checked === true ? 'true' : undefined)
          }
        />
        Dealer channels
      </Label>
      {customerId && (
        <Button
          className="col-span-2"
          variant="ghost"
          size="sm"
          onClick={() => setFilter('customer_id', undefined)}
        >
          Clear customer filter
        </Button>
      )}
    </div>
  )
}

function FollowUpDialog({
  id,
  canManage,
  canBlockCustomer,
  onClose,
}: {
  id: string
  canManage: boolean
  canBlockCustomer: boolean
  onClose: () => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [channel, setChannel] = useState<Exclude<FollowUpChannel, 'SYSTEM'>>('CALL')
  const [outcome, setOutcome] = useState<FollowUpOutcome | ''>('')
  const [note, setNote] = useState('')
  const [noteNextAction, setNoteNextAction] = useState('')
  const [promiseAmount, setPromiseAmount] = useState('')
  const [promiseDate, setPromiseDate] = useState(toDateInput(new Date()))
  const [assignedTo, setAssignedTo] = useState<string | null | undefined>(undefined)
  const [nextAction, setNextAction] = useState<string | null | undefined>(undefined)
  const [escalationReason, setEscalationReason] = useState('')
  const [blockCustomer, setBlockCustomer] = useState(false)
  const [resolutionNote, setResolutionNote] = useState('')
  const [cancellationReason, setCancellationReason] = useState('')

  const detail = useQuery({
    queryKey: queryKeys.collections.detail(id),
    queryFn: ({ signal }) => api.get<FollowUpDetail>(`/collections/follow-ups/${id}`, { signal }),
    staleTime: staleTimes.transactional,
  })
  const notes = useInfiniteQuery({
    queryKey: queryKeys.collections.notes(id),
    queryFn: ({ pageParam, signal }) =>
      api.getCursorPage<FollowUpNote>(`/collections/follow-ups/${id}/notes`, {
        params: { limit: CURSOR_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) },
        signal,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    enabled: Boolean(id),
  })
  const staff = useUserOptions({ enabled: canManage && can(P.USER_VIEW) })

  const invalidateFollowUp = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.collections.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.collections.detail(id) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.collections.notes(id) }),
    ])
  }

  const change = useMutation({
    mutationFn: async ({
      action,
      payload,
    }: {
      action: 'notes' | 'promise' | 'escalate' | 'resolve' | 'cancel'
      payload: Record<string, unknown>
    }) => {
      const path = `/collections/follow-ups/${id}/${action}`
      if (action === 'notes') return api.post<FollowUpNote>(path, payload)
      return api.post<FollowUpDetail>(path, payload)
    },
    onSuccess: async (_result, variables) => {
      toast.success(
        variables.action === 'notes'
          ? success.recorded('Contact attempt')
          : success.updated('Payment follow-up'),
      )
      if (variables.action === 'notes') {
        setNote('')
        setOutcome('')
        setNoteNextAction('')
      }
      await invalidateFollowUp()
    },
    // The drawer's inputs are not form fields, so a 422 names them here.
    meta: { errorTitle: UPDATE_FOLLOW_UP_FAILED },
    onError: (error) => reportFieldErrors(error, UPDATE_FOLLOW_UP_FAILED),
  })

  const update = useMutation({
    mutationFn: (payload: { assigned_to_id: string | null; next_action_date: string | null }) =>
      api.patch<FollowUpDetail>(`/collections/follow-ups/${id}`, payload),
    onSuccess: async () => {
      toast.success(success.updated('Payment follow-up'))
      await invalidateFollowUp()
    },
    meta: { errorTitle: UPDATE_FOLLOW_UP_FAILED },
    onError: (error) => reportFieldErrors(error, UPDATE_FOLLOW_UP_FAILED),
  })

  const current = detail.data
  const noteRows = notes.data?.pages.flatMap((page) => page.data) ?? []

  const submitNote = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    change.mutate({
      action: 'notes',
      payload: {
        channel,
        outcome: outcome || null,
        note: note.trim(),
        next_action_date: noteNextAction || null,
      },
    })
  }
  const submitPromise = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    change.mutate({
      action: 'promise',
      payload: { promised_amount: promiseAmount, promised_date: promiseDate },
    })
  }
  const submitEscalation = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    change.mutate({
      action: 'escalate',
      payload: {
        reason: escalationReason.trim(),
        block_customer: blockCustomer && canBlockCustomer,
      },
    })
  }
  const submitResolution = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    change.mutate({ action: 'resolve', payload: { note: resolutionNote.trim() } })
  }
  const submitCancellation = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    change.mutate({ action: 'cancel', payload: { reason: cancellationReason.trim() } })
  }
  const submitAssignment = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!current) return
    update.mutate({
      assigned_to_id: assignedTo === undefined ? current.assigned_to_id : assignedTo,
      next_action_date: nextAction === undefined ? current.next_action_date : nextAction || null,
    })
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        size="xl"
        className="left-auto right-0 top-0 h-svh max-h-svh w-[min(48rem,100vw)] translate-x-0 translate-y-0 rounded-none"
      >
        <DialogHeader>
          <DialogTitle>
            {current ? `${current.code} · ${current.customer_name}` : 'Payment follow-up'}
          </DialogTitle>
          <DialogDescription>
            {current
              ? `${current.customer_code} · ${current.currency} · opened ${formatDateTime(current.created_at)}`
              : 'Loading follow-up details.'}
          </DialogDescription>
        </DialogHeader>
        {detail.isLoading ? (
          <p role="status">Loading follow-up details…</p>
        ) : detail.error ? (
          <LoadError error={detail.error} onRetry={() => void detail.refetch()} />
        ) : current ? (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Summary label="Status">
                <StatusBadge status={current.status} />
              </Summary>
              <Summary label="Current overdue">
                {formatMoney(current.current_overdue_amount, { currency: current.currency })}
              </Summary>
              <Summary label="Oldest due">{formatDate(current.oldest_due_date)}</Summary>
              <Summary label="Next action">{formatDate(current.next_action_date)}</Summary>
            </div>

            <Card>
              <CardContent className="pt-5">
                <h3 className="mb-3 text-sm font-semibold">Overdue invoices</h3>
                {current.invoices?.length ? (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[42rem] text-sm">
                      <thead className="text-left text-xs text-muted-foreground">
                        <tr>
                          <th className="pb-2">Invoice</th>
                          <th className="pb-2">Due date</th>
                          <th className="pb-2 text-right">Days overdue</th>
                          <th className="pb-2 text-right">Balance</th>
                          <th className="pb-2">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {current.invoices.map((invoice) => (
                          <tr key={invoice.id} className="border-t">
                            <td className="py-2">
                              <Link
                                className="font-medium hover:underline"
                                to={`/finance/sales-invoices?invoice_id=${invoice.id}`}
                                onClick={onClose}
                              >
                                {invoice.code}
                              </Link>
                            </td>
                            <td>{formatDate(invoice.due_date)}</td>
                            <td className="text-right tabular-nums">
                              {formatNumber(invoice.overdue_days)}
                            </td>
                            <td className="text-right tabular-nums">
                              {formatMoney(invoice.balance_amount, { currency: invoice.currency })}
                            </td>
                            <td>
                              <StatusBadge status={invoice.status} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No overdue invoices remain.</p>
                )}
              </CardContent>
            </Card>

            {canManage && current.status !== 'RESOLVED' && current.status !== 'CANCELLED' && (
              <>
                <Card>
                  <CardContent className="pt-5">
                    <form className="grid gap-3 sm:grid-cols-3" onSubmit={submitAssignment}>
                      <div className="space-y-1.5">
                        <Label htmlFor="follow-up-assignee">Assigned to</Label>
                        <Select
                          value={
                            assignedTo === undefined
                              ? (current.assigned_to_id ?? SELECT_NONE)
                              : (assignedTo ?? SELECT_NONE)
                          }
                          onValueChange={(value) =>
                            setAssignedTo(value === SELECT_NONE ? null : value)
                          }
                        >
                          <SelectTrigger id="follow-up-assignee">
                            <SelectValue placeholder={current.assigned_to_name ?? 'Unassigned'} />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>Unassigned</SelectItem>
                            {(staff.data ?? []).map((user) => (
                              <SelectItem key={user.id} value={user.id}>
                                {user.full_name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="follow-up-next-action">Next action date</Label>
                        <Input
                          id="follow-up-next-action"
                          type="date"
                          value={nextAction || current.next_action_date || ''}
                          onChange={(event) => setNextAction(event.target.value)}
                        />
                      </div>
                      <div className="flex items-end">
                        <Button type="submit" variant="outline" loading={update.isPending}>
                          Save assignment
                        </Button>
                      </div>
                    </form>
                  </CardContent>
                </Card>

                <Card>
                  <CardContent className="pt-5">
                    <form className="space-y-3" onSubmit={submitNote}>
                      <h3 className="text-sm font-semibold">Log a contact attempt</h3>
                      <div className="grid gap-3 sm:grid-cols-3">
                        <div className="space-y-1.5">
                          <Label htmlFor="follow-up-channel">Channel</Label>
                          <Select
                            value={channel}
                            onValueChange={(value) => setChannel(value as typeof channel)}
                          >
                            <SelectTrigger id="follow-up-channel">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {CONTACT_CHANNELS.map((item) => (
                                <SelectItem key={item} value={item}>
                                  {humanise(item)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="space-y-1.5">
                          <Label htmlFor="follow-up-outcome">Outcome</Label>
                          <Select
                            value={outcome || SELECT_NONE}
                            onValueChange={(value) =>
                              setOutcome(value === SELECT_NONE ? '' : (value as FollowUpOutcome))
                            }
                          >
                            <SelectTrigger id="follow-up-outcome">
                              <SelectValue placeholder="Choose outcome" />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value={SELECT_NONE}>No outcome recorded</SelectItem>
                              {NOTE_OUTCOMES.map((item) => (
                                <SelectItem key={item} value={item}>
                                  {humanise(item)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="space-y-1.5">
                          <Label htmlFor="follow-up-note-next-action">Next action date</Label>
                          <Input
                            id="follow-up-note-next-action"
                            type="date"
                            value={noteNextAction}
                            onChange={(event) => setNoteNextAction(event.target.value)}
                          />
                        </div>
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="follow-up-note" required>
                          Contact notes
                        </Label>
                        <Textarea
                          id="follow-up-note"
                          required
                          maxLength={4000}
                          value={note}
                          onChange={(event) => setNote(event.target.value)}
                        />
                      </div>
                      <Button type="submit" loading={change.isPending}>
                        Record attempt
                      </Button>
                    </form>
                  </CardContent>
                </Card>

                {(current.status === 'OPEN' || current.status === 'PROMISED') && (
                  <Card>
                    <CardContent className="pt-5">
                      <form className="space-y-3" onSubmit={submitPromise}>
                        <h3 className="text-sm font-semibold">Record a payment promise</h3>
                        <div className="grid gap-3 sm:grid-cols-3">
                          <div className="space-y-1.5">
                            <Label htmlFor="promise-amount" required>
                              Promised amount
                            </Label>
                            <Input
                              id="promise-amount"
                              type="number"
                              min="0.0001"
                              step="0.0001"
                              required
                              value={promiseAmount}
                              onChange={(event) => setPromiseAmount(event.target.value)}
                            />
                          </div>
                          <div className="space-y-1.5">
                            <Label htmlFor="promise-date" required>
                              Promised date
                            </Label>
                            <Input
                              id="promise-date"
                              type="date"
                              min={toDateInput(new Date())}
                              required
                              value={promiseDate}
                              onChange={(event) => setPromiseDate(event.target.value)}
                            />
                          </div>
                          <div className="flex items-end">
                            <Button type="submit" loading={change.isPending}>
                              Record promise
                            </Button>
                          </div>
                        </div>
                      </form>
                    </CardContent>
                  </Card>
                )}

                {current.status !== 'ESCALATED' && (
                  <Card>
                    <CardContent className="pt-5">
                      <form className="space-y-3" onSubmit={submitEscalation}>
                        <h3 className="text-sm font-semibold">Escalate follow-up</h3>
                        <div className="space-y-1.5">
                          <Label htmlFor="escalation-reason" required>
                            Reason
                          </Label>
                          <Input
                            id="escalation-reason"
                            minLength={3}
                            maxLength={500}
                            required
                            value={escalationReason}
                            onChange={(event) => setEscalationReason(event.target.value)}
                          />
                        </div>
                        {canBlockCustomer && (
                          <Label className="flex items-center gap-2 text-sm">
                            <Checkbox
                              checked={blockCustomer}
                              onCheckedChange={(checked) => setBlockCustomer(checked === true)}
                            />
                            Place the customer on credit hold
                          </Label>
                        )}
                        <Button type="submit" variant="outline" loading={change.isPending}>
                          Escalate
                        </Button>
                      </form>
                    </CardContent>
                  </Card>
                )}

                <div className="grid gap-3 md:grid-cols-2">
                  <Card>
                    <CardContent className="pt-5">
                      <form className="space-y-3" onSubmit={submitResolution}>
                        <h3 className="text-sm font-semibold">Resolve follow-up</h3>
                        <div className="space-y-1.5">
                          <Label htmlFor="resolution-note" required>
                            Resolution note
                          </Label>
                          <Textarea
                            id="resolution-note"
                            minLength={3}
                            maxLength={2000}
                            required
                            value={resolutionNote}
                            onChange={(event) => setResolutionNote(event.target.value)}
                          />
                        </div>
                        <Button type="submit" variant="outline" loading={change.isPending}>
                          Resolve
                        </Button>
                      </form>
                    </CardContent>
                  </Card>
                  <Card>
                    <CardContent className="pt-5">
                      <form className="space-y-3" onSubmit={submitCancellation}>
                        <h3 className="text-sm font-semibold">Cancel follow-up</h3>
                        <div className="space-y-1.5">
                          <Label htmlFor="cancellation-reason" required>
                            Reason
                          </Label>
                          <Input
                            id="cancellation-reason"
                            minLength={3}
                            maxLength={500}
                            required
                            value={cancellationReason}
                            onChange={(event) => setCancellationReason(event.target.value)}
                          />
                        </div>
                        <Button type="submit" variant="destructive" loading={change.isPending}>
                          Cancel follow-up
                        </Button>
                      </form>
                    </CardContent>
                  </Card>
                </div>
              </>
            )}

            <Card>
              <CardContent className="pt-5">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold">Contact history</h3>
                  <span className="text-xs text-muted-foreground">
                    Last contact{' '}
                    {current.last_reminded_at
                      ? formatDateTime(current.last_reminded_at)
                      : 'not recorded'}
                  </span>
                </div>
                {notes.isLoading ? (
                  <p role="status" className="text-sm text-muted-foreground">
                    Loading contact history…
                  </p>
                ) : notes.error ? (
                  <LoadError error={notes.error} onRetry={() => void notes.refetch()} />
                ) : noteRows.length ? (
                  <ol className="space-y-3">
                    {noteRows.map((entry) => (
                      <li key={entry.id} className="border-l-2 pl-3">
                        <div className="flex flex-wrap items-center gap-2 text-xs">
                          <StatusBadge status={entry.channel} />
                          {entry.outcome && <StatusBadge status={entry.outcome} />}
                          <time className="text-muted-foreground">
                            {formatDateTime(entry.created_at)}
                          </time>
                          {entry.created_by_name && (
                            <span className="text-muted-foreground">
                              by {entry.created_by_name}
                            </span>
                          )}
                        </div>
                        <p className="mt-1 whitespace-pre-wrap text-sm">{entry.note}</p>
                        {entry.next_action_date && (
                          <p className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground">
                            <CalendarClock className="h-3 w-3" /> Next action{' '}
                            {formatDate(entry.next_action_date)}
                          </p>
                        )}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-sm text-muted-foreground">No contact attempts recorded.</p>
                )}
                {notes.hasNextPage && (
                  <Button
                    className="mt-3"
                    size="sm"
                    variant="outline"
                    loading={notes.isFetchingNextPage}
                    onClick={() => void notes.fetchNextPage()}
                  >
                    Load older notes
                  </Button>
                )}
              </CardContent>
            </Card>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function Summary({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="space-y-1.5 pt-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <div className="font-medium tabular-nums">{children}</div>
      </CardContent>
    </Card>
  )
}

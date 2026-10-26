/**
 * Notifications.
 *
 * Unread first, then newest first - that ordering is the server's, not a sort
 * applied to a page that has already been cut to fifty rows. The list is keyset
 * paginated because `notifications` grows for as long as the company operates.
 *
 * Preferences are per event type, in-app and email separately: a store keeper
 * who wants a GRN in the bell but not in their inbox should not have to choose
 * between all of it and none of it.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertOctagon,
  AlertTriangle,
  BellOff,
  CheckCheck,
  CheckCircle2,
  Info,
  Inbox,
  Mail,
  MonitorSmartphone,
} from 'lucide-react'
import { lazy, Suspense, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { EmptyState, PageHeader, PanelLoader } from '@/components/shared'
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
  Checkbox,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { NOTIFICATION_POLL_MS, SELECT_ALL } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { formatDateTime, formatRelative, humanise } from '@/lib/format'
import { describeError, notificationsRead, success } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  fetchNotificationPage,
  NOTIFICATION_TYPES,
  SEVERITIES,
  type MarkAllReadResult,
  type NotificationPreference,
  type NotificationRow,
  type UnreadCount,
} from './notification-types'

const PAGE_SIZE = 30
const PushDeviceCard = lazy(() => import('./push-device-card'))

const SEVERITY_ICONS = {
  INFO: Info,
  SUCCESS: CheckCircle2,
  WARNING: AlertTriangle,
  CRITICAL: AlertOctagon,
} as const

const SEVERITY_CLASSES: Record<string, string> = {
  INFO: 'text-primary',
  SUCCESS: 'text-success',
  WARNING: 'text-warning',
  CRITICAL: 'text-destructive',
}

function SeverityIcon({ severity }: { severity: string }) {
  const key = severity.toUpperCase()
  const Icon = SEVERITY_ICONS[key as keyof typeof SEVERITY_ICONS] ?? Info
  return (
    <Icon
      className={cn('mt-0.5 h-4 w-4 shrink-0', SEVERITY_CLASSES[key] ?? 'text-muted-foreground')}
      aria-hidden
    />
  )
}

// ----------------------------------------------------------------- inbox ----

function NotificationCard({
  notification,
  onMarkRead,
  isMarking,
}: {
  notification: NotificationRow
  onMarkRead: (id: string) => void
  isMarking: boolean
}) {
  // The API decides where a notification points and hands it over as
  // `action_url`; the row carries no document code for the UI to route from.
  const href = notification.action_url

  return (
    <Card className={cn(!notification.is_read && 'border-primary/40 bg-primary/[0.03]')}>
      <CardContent className="flex items-start gap-3 p-4">
        <SeverityIcon severity={notification.severity} />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className={cn('text-sm', !notification.is_read && 'font-semibold')}>
              {notification.title}
            </p>
            {!notification.is_read && <Badge variant="default">New</Badge>}
            <Badge variant="muted">{humanise(notification.notification_type)}</Badge>
            {/* Repeats are folded into one row server-side, so the count is the
                difference between "a machine stopped" and "a machine has
                stopped eleven times". */}
            {notification.occurrence_count > 1 && (
              <Badge variant="secondary">{notification.occurrence_count} times</Badge>
            )}
          </div>

          {notification.body && (
            <p className="mt-1 text-sm text-muted-foreground">{notification.body}</p>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <time
              dateTime={notification.created_at}
              title={formatDateTime(notification.created_at)}
            >
              {formatRelative(notification.created_at)}
            </time>
            {href && (
              <Link to={href} className="text-primary hover:underline">
                Open {humanise(notification.entity_type ?? 'record').toLowerCase()}
              </Link>
            )}
          </div>
        </div>

        {!notification.is_read && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onMarkRead(notification.id)}
            disabled={isMarking}
          >
            Mark read
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

function InboxTab() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const readState = searchParams.get('state') ?? 'unread'
  const severity = searchParams.get('severity') ?? ''
  const type = searchParams.get('type') ?? ''

  const setFilter = (key: string, value: string | undefined) => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        if (!value) next.delete(key)
        else next.set(key, value)
        return next
      },
      { replace: true },
    )
  }

  // The server does the ordering: unread first, then newest. Sorting a fetched
  // page in the browser would only reorder the slice on screen.
  //
  // `unread_only` is the parameter the endpoint takes - it accepts no
  // `is_read`, so the unread tab used to be indistinguishable from Everything.
  const filters = useMemo(
    () => ({
      limit: PAGE_SIZE,
      ...(readState === 'unread' ? { unread_only: true } : {}),
      ...(severity ? { severity } : {}),
      ...(type ? { notification_type: type } : {}),
    }),
    [readState, severity, type],
  )

  const notifications = useInfiniteQuery({
    queryKey: [...queryKeys.notifications.all, 'list', filters] as const,
    queryFn: ({ pageParam }) =>
      fetchNotificationPage({ ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    staleTime: staleTimes.transactional,
  })

  const rows = useMemo(
    () => notifications.data?.pages.flatMap((page) => page.data) ?? [],
    [notifications.data],
  )

  // The same count the header bell reads, so the two cannot disagree - and it
  // covers unread items beyond the pages loaded here.
  const unread = useQuery({
    queryKey: queryKeys.notifications.unreadCount,
    queryFn: () => api.get<UnreadCount>('/notifications/unread-count'),
    // The same key the header bell polls, so the same staleness: half the poll
    // interval, exactly as `app-layout.tsx` declares it.
    staleTime: NOTIFICATION_POLL_MS / 2,
  })

  const unreadCount = unread.data?.unread_count ?? 0
  // Past the ceiling the API stops counting exactly and returns a floor, so the
  // badge must not claim a precise number it was not given.
  const unreadLabel = unread.data?.is_capped ? `${unreadCount}+ unread` : `${unreadCount} unread`

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.notifications.all })
  }

  const markRead = useMutation({
    mutationFn: (id: string) => api.post(`/notifications/${id}/read`),
    onSuccess: invalidate,
  })

  const markAllRead = useMutation({
    mutationFn: () => api.post<MarkAllReadResult>('/notifications/read-all'),
    onSuccess: (result) => {
      invalidate()
      const marked = result?.marked ?? 0
      const remaining = result?.remaining_unread ?? 0
      const message = notificationsRead(marked, remaining)
      toast.success(message.title, message.description)
    },
  })

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Select value={readState} onValueChange={(value) => setFilter('state', value)}>
          <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by read state">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="unread">Unread only</SelectItem>
            <SelectItem value="all">Everything</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={severity || SELECT_ALL}
          onValueChange={(value) => setFilter('severity', value === SELECT_ALL ? undefined : value)}
        >
          <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by severity">
            <SelectValue placeholder="All severities" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={SELECT_ALL}>All severities</SelectItem>
            {SEVERITIES.map((value) => (
              <SelectItem key={value} value={value}>
                {humanise(value)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={type || SELECT_ALL}
          onValueChange={(value) => setFilter('type', value === SELECT_ALL ? undefined : value)}
        >
          <SelectTrigger className="w-full sm:w-[220px]" aria-label="Filter by event type">
            <SelectValue placeholder="All event types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={SELECT_ALL}>All event types</SelectItem>
            {NOTIFICATION_TYPES.map((value) => (
              <SelectItem key={value} value={value}>
                {humanise(value)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <span className="ml-auto flex flex-wrap items-center gap-2">
          {unreadCount > 0 && (
            <span className="text-sm text-muted-foreground" role="status">
              {unreadLabel}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            disabled={unreadCount === 0}
            loading={markAllRead.isPending}
            onClick={() => markAllRead.mutate()}
          >
            <CheckCheck />
            Mark all as read
          </Button>
        </span>
      </div>

      {notifications.isLoading && (
        <div className="space-y-2" aria-busy="true">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-[5.5rem] w-full" />
          ))}
        </div>
      )}

      {notifications.error && !notifications.isLoading && (
        <Alert variant="destructive">
          <AlertTitle>Could not load notifications</AlertTitle>
          <AlertDescription>
            {describeError(notifications.error)}
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => void notifications.refetch()}
            >
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {!notifications.isLoading && !notifications.error && rows.length === 0 && (
        <EmptyState
          icon={readState === 'unread' ? CheckCheck : Inbox}
          title={readState === 'unread' ? 'Nothing unread' : 'No notifications'}
          description={
            readState === 'unread'
              ? 'Everything addressed to you has been read. Switch to Everything to look back.'
              : 'Approvals, receipts, quality decisions and job failures land here as they happen.'
          }
        />
      )}

      <div className="space-y-2">
        {rows.map((notification) => (
          <NotificationCard
            key={notification.id}
            notification={notification}
            onMarkRead={(id) => markRead.mutate(id)}
            isMarking={markRead.isPending && markRead.variables === notification.id}
          />
        ))}
      </div>

      {rows.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
          <p className="text-muted-foreground" role="status">
            {rows.length} loaded
          </p>
          {notifications.hasNextPage ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => void notifications.fetchNextPage()}
              loading={notifications.isFetchingNextPage}
            >
              Load more
            </Button>
          ) : (
            <p className="text-xs text-muted-foreground">That is everything for these filters.</p>
          )}
        </div>
      )}
    </div>
  )
}

// ----------------------------------------------------------- preferences ----

function PreferencesTab() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, NotificationPreference> | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const preferences = useQuery({
    queryKey: [...queryKeys.notifications.all, 'preferences'] as const,
    queryFn: () => api.get<NotificationPreference[]>('/notifications/preferences'),
    staleTime: staleTimes.reference,
  })

  const stored = preferences.data ?? []
  const current = (type: string): NotificationPreference | undefined =>
    draft?.[type] ?? stored.find((row) => row.notification_type === type)

  const setChannel = (
    preference: NotificationPreference,
    channel: 'in_app_enabled' | 'email_enabled' | 'push_enabled',
    value: boolean,
  ) => {
    setDraft((existing) => {
      const next = {
        ...(existing ?? {}),
        [preference.notification_type]: { ...preference, [channel]: value },
      }
      const original = stored.find((row) => row.notification_type === preference.notification_type)
      const changed = next[preference.notification_type]!
      if (
        original &&
        original.in_app_enabled === changed.in_app_enabled &&
        original.email_enabled === changed.email_enabled &&
        (original.push_enabled ?? false) === (changed.push_enabled ?? false)
      )
        delete next[preference.notification_type]
      return Object.keys(next).length ? next : null
    })
  }

  const save = useMutation({
    // PUT, not PATCH: the endpoint offers no partial update, so a PATCH here
    // was a 405 and preferences could never be saved at all. Only the touched
    // rows are still sent - the API upserts by `notification_type` and leaves
    // the rest alone, so this remains a delta despite the verb.
    mutationFn: (rows: NotificationPreference[]) =>
      api.put<NotificationPreference[]>('/notifications/preferences', {
        preferences: rows.map((row) => ({
          notification_type: row.notification_type,
          in_app_enabled: row.in_app_enabled,
          email_enabled: row.email_enabled,
          push_enabled: row.push_enabled ?? false,
        })),
      }),
    onMutate: () => setSaveError(null),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.notifications.all })
      setDraft(null)
      toast.success(success.saved('Preferences'))
    },
    onError: (error) => {
      // The rows are one grid with no control to hang a field error on, so a
      // 422 keyed on `preferences.N.*` is shown above it. The mutation cache
      // stays quiet for a 422 with field detail on the understanding that the
      // screen shows it.
      const message = fieldErrorSummary(error)
      if (message) setSaveError(message)
    },
  })

  if (preferences.isLoading) {
    return (
      <div className="space-y-2" aria-busy="true">
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-12 w-full" />
        ))}
      </div>
    )
  }

  if (preferences.error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load your preferences</AlertTitle>
        <AlertDescription>{describeError(preferences.error)}</AlertDescription>
      </Alert>
    )
  }

  if (stored.length === 0) {
    return (
      <EmptyState
        icon={BellOff}
        title="No configurable notifications"
        description="Nothing in this company raises a notification you can switch off yet."
      />
    )
  }

  const dirty = draft !== null && Object.keys(draft).length > 0

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          In-app controls the bell in the header. Email goes to your profile address. Push goes to
          devices where you have enabled notifications.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {dirty && (
            <Button variant="ghost" size="sm" onClick={() => setDraft(null)}>
              Discard
            </Button>
          )}
          <Button
            size="sm"
            disabled={!dirty}
            loading={save.isPending}
            onClick={() => save.mutate(Object.values(draft ?? {}))}
          >
            Save preferences
          </Button>
        </div>
      </div>

      {saveError && (
        <Alert variant="destructive" className="mb-3">
          <AlertTitle>The preferences were not saved</AlertTitle>
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      )}

      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <caption className="sr-only">Notification channels per event type</caption>
          <thead className="bg-muted/50">
            <tr className="border-b">
              <th
                scope="col"
                className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
              >
                Event
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground"
              >
                <span className="inline-flex items-center gap-1.5">
                  <MonitorSmartphone className="h-3.5 w-3.5" aria-hidden />
                  In app
                </span>
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground"
              >
                <span className="inline-flex items-center gap-1.5">
                  <Mail className="h-3.5 w-3.5" aria-hidden />
                  Email
                </span>
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground"
              >
                Push
              </th>
            </tr>
          </thead>
          <tbody>
            {stored.map((row) => {
              const value = current(row.notification_type) ?? row
              // The API sends the enum member and nothing else - no display
              // label, no help text - so the key is humanised here.
              const label = humanise(row.notification_type)
              return (
                <tr key={row.notification_type} className="border-b last:border-0">
                  <td className="px-3 py-2">
                    <span className="font-medium">{label}</span>
                    {value.is_customised === false && (
                      <span className="block text-xs text-muted-foreground">
                        Using the default for this event
                      </span>
                    )}
                  </td>
                  {(
                    [
                      ['in_app_enabled', 'in-app', 'in app'],
                      ['email_enabled', 'email', 'by email'],
                      ['push_enabled', 'push', 'by push'],
                    ] as const
                  ).map(([channel, prefix, description]) => (
                    <td key={channel} className="px-3 py-2 text-center">
                      <Label
                        htmlFor={`${prefix}-${row.notification_type}`}
                        className="inline-flex min-h-11 min-w-11 items-center justify-center"
                      >
                        <Checkbox
                          id={`${prefix}-${row.notification_type}`}
                          checked={value[channel] ?? false}
                          onCheckedChange={(next) => setChannel(value, channel, next === true)}
                        />
                        <span className="sr-only">
                          {label} {description}
                        </span>
                      </Label>
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ page ----

export default function NotificationsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') === 'preferences' ? 'preferences' : 'inbox'

  const selectTab = (next: string) => {
    setSearchParams(new URLSearchParams(next === 'inbox' ? {} : { tab: next }), { replace: true })
  }

  return (
    <div>
      <PageHeader
        title="Notifications"
        description="Approvals waiting on you, receipts posted, quality decisions taken and jobs that failed."
      />

      <Tabs value={tab} onValueChange={selectTab}>
        <TabsList>
          <TabsTrigger value="inbox">
            <Inbox className="h-4 w-4" aria-hidden />
            Inbox
          </TabsTrigger>
          <TabsTrigger value="preferences">
            <MonitorSmartphone className="h-4 w-4" aria-hidden />
            Preferences
          </TabsTrigger>
        </TabsList>

        <TabsContent value="inbox">
          <InboxTab />
        </TabsContent>

        <TabsContent value="preferences" className="space-y-4">
          <Suspense fallback={<PanelLoader />}>
            <PushDeviceCard />
          </Suspense>
          <PreferencesTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

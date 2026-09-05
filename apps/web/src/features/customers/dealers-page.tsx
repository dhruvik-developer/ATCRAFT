/**
 * The dealer channel as its own list.
 *
 * **Read-only, and deliberately so.** `GET /customers/dealers` is the entire
 * dealer surface of the API - there is no dealer create, update, block or
 * delete route to put behind a button. Dealer and distributor accounts are
 * opened and amended on the Customers screen, where the service demands
 * `dealer.manage` on top of the customer right before it will write a row in
 * either channel. Nobody should go looking for the missing dialogs here: they
 * are not hidden by a permission, they do not exist.
 *
 * What this screen is for is the credit position of the channel. Every
 * narrowing below is a query parameter the API answers - a dealer network runs
 * to thousands of accounts and the browser never receives more than one page
 * (master rules 31/36/78).
 *
 * `CustomerSummary` carries `assigned_sales_user_id` but no user name, so the
 * sales owner is a filter ("only mine", resolved against the signed-in user's
 * own id) rather than a column: rendering a name per row would be a request per
 * line of the grid.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Infinity as InfinityIcon, ShieldAlert, ShieldOff, X } from 'lucide-react'
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { EmptyState, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney, formatNumber, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  DEALER_CHANNELS,
  DEALER_CHANNEL_LABELS,
  PRICE_TYPES,
  PRICE_TYPE_LABELS,
  creditHeadroom,
  dealerKeys,
  isCompleteStateCode,
  isOverCreditLimit,
  isUnlimitedCredit,
  toStateCodeInput,
  type Dealer,
} from './dealer-model'

/**
 * Every filter key this screen owns, named once.
 *
 * Clear reads the same list the count does, so a narrowing added to the toolbar
 * cannot end up counted but not cleared.
 */
const FILTER_KEYS = [
  'customer_type',
  'price_list_type',
  'standing',
  'is_active',
  'state_code',
  'mine',
] as const

export default function DealersPage() {
  const { canAny, user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  /**
   * The route enforces `require_any_permission(DEALER_VIEW, CUSTOMER_VIEW)` -
   * either right opens this list, and demanding `dealer.view` alone would hide
   * it from a sales manager the API would have answered. Mirrored here as well
   * as in the router so the request is never fired into a 403.
   */
  const canView = canAny(P.DEALER_VIEW, P.CUSTOMER_VIEW)

  const channel = searchParams.get('customer_type') ?? ''
  const priceList = searchParams.get('price_list_type') ?? ''
  const standing = searchParams.get('standing') ?? ''
  // 'true' (the default), 'false', or 'all'. Retired accounts are excluded by
  // the API whichever is chosen, so "all" means active plus inactive and never
  // widens to rows the grid could not label.
  const status = searchParams.get('is_active') ?? 'true'
  const stateCode = toStateCodeInput(searchParams.get('state_code') ?? '')
  const salesUserId = user?.id ?? null
  const mine = searchParams.get('mine') === 'true' && salesUserId !== null

  const list = useListQuery<Dealer>({
    path: '/customers/dealers',
    queryKey: dealerKeys.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    enabled: canView,
    filters: {
      ...(channel ? { customer_type: channel } : {}),
      ...(priceList ? { price_list_type: priceList } : {}),
      ...(standing === 'blocked' ? { is_blocked: true } : {}),
      ...(standing === 'clear' ? { is_blocked: false } : {}),
      // The API decides who is over their limit: it compares the stored
      // outstanding against the stored ceiling in SQL, which is the only way to
      // get the right rows on page two and after.
      ...(standing === 'over_limit' ? { over_credit_limit: true } : {}),
      ...(status === 'all' ? {} : { is_active: status !== 'false' }),
      // A single digit is a state code still being typed, not a filter.
      ...(isCompleteStateCode(stateCode) ? { state_code: stateCode } : {}),
      ...(mine && salesUserId ? { assigned_sales_user_id: salesUserId } : {}),
    },
  })

  const { setParam } = list

  // The status filter defaults to active-only, so having it set to 'true' is
  // not a narrowing the user applied and Clear returns it to that default
  // rather than to "any status".
  const filterCount =
    (channel ? 1 : 0) +
    (priceList ? 1 : 0) +
    (standing ? 1 : 0) +
    (status === 'true' ? 0 : 1) +
    (stateCode ? 1 : 0) +
    (mine ? 1 : 0)

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the params captured when the callback was created. Six `setParam`
   * calls in a row would therefore all start from the same stale base and only
   * the last navigation would survive.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        // The page number belonged to the narrowed list, not to this one.
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  const columns = useMemo<ColumnDef<Dealer, unknown>[]>(
    () => [
      {
        // `code`, `name`, `customer_type`, `credit_limit`, `outstanding_amount`
        // and `created_at` are the only fields the repository will sort on.
        // Every other header is fixed rather than sending a 400.
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <span className="block truncate">{row.original.name}</span>
            {row.original.legal_name && row.original.legal_name !== row.original.name && (
              <span className="block truncate text-xs text-muted-foreground">
                {row.original.legal_name}
              </span>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'customer_type',
        header: 'Channel',
        cell: ({ row }) => (
          <div className="space-y-0.5">
            <Badge variant="secondary">{humanise(row.original.customer_type)}</Badge>
            {/* Which price list order entry quotes from. It is a separate
                field from the channel by design - the API is explicit that
                `customer_type` classifies the account and `price_list_type`
                decides what it is charged - so a dealer on sale prices is a
                fact worth showing rather than an inconsistency to hide. */}
            <span className="block text-xs text-muted-foreground">
              {PRICE_TYPE_LABELS[row.original.price_list_type]} prices
            </span>
          </div>
        ),
      },
      {
        id: 'gstin',
        header: 'GSTIN',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.gstin ? (
            <span className="tabular text-xs">{row.original.gstin}</span>
          ) : (
            <span className="text-muted-foreground">Unregistered</span>
          ),
      },
      {
        accessorKey: 'credit_limit',
        header: 'Credit limit',
        meta: { align: 'right' },
        // Null is unlimited, not zero. Printing a zero for an account with no
        // ceiling would say the exact opposite of what the record means.
        cell: ({ row }) =>
          isUnlimitedCredit(row.original) ? (
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <InfinityIcon className="h-3.5 w-3.5" aria-hidden />
              Unlimited
            </span>
          ) : (
            formatMoney(row.original.credit_limit)
          ),
      },
      {
        accessorKey: 'outstanding_amount',
        header: 'Outstanding',
        meta: { align: 'right' },
        cell: ({ row }) => {
          const over = isOverCreditLimit(row.original)
          const headroom = creditHeadroom(row.original)
          return (
            <div>
              <span className={over ? 'font-medium text-destructive' : undefined}>
                {formatMoney(row.original.outstanding_amount)}
              </span>
              {/* Headroom is limit less outstanding, the same subtraction the
                  API publishes as `credit_headroom` on the detail read - which
                  the list projection does not carry. Rendered from a Decimal
                  string so no float touches a credit figure. */}
              {headroom !== null && (
                <span
                  className={
                    over ? 'block text-xs text-destructive' : 'block text-xs text-muted-foreground'
                  }
                >
                  {over
                    ? `${formatMoney(headroom.negated().toFixed())} over`
                    : `${formatMoney(headroom.toFixed())} left`}
                </span>
              )}
            </div>
          )
        },
      },
      {
        id: 'credit_days',
        header: 'Credit days',
        // Not one of the repository's sortable fields, and asking for it is a 400.
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatNumber(row.original.credit_days)}</span>
        ),
      },
      {
        id: 'standing',
        header: 'Standing',
        enableSorting: false,
        // The block reason lives on the customer detail, not on this
        // projection, so the badge states the fact and does not invent a cause.
        cell: ({ row }) =>
          row.original.is_blocked ? (
            <Badge variant="destructive">
              <ShieldAlert className="h-3 w-3" aria-hidden />
              Blocked
            </Badge>
          ) : (
            <span className="text-muted-foreground">Clear</span>
          ),
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
      {
        accessorKey: 'created_at',
        header: 'Opened',
        // When the account joined the channel. Useful next to an outstanding
        // balance: a week-old dealer at its limit is a different conversation
        // from a five-year-old one.
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {formatDate(row.original.created_at)}
          </span>
        ),
      },
    ],
    [],
  )

  if (!canView) {
    return (
      <>
        <PageHeader
          title="Dealers"
          description="Dealer and distributor accounts, with the credit position of the channel."
        />
        <EmptyState
          icon={ShieldOff}
          title="You cannot see the dealer list"
          description="This list needs either the dealer.view or the customer.view permission. Ask an administrator to grant one if you work with the channel."
        />
      </>
    )
  }

  const stateCodeIncomplete = stateCode.length === 1

  return (
    <>
      {/* No breadcrumb back to Customers: that route needs `customer.view`, and
          a holder of `dealer.view` alone would follow it into the not-permitted
          page. */}
      <PageHeader
        title="Dealers"
        description="Dealer and distributor accounts, with the credit standing that governs their order entry. Read-only: accounts in this channel are opened and amended on the Customers screen."
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
        searchPlaceholder="Search code, name, legal name or GSTIN..."
        getRowId={(row) => row.id}
        emptyTitle="No dealer accounts found"
        emptyDescription={
          filterCount > 0 || list.search
            ? 'Nothing matches these filters. Clear one, or widen the search.'
            : 'An account appears here as soon as it is opened on the Customers screen with the Dealer or Distributor channel.'
        }
        toolbar={
          <>
            <Select
              value={channel || SELECT_ALL}
              onValueChange={(next) =>
                setParam('customer_type', next === SELECT_ALL ? undefined : next)
              }
            >
              <SelectTrigger className="w-full sm:w-[10.5rem]" aria-label="Filter by channel">
                <SelectValue placeholder="Both channels" />
              </SelectTrigger>
              <SelectContent>
                {/* Only the two dealer channels are offered: the endpoint
                    refuses CUSTOMER and OEM outright rather than answering an
                    empty page, so listing them would build a 422 into a menu. */}
                <SelectItem value={SELECT_ALL}>Both channels</SelectItem>
                {DEALER_CHANNELS.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {DEALER_CHANNEL_LABELS[entry]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={priceList || SELECT_ALL}
              onValueChange={(next) =>
                setParam('price_list_type', next === SELECT_ALL ? undefined : next)
              }
            >
              <SelectTrigger className="w-full sm:w-[10.5rem]" aria-label="Filter by price list">
                <SelectValue placeholder="Any price list" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any price list</SelectItem>
                {PRICE_TYPES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {PRICE_TYPE_LABELS[entry]} prices
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={standing || SELECT_ALL}
              onValueChange={(next) => setParam('standing', next === SELECT_ALL ? undefined : next)}
            >
              <SelectTrigger
                className="w-full sm:w-[11.5rem]"
                aria-label="Filter by credit standing"
              >
                <SelectValue placeholder="Any standing" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any standing</SelectItem>
                <SelectItem value="blocked">Blocked only</SelectItem>
                <SelectItem value="clear">Not blocked</SelectItem>
                <SelectItem value="over_limit">Over credit limit</SelectItem>
              </SelectContent>
            </Select>

            <Select value={status} onValueChange={(next) => setParam('is_active', next)}>
              <SelectTrigger className="w-full sm:w-[9.5rem]" aria-label="Filter by status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="true">Active only</SelectItem>
                <SelectItem value="false">Inactive only</SelectItem>
                <SelectItem value="all">Any status</SelectItem>
              </SelectContent>
            </Select>

            <div className="flex items-center gap-1.5">
              <Label htmlFor="dealers-state-code" className="sr-only">
                GST state code
              </Label>
              <Input
                id="dealers-state-code"
                className="w-[7.5rem]"
                value={stateCode}
                inputMode="numeric"
                maxLength={2}
                placeholder="State 27"
                aria-invalid={stateCodeIncomplete}
                aria-describedby={stateCodeIncomplete ? 'dealers-state-code-hint' : undefined}
                // Filters on the account's addresses, not on the account row,
                // so it answers "dealers in Maharashtra" rather than "dealers
                // whose own record mentions 27".
                onChange={(event) =>
                  setParam('state_code', toStateCodeInput(event.target.value) || undefined)
                }
              />
              {stateCodeIncomplete && (
                <span id="dealers-state-code-hint" className="text-2xs text-muted-foreground">
                  Two digits
                </span>
              )}
            </div>

            {/* Hidden rather than disabled when the session carries no user id:
                there is no user picker on this screen, so "mine" is the only
                sales-owner narrowing available and it needs an id to send. */}
            {salesUserId && (
              <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
                <Checkbox
                  id="dealers-mine"
                  checked={mine}
                  onCheckedChange={(checked) =>
                    setParam('mine', checked === true ? 'true' : undefined)
                  }
                />
                <Label htmlFor="dealers-mine" className="cursor-pointer text-xs font-normal">
                  Assigned to me
                </Label>
              </div>
            )}

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />
    </>
  )
}

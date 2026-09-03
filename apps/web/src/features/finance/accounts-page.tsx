/**
 * The chart of accounts - every ledger account in the company, and what one
 * account is.
 *
 * A read-only screen, deliberately. Opening, amending and retiring an account
 * are `settings.manage` operations (the chart is configuration, not a finance
 * document), while everything reachable from here sits on `ledger.view`. A
 * button a reader's token cannot use is worse than no button, so those writes
 * are absent rather than shown and refused.
 *
 * Two shapes the contract imposes:
 *
 * **The list is flat and paginated, so this does not draw a tree.** A tree
 * needs every level in the browser at once, which is the fetch-everything-and-
 * filter-locally this codebase forbids (master rules 31/36/78). `parent_id` is
 * a server-side filter instead: open an account and drill to the accounts
 * beneath it, one level per request. The row carries no child count either,
 * which is why no row promises children it may not have.
 *
 * **A row cannot name its own parent.** `LedgerAccountRead` carries `parent_id`
 * and no parent code, and resolving a name per row would be exactly the N+1 the
 * codebase forbids. The list says only whether an account sits under another;
 * the parent is named in the detail dialog and in the drill-down chip, where it
 * is one account and one request.
 *
 * The party ledger hangs off the two control accounts, because "who makes up
 * this balance" is the next question a reader of Accounts Receivable has. The
 * statement it opens is scoped by *party*, not by this account - the copy on
 * the section says so, since the totals will not tie to the control account
 * when the party also has postings elsewhere.
 */

import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { ListTree, Lock, Scale, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DescriptionList, EmptyState, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Alert, AlertDescription, AlertTitle, Badge, Skeleton } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDate, formatDateTime, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  ACCOUNT_TYPES,
  ledgerKeys,
  NORMAL_BALANCE,
  PARTY_KIND_LABEL,
  partyKindForControlAccount,
  toPartyKind,
  type LedgerAccount,
  type LedgerAccountType,
  type PartyKind,
} from './ledger-model'
import { PartyLedgerPanel } from './party-ledger-panel'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/**
 * The status control is three-valued, and the third value reaches the URL.
 *
 * `is_active` is absent for the default (active only) and carries `true`/`false`
 * to the API, so "both" needs a word of its own that the filter recognises and
 * never forwards - spelled readably, because it is what a shared link shows.
 */
const BOTH_STATUSES = 'any'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = ['account_type', 'is_active', 'is_system', 'parent_id'] as const

/**
 * A colour per account class, so the five classes become learnable at a glance.
 *
 * None of them is `destructive`: a liability is not a problem and an expense is
 * not an error, and colouring them as one would teach the wrong thing about the
 * chart.
 */
const ACCOUNT_TYPE_VARIANT: Record<
  LedgerAccountType,
  'default' | 'secondary' | 'success' | 'muted' | 'outline'
> = {
  ASSET: 'default',
  LIABILITY: 'secondary',
  INCOME: 'success',
  EXPENSE: 'muted',
  EQUITY: 'outline',
}

/**
 * One account, named rather than shown as a UUID.
 *
 * Safe here and nowhere near a table cell: this renders for a single account -
 * the parent in the detail dialog, the account being drilled into in the
 * toolbar chip - and both share `ledgerKeys.account`, so the two of them are
 * one request. Dropped into a column it would be an N+1 per page.
 */
function AccountName({ accountId }: { accountId: string }) {
  const account = useQuery({
    queryKey: ledgerKeys.account(accountId),
    queryFn: () => api.get<LedgerAccount>(`/finance/accounts/${accountId}`),
    staleTime: staleTimes.master,
  })

  if (account.isLoading) return <Skeleton className="inline-block h-4 w-32 align-middle" />
  if (!account.data) return <span className="text-muted-foreground">Unknown account</span>

  return (
    <span>
      <span className="font-mono text-xs">{account.data.code}</span>
      <span className="text-muted-foreground"> - {account.data.name}</span>
    </span>
  )
}

/**
 * What one account is.
 *
 * Seeded with the row that was clicked - the list and the detail answer the
 * same `LedgerAccountRead`, so the dialog paints immediately - and then re-read
 * by id, which is what corrects a name or a status that changed since the page
 * was loaded.
 */
function AccountDetailDialog({
  account,
  onOpenChange,
  onShowChildren,
  onOpenPartyLedger,
}: {
  account: LedgerAccount | null
  onOpenChange: (open: boolean) => void
  onShowChildren: (accountId: string) => void
  onOpenPartyLedger: (kind: PartyKind) => void
}) {
  const { can } = useAuth()

  const detail = useQuery({
    queryKey: ledgerKeys.account(account?.id ?? ''),
    queryFn: () => api.get<LedgerAccount>(`/finance/accounts/${account?.id}`),
    enabled: Boolean(account),
    staleTime: staleTimes.master,
    placeholderData: account ?? undefined,
  })

  const shown = detail.data ?? account
  const partyKind = shown ? partyKindForControlAccount(shown.system_code) : null

  // The drill-down leads to a party picker, and that picker reads `/customers`
  // or `/vendors` - a different right from the `ledger.view` that answers for
  // the statement itself. Offering it to somebody who cannot search the party
  // master would open a section with nothing usable in it.
  const canPickParty =
    partyKind === 'customer'
      ? can(P.CUSTOMER_VIEW)
      : partyKind === 'vendor'
        ? can(P.VENDOR_VIEW)
        : false

  return (
    <Dialog open={Boolean(account)} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{shown ? `${shown.code} - ${shown.name}` : 'Ledger account'}</DialogTitle>
          <DialogDescription>
            {shown
              ? `${humanise(shown.account_type)} account. Its balance is the sum of every posting made against it.`
              : 'One account in the chart.'}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {shown && (
            <>
              <DescriptionList
                columns={2}
                items={[
                  { label: 'Code', value: <span className="font-mono text-xs">{shown.code}</span> },
                  { label: 'Name', value: shown.name },
                  {
                    label: 'Class',
                    value: (
                      <Badge variant={ACCOUNT_TYPE_VARIANT[shown.account_type]}>
                        {humanise(shown.account_type)}
                      </Badge>
                    ),
                  },
                  {
                    // A property of the class rather than a stored field: assets
                    // and expenses grow on the debit side, the other three on
                    // the credit side. Worth stating, because it is what decides
                    // whether a figure on a report reads as a plus or a minus.
                    label: 'Normal balance',
                    value: `${NORMAL_BALANCE[shown.account_type]} side`,
                  },
                  {
                    label: 'Parent',
                    value: shown.parent_id ? (
                      <AccountName accountId={shown.parent_id} />
                    ) : (
                      <span className="text-muted-foreground">Top level</span>
                    ),
                  },
                  {
                    label: 'Status',
                    value: shown.is_active ? (
                      <Badge variant="secondary">Active</Badge>
                    ) : (
                      <Badge variant="muted">Retired</Badge>
                    ),
                  },
                  { label: 'Opened', value: formatDateTime(shown.created_at) },
                  { label: 'Last amended', value: formatDateTime(shown.updated_at) },
                ]}
              />

              {shown.description && (
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Description
                  </p>
                  <p className="mt-0.5 whitespace-pre-line text-sm">{shown.description}</p>
                </div>
              )}

              {/* Being a control account is the one thing a reader has to know
                  before acting on it: the automatic postings resolve it by
                  `system_code`, not by its code or name, so renaming it is safe
                  and retiring it is refused. */}
              {shown.is_system && (
                <Alert variant="info">
                  <AlertTitle>Control account</AlertTitle>
                  <AlertDescription>
                    The finance module posts here automatically and resolves the account by its
                    system code
                    {shown.system_code ? (
                      <span className="font-mono text-xs"> {shown.system_code}</span>
                    ) : null}
                    , never by its code or name. It can be renamed; it cannot be retired.
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          {shown && (
            <Button variant="outline" onClick={() => onShowChildren(shown.id)}>
              <ListTree />
              Accounts beneath this one
            </Button>
          )}
          {partyKind && canPickParty && (
            <Button onClick={() => onOpenPartyLedger(partyKind)}>
              <Scale />
              Open a {PARTY_KIND_LABEL[partyKind]} ledger
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default function AccountsPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selected, setSelected] = useState<LedgerAccount | null>(null)

  // Every endpoint this screen reads - the account list, one account, and the
  // statement mounted below - is gated on `ledger.view`. Checking it here as
  // well as in the router keeps queries from being fired that the API would
  // answer with a 403 the reader can do nothing about.
  const canViewLedger = can(P.LEDGER_VIEW)

  const accountType = searchParams.get('account_type') ?? ''
  // Retired accounts are noise on a chart that is usually read to find where
  // something posts, so the default narrows to active and the control says so.
  const activeState = searchParams.get('is_active') ?? 'true'
  const systemState = searchParams.get('is_system') ?? ''
  const parentId = searchParams.get('parent_id') ?? ''

  const partyKind = toPartyKind(searchParams.get('party_kind'))
  const partyId = searchParams.get('party_id') ?? ''

  const list = useListQuery<LedgerAccount>({
    path: '/finance/accounts',
    queryKey: ledgerKeys.accounts,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    enabled: canViewLedger,
    filters: {
      ...(accountType ? { account_type: accountType } : {}),
      // "Active and retired" drops the restriction; the other two send the
      // boolean the API filters on.
      ...(activeState === BOTH_STATUSES ? {} : { is_active: activeState === 'true' }),
      ...(systemState ? { is_system: systemState === 'true' } : {}),
      ...(parentId ? { parent_id: parentId } : {}),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Write every key a handler changes in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` captured when the callback was created, then
   * navigates. Two calls in one handler therefore both start from the same
   * stale base and only the second survives - which is how a screen comes to
   * set `party_kind` and silently drop the `party_id` beside it.
   */
  const updateParams = useCallback(
    (changes: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          for (const [key, value] of Object.entries(changes)) {
            if (value === undefined || value === '') next.delete(key)
            else next.set(key, value)
          }
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

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

  const showChildren = useCallback(
    (accountId: string) => {
      setSelected(null)
      updateParams({ parent_id: accountId, page: undefined })
    },
    [updateParams],
  )

  const openPartyLedger = useCallback(
    (kind: PartyKind) => {
      setSelected(null)
      // A party chosen on the customer side is not a vendor, so the id goes
      // with the side it was picked on.
      updateParams({ party_kind: kind, party_id: undefined })
    },
    [updateParams],
  )

  const columns = useMemo<ColumnDef<LedgerAccount, unknown>[]>(
    () => [
      {
        // `code`, `name`, `account_type` and `created_at` are the only fields
        // `LedgerAccountRepository.sortable_fields` accepts, so every other
        // header below is fixed rather than sending a sort that 400s.
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => (
          <span className="font-mono text-xs font-medium">{row.original.code}</span>
        ),
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <div className="max-w-[24rem]">
            <span className="block truncate">{row.original.name}</span>
            {row.original.description && (
              <span className="block truncate text-2xs text-muted-foreground">
                {row.original.description}
              </span>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'account_type',
        header: 'Class',
        cell: ({ row }) => (
          <Badge variant={ACCOUNT_TYPE_VARIANT[row.original.account_type]}>
            {humanise(row.original.account_type)}
          </Badge>
        ),
      },
      {
        id: 'placement',
        header: 'Placement',
        enableSorting: false,
        // Whether an account hangs under another is the part that fits in a
        // list. *Which* one is answered in the dialog, where naming it costs
        // one request rather than one per row.
        cell: ({ row }) =>
          row.original.parent_id ? (
            <span className="text-xs text-muted-foreground">Sub-account</span>
          ) : (
            <span className="text-xs">Top level</span>
          ),
      },
      {
        id: 'kind',
        header: 'Kind',
        enableSorting: false,
        // The system code is shown rather than hidden: it is the name the
        // automatic postings resolve, so it is what a reader tracing a posting
        // is actually looking for.
        cell: ({ row }) =>
          row.original.is_system ? (
            <div>
              <Badge>Control</Badge>
              {row.original.system_code && (
                <span className="mt-0.5 block font-mono text-2xs text-muted-foreground">
                  {row.original.system_code}
                </span>
              )}
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">User-defined</span>
          ),
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_active ? (
            <Badge variant="secondary">Active</Badge>
          ) : (
            <Badge variant="muted">Retired</Badge>
          ),
      },
      {
        accessorKey: 'created_at',
        header: 'Opened',
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {formatDate(row.original.created_at)}
          </span>
        ),
      },
    ],
    [],
  )

  const header = (
    <PageHeader
      title="Chart of accounts"
      description="Every ledger account in the company. The accounts the automatic postings depend on are provisioned by the finance module itself and marked as control accounts."
      breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Chart of accounts' }]}
    />
  )

  if (!canViewLedger) {
    return (
      <div>
        {header}
        <EmptyState
          icon={Lock}
          title="The ledger is not visible to this account"
          description="Reading the chart of accounts needs the ledger view right (ledger.view). An administrator can grant it against your role."
        />
      </div>
    )
  }

  const canPickParty =
    partyKind === 'customer'
      ? can(P.CUSTOMER_VIEW)
      : partyKind === 'vendor'
        ? can(P.VENDOR_VIEW)
        : false

  return (
    <div>
      {header}

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
        onRowClick={(row) => setSelected(row)}
        emptyTitle="No accounts match"
        emptyDescription="Nothing here matches the current filters. Clear one, or widen the status to include retired accounts. A company's control accounts appear the first time the module posts to them."
        toolbar={
          <>
            <Select
              value={accountType || ALL}
              onValueChange={(next) => setParam('account_type', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[11rem]" aria-label="Account class">
                <SelectValue placeholder="Any class" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any class</SelectItem>
                {ACCOUNT_TYPES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={activeState}
              onValueChange={(next) => setParam('is_active', next === 'true' ? undefined : next)}
            >
              <SelectTrigger className="w-[12rem]" aria-label="Account status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="true">Active</SelectItem>
                <SelectItem value="false">Retired</SelectItem>
                <SelectItem value={BOTH_STATUSES}>Active and retired</SelectItem>
              </SelectContent>
            </Select>

            <Select
              value={systemState || ALL}
              onValueChange={(next) => setParam('is_system', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[12rem]" aria-label="Account kind">
                <SelectValue placeholder="Any kind" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any kind</SelectItem>
                <SelectItem value="true">Control accounts</SelectItem>
                <SelectItem value="false">User-defined</SelectItem>
              </SelectContent>
            </Select>

            {parentId && (
              <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3 text-xs">
                <ListTree className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                <span className="text-muted-foreground">Beneath</span>
                <AccountName accountId={parentId} />
                <button
                  type="button"
                  onClick={() => setParam('parent_id', undefined)}
                  aria-label="Show the whole chart again"
                  className="rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
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

      <AccountDetailDialog
        account={selected}
        onOpenChange={(open) => !open && setSelected(null)}
        onShowChildren={showChildren}
        onOpenPartyLedger={openPartyLedger}
      />

      {partyKind && (
        <section className="mt-8 space-y-4 border-t pt-6" aria-label="Party ledger">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="max-w-3xl">
              <h2 className="text-base font-semibold">
                {partyKind === 'customer' ? 'Customer ledger' : 'Vendor ledger'}
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                A control account's balance is the sum of its parties, but a statement is scoped by
                party rather than by account: it carries every posting against the party chosen
                here, including legs on other accounts. Read it as that party's position, not as a
                breakdown of one account.
              </p>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => updateParams({ party_kind: undefined, party_id: undefined })}
            >
              <X />
              Close
            </Button>
          </div>

          {canPickParty &&
            (partyKind === 'customer' ? (
              <CustomerPicker
                id="accounts-ledger-customer"
                label="Customer"
                placeholder="Choose a customer"
                value={partyId || null}
                onChange={(next) => updateParams({ party_id: next ?? undefined })}
              />
            ) : (
              <VendorPicker
                id="accounts-ledger-vendor"
                label="Vendor"
                placeholder="Choose a vendor"
                value={partyId || null}
                onChange={(next) => updateParams({ party_id: next ?? undefined })}
              />
            ))}

          {partyId ? (
            // The statement itself needs only `ledger.view`, so a party already
            // named in the URL is shown even when this account cannot search the
            // party master to choose a different one.
            <PartyLedgerPanel kind={partyKind} partyId={partyId} />
          ) : (
            <EmptyState
              icon={canPickParty ? Scale : Lock}
              title={
                canPickParty
                  ? `Choose a ${PARTY_KIND_LABEL[partyKind]}`
                  : `${partyKind === 'customer' ? 'Customers' : 'Vendors'} are not visible to this account`
              }
              description={
                canPickParty
                  ? 'A ledger is read one party at a time. Pick one to see every posting against it, with the balance after each.'
                  : `Choosing a party needs the ${partyKind === 'customer' ? 'customer' : 'vendor'} view right (${partyKind === 'customer' ? 'customer.view' : 'vendor.view'}). A statement opened from elsewhere will still show here.`
              }
            />
          )}
        </section>
      )}
    </div>
  )
}

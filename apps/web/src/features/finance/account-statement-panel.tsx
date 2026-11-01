/**
 * One general-ledger account's statement.
 *
 * The account arrives as a prop, because this panel is opened from the chart of
 * accounts where the account is already known. It is the *account* statement -
 * every posting that landed on this one account - and it is deliberately not the
 * party statement beside it: a control account's party statement spans every
 * account that party touches, while this one reads down the account.
 *
 * Two contract facts shape the screen.
 *
 * **The pages are a keyset, not a page number.** `GET /finance/ledger/account/
 * {id}` answers a statement carrying `pagination.next_cursor`, so "load more"
 * asks for the cursor the server handed back. A numbered page would make every
 * click cost more as the ledger grows, which is the scan the endpoint exists to
 * avoid.
 *
 * **The running balance is the server's, computed over the whole window.**
 * The cursor is applied outside that window, so page two starts at the balance
 * page one ended on. Summing the loaded rows in the browser would restart the
 * total at every page boundary and be wrong from the second page on.
 */

import { useInfiniteQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Lock, Scale } from 'lucide-react'
import { useId, useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, EmptyState, StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDate, formatMoney, humanise, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import {
  accountStatementRows,
  ledgerKeys,
  type AccountStatement,
  type AccountStatementRow,
} from './ledger-model'

export interface AccountStatementPanelProps {
  accountId: string
  className?: string
}

/** `/finance/ledger/account/{id}`. */
function accountStatementPath(accountId: string): string {
  return `/finance/ledger/account/${accountId}`
}

export function AccountStatementPanel({ accountId, className }: AccountStatementPanelProps) {
  const { can } = useAuth()
  const fieldId = useId()

  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  // The endpoint, the chart and every other finance read sit on `ledger.view`;
  // nothing on this panel writes, so that is the only right it needs. Gating the
  // query as well as the markup keeps a 403 from a screen that merely mounted
  // this panel from being shown to somebody who did nothing wrong.
  const canViewLedger = can(P.LEDGER_VIEW)

  const filters = useMemo(
    () => ({
      limit,
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
    }),
    [limit, dateFrom, dateTo],
  )

  const statement = useInfiniteQuery({
    queryKey: ledgerKeys.accountStatement(accountId, filters),
    queryFn: ({ pageParam }) =>
      api.get<AccountStatement>(accountStatementPath(accountId), {
        params: { ...filters, ...(pageParam ? { cursor: pageParam } : {}) },
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination?.has_next ? (lastPage.pagination.next_cursor ?? undefined) : undefined,
    enabled: canViewLedger && Boolean(accountId),
    staleTime: staleTimes.transactional,
  })

  const header = statement.data?.pages[0]
  const rows = useMemo(
    () => statement.data?.pages.flatMap((page) => accountStatementRows(page)) ?? [],
    [statement.data],
  )

  const columns = useMemo<ColumnDef<AccountStatementRow, unknown>[]>(
    () => [
      {
        id: 'entry_date',
        header: 'Date',
        // Nothing is sortable: the running balance only means anything in the
        // order the window function computed it, and a re-sorted page would put
        // balances against the wrong rows.
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex items-center gap-2">
            <span className="whitespace-nowrap">{formatDate(row.original.entry_date)}</span>
            {row.original.is_reversal && <Badge variant="muted">Reversal</Badge>}
          </div>
        ),
      },
      {
        id: 'reference',
        header: 'Reference',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <span className="block font-medium">{row.original.reference_code ?? '-'}</span>
            <span className="block text-2xs text-muted-foreground">
              {humanise(row.original.reference_type)}
            </span>
          </div>
        ),
      },
      {
        id: 'narration',
        header: 'Narration',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[24rem] truncate text-muted-foreground">
            {row.original.narration ?? '-'}
          </span>
        ),
      },
      {
        id: 'cost_centre',
        header: 'Cost centre',
        enableSorting: false,
        // A statement row may carry no cost centre at all (a bank leg, a tax
        // posting). An empty cell means "not allocated", not "unknown".
        cell: ({ row }) =>
          row.original.cost_centre_code ? (
            <div className="max-w-[12rem]">
              <span className="block font-mono text-xs">{row.original.cost_centre_code}</span>
              <span className="block truncate text-2xs text-muted-foreground">
                {row.original.cost_centre_name}
              </span>
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">-</span>
          ),
      },
      {
        id: 'party',
        header: 'Party',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.party_code || row.original.party_name ? (
            <div className="max-w-[14rem]">
              <span className="block font-mono text-xs">{row.original.party_code ?? '-'}</span>
              <span className="block truncate text-2xs text-muted-foreground">
                {row.original.party_name ?? humanise(row.original.party_type)}
              </span>
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">-</span>
          ),
      },
      {
        id: 'debit_amount',
        header: 'Debit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          toDecimal(row.original.debit_amount).isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatMoney(row.original.debit_amount)
          ),
      },
      {
        id: 'credit_amount',
        header: 'Credit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          toDecimal(row.original.credit_amount).isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatMoney(row.original.credit_amount)
          ),
      },
      {
        id: 'running_balance',
        header: 'Balance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span
            className={cn(
              'font-medium',
              // Debit-positive: a negative running balance is the credit side
              // of the account, not a loss.
              toDecimal(row.original.running_balance).isNegative() && 'text-warning',
            )}
          >
            {formatMoney(row.original.running_balance)}
          </span>
        ),
      },
    ],
    [],
  )

  if (!canViewLedger) {
    return (
      <EmptyState
        icon={Lock}
        title="The account statement is not visible to this account"
        description="Reading ledger postings needs the ledger view right (ledger.view). An administrator can grant it against your role."
      />
    )
  }

  const periodLabel = header
    ? `${header.date_from ? formatDate(header.date_from) : 'The first posting'} to ${formatDate(header.date_to)}`
    : undefined

  return (
    <section className={cn('space-y-4', className)} aria-label="Account statement">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Scale className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">
              {header
                ? `${header.account_code} - ${header.account_name}`
                : statement.isLoading
                  ? 'Loading the statement...'
                  : 'Statement unavailable'}
            </p>
            <p className="truncate text-xs text-muted-foreground">
              {header
                ? `${humanise(header.account_type)} account, debit-positive`
                : 'Account statement'}
            </p>
          </div>
        </div>

        <DateRangeFilter
          id={fieldId}
          from={dateFrom}
          to={dateTo}
          fromInputProps={{ max: dateTo || undefined }}
          toInputProps={{ min: dateFrom || undefined }}
          onFromChange={setDateFrom}
          onToChange={setDateTo}
        />
      </div>

      {/* The statement's own figures cover the whole filtered window, not the
          pages loaded so far - which is why they are read from the header. */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Opening balance"
          value={header ? formatMoney(header.opening_balance) : '-'}
          hint="Carried in from before this window"
          isLoading={statement.isLoading}
        />
        <StatTile
          label="Debits"
          value={header ? formatMoney(header.total_debit) : '-'}
          hint={periodLabel}
          isLoading={statement.isLoading}
        />
        <StatTile
          label="Credits"
          value={header ? formatMoney(header.total_credit) : '-'}
          hint={periodLabel}
          isLoading={statement.isLoading}
        />
        <StatTile
          label="Closing balance"
          value={header ? formatMoney(header.closing_balance) : '-'}
          hint="Debit-positive: negative means the account is in credit"
          isLoading={statement.isLoading}
        />
      </div>

      <DataTable
        columns={columns}
        data={rows}
        isLoading={statement.isLoading}
        isFetching={statement.isFetching && !statement.isFetchingNextPage}
        error={statement.error as Error | null}
        onRefresh={() => void statement.refetch()}
        onLimitChange={(next) => setLimit(next)}
        getRowId={(row) => row.entry_id}
        virtualise={rows.length > 150}
        maxHeight="60svh"
        emptyTitle="No postings in this window"
        emptyDescription={
          dateFrom || dateTo
            ? 'Nothing was posted against this account between those dates. Clear the dates to read the whole history.'
            : 'This account has no ledger entries yet. Postings appear here as documents are approved and posted.'
        }
      />

      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <p className="text-muted-foreground" role="status">
          {rows.length === 0
            ? 'No postings'
            : `${rows.length} ${rows.length === 1 ? 'posting' : 'postings'} loaded`}
        </p>

        {statement.hasNextPage ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => void statement.fetchNextPage()}
            loading={statement.isFetchingNextPage}
          >
            Load more
          </Button>
        ) : (
          rows.length > 0 && (
            <p className="text-xs text-muted-foreground">End of the statement for these dates.</p>
          )
        )}
      </div>
    </section>
  )
}

/**
 * One party's ledger, as a panel that can be mounted anywhere the party is
 * already known - a customer or vendor detail screen, or the control account it
 * was drilled into from on the chart of accounts.
 *
 * The party arrives as props rather than being chosen here, which is what makes
 * this reusable: the host already knows whose ledger it is showing.
 *
 * Three decisions worth explaining.
 *
 * **The running balance is the server's.** PostgreSQL computes it with a window
 * function over the whole filtered set plus the opening balance carried in.
 * Adding the rows up in the browser would restart the total on page two, and a
 * balance that is only right on page one is worse than none at all.
 *
 * **Paging state is local, not in the URL.** The panel is mounted beside views
 * that own the query string - a list with its own `page`, a tabbed detail
 * screen - and claiming `page` there would move two things with one click. The
 * host owns the URL; the panel owns its own scroll through the statement.
 *
 * **Statement rows are never filtered here.** The date window and the page go
 * to the API (master rules 31/36/78). There is no search box because the
 * endpoint takes no search parameter, and offering one that filtered the loaded
 * page would claim to search a ledger while reading fifty rows of it.
 */

import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Lock, Scale } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, EmptyState, StatTile } from '@/components/shared'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDate, formatMoney, humanise, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import {
  describeClosingBalance,
  inferStatementPagination,
  ledgerKeys,
  PARTY_KIND_LABEL,
  statementPath,
  statementRows,
  type LedgerStatement,
  type LedgerStatementRow,
  type PartyKind,
} from './ledger-model'

export interface PartyLedgerPanelProps {
  /** Which endpoint answers for this party: customers and vendors have one each. */
  kind: PartyKind
  partyId: string
  className?: string
}

export function PartyLedgerPanel({ kind, partyId, className }: PartyLedgerPanelProps) {
  const { can } = useAuth()
  const fieldId = useId()

  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  // The statement, the chart of accounts and every other finance read sit on
  // `ledger.view`; nothing on this panel writes, so it is the only right it
  // needs. Gating the query rather than only the markup matters here: the panel
  // is mounted inside screens with their own permission (a customer page opens
  // on `customer.view`), and firing a request the API will refuse would put a
  // 403 toast in front of a user who did nothing wrong.
  const canViewLedger = can(P.LEDGER_VIEW)

  // The host can swap the party without unmounting - a picker above the panel,
  // or a route parameter changing. Page 4 of the previous party's statement is
  // not a page of this one.
  useEffect(() => {
    setPage(1)
  }, [kind, partyId])

  const params = useMemo(
    () => ({
      page,
      limit,
      // The router discards the pagination block this would pay a COUNT for, so
      // asking for it buys a second scan of an append-heavy table and nothing
      // else. `inferStatementPagination` reports what is actually known.
      with_total: false,
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
    }),
    [page, limit, dateFrom, dateTo],
  )

  const statement = useQuery({
    queryKey: ledgerKeys.statement(kind, partyId, params),
    queryFn: () => api.get<LedgerStatement>(statementPath(kind, partyId), { params }),
    enabled: canViewLedger && Boolean(partyId),
    staleTime: staleTimes.transactional,
  })

  const data = statement.data
  const rows = statementRows(data)
  // Only a loaded statement has a closing balance to describe; with nothing
  // loaded the tile would otherwise read "settled", which a failed read is not.
  const closing = data ? describeClosingBalance(kind, data.closing_balance) : null

  const columns = useMemo<ColumnDef<LedgerStatementRow, unknown>[]>(
    () => [
      {
        id: 'entry_date',
        header: 'Date',
        // Nothing on a statement is sortable: the running balance is only
        // meaningful in the order the window function computed it, and a
        // re-sorted page would put balances against the wrong rows.
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex items-center gap-2">
            <span className="whitespace-nowrap">{formatDate(row.original.entry_date)}</span>
            {/* A reversal is a posting in its own right, not a deletion - it is
                marked so a reader is not left wondering why a charge appears
                twice with opposite signs. */}
            {row.original.is_reversal && <Badge variant="muted">Reversal</Badge>}
          </div>
        ),
      },
      {
        id: 'account',
        header: 'Account',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[16rem]">
            <span className="block truncate font-mono text-xs">{row.original.account_code}</span>
            <span className="block truncate text-2xs text-muted-foreground">
              {row.original.account_name}
            </span>
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
          <span className="block max-w-[26rem] truncate text-muted-foreground">
            {row.original.narration ?? '-'}
          </span>
        ),
      },
      {
        id: 'debit_amount',
        header: 'Debit',
        enableSorting: false,
        meta: { align: 'right' },
        // Exactly one of debit/credit is non-zero on a ledger row, so the empty
        // side is a dash: a column of "0.00" against every charge reads as an
        // amount that was posted and happened to be nothing.
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
              // Negative means the party is in credit, which on a statement is
              // an anomaly to notice rather than a loss.
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
        title="Ledger entries are not visible to this account"
        description={`Reading a ${PARTY_KIND_LABEL[kind]}'s postings needs the ledger view right (ledger.view). An administrator can grant it against your role.`}
      />
    )
  }

  // The window the server actually used, not the one that was asked for: with no
  // `date_to` the API dates the statement to its own today, and quoting the
  // browser's clock instead would mis-state the period on a late shift or on a
  // machine in another zone.
  const periodLabel = data
    ? `${data.date_from ? formatDate(data.date_from) : 'The first posting'} to ${formatDate(data.date_to)}`
    : undefined

  return (
    <section className={cn('space-y-4', className)} aria-label={`${PARTY_KIND_LABEL[kind]} ledger`}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Scale className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0">
            {/* The party names itself from the statement, so a panel mounted
                with nothing but an id still says whose ledger this is. */}
            <p className="truncate text-sm font-medium">
              {data
                ? data.party_name
                : statement.isLoading
                  ? 'Loading the statement...'
                  : 'Statement unavailable'}
            </p>
            <p className="truncate text-xs text-muted-foreground">
              {data
                ? `${data.party_code} - ${humanise(data.party_type)}`
                : `${PARTY_KIND_LABEL[kind]} statement`}
            </p>
          </div>
        </div>

        <DateRangeFilter
          id={fieldId}
          from={dateFrom}
          to={dateTo}
          fromInputProps={{ max: dateTo || undefined }}
          toInputProps={{ min: dateFrom || undefined }}
          onFromChange={(value) => {
            setDateFrom(value)
            // A narrower window is a different statement; page 3 of the old
            // one addresses rows this one may not contain.
            setPage(1)
          }}
          onToChange={(value) => {
            setDateTo(value)
            setPage(1)
          }}
        />
      </div>

      {/* The statement's own figures. They cover the whole filtered period, not
          the page on screen, which is why they are read from the header rather
          than summed from the rows in hand. */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Opening balance"
          value={data ? formatMoney(data.opening_balance) : '-'}
          hint="Carried in from before this window"
          isLoading={statement.isLoading}
        />
        <StatTile
          label="Debits"
          value={data ? formatMoney(data.total_debit) : '-'}
          hint={periodLabel}
          isLoading={statement.isLoading}
        />
        <StatTile
          label="Credits"
          value={data ? formatMoney(data.total_credit) : '-'}
          hint={periodLabel}
          isLoading={statement.isLoading}
        />
        <StatTile
          label="Closing balance"
          value={data ? formatMoney(data.closing_balance) : '-'}
          hint={closing?.label}
          tone={closing?.tone ?? 'default'}
          isLoading={statement.isLoading}
        />
      </div>

      <DataTable
        columns={columns}
        data={rows}
        pagination={inferStatementPagination(rows.length, page, limit)}
        isLoading={statement.isLoading}
        isFetching={statement.isFetching}
        error={statement.error as Error | null}
        onPageChange={setPage}
        onLimitChange={(next) => {
          setLimit(next)
          setPage(1)
        }}
        onRefresh={() => void statement.refetch()}
        getRowId={(row) => row.entry_id}
        emptyTitle="No postings in this window"
        emptyDescription={
          dateFrom || dateTo
            ? 'Nothing was posted against this party between those dates. Clear the dates to read the whole history.'
            : 'This party has no ledger entries yet. Postings appear here as invoices are approved and payments are recorded.'
        }
      />
    </section>
  )
}

/**
 * Party ledger - every posting against one customer or vendor.
 *
 * The running balance comes from the API, computed by PostgreSQL with a window
 * function over the whole filtered set plus the opening balance carried in. It
 * cannot be accumulated in the browser: page three of a ledger has no idea what
 * the first two pages did, and a balance that only makes sense on page one is
 * worse than no balance at all.
 *
 * `GET /finance/ledger/{customer|vendor}/{id}` answers an
 * `ApiResponse<LedgerStatement>`, not a `PaginatedResponse`: the rows arrive
 * inside a statement that also carries the opening and closing balances, so
 * `useListQuery` - which reads the envelope-level `pagination` - cannot drive
 * it. Paging state is held in the URL here instead.
 *
 * The endpoint accepts `page` and `limit` but returns no pagination block, so
 * "there is more" is inferred from a full page of rows and the total is
 * reported as unknown rather than invented.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Scale } from 'lucide-react'
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, EmptyState, StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/primitives'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useFilterWriter } from '@/hooks/use-filter-writer'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { EMPTY_VALUE, formatDate, formatMoney, humanise, toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'
import type { OffsetPagination } from '@/types/api'

import {
  financeKeys,
  type LedgerSide,
  type LedgerStatement,
  type LedgerStatementRow,
} from './finance-types'

export function PartyLedgerTab() {
  const [searchParams] = useSearchParams()

  const partyType: LedgerSide = searchParams.get('party_type') === 'VENDOR' ? 'VENDOR' : 'CUSTOMER'
  const partyId = searchParams.get('party_id') ?? ''
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''
  const page = Number(searchParams.get('page') ?? 1)
  const limit = Number(searchParams.get('limit') ?? DEFAULT_PAGE_SIZE)

  /**
   * Every key a handler changes, in ONE update: two calls in one handler both
   * start from the same captured URL and the second overwrites the first -
   * which is how choosing a party used to set `page` and drop `party_id`,
   * leaving this tab unable to load a statement. A narrowing goes back to the
   * first page; turning the page moves nothing else.
   */
  const writeFilters = useFilterWriter()
  const turnPage = useFilterWriter(null)

  const params = useMemo(
    () => ({
      page,
      limit,
      // The router discards the pagination block this would pay a COUNT for,
      // so asking for it buys a second scan of an append-heavy table and
      // nothing else; "there is more" is inferred from a full page below.
      with_total: false,
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
    }),
    [page, limit, dateFrom, dateTo],
  )

  const statement = useQuery({
    queryKey: financeKeys.ledger(partyType, partyId, params),
    queryFn: ({ signal }) =>
      api.get<LedgerStatement>(
        `/finance/ledger/${partyType === 'CUSTOMER' ? 'customer' : 'vendor'}/${partyId}`,
        { params, signal },
      ),
    // A ledger with no party is not a smaller request, it is a different one -
    // and the API would reject the path outright.
    enabled: Boolean(partyId),
    staleTime: staleTimes.transactional,
    // Hold the current page on screen while the next one loads. Without it
    // the table collapses to a skeleton on every page click - `useListQuery`
    // sets this for every list that goes through it, and these four are the
    // hand-rolled ones that did not.
    placeholderData: keepPreviousData,
  })

  const data = statement.data ?? null
  const rows = data?.rows ?? []

  /**
   * The API returns no pagination meta for a statement, so this is what is
   * actually known: which page was asked for, and whether it came back full.
   * `total` stays null - "unknown", which the table renders as such rather than
   * as a count nobody counted.
   */
  const pagination: OffsetPagination = {
    page,
    limit,
    total: null,
    total_pages: null,
    has_next: rows.length >= limit,
    has_previous: page > 1,
  }

  const columns = useMemo<ColumnDef<LedgerStatementRow, unknown>[]>(
    () => [
      {
        id: 'entry_date',
        header: 'Date',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex items-center gap-2">
            <span>{formatDate(row.original.entry_date)}</span>
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
        id: 'reference_code',
        header: 'Reference',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <span className="block font-medium">{row.original.reference_code ?? EMPTY_VALUE}</span>
            {row.original.reference_type && (
              <span className="block text-2xs text-muted-foreground">
                {humanise(row.original.reference_type)}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'narration',
        header: 'Narration',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[28rem] truncate text-muted-foreground">
            {row.original.narration ?? EMPTY_VALUE}
          </span>
        ),
      },
      {
        id: 'debit_amount',
        header: 'Debit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const value = toDecimal(row.original.debit_amount)
          return value.isZero() ? (
            <span className="text-muted-foreground">{EMPTY_VALUE}</span>
          ) : (
            formatMoney(row.original.debit_amount)
          )
        },
      },
      {
        id: 'credit_amount',
        header: 'Credit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const value = toDecimal(row.original.credit_amount)
          return value.isZero() ? (
            <span className="text-muted-foreground">{EMPTY_VALUE}</span>
          ) : (
            formatMoney(row.original.credit_amount)
          )
        },
      },
      {
        id: 'running_balance',
        header: 'Balance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const balance = toDecimal(row.original.running_balance)
          return (
            <span className={cn('font-medium', balance.isNegative() && 'text-destructive')}>
              {formatMoney(row.original.running_balance)}
            </span>
          )
        },
      },
    ],
    [],
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <div
          className="inline-flex rounded-md border border-input p-0.5"
          role="group"
          aria-label="Party kind"
        >
          {(
            [
              { value: 'CUSTOMER', label: 'Customer' },
              { value: 'VENDOR', label: 'Vendor' },
            ] as const
          ).map((side) => (
            <Button
              key={side.value}
              type="button"
              size="sm"
              variant={partyType === side.value ? 'default' : 'ghost'}
              aria-pressed={partyType === side.value}
              onClick={() => writeFilters({ party_type: side.value, party_id: undefined })}
            >
              {side.label}
            </Button>
          ))}
        </div>

        {partyType === 'CUSTOMER' ? (
          <CustomerPicker
            id="ledger-customer"
            label="Customer"
            hideLabel
            placeholder="Choose a customer"
            value={partyId || null}
            onChange={(next) => writeFilters({ party_id: next ?? undefined })}
          />
        ) : (
          <VendorPicker
            id="ledger-vendor"
            label="Vendor"
            hideLabel
            placeholder="Choose a vendor"
            value={partyId || null}
            onChange={(next) => writeFilters({ party_id: next ?? undefined })}
          />
        )}

        <DateRangeFilter
          id="ledger"
          from={dateFrom}
          to={dateTo}
          fromInputProps={{ max: dateTo || undefined }}
          toInputProps={{ min: dateFrom || undefined }}
          onFromChange={(value) => writeFilters({ date_from: value || undefined })}
          onToChange={(value) => writeFilters({ date_to: value || undefined })}
        />
      </div>

      {partyId ? (
        <>
          {/* The statement's own figures. They cover the whole filtered period,
              not the page on screen, which is why they are read from the
              statement rather than summed from the rows. */}
          {/* A dash when nothing loaded, never a formatted zero: a failed read
              must not look like a party that owes nothing. */}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Opening balance"
              value={data ? formatMoney(data.opening_balance) : EMPTY_VALUE}
              hint={
                data
                  ? `${data.party_name}${data.party_code ? ` (${data.party_code})` : ''}`
                  : undefined
              }
              isLoading={statement.isLoading}
            />
            <StatTile
              label="Debits"
              value={data ? formatMoney(data.total_debit) : EMPTY_VALUE}
              hint={
                data
                  ? `${data.date_from ? formatDate(data.date_from) : 'From the first posting'} to ${formatDate(data.date_to)}`
                  : undefined
              }
              isLoading={statement.isLoading}
            />
            <StatTile
              label="Credits"
              value={data ? formatMoney(data.total_credit) : EMPTY_VALUE}
              isLoading={statement.isLoading}
            />
            <StatTile
              label="Closing balance"
              value={data ? formatMoney(data.closing_balance) : EMPTY_VALUE}
              hint="A positive balance is money owed"
              tone={
                data && toDecimal(data.closing_balance).isNegative() ? 'destructive' : 'default'
              }
              isLoading={statement.isLoading}
            />
          </div>

          <DataTable
            columns={columns}
            data={rows}
            pagination={pagination}
            isLoading={statement.isLoading}
            isFetching={statement.isFetching}
            error={statement.error as Error | null}
            onPageChange={(next) => turnPage({ page: next })}
            onLimitChange={(next) => writeFilters({ limit: next })}
            onRefresh={() => void statement.refetch()}
            getRowId={(row) => row.entry_id}
            emptyTitle="No postings in this window"
            emptyDescription="Widen the date range to see earlier entries."
          />
        </>
      ) : (
        <EmptyState
          icon={Scale}
          title={`Choose a ${partyType === 'CUSTOMER' ? 'customer' : 'vendor'}`}
          description="A ledger is read one party at a time. Pick one above to see every posting against it."
        />
      )}
    </div>
  )
}

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

import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Scale } from 'lucide-react'
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { EmptyState, StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge, Input, Label } from '@/components/ui/primitives'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney, humanise, toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'
import type { OffsetPagination } from '@/types/api'

import {
  financeKeys,
  type LedgerSide,
  type LedgerStatement,
  type LedgerStatementRow,
} from './finance-types'

const DEFAULT_LIMIT = 50

export function PartyLedgerTab() {
  const [searchParams, setSearchParams] = useSearchParams()

  const partyType: LedgerSide = searchParams.get('party_type') === 'VENDOR' ? 'VENDOR' : 'CUSTOMER'
  const partyId = searchParams.get('party_id') ?? ''
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''
  const page = Number(searchParams.get('page') ?? 1)
  const limit = Number(searchParams.get('limit') ?? DEFAULT_LIMIT)

  /**
   * Write every key a handler changes in ONE update.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` of the render that created the callback and then
   * navigates. Two calls in one handler therefore both start from the same
   * stale base and the second navigation overwrites the first - which is how
   * choosing a party used to set `page` and silently drop `party_id`, leaving
   * this tab permanently unable to load a statement. One call, one base, every
   * key.
   */
  const updateParams = useCallback(
    (changes: Record<string, string | number | undefined>) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          for (const [key, value] of Object.entries(changes)) {
            if (value === undefined || value === '') next.delete(key)
            else next.set(key, String(value))
          }
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const params = useMemo(
    () => ({
      page,
      limit,
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
    }),
    [page, limit, dateFrom, dateTo],
  )

  const statement = useQuery({
    queryKey: financeKeys.ledger(partyType, partyId, params),
    queryFn: () =>
      api.get<LedgerStatement>(
        `/finance/ledger/${partyType === 'CUSTOMER' ? 'customer' : 'vendor'}/${partyId}`,
        { params },
      ),
    // A ledger with no party is not a smaller request, it is a different one -
    // and the API would reject the path outright.
    enabled: Boolean(partyId),
    staleTime: staleTimes.transactional,
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
            <span className="block font-medium">{row.original.reference_code ?? '-'}</span>
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
            {row.original.narration ?? '-'}
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
            <span className="text-muted-foreground">-</span>
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
            <span className="text-muted-foreground">-</span>
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
              onClick={() =>
                updateParams({
                  party_type: side.value,
                  party_id: undefined,
                  page: undefined,
                })
              }
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
            onChange={(next) => updateParams({ party_id: next ?? undefined, page: undefined })}
          />
        ) : (
          <VendorPicker
            id="ledger-vendor"
            label="Vendor"
            hideLabel
            placeholder="Choose a vendor"
            value={partyId || null}
            onChange={(next) => updateParams({ party_id: next ?? undefined, page: undefined })}
          />
        )}

        <div className="space-y-1.5">
          <Label htmlFor="ledger-from" className="text-xs">
            From
          </Label>
          <Input
            id="ledger-from"
            type="date"
            className="w-[11rem]"
            value={dateFrom}
            max={dateTo || undefined}
            onChange={(event) =>
              updateParams({ date_from: event.target.value || undefined, page: undefined })
            }
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ledger-to" className="text-xs">
            To
          </Label>
          <Input
            id="ledger-to"
            type="date"
            className="w-[11rem]"
            value={dateTo}
            min={dateFrom || undefined}
            onChange={(event) =>
              updateParams({ date_to: event.target.value || undefined, page: undefined })
            }
          />
        </div>
      </div>

      {partyId ? (
        <>
          {/* The statement's own figures. They cover the whole filtered period,
              not the page on screen, which is why they are read from the
              statement rather than summed from the rows. */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="Opening balance"
              value={formatMoney(data?.opening_balance)}
              hint={
                data
                  ? `${data.party_name}${data.party_code ? ` (${data.party_code})` : ''}`
                  : undefined
              }
              isLoading={statement.isLoading}
            />
            <StatTile
              label="Debits"
              value={formatMoney(data?.total_debit)}
              hint={
                data
                  ? `${data.date_from ? formatDate(data.date_from) : 'From the first posting'} to ${formatDate(data.date_to)}`
                  : undefined
              }
              isLoading={statement.isLoading}
            />
            <StatTile
              label="Credits"
              value={formatMoney(data?.total_credit)}
              isLoading={statement.isLoading}
            />
            <StatTile
              label="Closing balance"
              value={formatMoney(data?.closing_balance)}
              hint="A positive balance is money owed"
              tone={toDecimal(data?.closing_balance).isNegative() ? 'destructive' : 'default'}
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
            onPageChange={(next) => updateParams({ page: next })}
            onLimitChange={(next) => updateParams({ limit: next, page: undefined })}
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

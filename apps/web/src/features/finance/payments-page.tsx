/**
 * Payments and receipts - the settlement side of both ledgers.
 *
 * The register is a queue, and which queue depends on the filters: money out
 * awaiting approval, receipts banked last week, or approved payments that have
 * not been matched to a bill yet. Every one of those narrowings is a query
 * parameter the API answers - status, direction, party, method, the date window
 * and the unallocated flag - because a payment register is never pulled into the
 * browser to be filtered there (master rules 31/36/78).
 *
 * `PaymentSummary` carries `party_id` and no name, so this list cannot show one:
 * resolving a name per row is the N+1 the codebase forbids, and neither party
 * master takes an id filter to batch it with. The filter names the party
 * instead, each row is identified by its own document number, and the dialogs -
 * which show one payment at a time - resolve the name in a single request.
 *
 * Every row states its own currency and is formatted in it; nothing on this
 * screen adds two denominations together.
 *
 * The three rights this screen spans are genuinely different jobs, and the
 * affordances follow the endpoints rather than the page:
 *
 * * `payment.view` - the list and the detail dialog.
 * * `payment.record` - recording, amending, deleting a draft, and settling an
 *   approved payment against invoices. Settlement is *not* an approval: it
 *   decides which bills the money paid, never how much money moved.
 * * `payment.approve` - approving, and cancelling, which reverses the posting.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { MoreHorizontal, Plus, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  PaymentAllocationDialog,
  PaymentCommandDialog,
  PaymentDetailDialog,
  PaymentFormDialog,
} from './payment-dialogs'
import {
  canCancel,
  canSettleInvoices,
  isDraft,
  paymentKeys,
  PAYMENT_COMMANDS,
  PAYMENT_METHOD_LABELS,
  PAYMENT_METHODS,
  PAYMENT_PARTY_LABELS,
  PAYMENT_PARTY_SIDES,
  PAYMENT_STATUS_LABELS,
  PAYMENT_STATUSES,
  PAYMENT_TYPE_LABELS,
  PAYMENT_TYPE_SHORT,
  PAYMENT_TYPES,
  type PaymentCommand,
  type PaymentRow,
} from './payment-model'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = [
  'status',
  'payment_type',
  'party_type',
  'party_id',
  'payment_method',
  'date_from',
  'date_to',
  'unallocated_only',
] as const

/** One row-action, already decided: it is only built when it is permitted. */
interface MenuEntry {
  key: string
  label: string
  destructive?: boolean
  run: () => void
}

export default function PaymentsPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [formOpen, setFormOpen] = useState(false)
  const [formPaymentId, setFormPaymentId] = useState<string | null>(null)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [allocationId, setAllocationId] = useState<string | null>(null)
  const [command, setCommand] = useState<{ command: PaymentCommand; payment: PaymentRow } | null>(
    null,
  )

  // UX only - the API authorises every one of these independently
  // (master rules 38/39).
  const canRecord = can(P.PAYMENT_RECORD)
  const canApprove = can(P.PAYMENT_APPROVE)

  const status = searchParams.get('status') ?? ''
  const paymentType = searchParams.get('payment_type') ?? ''
  const partyType = searchParams.get('party_type') ?? ''
  const partyId = searchParams.get('party_id') ?? ''
  const paymentMethod = searchParams.get('payment_method') ?? ''
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''
  const unallocatedOnly = searchParams.get('unallocated_only') === 'true'

  const list = useListQuery<PaymentRow>({
    path: '/finance/payments',
    queryKey: paymentKeys.list,
    defaultSortBy: 'payment_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(paymentType ? { payment_type: paymentType } : {}),
      ...(partyType ? { party_type: partyType } : {}),
      ...(partyId ? { party_id: partyId } : {}),
      ...(paymentMethod ? { payment_method: paymentMethod } : {}),
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
      ...(unallocatedOnly ? { unallocated_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Write several filter keys in ONE navigation.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the params captured when the callback was created, so two calls in
   * a row both start from the same stale base and only the last one survives.
   * Choosing a side has to clear the party with it, and clearing has to clear
   * all eight, so both go through here.
   */
  const setFilters = useCallback(
    (next: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (value === undefined || value === '') updated.delete(key)
            else updated.set(key, value)
          }
          // The page belonged to the previous narrowing, not to this one.
          updated.delete('page')
          return updated
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const clearFilters = useCallback(() => {
    setFilters(Object.fromEntries(FILTER_KEYS.map((key) => [key, undefined])))
  }, [setFilters])

  const openForm = useCallback((paymentId: string | null) => {
    setFormPaymentId(paymentId)
    setFormOpen(true)
  }, [])

  const columns = useMemo<ColumnDef<PaymentRow, unknown>[]>(() => {
    const defs: ColumnDef<PaymentRow, unknown>[] = [
      {
        // `code`, `payment_date`, `amount`, `status` and `created_at` are the
        // only fields the API will sort on, so every other header is fixed
        // rather than sending a 400.
        accessorKey: 'code',
        header: 'Document',
        cell: ({ row }) => (
          <div>
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                setDetailId(row.original.id)
              }}
              className="font-medium hover:underline"
            >
              {row.original.code}
            </button>
            {row.original.reference_number && (
              <span className="block truncate font-mono text-2xs text-muted-foreground">
                {row.original.reference_number}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'payment_type',
        header: 'Direction',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={row.original.payment_type === 'RECEIPT' ? 'success' : 'secondary'}>
            {PAYMENT_TYPE_SHORT[row.original.payment_type]}
          </Badge>
        ),
      },
      {
        id: 'party',
        header: 'Party',
        enableSorting: false,
        // No name: the row carries an id, and one lookup per row is the N+1 the
        // codebase forbids. Opening the payment resolves it in one request.
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <Badge variant="muted">
              {row.original.party_type === 'NONE'
                ? 'No party'
                : PAYMENT_PARTY_LABELS[row.original.party_type]}
            </Badge>
            <span
              className="font-mono text-2xs text-muted-foreground"
              title={row.original.party_id}
            >
              {row.original.party_id.slice(0, 8)}
            </span>
          </span>
        ),
      },
      {
        accessorKey: 'payment_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.payment_date),
      },
      {
        id: 'payment_method',
        header: 'Method',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs">{PAYMENT_METHOD_LABELS[row.original.payment_method]}</span>
        ),
      },
      {
        id: 'currency',
        header: 'Currency',
        enableSorting: false,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.currency}</span>,
      },
      {
        accessorKey: 'amount',
        header: 'Amount',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'allocated_amount',
        header: 'Settled',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.payment_type === 'RECEIPT' ? (
            // A receipt is never allocated - there is no receivable document in
            // this contract to match it against - so a zero here would read as
            // an outstanding job rather than as "not applicable".
            <span className="text-2xs text-muted-foreground">n/a</span>
          ) : (
            formatMoney(row.original.allocated_amount, { currency: row.original.currency })
          ),
      },
      {
        id: 'unallocated_amount',
        header: 'Unsettled',
        enableSorting: false,
        meta: { align: 'right' },
        // Derived by the server, never by subtracting two columns here: the
        // payables list, the ageing report and this row must agree.
        cell: ({ row }) =>
          row.original.payment_type === 'RECEIPT' ? (
            <span className="text-2xs text-muted-foreground">n/a</span>
          ) : (
            formatMoney(row.original.unallocated_amount, { currency: row.original.currency })
          ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ]

    if (canRecord || canApprove) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const payment = row.original
          const draft = isDraft(payment)

          /**
           * Every entry is gated on the right its own endpoint enforces.
           *
           * The lifecycle three read theirs out of `PAYMENT_COMMANDS`, which is
           * the one place that records which right each endpoint checks, so a
           * menu entry cannot drift from the endpoint behind it. Amending and
           * settling are `payment.record` - settling in particular is not an
           * approval, and gating it on one would lock the clerk out of matching
           * a payment they recorded.
           */
          const entries: MenuEntry[] = []

          if (draft && canRecord) {
            entries.push({ key: 'amend', label: 'Amend draft', run: () => openForm(payment.id) })
          }
          if (draft && can(PAYMENT_COMMANDS.approve.permission)) {
            entries.push({
              key: 'approve',
              label: PAYMENT_COMMANDS.approve.label,
              run: () => setCommand({ command: 'approve', payment }),
            })
          }
          if (canSettleInvoices(payment) && canRecord) {
            entries.push({
              key: 'settle',
              label: 'Settle invoices',
              run: () => setAllocationId(payment.id),
            })
          }
          if (canCancel(payment) && can(PAYMENT_COMMANDS.cancel.permission)) {
            entries.push({
              key: 'cancel',
              label: PAYMENT_COMMANDS.cancel.label,
              destructive: true,
              run: () => setCommand({ command: 'cancel', payment }),
            })
          }
          if (draft && can(PAYMENT_COMMANDS.delete.permission)) {
            entries.push({
              key: 'delete',
              label: PAYMENT_COMMANDS.delete.label,
              destructive: true,
              run: () => setCommand({ command: 'delete', payment }),
            })
          }

          if (entries.length === 0) return null

          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for ${payment.code}`}
                  onClick={(event) => event.stopPropagation()}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {entries.map((entry) => (
                  <DropdownMenuItem
                    key={entry.key}
                    destructive={entry.destructive}
                    onSelect={entry.run}
                  >
                    {entry.label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )
        },
      })
    }

    return defs
  }, [can, canApprove, canRecord, openForm])

  const recordButton = canRecord ? (
    <Button onClick={() => openForm(null)}>
      <Plus />
      Record payment
    </Button>
  ) : null

  return (
    <div>
      <PageHeader
        title="Payments"
        description="Money out to vendors and money in from customers. A payment posts when it is approved; which invoices it settled is decided afterwards."
        breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Payments' }]}
        actions={recordButton}
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
        searchPlaceholder="Search document number or reference"
        getRowId={(row) => row.id}
        onRowClick={(row) => setDetailId(row.id)}
        emptyTitle="No payments"
        emptyDescription="Nothing matches these filters. A payment or receipt appears here as soon as it is recorded, and stays in draft until someone approves it."
        emptyAction={recordButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(next) => setParam('status', next === SELECT_ALL ? undefined : next)}
            >
              <SelectTrigger className="w-full sm:w-[11rem]" aria-label="Payment status">
                <SelectValue placeholder="Any status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any status</SelectItem>
                {PAYMENT_STATUSES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {PAYMENT_STATUS_LABELS[entry]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={paymentType || SELECT_ALL}
              onValueChange={(next) =>
                setParam('payment_type', next === SELECT_ALL ? undefined : next)
              }
            >
              <SelectTrigger className="w-full sm:w-[12rem]" aria-label="Direction">
                <SelectValue placeholder="Both directions" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Both directions</SelectItem>
                {PAYMENT_TYPES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {PAYMENT_TYPE_LABELS[entry]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={partyType || SELECT_ALL}
              onValueChange={(next) =>
                // The party goes with the side it was chosen from: a vendor id
                // filtered by `party_type=CUSTOMER` matches nothing at all.
                setFilters({
                  party_type: next === SELECT_ALL ? undefined : next,
                  party_id: undefined,
                })
              }
            >
              <SelectTrigger className="w-full sm:w-[10rem]" aria-label="Party side">
                <SelectValue placeholder="Either side" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Either side</SelectItem>
                {PAYMENT_PARTY_SIDES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {PAYMENT_PARTY_LABELS[entry]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* Only once a side is chosen: a party id means nothing until it is
                known which master it points into. */}
            {partyType === 'VENDOR' && (
              <VendorPicker
                id="payments-vendor"
                label="Vendor"
                hideLabel
                value={partyId || null}
                onChange={(next) => setParam('party_id', next ?? undefined)}
              />
            )}
            {partyType === 'CUSTOMER' && (
              <CustomerPicker
                id="payments-customer"
                label="Customer"
                hideLabel
                value={partyId || null}
                onChange={(next) => setParam('party_id', next ?? undefined)}
              />
            )}

            <Select
              value={paymentMethod || SELECT_ALL}
              onValueChange={(next) =>
                setParam('payment_method', next === SELECT_ALL ? undefined : next)
              }
            >
              <SelectTrigger className="w-full sm:w-[11rem]" aria-label="Payment method">
                <SelectValue placeholder="Any method" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any method</SelectItem>
                {PAYMENT_METHODS.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {PAYMENT_METHOD_LABELS[entry]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <DateRangeFilter
              id="payments"
              fromLabel="Paid from"
              toLabel="Paid to"
              from={dateFrom}
              to={dateTo}
              fromInputProps={{ max: dateTo || undefined }}
              toInputProps={{ min: dateFrom || undefined }}
              onFromChange={(value) => setParam('date_from', value || undefined)}
              onToChange={(value) => setParam('date_to', value || undefined)}
            />

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="payments-unallocated"
                checked={unallocatedOnly}
                onCheckedChange={(checked) =>
                  setParam('unallocated_only', checked === true ? 'true' : undefined)
                }
              />
              {/* The API's narrowing is "approved and not fully allocated",
                  which does not exclude receipts - and a receipt is never
                  allocated. Pair it with Direction to get the payables queue. */}
              <Label htmlFor="payments-unallocated" className="cursor-pointer text-xs font-normal">
                Not fully settled
              </Label>
            </div>

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />

      <PaymentDetailDialog
        paymentId={detailId}
        onClose={() => setDetailId(null)}
        // Settling is `payment.record`, so the door out of the reader appears
        // only for someone who may walk through it.
        onSettle={
          canRecord
            ? (paymentId) => {
                setDetailId(null)
                setAllocationId(paymentId)
              }
            : undefined
        }
      />

      {canRecord && (
        <>
          <PaymentFormDialog open={formOpen} onOpenChange={setFormOpen} paymentId={formPaymentId} />
          <PaymentAllocationDialog paymentId={allocationId} onClose={() => setAllocationId(null)} />
        </>
      )}

      <PaymentCommandDialog
        command={command?.command ?? null}
        payment={command?.payment ?? null}
        onClose={() => setCommand(null)}
      />
    </div>
  )
}

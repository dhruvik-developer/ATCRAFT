/**
 * Vendor invoices awaiting match and approval.
 *
 * The list is the queue; the detail dialog is where the three-way match is read
 * and the invoice is approved. Status, vendor, match outcome, the invoice date
 * window and the open/overdue narrowings are all query parameters the API
 * answers - an invoice register is never pulled into the browser to be filtered
 * there (master rules 31/36/78).
 *
 * `VendorInvoiceSummary` carries no vendor name or code, so this list cannot
 * show one: resolving a name per row would be exactly the N+1 the codebase
 * forbids, and the vendor list endpoint takes no id filter to batch it with.
 * The vendor filter names the supplier instead, and each row is identified by
 * its own document number.
 *
 * Every row states its own currency and is formatted in it; nothing on this
 * screen adds two denominations together.
 *
 * Entry lives here too. The queue used to be able to read, match and approve a
 * bill it had no way to enter, so "Record invoice" opens the same form the
 * detail dialog reopens to amend one - held on the page rather than inside the
 * detail so the two are never stacked. It is gated on `vendor_invoice.create`,
 * the right the API declares on POST, PATCH, DELETE and submit alike, and not
 * on the `vendor_invoice.approve` that clears a bill for payment.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { Plus, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
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
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  financeKeys,
  type MatchStatus,
  type VendorInvoiceDetail,
  type VendorInvoiceStatus,
  type VendorInvoiceSummary,
} from './finance-types'
import { VendorInvoiceFormDialog } from './vendor-invoice-form-dialog'
import { VendorInvoiceMatchDialog } from './vendor-invoice-match-dialog'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

const INVOICE_STATUSES: VendorInvoiceStatus[] = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'PARTIALLY_PAID',
  'PAID',
  'DISPUTED',
  'CANCELLED',
]

const MATCH_STATUSES: MatchStatus[] = ['NOT_MATCHED', 'MATCHED', 'VARIANCE', 'OVERRIDDEN']

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = [
  'status',
  'match_status',
  'vendor_id',
  'invoice_from',
  'invoice_to',
  'open_only',
  'overdue_only',
] as const

const MATCH_VARIANT: Record<MatchStatus, 'success' | 'warning' | 'destructive' | 'muted'> = {
  MATCHED: 'success',
  VARIANCE: 'destructive',
  // Approved over a variance with a recorded reason - resolved, but not clean.
  OVERRIDDEN: 'warning',
  NOT_MATCHED: 'muted',
}

export default function VendorInvoicesPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [openInvoiceId, setOpenInvoiceId] = useState<string | null>(null)

  /**
   * The entry form: closed, recording a new bill (null), or amending one.
   *
   * Held as a single piece of state so "record" and "amend" cannot both be open
   * at once, and so the invoice handed over for amendment keeps a stable
   * identity - the form resets itself whenever that prop changes.
   */
  const [formOpen, setFormOpen] = useState(false)
  const [amending, setAmending] = useState<VendorInvoiceDetail | null>(null)

  // UX only; the API authorises every one of these routes itself.
  const canRecord = can(P.VENDOR_INVOICE_CREATE)

  const status = searchParams.get('status') ?? ''
  const matchStatus = searchParams.get('match_status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const invoiceFrom = searchParams.get('invoice_from') ?? ''
  const invoiceTo = searchParams.get('invoice_to') ?? ''
  const openOnly = searchParams.get('open_only') === 'true'
  const overdueOnly = searchParams.get('overdue_only') === 'true'

  const list = useListQuery<VendorInvoiceSummary>({
    path: '/finance/vendor-invoices',
    queryKey: financeKeys.vendorInvoices,
    defaultSortBy: 'invoice_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(matchStatus ? { match_status: matchStatus } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(invoiceFrom ? { invoice_from: invoiceFrom } : {}),
      ...(invoiceTo ? { invoice_to: invoiceTo } : {}),
      ...(openOnly ? { open_only: true } : {}),
      ...(overdueOnly ? { overdue_only: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` captured when the callback was created, then
   * navigates. Seven `setParam` calls in a row therefore all started from the
   * same stale base and only the last navigation survived, so Clear removed
   * `overdue_only` and left the other six filters exactly where they were.
   */
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

  const columns = useMemo<ColumnDef<VendorInvoiceSummary, unknown>[]>(
    () => [
      {
        // Sorted by the ERP document number: `code`, `invoice_date`, `due_date`,
        // `total_amount` and `status` are the only fields the API will sort on,
        // so every other header is fixed rather than sending a 400.
        id: 'code',
        header: 'Invoice',
        cell: ({ row }) => (
          <div>
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                setOpenInvoiceId(row.original.id)
              }}
              className="font-medium hover:underline"
            >
              {row.original.vendor_invoice_number}
            </button>
            <span className="block font-mono text-2xs text-muted-foreground">
              {row.original.code}
            </span>
          </div>
        ),
      },
      {
        id: 'documents',
        header: 'Order / receipt',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex flex-col gap-0.5">
            {row.original.purchase_order_id ? (
              <Link
                to={`/procurement/purchase-orders/${row.original.purchase_order_id}`}
                className="text-xs hover:underline"
                onClick={(event) => event.stopPropagation()}
              >
                Purchase order
              </Link>
            ) : (
              <span className="text-2xs text-muted-foreground">No purchase order</span>
            )}
            <span className="text-2xs text-muted-foreground">
              {row.original.goods_receipt_id ? 'Receipted' : 'No goods receipt'}
            </span>
          </div>
        ),
      },
      {
        accessorKey: 'invoice_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.invoice_date),
      },
      {
        accessorKey: 'due_date',
        header: 'Due',
        // The API no longer reports days overdue on this row, and re-deriving
        // it from the browser clock is what the "Overdue only" filter exists to
        // avoid - that narrowing is decided server-side.
        cell: ({ row }) => formatDate(row.original.due_date),
      },
      {
        id: 'currency',
        header: 'Currency',
        enableSorting: false,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.currency}</span>,
      },
      {
        accessorKey: 'total_amount',
        header: 'Total',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.total_amount, { currency: row.original.currency }),
      },
      {
        id: 'outstanding_amount',
        header: 'Outstanding',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.outstanding_amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'match_status',
        header: 'Match',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={MATCH_VARIANT[row.original.match_status] ?? 'muted'}>
            {humanise(row.original.match_status)}
          </Badge>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [],
  )

  return (
    <div>
      <PageHeader
        title="Vendor invoices"
        description="Every supplier bill, matched line by line against what was ordered and what was actually received."
        breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Vendor invoices' }]}
        actions={
          canRecord && (
            <Button
              onClick={() => {
                setAmending(null)
                setFormOpen(true)
              }}
            >
              <Plus />
              Record invoice
            </Button>
          )
        }
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
        searchPlaceholder="Search invoice number"
        getRowId={(row) => row.id}
        onRowClick={(row) => setOpenInvoiceId(row.id)}
        emptyTitle="No vendor invoices"
        emptyDescription="Nothing matches these filters. Invoices appear here once they are booked against a purchase order."
        toolbar={
          <>
            <Select
              value={status || ALL}
              onValueChange={(next) => setParam('status', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[11rem]" aria-label="Invoice status">
                <SelectValue placeholder="Any status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any status</SelectItem>
                {INVOICE_STATUSES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={matchStatus || ALL}
              onValueChange={(next) => setParam('match_status', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[11rem]" aria-label="Match result">
                <SelectValue placeholder="Any match result" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any match result</SelectItem>
                {MATCH_STATUSES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <VendorPicker
              id="invoices-vendor"
              label="Vendor"
              hideLabel
              value={vendorId || null}
              onChange={(next) => setParam('vendor_id', next ?? undefined)}
            />

            <div className="flex items-center gap-1.5">
              <Label htmlFor="invoices-from" className="sr-only">
                Invoiced from
              </Label>
              <Input
                id="invoices-from"
                type="date"
                className="w-[10.5rem]"
                value={invoiceFrom}
                max={invoiceTo || undefined}
                onChange={(event) => setParam('invoice_from', event.target.value || undefined)}
              />
              <span className="text-xs text-muted-foreground" aria-hidden>
                to
              </span>
              <Label htmlFor="invoices-to" className="sr-only">
                Invoiced to
              </Label>
              <Input
                id="invoices-to"
                type="date"
                className="w-[10.5rem]"
                value={invoiceTo}
                min={invoiceFrom || undefined}
                onChange={(event) => setParam('invoice_to', event.target.value || undefined)}
              />
            </div>

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="invoices-open-only"
                checked={openOnly}
                onCheckedChange={(checked) =>
                  setParam('open_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="invoices-open-only" className="cursor-pointer text-xs font-normal">
                Unpaid only
              </Label>
            </div>

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="invoices-overdue-only"
                checked={overdueOnly}
                onCheckedChange={(checked) =>
                  setParam('overdue_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="invoices-overdue-only" className="cursor-pointer text-xs font-normal">
                Overdue only
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

      <VendorInvoiceMatchDialog
        invoiceId={openInvoiceId}
        onClose={() => setOpenInvoiceId(null)}
        onAmend={(invoice) => {
          // The detail closes as the form opens: one document, one dialog.
          setOpenInvoiceId(null)
          setAmending(invoice)
          setFormOpen(true)
        }}
      />

      <VendorInvoiceFormDialog
        open={formOpen}
        invoice={amending}
        onOpenChange={(next) => {
          setFormOpen(next)
          if (!next) setAmending(null)
        }}
        // Straight back to the document that was just written, so a new bill
        // can be submitted and an amended one re-matched without hunting for it
        // in the list.
        onSaved={(saved) => setOpenInvoiceId(saved.id)}
      />
    </div>
  )
}

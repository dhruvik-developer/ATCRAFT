/**
 * Vendor quotations, and the RFQ comparison that decides between them.
 *
 * Two screens in one, because they are one job. The table is the register of
 * every offer received; narrowing it to a single RFQ turns the page into the
 * side-by-side board a buyer actually chooses from, with the decision taken
 * from the column the price is on rather than from a grid row several clicks
 * away.
 *
 * Every narrowing - status, vendor, RFQ, and the validity date - is a query
 * parameter the API answers. A company that runs three RFQs a week accumulates
 * quotations faster than anything else in procurement, and the browser never
 * receives more than one page of them (master rules 31/36/78).
 *
 * `QuotationSummary` carries `vendor_id` and `rfq_id` and no names for either.
 * The vendor is named through the bounded `/vendors/options` projection that
 * the order and receipt grids already use - one cached reference query, not a
 * lookup per row. The RFQ is not named on a row at all: there is no equivalent
 * projection for RFQs, and resolving one per row is exactly the N+1 the rules
 * forbid. The RFQ column therefore *filters* to that RFQ instead, and the
 * comparison board it opens states the code itself.
 *
 * Sorting is restricted to what `QuotationRepository.sortable_fields` accepts -
 * quotation date, validity, total, lead time, status and creation - so no
 * header offers a sort the API answers with a 400.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { GitCompareArrows, MoreHorizontal, Plus, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
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
import { Badge, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatMoney, formatNumber, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import {
  QuotationDecisionDialog,
  QuotationDetailDialog,
  QuotationFormDialog,
} from './quotation-dialogs'
import {
  QUOTATION_STATUSES,
  decidableFromRow,
  hasLapsed,
  isDecidable,
  isQuotationRevisable,
  quotationKeys,
  type DecidableQuotation,
  type QuotationDecision,
  type QuotationRow,
} from './quotation-model'
import { RfqComparisonPanel } from './rfq-comparison-panel'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = ['status', 'vendor_id', 'rfq_id', 'valid_on'] as const

export default function QuotationsPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canManage = can(P.QUOTATION_MANAGE)

  const [detailId, setDetailId] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [formQuotationId, setFormQuotationId] = useState<string | null>(null)
  const [pending, setPending] = useState<{
    quotation: DecidableQuotation
    decision: QuotationDecision
  } | null>(null)

  const status = searchParams.get('status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const rfqId = searchParams.get('rfq_id') ?? ''
  const validOn = searchParams.get('valid_on') ?? ''

  const list = useListQuery<QuotationRow>({
    path: '/procurement/quotations',
    queryKey: quotationKeys.list,
    defaultSortBy: 'quotation_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(rfqId ? { rfq_id: rfqId } : {}),
      ...(validOn ? { valid_on: validOn } : {}),
    },
  })

  // Names for the vendor column. One bounded reference query shared with every
  // other procurement screen, rather than a lookup per row.
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` captured when the callback was created, then
   * navigates. Four `setParam` calls in a row would all start from the same
   * stale base and only the last navigation would survive - the bug the vendor
   * invoice register already shipped once.
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

  const openRevision = (quotationId: string) => {
    setDetailId(null)
    setFormQuotationId(quotationId)
    setFormOpen(true)
  }

  const openDecision = (quotation: DecidableQuotation, decision: QuotationDecision) => {
    setDetailId(null)
    setPending({ quotation, decision })
  }

  const columns = useMemo<ColumnDef<QuotationRow, unknown>[]>(() => {
    const defs: ColumnDef<QuotationRow, unknown>[] = [
      {
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const vendor = vendorById.get(row.original.vendor_id)
          if (!vendor) {
            // A suspended vendor is not in the picker projection, and the row
            // carries no name of its own. A dash is honest; the quotation's own
            // dialog identifies the offer by the vendor's reference.
            return <span className="text-muted-foreground">-</span>
          }
          return (
            <div className="max-w-[16rem]">
              <span className="block truncate">{vendor.name}</span>
              <span className="block truncate text-xs text-muted-foreground">{vendor.code}</span>
            </div>
          )
        },
      },
      {
        id: 'vendor_reference',
        header: 'Quotation no.',
        // Searchable server-side (`QuotationRepository.searchable_fields`) but
        // not sortable, so the header does not offer a sort the API refuses.
        enableSorting: false,
        cell: ({ row }) => (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              setDetailId(row.original.id)
            }}
            className="rounded-sm font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {row.original.vendor_reference}
          </button>
        ),
      },
      {
        accessorKey: 'quotation_date',
        header: 'Quoted',
        cell: ({ row }) => formatDate(row.original.quotation_date),
      },
      {
        accessorKey: 'valid_until',
        header: 'Valid until',
        cell: ({ row }) =>
          row.original.valid_until === null ? (
            // Nullable, and a different claim from a date in the past: an
            // open-ended offer never lapses.
            <span className="text-muted-foreground">Open-ended</span>
          ) : (
            <span className="flex items-center gap-2">
              {formatDate(row.original.valid_until)}
              {/* The browser's reading, shown as a hint. The API decides the
                  question for real when a selection is attempted. */}
              {hasLapsed(row.original.valid_until) && <Badge variant="warning">Lapsed</Badge>}
            </span>
          ),
      },
      {
        accessorKey: 'lead_time_days',
        header: 'Lead time',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatNumber(row.original.lead_time_days)} d</span>
        ),
      },
      {
        id: 'payment_terms_days',
        header: 'Terms',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatNumber(row.original.payment_terms_days)} d</span>
        ),
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
        // Every row states its own currency and is formatted in it; nothing on
        // this screen adds two denominations together.
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.total_amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => (
          <div className="flex items-center gap-1.5">
            <StatusBadge status={row.original.status} />
            {row.original.converted_purchase_order_id && <Badge variant="secondary">Ordered</Badge>}
          </div>
        ),
      },
      {
        id: 'rfq',
        header: 'RFQ',
        enableSorting: false,
        // The row carries the RFQ's id and no code, and one lookup per row to
        // resolve it is the N+1 the rules forbid. Narrowing the list to that
        // RFQ opens the comparison board, which names it properly.
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Compare every quotation on this RFQ"
            onClick={(event) => {
              event.stopPropagation()
              setParam('rfq_id', row.original.rfq_id)
            }}
          >
            <GitCompareArrows />
          </Button>
        ),
      },
    ]

    // Gating is UX only - the API authorises each of these independently, and
    // all three sit behind `quotation.manage`, never the `quotation.view` that
    // rendered the table.
    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const decidable = isDecidable(row.original.status)
          const revisable = isQuotationRevisable(row.original.status)
          if (!decidable && !revisable) {
            // SELECTED, REJECTED and EXPIRED are terminal in QUOTATION_LIFECYCLE.
            // An empty cell is right: there is no command left to offer.
            return null
          }
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for quotation ${row.original.vendor_reference}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {decidable && (
                  <DropdownMenuItem
                    onSelect={() => openDecision(decidableFromRow(row.original), 'select')}
                  >
                    Select this quotation
                  </DropdownMenuItem>
                )}
                {revisable && (
                  <DropdownMenuItem onSelect={() => openRevision(row.original.id)}>
                    Record revised offer
                  </DropdownMenuItem>
                )}
                {decidable && (
                  <DropdownMenuItem
                    destructive
                    onSelect={() => openDecision(decidableFromRow(row.original), 'reject')}
                  >
                    Reject
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )
        },
      })
    }

    return defs
  }, [canManage, setParam, vendorById])

  const recordButton = canManage ? (
    <Button
      onClick={() => {
        setFormQuotationId(null)
        setFormOpen(true)
      }}
    >
      <Plus />
      Record quotation
    </Button>
  ) : null

  return (
    <div>
      <PageHeader
        title="Vendor quotations"
        description="Every offer received against an RFQ. Narrow the list to one RFQ to compare the vendors side by side and choose between them."
        actions={recordButton}
      />

      {/* The board only exists for one RFQ at a time, so it appears exactly
          when the list has been narrowed to one. */}
      {rfqId && (
        <div className="mb-6">
          <RfqComparisonPanel
            rfqId={rfqId}
            onRecordQuotation={
              canManage
                ? () => {
                    setFormQuotationId(null)
                    setFormOpen(true)
                  }
                : undefined
            }
          />
        </div>
      )}

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
        searchPlaceholder="Search the vendor's quotation number..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setDetailId(row.id)}
        emptyTitle="No quotations found"
        emptyDescription={
          filterCount > 0
            ? 'Nothing matches these filters. Clear one, or record what the vendor quoted.'
            : 'Quotations appear here once a vendor answers an RFQ. Record what each one quoted and the comparison ranks them for you.'
        }
        emptyAction={recordButton}
        toolbar={
          <>
            <Select
              value={status || ALL}
              onValueChange={(next) => setParam('status', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[11rem]" aria-label="Quotation status">
                <SelectValue placeholder="Any status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any status</SelectItem>
                {QUOTATION_STATUSES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* The vendor search reads `/vendors`, which enforces `vendor.view`.
                Offering it without that right earns a 403 on every keystroke. */}
            {can(P.VENDOR_VIEW) && (
              <VendorPicker
                id="quotations-vendor"
                label="Vendor"
                hideLabel
                value={vendorId || null}
                onChange={(next) => setParam('vendor_id', next ?? undefined)}
              />
            )}

            <div className="flex items-center gap-1.5">
              <Label htmlFor="quotations-valid-on" className="sr-only">
                Still valid on
              </Label>
              <Input
                id="quotations-valid-on"
                type="date"
                className="w-[10.5rem]"
                value={validOn}
                aria-label="Show only offers still valid on this date"
                onChange={(event) => setParam('valid_on', event.target.value || undefined)}
              />
            </div>

            {rfqId && (
              <Badge variant="secondary" className="h-9 px-3">
                Narrowed to one RFQ
              </Badge>
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

      <QuotationDetailDialog
        quotationId={detailId}
        onClose={() => setDetailId(null)}
        onDecide={canManage ? openDecision : undefined}
        onRevise={canManage ? (quotation) => openRevision(quotation.id) : undefined}
      />

      <QuotationDecisionDialog
        quotation={pending?.quotation ?? null}
        decision={pending?.decision ?? 'select'}
        onOpenChange={(open) => !open && setPending(null)}
      />

      {canManage && (
        <QuotationFormDialog
          open={formOpen}
          onOpenChange={setFormOpen}
          quotationId={formQuotationId}
          // A quotation raised while the list is narrowed to one RFQ is
          // overwhelmingly for that RFQ; the picker is still there to change it.
          defaultRfqId={rfqId || null}
        />
      )}
    </div>
  )
}

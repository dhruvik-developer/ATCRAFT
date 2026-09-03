/**
 * The RFQ comparison board: one column per vendor, one row per item.
 *
 * This is the screen a buyer opens for every purchase they make, so the API
 * builds the whole board in four batched reads and hands it over denormalised -
 * vendor code and name, item code and name, the effective unit price after
 * discount, a score and a rank per offer, and the flags that say which offer is
 * the cheapest and which is the fastest. Nothing here needs a lookup, and
 * nothing here recomputes what the API already decided.
 *
 * What this panel adds on top of the payload is the money question the payload
 * leaves implicit: how much more each vendor costs than the cheapest offer that
 * is still valid. That figure is computed with `toDecimal`, because a rupee of
 * float error in a purchasing decision is a real defect.
 *
 * Three rules the board obeys, all of them the API's:
 *
 * - **Lapsed offers stay on the board.** `rank_offers` ranks an expired offer
 *   last with a zero score rather than dropping it, so "the buyer needs to see
 *   that the cheapest price on the table has lapsed, not to wonder why a vendor
 *   they remember quoting has vanished". The comparison against the cheapest
 *   therefore measures against the cheapest *live* quote, never a lapsed one.
 * - **A mixed-currency board is not scored.** The API names no recommendation
 *   and writes the reason; this panel shows that reason and suppresses its own
 *   arithmetic rather than comparing magnitudes across currencies.
 * - **An item nobody quoted is not zero.** Rows are built from the union of the
 *   RFQ's lines and every line any vendor quoted, so a missing offer is a gap
 *   in the vendor's response and reads as "not quoted".
 *
 * Reading the board is `quotation.view`. Selecting or rejecting from it is
 * `quotation.manage` - a different right, and the one the two decision
 * endpoints actually enforce.
 */

import { useQuery } from '@tanstack/react-query'
import { Check, Inbox, Zap } from 'lucide-react'
import { useMemo, useState } from 'react'

import { EmptyState, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle, Badge, Skeleton } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import {
  formatDate,
  formatMoney,
  formatNumber,
  formatPercent,
  formatQuantity,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { QuotationDecisionDialog } from './quotation-dialogs'
import {
  cheapestLiveColumn,
  decidableFromColumn,
  isDecidable,
  linesWonByVendor,
  offersByVendor,
  quotationKeys,
  type DecidableQuotation,
  type QuotationComparison,
  type QuotationDecision,
} from './quotation-model'

function VendorRating({ rating, sampleSize }: { rating: string | null; sampleSize: number }) {
  if (rating === null) {
    // Never rendered as zero: "no evidence yet" is a different claim from "bad".
    return <span className="text-muted-foreground">Unrated</span>
  }
  return (
    <span className="tabular">
      {toDecimal(rating).toDecimalPlaces(1).toFixed(1)} / 5
      <span className="text-muted-foreground"> ({formatNumber(sampleSize)})</span>
    </span>
  )
}

export function RfqComparisonPanel({
  rfqId,
  onRecordQuotation,
}: {
  rfqId: string
  /**
   * Opens the caller's "record a quotation" form from the empty state. Omitted
   * by a host that has no such form; the empty state then only explains why the
   * board is empty.
   */
  onRecordQuotation?: () => void
}) {
  const { can } = useAuth()
  const canManage = can(P.QUOTATION_MANAGE)
  const [pending, setPending] = useState<{
    quotation: DecidableQuotation
    decision: QuotationDecision
  } | null>(null)

  const comparison = useQuery({
    queryKey: quotationKeys.comparison(rfqId),
    queryFn: () => api.get<QuotationComparison>(`/procurement/rfqs/${rfqId}/comparison`),
    enabled: Boolean(rfqId),
    staleTime: staleTimes.transactional,
  })

  const board = comparison.data

  const linesWon = useMemo(
    () => (board ? linesWonByVendor(board) : new Map<string, number>()),
    [board],
  )
  const cheapest = useMemo(() => (board ? cheapestLiveColumn(board) : null), [board])

  if (comparison.isLoading) {
    return (
      <div className="space-y-3" aria-hidden>
        <Skeleton className="h-5 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (comparison.error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not build the comparison</AlertTitle>
        <AlertDescription>{comparison.error.message}</AlertDescription>
      </Alert>
    )
  }

  if (!board) return null

  if (board.vendors.length === 0) {
    return (
      <EmptyState
        icon={Inbox}
        title={`No quotations received on ${board.rfq_code}`}
        description="Nothing to compare yet. Record what each vendor quoted and their offers appear here side by side, priced and ranked."
        action={
          canManage && onRecordQuotation ? (
            <Button onClick={onRecordQuotation}>Record a quotation</Button>
          ) : undefined
        }
      />
    )
  }

  const totalRows = board.items.length

  return (
    <section aria-label={`Quotation comparison for ${board.rfq_code}`} className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">Comparison for {board.rfq_code}</h2>
        <StatusBadge status={board.rfq_status} />
        <span className="text-xs text-muted-foreground">
          {formatNumber(board.vendors.length)} vendor
          {board.vendors.length === 1 ? '' : 's'} · {formatNumber(totalRows)} item
          {totalRows === 1 ? '' : 's'}
        </span>
      </div>

      {/* The API writes this sentence, including the case where it declines to
          name a winner. Restating it in the screen's own words would let the two
          drift apart. */}
      <Alert variant={board.recommended_vendor_id ? 'info' : 'warning'}>
        <AlertTitle>
          {board.recommended_vendor_id ? 'Recommendation' : 'No recommendation'}
        </AlertTitle>
        <AlertDescription>{board.recommendation_reason}</AlertDescription>
      </Alert>

      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full min-w-[48rem] border-collapse text-sm">
          <caption className="sr-only">
            Every vendor&rsquo;s offer for every item on {board.rfq_code}
          </caption>
          <thead>
            <tr className="border-b bg-muted/40 align-top">
              <th scope="col" className="w-[16rem] px-3 py-3 text-left font-medium">
                Item
              </th>
              {board.vendors.map((column) => {
                const isRecommended = column.vendor_id === board.recommended_vendor_id
                const won = linesWon.get(column.vendor_id) ?? 0
                // Suppressed on a mixed-currency board: subtracting 9,000 INR
                // from 100 EUR produces a number, and it means nothing.
                const gap =
                  cheapest && !board.mixed_currency
                    ? toDecimal(column.total_amount).minus(toDecimal(cheapest.total_amount))
                    : null

                return (
                  <th
                    key={column.quotation_id}
                    scope="col"
                    className={cn(
                      'min-w-[13rem] border-l px-3 py-3 text-left font-normal',
                      isRecommended && 'bg-success/8',
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-medium">{column.vendor_code}</span>
                      {isRecommended && <Badge variant="success">Recommended</Badge>}
                      {column.is_expired && <Badge variant="warning">Lapsed</Badge>}
                    </div>
                    <p
                      className="truncate text-xs text-muted-foreground"
                      title={column.vendor_name}
                    >
                      {column.vendor_name}
                    </p>

                    <p className="mt-1.5 tabular text-base font-semibold">
                      {formatMoney(column.total_amount, { currency: column.currency })}
                    </p>
                    {gap !== null && (
                      <p className="tabular text-xs">
                        {gap.isZero() ? (
                          <span className="text-success">Cheapest still valid</span>
                        ) : gap.isPositive() ? (
                          <span className="text-warning">
                            +
                            {formatMoney(gap.toFixed(), {
                              currency: column.currency,
                              showSymbol: false,
                            })}{' '}
                            vs cheapest
                          </span>
                        ) : (
                          <span className="text-muted-foreground">
                            {formatMoney(gap.abs().toFixed(), {
                              currency: column.currency,
                              showSymbol: false,
                            })}{' '}
                            below the cheapest live quote
                          </span>
                        )}
                      </p>
                    )}

                    <dl className="mt-2 space-y-0.5 text-xs text-muted-foreground">
                      <div className="flex justify-between gap-2">
                        <dt>Lead time</dt>
                        <dd className="tabular">{formatNumber(column.lead_time_days)} d</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt>Payment terms</dt>
                        <dd className="tabular">{formatNumber(column.payment_terms_days)} d</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt>Rating</dt>
                        <dd>
                          <VendorRating
                            rating={column.vendor_rating}
                            sampleSize={column.rating_sample_size}
                          />
                        </dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt>Valid until</dt>
                        <dd>
                          {column.valid_until ? formatDate(column.valid_until) : 'Open-ended'}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt>Quoted</dt>
                        <dd className="tabular">
                          {formatNumber(column.quoted_item_count)} of {formatNumber(totalRows)}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt>Best on</dt>
                        <dd className="tabular">
                          {formatNumber(won)} line{won === 1 ? '' : 's'}
                        </dd>
                      </div>
                    </dl>

                    <div className="mt-2 flex items-center gap-1.5">
                      <StatusBadge status={column.quotation_status} />
                    </div>

                    {/* Both endpoints behind these buttons enforce
                        `quotation.manage`, not the `quotation.view` that built
                        this board. A settled quotation gets no buttons at all:
                        SELECTED, REJECTED and EXPIRED are terminal states and
                        `assert_transition` refuses every edge out of them. */}
                    {canManage && isDecidable(column.quotation_status) && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        <Button
                          size="sm"
                          onClick={() =>
                            setPending({
                              quotation: decidableFromColumn(column, board),
                              decision: 'select',
                            })
                          }
                        >
                          Select
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            setPending({
                              quotation: decidableFromColumn(column, board),
                              decision: 'reject',
                            })
                          }
                        >
                          Reject
                        </Button>
                      </div>
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>

          <tbody className="divide-y">
            {board.items.map((row) => {
              const offers = offersByVendor(row)
              return (
                <tr key={row.item_id} className="align-top">
                  <th scope="row" className="px-3 py-3 text-left font-normal">
                    <span className="block font-medium">{row.item_code}</span>
                    <span className="block text-xs text-muted-foreground">{row.item_name}</span>
                    <span className="mt-1 block tabular text-xs text-muted-foreground">
                      {/* The unit of measure arrives as an id with no code, so
                          the quantity is shown unqualified rather than against
                          an invented unit. */}
                      {formatQuantity(row.quantity)} required
                    </span>
                    {row.recommendation_reason && (
                      <span className="mt-1 block text-2xs text-muted-foreground">
                        {row.recommendation_reason}
                      </span>
                    )}
                  </th>

                  {board.vendors.map((column) => {
                    const offer = offers.get(column.vendor_id)
                    const isBest = row.recommended_vendor_id === column.vendor_id

                    if (!offer) {
                      return (
                        <td
                          key={column.quotation_id}
                          className="border-l px-3 py-3 text-muted-foreground"
                        >
                          Not quoted
                        </td>
                      )
                    }

                    const hasDiscount = !toDecimal(offer.discount_percentage).isZero()

                    return (
                      <td
                        key={column.quotation_id}
                        className={cn(
                          'border-l px-3 py-3',
                          isBest && 'bg-success/8',
                          offer.is_expired && 'opacity-60',
                        )}
                      >
                        <div className="flex items-baseline gap-1.5">
                          <span className="tabular font-medium">
                            {formatMoney(offer.effective_unit_price, {
                              currency: column.currency,
                            })}
                          </span>
                          {isBest && (
                            <>
                              <Check className="h-3.5 w-3.5 text-success" aria-hidden />
                              <span className="sr-only">Recommended for this item</span>
                            </>
                          )}
                        </div>

                        {hasDiscount && (
                          <p className="text-2xs text-muted-foreground">
                            list {formatMoney(offer.unit_price, { currency: column.currency })} less{' '}
                            {formatPercent(offer.discount_percentage, 2)}
                          </p>
                        )}

                        <p className="tabular text-xs text-muted-foreground">
                          {formatMoney(offer.line_total, { currency: column.currency })} for{' '}
                          {formatQuantity(offer.quantity)}
                        </p>

                        <div className="mt-1.5 flex flex-wrap items-center gap-1">
                          {offer.is_lowest_price && <Badge variant="success">Lowest</Badge>}
                          {offer.is_fastest && (
                            <Badge variant="default">
                              <Zap className="h-3 w-3" aria-hidden />
                              Fastest
                            </Badge>
                          )}
                          {offer.is_expired && <Badge variant="muted">Lapsed</Badge>}
                          <span className="tabular text-2xs text-muted-foreground">
                            {formatNumber(offer.lead_time_days)} d · rank {formatNumber(offer.rank)}
                          </span>
                        </div>
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <QuotationDecisionDialog
        quotation={pending?.quotation ?? null}
        decision={pending?.decision ?? 'select'}
        onOpenChange={(open) => !open && setPending(null)}
      />
    </section>
  )
}

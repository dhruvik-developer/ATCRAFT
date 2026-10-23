/**
 * The three things a buyer does with one vendor quotation: read it, record or
 * revise it, and decide on it.
 *
 * The decision dialog is the important one. Selecting a quotation is the buying
 * decision - everything downstream, the purchase order and the money, follows
 * from it - so it is never a one-click action on a grid row. It states what is
 * being chosen, what the alternatives cost, whether it agrees with the
 * comparison's recommendation, and it will not proceed without the buyer's
 * reason, which the API requires "even when following the recommendation:
 * the buyer's judgement is the record, and the comparison's score is only its
 * input".
 *
 * Permissions, which are not all the same and are the thing this codebase gets
 * wrong most often:
 *
 * - reading a quotation, its lines and the RFQ comparison: `quotation.view`
 * - recording, revising, selecting and rejecting: `quotation.manage`
 * - listing RFQs to record a quotation against one: `rfq.view`, enforced by
 *   `/procurement/rfqs`, which is a different right from either of the above
 * - searching the vendor master: `vendor.view`
 *
 * A screen that offered "Select" behind `quotation.view` would be handing the
 * buying decision to anyone allowed to look at prices.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Plus, RefreshCw, Search, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { z } from 'zod'

import { DescriptionList, FieldError, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { DEFAULT_CURRENCY, PICKER_PAGE_SIZE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatQuantity,
  toDateInput,
  toDecimal,
} from '@/lib/format'
import { describeError, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  MAX_DECISION_REASON,
  MIN_DECISION_REASON,
  decidableFromRow,
  hasLapsed,
  isDecidable,
  isQuotationRevisable,
  quotationKeys,
  totalsStanding,
  type DecidableQuotation,
  type QuotationComparison,
  type QuotationCreatePayload,
  type QuotationDecision,
  type QuotationDetail,
  type QuotationLinePayload,
  type QuotationRow,
  type QuotationUpdatePayload,
} from './quotation-model'
import { RFQ_TERMINAL_STATUSES, rfqKeys, type RfqRow } from './rfq-model'

// --------------------------------------------------------------- helpers ----

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/**
 * The reason both decisions require.
 *
 * `QuotationDecisionRequest` makes it mandatory on select *and* reject, with a
 * 3-255 character bound, and the field says why: the buyer's judgement is the
 * record. The maximum is enforced by the control rather than only by the
 * validator, so a long paragraph is stopped where it is typed instead of coming
 * back as a 422 after the decision was thought to be made.
 */
function DecisionReasonField({
  id,
  value,
  onChange,
  invalid,
}: {
  id: string
  value: string
  onChange: (next: string) => void
  invalid: boolean
}) {
  return (
    <div>
      <Label htmlFor={id} required>
        Reason
      </Label>
      <Textarea
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={3}
        maxLength={MAX_DECISION_REASON}
        autoComplete="off"
        className="mt-1.5"
        aria-invalid={invalid}
      />
      <p className="mt-1 text-xs text-muted-foreground">
        Recorded on the quotation and in the audit trail. This is the buyer&rsquo;s justification,
        not the comparison&rsquo;s score.
      </p>
      {invalid && (
        <FieldError
          message={`Record at least ${MIN_DECISION_REASON} characters of reason (at most ${MAX_DECISION_REASON}).`}
        />
      )}
    </div>
  )
}

// ------------------------------------------------------------- RfqPicker ----

/**
 * Pick the RFQ a quotation answers, searched on the server.
 *
 * Not exported: an RFQ picker belongs to whoever owns the RFQ screens, and this
 * one exists only because a quotation cannot be recorded without naming its
 * RFQ. It reads `/procurement/rfqs`, which enforces `rfq.view` - a different
 * right from the `quotation.manage` that lets this form post - so the caller
 * checks that right before rendering it.
 *
 * A closed or cancelled RFQ is listed but cannot be chosen: `_load_rfq_for_quoting`
 * refuses one with "it can no longer take quotations", and finding that out
 * after the whole sheet has been keyed in wastes the entry. It is listed rather
 * than filtered out so a buyer searching for a code they remember is told why
 * it is unavailable instead of concluding the RFQ has vanished.
 */
function RfqPicker({
  id,
  value,
  onChange,
  invalid = false,
}: {
  id: string
  value: string | null
  onChange: (rfqId: string | null) => void
  invalid?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [picked, setPicked] = useState<RfqRow | null>(null)

  // One request per keystroke would put a scan of the RFQ register behind every
  // character typed.
  const debouncedTerm = useDebouncedValue(term)

  const searchParams = {
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    sort_by: 'rfq_date',
    sort_dir: 'desc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: rfqKeys.list(searchParams),
    queryFn: () => api.getPage<RfqRow>('/procurement/rfqs', { params: searchParams }),
    enabled: open,
    staleTime: staleTimes.transactional,
  })

  // An RFQ arriving as a bare id - prefilled from the list's own filter - is
  // resolved by id so the field reads as a code rather than a UUID.
  const detail = useQuery({
    queryKey: rfqKeys.detail(value ?? ''),
    queryFn: () => api.get<RfqRow>(`/procurement/rfqs/${value}`),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.transactional,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null
  const rows = results.data?.data ?? []

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} required>
        RFQ
      </Label>
      <div className="flex items-center gap-1">
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              id={id}
              type="button"
              variant="outline"
              role="combobox"
              aria-expanded={open}
              aria-label="RFQ"
              aria-invalid={invalid}
              className={cn(
                'h-9 min-w-[200px] flex-1 justify-between font-normal',
                !selected && 'text-muted-foreground',
                invalid && 'border-destructive',
              )}
            >
              <span className="truncate">{selected ? selected.code : 'Search open RFQs'}</span>
              <ChevronsUpDown className="opacity-50" aria-hidden />
            </Button>
          </PopoverTrigger>

          <PopoverContent
            align="start"
            collisionPadding={8}
            className="w-[24rem] max-w-[var(--radix-popover-content-available-width)] p-0"
          >
            <div className="relative border-b p-2">
              <Search
                className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                autoFocus
                value={term}
                onChange={(event) => setTerm(event.target.value)}
                placeholder="Search by RFQ code"
                aria-label="Search requests for quotation"
                className="pl-8"
              />
            </div>

            <div className="max-h-64 overflow-y-auto p-1" role="listbox" aria-label="RFQ">
              {results.isLoading && (
                <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                  Searching...
                </p>
              )}

              {!results.isLoading && results.error && (
                <p className="px-2 py-6 text-sm text-destructive">{describeError(results.error)}</p>
              )}

              {!results.isLoading && !results.error && rows.length === 0 && (
                <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                  {debouncedTerm
                    ? 'No RFQ matches that search.'
                    : 'Type to search requests for quotation.'}
                </p>
              )}

              {rows.map((rfq) => {
                const settled = RFQ_TERMINAL_STATUSES.includes(rfq.status)
                return (
                  <button
                    key={rfq.id}
                    type="button"
                    role="option"
                    aria-selected={rfq.id === value}
                    disabled={settled}
                    onClick={() => {
                      setPicked(rfq)
                      onChange(rfq.id)
                      setOpen(false)
                    }}
                    className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50"
                  >
                    <Check
                      className={cn('mt-0.5 h-4 w-4 shrink-0', rfq.id !== value && 'invisible')}
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate font-medium">{rfq.code}</span>
                        <StatusBadge status={rfq.status} />
                      </span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {formatDate(rfq.rfq_date)}
                        {settled && ' - no longer accepts quotations'}
                      </span>
                    </span>
                  </button>
                )
              })}
            </div>
          </PopoverContent>
        </Popover>

        {value && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Clear RFQ"
            onClick={() => {
              setPicked(null)
              onChange(null)
            }}
          >
            <X />
          </Button>
        )}
      </div>
    </div>
  )
}

// -------------------------------------------------- QuotationDetailDialog ---

/**
 * One quotation, with its lines and the terms the vendor attached.
 *
 * The lines name themselves: `description` defaults to the item's name when the
 * vendor did not send one of their own, so the grid needs no item lookup and
 * makes no per-row request. The unit of measure is only an id on the wire, so
 * quantities are shown unqualified rather than against an invented code.
 *
 * Tax is shown but never entered anywhere: `_apply_quotation_line` takes
 * `tax_rate` from the item master and ignores anything the payload offers,
 * "because a quotation that understated GST would flow straight onto a purchase
 * order and then onto a statutory document".
 */
export function QuotationDetailDialog({
  quotationId,
  onClose,
  onDecide,
  onRevise,
}: {
  quotationId: string | null
  onClose: () => void
  /** Opens the decision dialog. Only called when the caller can manage. */
  onDecide?: (quotation: DecidableQuotation, decision: QuotationDecision) => void
  onRevise?: (quotation: QuotationDetail) => void
}) {
  const { can } = useAuth()
  const canManage = can(P.QUOTATION_MANAGE)

  const quotation = useQuery({
    queryKey: quotationKeys.detail(quotationId ?? ''),
    queryFn: () => api.get<QuotationDetail>(`/procurement/quotations/${quotationId}`),
    enabled: Boolean(quotationId),
    staleTime: staleTimes.transactional,
  })

  const data = quotation.data
  const currency = data?.currency
  /**
   * `lines` is *optional* on the wire.
   *
   * `QuotationDetail` declares it with a default rather than as a required key,
   * so the generated type makes it possibly absent and every reader has to
   * treat it that way - the same trap `RfqDetail.lines` carries.
   */
  const lines = data?.lines ?? []

  return (
    <Dialog open={Boolean(quotationId)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>
            {data ? `Quotation ${data.vendor_reference}` : 'Vendor quotation'}
          </DialogTitle>
          <DialogDescription>
            What this vendor offered against the RFQ, exactly as it was recorded.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {quotation.isLoading && (
            <div className="space-y-3" aria-hidden>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-24 w-full" />
            </div>
          )}

          {!quotation.isLoading && quotation.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this quotation</AlertTitle>
              <AlertDescription>{describeError(quotation.error)}</AlertDescription>
            </Alert>
          )}

          {data && (
            <>
              <DescriptionList
                columns={4}
                items={[
                  { label: 'Status', value: <StatusBadge status={data.status} /> },
                  { label: 'Vendor reference', value: data.vendor_reference },
                  { label: 'Quoted on', value: formatDate(data.quotation_date) },
                  {
                    label: 'Valid until',
                    value: data.valid_until ? (
                      <span className="flex items-center gap-2">
                        {formatDate(data.valid_until)}
                        {hasLapsed(data.valid_until) && <Badge variant="warning">Lapsed</Badge>}
                      </span>
                    ) : (
                      // Nullable on the wire, and a genuinely different claim
                      // from a date in the past: an open-ended offer never
                      // lapses and can always be selected.
                      'Open-ended'
                    ),
                  },
                  { label: 'Currency', value: data.currency },
                  { label: 'Lead time', value: `${formatNumber(data.lead_time_days)} days` },
                  {
                    label: 'Payment terms',
                    value: `${formatNumber(data.payment_terms_days)} days`,
                  },
                  {
                    label: 'Ordered against',
                    value: data.converted_purchase_order_id ? (
                      <Badge variant="success">Purchase order raised</Badge>
                    ) : (
                      <span className="text-muted-foreground">No purchase order yet</span>
                    ),
                  },
                ]}
              />

              {data.decision_reason && (
                <Alert variant={data.status === 'REJECTED' ? 'warning' : 'info'}>
                  <AlertTitle>
                    {data.status === 'SELECTED'
                      ? `Selected${data.selected_at ? ` on ${formatDateTime(data.selected_at)}` : ''}`
                      : 'Decision recorded'}
                  </AlertTitle>
                  <AlertDescription>{data.decision_reason}</AlertDescription>
                </Alert>
              )}

              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[44rem] text-sm">
                  <caption className="sr-only">Quotation lines</caption>
                  <thead className="whitespace-nowrap bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th scope="col" className="px-3 py-2 text-left font-medium">
                        #
                      </th>
                      <th scope="col" className="px-3 py-2 text-left font-medium">
                        Item
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Quantity
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Unit price
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Discount
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Tax
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Line total
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Lead time
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {lines.length === 0 && (
                      <tr>
                        <td colSpan={8} className="px-3 py-8 text-center text-muted-foreground">
                          This quotation has no lines. Record the revised offer to add them.
                        </td>
                      </tr>
                    )}
                    {lines.map((line) => (
                      <tr key={line.id}>
                        <td className="px-3 py-2 tabular text-muted-foreground">
                          {line.line_number}
                        </td>
                        <td className="max-w-[18rem] px-3 py-2">
                          <span className="block truncate">{line.description ?? '-'}</span>
                          {line.notes && (
                            <span className="block truncate text-xs text-muted-foreground">
                              {line.notes}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {formatQuantity(line.quantity)}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {formatMoney(line.unit_price, { currency })}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {toDecimal(line.discount_percentage).isZero()
                            ? '-'
                            : formatPercent(line.discount_percentage, 2)}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {formatPercent(line.tax_rate, 2)}
                        </td>
                        <td className="px-3 py-2 text-right tabular font-medium">
                          {formatMoney(line.line_total, { currency })}
                        </td>
                        <td className="px-3 py-2 text-right tabular text-muted-foreground">
                          {/* Nullable: the header's lead time applies when a
                              line does not promise its own. */}
                          {line.lead_time_days === null
                            ? `${formatNumber(data.lead_time_days)} d`
                            : `${formatNumber(line.lead_time_days)} d`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot className="border-t bg-muted/30">
                    <tr>
                      <td colSpan={6} className="px-3 py-2 text-right text-muted-foreground">
                        Subtotal / discount / tax / freight
                      </td>
                      <td colSpan={2} className="px-3 py-2 text-right tabular">
                        {formatMoney(data.subtotal, { currency })} /{' '}
                        {formatMoney(data.discount, { currency })} /{' '}
                        {formatMoney(data.tax_amount, { currency })} /{' '}
                        {formatMoney(data.freight_amount, { currency })}
                      </td>
                    </tr>
                    <tr>
                      <td colSpan={6} className="px-3 py-2 text-right font-medium">
                        Total
                      </td>
                      <td
                        colSpan={2}
                        className="px-3 py-2 text-right tabular text-base font-semibold"
                      >
                        {formatMoney(data.total_amount, { currency })}
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>

              {(data.terms || data.notes) && (
                <DescriptionList
                  columns={2}
                  items={[
                    ...(data.terms ? [{ label: 'Terms', value: data.terms }] : []),
                    ...(data.notes ? [{ label: 'Notes', value: data.notes }] : []),
                  ]}
                />
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          {/* Every button here posts to an endpoint that enforces
              `quotation.manage`, never the `quotation.view` that opened the
              dialog. */}
          {data && canManage && isQuotationRevisable(data.status) && onRevise && (
            <Button variant="outline" onClick={() => onRevise(data)}>
              Record revised offer
            </Button>
          )}
          {data && canManage && isDecidable(data.status) && onDecide && (
            <>
              <Button
                variant="destructive"
                onClick={() => onDecide(decidableFromRow(data), 'reject')}
              >
                Reject
              </Button>
              <Button onClick={() => onDecide(decidableFromRow(data), 'select')}>
                Select this quotation
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------ QuotationDecisionDialog ---

const DECISION_COPY: Record<
  QuotationDecision,
  { title: string; description: string; confirmLabel: string; destructive: boolean }
> = {
  select: {
    title: 'Select this quotation?',
    description:
      'This is the buying decision. The vendor becomes the one this RFQ is ordered against, and the other quotations stay on the board for the record.',
    confirmLabel: 'Select this quotation',
    destructive: false,
  },
  reject: {
    title: 'Reject this quotation?',
    description:
      'The offer is closed out and can no longer be selected or revised. The vendor has to requote if it comes back into contention.',
    confirmLabel: 'Reject quotation',
    destructive: true,
  },
}

/**
 * Select or reject one quotation, with the money the choice costs on screen.
 *
 * The comparison board is fetched for the quotation's own RFQ so the buyer can
 * see the alternatives without leaving the dialog - the same query key the
 * comparison panel uses, so opening this from the panel costs no extra request.
 * Every figure is derived with `toDecimal`; the gap between two quotations is
 * exactly the kind of number a float gets wrong by a rupee.
 *
 * On a mixed-currency RFQ the arithmetic is refused rather than approximated.
 * The API declines to recommend at all in that case and says why; that sentence
 * is shown instead of a comparison this screen is in no position to make.
 */
export function QuotationDecisionDialog({
  quotation,
  decision,
  onOpenChange,
}: {
  /** Projected through `decidableFromRow` / `decidableFromColumn`. */
  quotation: DecidableQuotation | null
  decision: QuotationDecision
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  const open = Boolean(quotation)

  useEffect(() => {
    if (open) {
      setReason('')
      setTouched(false)
    }
  }, [open])

  const comparison = useQuery({
    queryKey: quotationKeys.comparison(quotation?.rfqId ?? ''),
    queryFn: () => api.get<QuotationComparison>(`/procurement/rfqs/${quotation?.rfqId}/comparison`),
    enabled: open,
    staleTime: staleTimes.transactional,
  })

  const decide = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) =>
      // Answers with `QuotationSummary`, not the detail - the lines are
      // unchanged by a decision, so the endpoint does not resend them.
      api.post<QuotationRow>(`/procurement/quotations/${id}/${decision}`, { reason: text }),
    onSuccess: (result) => {
      // Deciding one quotation changes the whole board: the comparison hangs
      // off the RFQ, so invalidating this quotation's own key alone would leave
      // a stale winner on screen.
      void queryClient.invalidateQueries({ queryKey: quotationKeys.all })
      toast.success(
        decision === 'select' ? success.selected('Quotation') : success.rejected('Quotation'),
        `${result.vendor_reference} is now ${result.status === 'SELECTED' ? 'the chosen offer' : 'closed out'}.`,
      )
      onOpenChange(false)
    },
  })

  const copy = DECISION_COPY[decision]
  const trimmed = reason.trim()
  const reasonInvalid = trimmed.length < MIN_DECISION_REASON || trimmed.length > MAX_DECISION_REASON

  const board = comparison.data
  const standing = board && quotation ? totalsStanding(board, quotation.id) : null
  const followsRecommendation = Boolean(
    board && quotation && board.recommended_vendor_id === quotation.vendorId,
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {quotation && (
            <DescriptionList
              columns={3}
              items={[
                { label: 'Quotation', value: quotation.label },
                {
                  label: 'Total',
                  value: (
                    <span className="tabular font-medium">
                      {formatMoney(quotation.totalAmount, { currency: quotation.currency })}
                    </span>
                  ),
                },
                { label: 'Lead time', value: `${formatNumber(quotation.leadTimeDays)} days` },
              ]}
            />
          )}

          {/* A browser-clock warning, not a gate: `decide_quotation` compares
              `valid_until` against the server's date and refuses the selection
              itself. Saying so before the round trip saves a pointless 422. */}
          {decision === 'select' && quotation && hasLapsed(quotation.validUntil) && (
            <Alert variant="warning">
              <AlertTitle>This offer looks lapsed</AlertTitle>
              <AlertDescription>
                It was valid until {formatDate(quotation.validUntil)}. The API refuses to select a
                lapsed quotation - ask the vendor to requote, then record the revised offer.
              </AlertDescription>
            </Alert>
          )}

          {comparison.isLoading && (
            <div className="space-y-2" aria-hidden>
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-16 w-full" />
            </div>
          )}

          {/* The comparison is context, not a precondition. A board that fails
              to load must not block a decision the buyer has already made on
              paper, so the failure is stated and the dialog carries on. */}
          {!comparison.isLoading && comparison.error && (
            <Alert variant="warning">
              <AlertTitle>The other quotations could not be loaded</AlertTitle>
              <AlertDescription>
                {describeError(comparison.error)} You can still record the decision.
              </AlertDescription>
            </Alert>
          )}

          {board?.mixed_currency && (
            <Alert variant="warning">
              <AlertTitle>Prices are not comparable</AlertTitle>
              <AlertDescription>{board.recommendation_reason}</AlertDescription>
            </Alert>
          )}

          {board && !board.mixed_currency && standing && (
            <div className="rounded-lg border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-medium">
                  Against the other quotes on {board.rfq_code}
                </h3>
                {followsRecommendation ? (
                  <Badge variant="success">Matches the recommendation</Badge>
                ) : board.recommended_vendor_id ? (
                  <Badge variant="warning">Not the recommended vendor</Badge>
                ) : null}
              </div>

              <p className="mt-2 text-sm">
                {standing.cheapest === null ? (
                  // Every quote on the board has lapsed, so there is nothing
                  // still selectable to measure against.
                  <span className="text-muted-foreground">
                    Every quotation on this RFQ has lapsed, so there is no live price to compare
                    against.
                  </span>
                ) : standing.premium.isZero() ? (
                  <span className="text-success">
                    This is the lowest total still valid on this RFQ.
                  </span>
                ) : standing.premium.isPositive() ? (
                  <>
                    <span className="tabular font-medium text-warning">
                      {formatMoney(standing.premium.toFixed(), {
                        currency: standing.chosen.currency,
                      })}
                    </span>{' '}
                    more than {standing.cheapest.vendor_name || standing.cheapest.vendor_code}, the
                    cheapest offer still valid.
                  </>
                ) : (
                  <>
                    <span className="tabular font-medium">
                      {formatMoney(standing.premium.negated().toFixed(), {
                        currency: standing.chosen.currency,
                      })}
                    </span>{' '}
                    below the cheapest offer still valid - this quotation has itself lapsed.
                  </>
                )}
              </p>

              {board.recommendation_reason && (
                <p className="mt-1.5 text-xs text-muted-foreground">
                  {board.recommendation_reason}
                </p>
              )}

              {standing.rivals.length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {standing.rivals.map(({ column, difference }) => (
                    <li
                      key={column.quotation_id}
                      className="flex items-center justify-between gap-3 text-xs"
                    >
                      <span className="min-w-0 truncate">
                        {column.vendor_name || column.vendor_code}
                        {column.is_expired && (
                          <Badge variant="muted" className="ml-2">
                            Lapsed
                          </Badge>
                        )}
                      </span>
                      <span className="tabular shrink-0 text-muted-foreground">
                        {formatMoney(column.total_amount, { currency: column.currency })}
                        {' · '}
                        <span
                          className={cn(
                            difference.isPositive() && 'text-warning',
                            difference.isNegative() && 'text-success',
                          )}
                        >
                          {difference.isZero()
                            ? 'same total'
                            : `${difference.isPositive() ? '+' : '-'}${formatMoney(
                                difference.abs().toFixed(),
                                { currency: column.currency, showSymbol: false },
                              )}`}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <DecisionReasonField
            id="quotation-decision-reason"
            value={reason}
            onChange={setReason}
            invalid={touched && reasonInvalid}
          />
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={decide.isPending}>
            Cancel
          </Button>
          <Button
            variant={copy.destructive ? 'destructive' : 'default'}
            loading={decide.isPending}
            onClick={() => {
              if (!quotation) return
              if (reasonInvalid) {
                setTouched(true)
                return
              }
              decide.mutate({ id: quotation.id, text: trimmed })
            }}
          >
            {copy.confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------- QuotationFormDialog ---

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const PRICE = /^\d{1,12}(\.\d{1,6})?$/
const MONEY = /^\d{1,14}(\.\d{1,4})?$/
const PERCENT = /^\d{1,3}(\.\d{1,4})?$/
const WHOLE_DAYS = /^\d{0,4}$/

const lineSchema = z.object({
  item_id: z.string().min(1, validation.choose('an item')),
  quantity: z
    .string()
    .trim()
    .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => toDecimal(value).greaterThan(0), validation.positive('Quantity')),
  unit_price: z.string().trim().regex(PRICE, 'Enter a price with up to 6 decimal places'),
  discount_percentage: z
    .string()
    .trim()
    .refine((value) => value === '' || PERCENT.test(value), 'Enter a percentage, e.g. 2.5')
    .refine(
      (value) => value === '' || toDecimal(value).lessThanOrEqualTo(100),
      'A discount cannot exceed 100%',
    ),
  lead_time_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
})

const schema = z
  .object({
    rfq_id: z.string().min(1, validation.choose('the RFQ this quotation answers')),
    vendor_id: z.string().min(1, validation.choose('the vendor who quoted')),
    vendor_reference: z
      .string()
      .trim()
      .min(1, "Record the vendor's own quotation number")
      .max(64, 'At most 64 characters'),
    quotation_date: z.string().min(1, validation.required('Date the vendor quoted')),
    valid_until: z.string(),
    currency: z
      .string()
      .trim()
      .regex(/^[A-Za-z]{3}$/, 'A three-letter currency code, e.g. INR'),
    payment_terms_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
    lead_time_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
    freight_amount: z
      .string()
      .trim()
      .refine((value) => value === '' || MONEY.test(value), 'Enter an amount, e.g. 1500.00'),
    terms: z.string().trim().max(4000, 'At most 4000 characters'),
    notes: z.string().trim().max(4000, 'At most 4000 characters'),
    lines: z.array(lineSchema).min(1, 'A quotation needs at least one line'),
  })
  // An offer that expired before it was made is a keying slip, and it would be
  // refused for selection the moment anyone tried to act on it.
  .superRefine((values, ctx) => {
    if (values.valid_until && values.valid_until < values.quotation_date) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['valid_until'],
        message: 'An offer cannot expire before it was quoted',
      })
    }
  })

type QuotationFormValues = z.infer<typeof schema>

const emptyLine = () => ({
  item_id: '',
  quantity: '',
  unit_price: '',
  discount_percentage: '',
  lead_time_days: '',
})

function emptyValues(rfqId: string | null): QuotationFormValues {
  return {
    rfq_id: rfqId ?? '',
    vendor_id: '',
    vendor_reference: '',
    quotation_date: toDateInput(new Date()),
    valid_until: '',
    currency: DEFAULT_CURRENCY,
    payment_terms_days: '',
    lead_time_days: '',
    freight_amount: '',
    terms: '',
    notes: '',
    lines: [emptyLine()],
  }
}

/**
 * The line payload, exactly as `QuotationLineIn` declares it.
 *
 * Two fields are deliberately absent. `uom_id` is omitted so the API falls back
 * to the item's own order UoM rather than this form guessing one, and there is
 * no tax field at all - the item master owns the GST rate and the payload
 * cannot override it.
 */
const toLinePayload = (
  line: QuotationFormValues['lines'][number],
  index: number,
): QuotationLinePayload => ({
  line_number: index + 1,
  item_id: line.item_id,
  // Quantities and prices stay strings on the wire; the API parses Decimals.
  quantity: line.quantity.trim(),
  unit_price: line.unit_price.trim(),
  discount_percentage: line.discount_percentage.trim() || null,
  // Null means "the header's lead time applies to this line", which is a
  // different statement from promising zero days.
  lead_time_days: line.lead_time_days.trim() === '' ? null : Number(line.lead_time_days.trim()),
})

/**
 * Record a vendor's quotation, or replace it with their revised offer.
 *
 * There is one live quotation per vendor per RFQ - the API refuses a second one
 * with a 409 naming the first - so revising is a PATCH on the existing
 * quotation and not a new document. `update_quotation` replaces the whole line
 * set when `lines` is present, so the form loads every existing line and
 * resends them all; sending only the changed one would delete the rest.
 *
 * A revision cannot move the quotation to a different vendor or RFQ:
 * `QuotationUpdate` declares neither field, so both are shown read-only.
 */
export function QuotationFormDialog({
  open,
  onOpenChange,
  quotationId,
  defaultRfqId = null,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Null raises a new quotation; an id records the revised offer on that one. */
  quotationId: string | null
  /** Prefills the RFQ when the list is already narrowed to one. */
  defaultRfqId?: string | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isRevision = quotationId !== null
  // The line editor searches `/products/items`, which enforces `item.view` -
  // again not the `quotation.manage` that lets this form post.
  const canPickItems = can(P.ITEM_VIEW)

  const existing = useQuery({
    queryKey: quotationKeys.detail(quotationId ?? ''),
    queryFn: () => api.get<QuotationDetail>(`/procurement/quotations/${quotationId}`),
    enabled: open && isRevision,
    staleTime: staleTimes.transactional,
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<QuotationFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(defaultRfqId),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'lines' })

  const loaded = existing.data

  useEffect(() => {
    if (open && !isRevision) reset(emptyValues(defaultRfqId))
  }, [open, isRevision, defaultRfqId, reset])

  // Split from the reset above on purpose: the loaded quotation arrives a tick
  // after the dialog opens, and folding both into one effect would re-run the
  // blank reset every time the detail query settles and wipe what was typed.
  useEffect(() => {
    if (!open || !isRevision || !loaded) return
    reset({
      rfq_id: loaded.rfq_id,
      vendor_id: loaded.vendor_id,
      vendor_reference: loaded.vendor_reference,
      quotation_date: toDateInput(loaded.quotation_date),
      valid_until: toDateInput(loaded.valid_until),
      currency: loaded.currency,
      payment_terms_days: String(loaded.payment_terms_days),
      lead_time_days: String(loaded.lead_time_days),
      freight_amount: loaded.freight_amount,
      terms: loaded.terms ?? '',
      notes: loaded.notes ?? '',
      // Optional on the wire, and the field array must never be empty: zod
      // requires a line, and a PATCH carrying `lines: []` would delete every
      // line the quotation has.
      lines: loaded.lines?.length
        ? loaded.lines.map((line) => ({
            item_id: line.item_id,
            quantity: line.quantity,
            unit_price: line.unit_price,
            discount_percentage: toDecimal(line.discount_percentage).isZero()
              ? ''
              : line.discount_percentage,
            lead_time_days: line.lead_time_days === null ? '' : String(line.lead_time_days),
          }))
        : [emptyLine()],
    })
  }, [open, isRevision, loaded, reset])

  const save = useMutation({
    mutationFn: (values: QuotationFormValues): Promise<QuotationDetail> => {
      const lines = values.lines.map(toLinePayload)
      if (isRevision) {
        /**
         * A blank field on a revision means "leave it alone", and is omitted.
         *
         * `update_quotation` walks its fields with `if value is not None`, so a
         * null is skipped rather than applied - sending one to clear a validity
         * date or a set of terms does nothing at all. Omitting the key states
         * the same intent honestly. It also keeps the two paths apart: a blank
         * `payment_terms_days` on a *create* is the vendor-master fallback,
         * while on an update it would be assigned as a literal zero days.
         */
        const payload: QuotationUpdatePayload = {
          vendor_reference: values.vendor_reference.trim(),
          quotation_date: values.quotation_date,
          currency: values.currency.trim().toUpperCase(),
          ...(values.valid_until ? { valid_until: values.valid_until } : {}),
          ...(values.payment_terms_days.trim()
            ? { payment_terms_days: Number(values.payment_terms_days.trim()) }
            : {}),
          ...(values.lead_time_days.trim()
            ? { lead_time_days: Number(values.lead_time_days.trim()) }
            : {}),
          ...(values.freight_amount.trim() ? { freight_amount: values.freight_amount.trim() } : {}),
          ...(values.terms.trim() ? { terms: values.terms.trim() } : {}),
          ...(values.notes.trim() ? { notes: values.notes.trim() } : {}),
          lines,
        }
        return api.patch<QuotationDetail>(`/procurement/quotations/${quotationId}`, payload)
      }
      const payload: QuotationCreatePayload = {
        rfq_id: values.rfq_id,
        vendor_id: values.vendor_id,
        vendor_reference: values.vendor_reference.trim(),
        quotation_date: values.quotation_date,
        valid_until: orNull(values.valid_until),
        currency: values.currency.trim().toUpperCase(),
        // Blank means "whatever the vendor master says": `create_quotation`
        // falls back to the vendor's own terms for an absent key, so the key is
        // omitted rather than sent as a zero the API would take literally.
        ...(values.payment_terms_days.trim()
          ? { payment_terms_days: Number(values.payment_terms_days.trim()) }
          : {}),
        ...(values.lead_time_days.trim()
          ? { lead_time_days: Number(values.lead_time_days.trim()) }
          : {}),
        freight_amount: values.freight_amount.trim() || '0',
        terms: orNull(values.terms),
        notes: orNull(values.notes),
        lines,
      }
      return api.post<QuotationDetail>('/procurement/quotations', payload)
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: quotationKeys.all })
      toast.success(
        isRevision ? success.updated('Quotation') : success.created('Quotation'),
        `${result.vendor_reference} totals ${formatMoney(result.total_amount, { currency: result.currency })}.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const lines = watch('lines')
  const freight = watch('freight_amount')

  /**
   * Indicative only, and labelled as such.
   *
   * Tax is the API's calculation from the item master's GST rate, which this
   * form never sees, so the figure below is net of discount plus freight and
   * is not the quotation's total. It exists so a fat-fingered price is obvious
   * before the sheet is saved.
   */
  const indicativeNet = lines
    .reduce(
      (sum, line) =>
        sum.plus(
          toDecimal(line.quantity)
            .times(toDecimal(line.unit_price))
            .times(toDecimal(100).minus(toDecimal(line.discount_percentage)).dividedBy(100)),
        ),
      toDecimal(0),
    )
    .plus(toDecimal(freight))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{isRevision ? 'Record a revised offer' : 'Record a quotation'}</DialogTitle>
          <DialogDescription>
            {isRevision
              ? 'The revision replaces this vendor’s offer in full - every line below is saved, and any line removed here is removed from the quotation.'
              : 'One quotation per vendor per RFQ. If this vendor has already quoted, record the revised offer on that quotation instead.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            {isRevision && existing.isLoading && (
              <div className="space-y-3" aria-hidden>
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-24 w-full" />
              </div>
            )}

            {isRevision && existing.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load this quotation</AlertTitle>
                <AlertDescription>{describeError(existing.error)}</AlertDescription>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  loading={existing.isFetching}
                  onClick={() => void existing.refetch()}
                >
                  <RefreshCw />
                  Try again
                </Button>
              </Alert>
            )}

            {/* Nothing editable until the revision has actually loaded. The
                dialog stays mounted between opens, so the fields would
                otherwise hold whatever the last quotation left in them - and a
                PATCH replaces the whole line set with whatever is shown. */}
            {(!isRevision || loaded) && (
              <>
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {/* Neither the RFQ nor the vendor can be changed by a revision -
                  `QuotationUpdate` declares no field for either - so on a
                  revision both are stated rather than offered. */}
                  {isRevision ? (
                    <div className="sm:col-span-2 lg:col-span-3">
                      <Alert variant="info">
                        <AlertTitle>Revising an existing offer</AlertTitle>
                        <AlertDescription>
                          The RFQ and the vendor are fixed - a different vendor&rsquo;s price is a
                          separate quotation. Every line below is saved as the offer in full, and a
                          field left blank keeps the value already recorded rather than clearing it.
                        </AlertDescription>
                      </Alert>
                    </div>
                  ) : (
                    <>
                      <div>
                        {/* `/procurement/rfqs` enforces `rfq.view`, which is not the
                        `quotation.manage` that lets this form post. Firing the
                        lookup regardless earns a 403 the buyer cannot act on. */}
                        {can(P.RFQ_VIEW) ? (
                          <>
                            <Controller
                              control={control}
                              name="rfq_id"
                              render={({ field }) => (
                                <RfqPicker
                                  id="quotation-rfq"
                                  value={field.value || null}
                                  onChange={(rfqId) => field.onChange(rfqId ?? '')}
                                  invalid={Boolean(errors.rfq_id)}
                                />
                              )}
                            />
                            <FieldError message={errors.rfq_id?.message} />
                          </>
                        ) : (
                          <Alert variant="warning">
                            <AlertTitle>You cannot choose an RFQ from here</AlertTitle>
                            <AlertDescription>
                              A quotation has to name the RFQ it answers, and your account cannot
                              read the RFQ list. Ask somebody who can to record it.
                            </AlertDescription>
                          </Alert>
                        )}
                      </div>

                      <div>
                        {can(P.VENDOR_VIEW) ? (
                          <>
                            <Controller
                              control={control}
                              name="vendor_id"
                              render={({ field }) => (
                                <VendorPicker
                                  id="quotation-vendor"
                                  label="Vendor"
                                  value={field.value || null}
                                  onChange={(vendorId) => field.onChange(vendorId ?? '')}
                                  placeholder="Search the vendor master"
                                  invalid={Boolean(errors.vendor_id)}
                                />
                              )}
                            />
                            <FieldError message={errors.vendor_id?.message} />
                            <p className="mt-1 text-xs text-muted-foreground">
                              Approval is not required to quote - only to raise the order that
                              follows.
                            </p>
                          </>
                        ) : (
                          <Alert variant="warning">
                            <AlertTitle>You cannot choose a vendor from here</AlertTitle>
                            <AlertDescription>
                              Your account cannot read the vendor master, and a quotation has to
                              name the vendor who sent it.
                            </AlertDescription>
                          </Alert>
                        )}
                      </div>
                    </>
                  )}

                  <div>
                    <Label htmlFor="quotation-reference" required>
                      Vendor quotation number
                    </Label>
                    <Input
                      id="quotation-reference"
                      {...register('vendor_reference')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.vendor_reference)}
                    />
                    <FieldError message={errors.vendor_reference?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      The reference on the vendor&rsquo;s own sheet. It identifies the quotation
                      everywhere in this system.
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="quotation-date" required>
                      Quoted on
                    </Label>
                    <Input
                      id="quotation-date"
                      type="date"
                      {...register('quotation_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.quotation_date)}
                    />
                    <FieldError message={errors.quotation_date?.message} />
                  </div>

                  <div>
                    <Label htmlFor="quotation-valid-until">Valid until</Label>
                    <Input
                      id="quotation-valid-until"
                      type="date"
                      {...register('valid_until')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.valid_until)}
                    />
                    <FieldError message={errors.valid_until?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      {isRevision
                        ? 'Cleared here it stays as recorded - a revision cannot remove a date the API already holds.'
                        : 'Left blank the offer is open-ended. Past this date the API refuses to select it.'}
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="quotation-currency" required>
                      Currency
                    </Label>
                    <Input
                      id="quotation-currency"
                      {...register('currency')}
                      autoComplete="off"
                      maxLength={3}
                      className="mt-1.5 uppercase"
                      aria-invalid={Boolean(errors.currency)}
                    />
                    <FieldError message={errors.currency?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Quotes in different currencies cannot be compared, and the board says so
                      rather than naming a winner.
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="quotation-payment-terms">Payment terms (days)</Label>
                    <Input
                      id="quotation-payment-terms"
                      inputMode="numeric"
                      {...register('payment_terms_days')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.payment_terms_days)}
                    />
                    <FieldError message={errors.payment_terms_days?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      {isRevision
                        ? 'Left blank, the recorded terms are kept.'
                        : 'Left blank, the vendor’s agreed terms apply.'}
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="quotation-lead-time">Lead time (days)</Label>
                    <Input
                      id="quotation-lead-time"
                      inputMode="numeric"
                      {...register('lead_time_days')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.lead_time_days)}
                    />
                    <FieldError message={errors.lead_time_days?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Applies to every line that does not promise its own.
                      {isRevision ? ' Left blank, the recorded lead time is kept.' : ''}
                    </p>
                  </div>

                  <div>
                    <Label htmlFor="quotation-freight">Freight</Label>
                    <Input
                      id="quotation-freight"
                      inputMode="decimal"
                      {...register('freight_amount')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.freight_amount)}
                    />
                    <FieldError message={errors.freight_amount?.message} />
                  </div>
                </div>

                {/* A quotation must carry at least one priced line, so without the
                right to read the item master there is nothing to key. Saying so
                beats a form that fails on a field it could never fill. */}
                {!canPickItems && (
                  <Alert variant="warning">
                    <AlertTitle>You cannot key the lines from here</AlertTitle>
                    <AlertDescription>
                      A quotation has to carry at least one priced line, and your account cannot
                      read the item master. Ask somebody who can to record it.
                    </AlertDescription>
                  </Alert>
                )}

                {canPickItems && (
                  <div>
                    <div className="flex items-center justify-between gap-3">
                      <h3 className="text-sm font-medium">Lines</h3>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => append(emptyLine())}
                      >
                        <Plus />
                        Add line
                      </Button>
                    </div>
                    <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />

                    <div className="mt-3 space-y-3">
                      {fields.map((field, index) => (
                        <div
                          key={field.id}
                          className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_auto]"
                        >
                          <div className="sm:col-span-2 lg:col-span-1">
                            <Controller
                              control={control}
                              name={`lines.${index}.item_id`}
                              render={({ field: itemField }) => (
                                <ItemPicker
                                  id={`quotation-line-item-${index}`}
                                  label={`Line ${index + 1} item`}
                                  value={itemField.value || null}
                                  onChange={(itemId) => itemField.onChange(itemId ?? '')}
                                  placeholder="Search the item master"
                                  invalid={Boolean(errors.lines?.[index]?.item_id)}
                                />
                              )}
                            />
                            <FieldError message={errors.lines?.[index]?.item_id?.message} />
                          </div>

                          <div>
                            <Label htmlFor={`quotation-line-qty-${index}`} required>
                              Quantity
                            </Label>
                            <Input
                              id={`quotation-line-qty-${index}`}
                              inputMode="decimal"
                              {...register(`lines.${index}.quantity`)}
                              autoComplete="off"
                              className="mt-1.5"
                              aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                            />
                            <FieldError message={errors.lines?.[index]?.quantity?.message} />
                          </div>

                          <div>
                            <Label htmlFor={`quotation-line-price-${index}`} required>
                              Unit price
                            </Label>
                            <Input
                              id={`quotation-line-price-${index}`}
                              inputMode="decimal"
                              {...register(`lines.${index}.unit_price`)}
                              autoComplete="off"
                              className="mt-1.5"
                              aria-invalid={Boolean(errors.lines?.[index]?.unit_price)}
                            />
                            <FieldError message={errors.lines?.[index]?.unit_price?.message} />
                          </div>

                          <div>
                            <Label htmlFor={`quotation-line-discount-${index}`}>Discount %</Label>
                            <Input
                              id={`quotation-line-discount-${index}`}
                              inputMode="decimal"
                              {...register(`lines.${index}.discount_percentage`)}
                              autoComplete="off"
                              className="mt-1.5"
                              aria-invalid={Boolean(errors.lines?.[index]?.discount_percentage)}
                            />
                            <FieldError
                              message={errors.lines?.[index]?.discount_percentage?.message}
                            />
                          </div>

                          <div>
                            <Label htmlFor={`quotation-line-lead-${index}`}>Lead days</Label>
                            <Input
                              id={`quotation-line-lead-${index}`}
                              inputMode="numeric"
                              {...register(`lines.${index}.lead_time_days`)}
                              autoComplete="off"
                              className="mt-1.5"
                              aria-invalid={Boolean(errors.lines?.[index]?.lead_time_days)}
                            />
                            <FieldError message={errors.lines?.[index]?.lead_time_days?.message} />
                          </div>

                          <div className="flex items-end sm:col-span-2 sm:justify-end lg:col-span-1 lg:justify-start">
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              aria-label={`Remove line ${index + 1}`}
                              disabled={fields.length === 1}
                              onClick={() => remove(index)}
                            >
                              <Trash2 />
                            </Button>
                          </div>
                        </div>
                      ))}
                    </div>

                    <p className="mt-3 text-right text-sm text-muted-foreground">
                      Indicative net of discount, plus freight:{' '}
                      <span className="tabular font-medium text-foreground">
                        {formatMoney(indicativeNet.toFixed(), {
                          currency: watch('currency') || DEFAULT_CURRENCY,
                        })}
                      </span>
                      . Tax is the API&rsquo;s calculation from the item master.
                    </p>
                  </div>
                )}

                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="quotation-terms">Terms</Label>
                    <Textarea
                      id="quotation-terms"
                      rows={3}
                      {...register('terms')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.terms)}
                    />
                    <FieldError message={errors.terms?.message} />
                  </div>
                  <div>
                    <Label htmlFor="quotation-notes">Notes</Label>
                    <Textarea
                      id="quotation-notes"
                      rows={3}
                      {...register('notes')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.notes)}
                    />
                    <FieldError message={errors.notes?.message} />
                  </div>
                </div>
              </>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={save.isPending}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              loading={save.isPending}
              disabled={!canPickItems || (isRevision && !loaded)}
            >
              {isRevision ? 'Save revised offer' : 'Record quotation'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

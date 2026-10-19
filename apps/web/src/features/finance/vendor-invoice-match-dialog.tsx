/**
 * Vendor invoice detail and its three-way match.
 *
 * The match compares three documents that were written by three different
 * parties at three different times: what was ordered (PO price and quantity),
 * what was actually accepted after QC (the goods receipt) and what the vendor
 * has billed. A variance between them is the single most common way money
 * leaves a manufacturer by mistake, so it is shown line by line rather than
 * summarised into a badge.
 *
 * Approval mirrors the API. `matched` is the endpoint's verdict and the single
 * question approval asks: false means the API refuses the approval unless an
 * `override_reason` is supplied, and `blocking_reasons` is empty exactly when
 * it is true. This screen never re-derives a rule of its own from the variance
 * figures, because a button that disagrees with the server is worse than no
 * button.
 *
 * The tolerances and the per-line variance figures arrive as *ratios* - 0.02 is
 * two per cent - so anything shown as a percentage is multiplied by a hundred
 * on a Decimal first, never on a float.
 *
 * This is also the invoice's detail view, so it carries the three commands that
 * act on the document itself: amend it, send it for approval, and delete a
 * draft that was keyed in error. All three are `vendor_invoice.create`, which
 * is the right the API declares on those routes and is deliberately NOT the
 * `vendor_invoice.approve` that gates the approval below - whoever keys a bill
 * must not be the person who clears it. Each is offered only in the status its
 * state machine actually permits, read through the `isInvoice*` predicates in
 * `finance-types.ts` rather than compared inline here.
 *
 * Deletion is a hard delete: `delete_draft_invoice` removes the row, because a
 * draft that was never approved never reached the ledger and there is nothing
 * to correct with an offsetting entry. It takes its reason as a QUERY
 * PARAMETER, not in a body - the codebase has both spellings and the wrong one
 * is a 422 at the moment of use.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import {
  CheckCircle2,
  Pencil,
  SendHorizontal,
  ShieldAlert,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, DescriptionList, FieldError, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { CostCentreName } from '@/features/admin/cost-centre-select'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import {
  formatDate,
  formatMoney,
  formatPercent,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'
import type { DecimalString } from '@/types/api'

import {
  MAX_INVOICE_REASON_LENGTH,
  MIN_INVOICE_REASON_LENGTH,
  financeKeys,
  isInvoiceDeletable,
  isInvoiceEditable,
  isInvoiceSubmittable,
  type InvoiceReasonPayload,
  type MatchLineVariance,
  type MatchResult,
  type MatchStatus,
  type VendorInvoiceDetail,
} from './finance-types'

/**
 * The one status approval is meaningful from.
 *
 * The API state machine is authoritative - `approve` refuses anything that is
 * not awaiting approval with a 409, and that message is surfaced globally. This
 * keeps the button off an invoice the endpoint would certainly reject.
 */
const APPROVABLE_STATUSES = new Set(['PENDING_APPROVAL'])

/** The API accepts a reason of three characters; this asks for a real sentence. */
const MIN_OVERRIDE_REASON = 10

const MATCH_BADGE: Record<
  MatchStatus,
  { variant: 'success' | 'warning' | 'destructive' | 'muted'; label: string }
> = {
  MATCHED: { variant: 'success', label: 'Matched' },
  VARIANCE: { variant: 'destructive', label: 'Variance' },
  OVERRIDDEN: { variant: 'warning', label: 'Approved over a variance' },
  NOT_MATCHED: { variant: 'muted', label: 'Not matched' },
}

/** A ratio (0.02) rendered as the percentage a human talks in. */
function ratioAsPercent(ratio: DecimalString): string {
  return formatPercent(toDecimal(ratio).times(100).toFixed(4))
}

function MatchBadge({ status }: { status: MatchStatus }) {
  const badge = MATCH_BADGE[status] ?? MATCH_BADGE.NOT_MATCHED
  return <Badge variant={badge.variant}>{badge.label}</Badge>
}

export function VendorInvoiceMatchDialog({
  invoiceId,
  onClose,
  onAmend,
}: {
  /** Null keeps the dialog shut; an id opens it on that invoice. */
  invoiceId: string | null
  onClose: () => void
  /**
   * Hand the loaded invoice to the entry form.
   *
   * The form lives on the page rather than inside this dialog so the two are
   * never stacked on top of each other, and so recording a new bill and
   * amending an existing one open the same one component.
   */
  onAmend?: (invoice: VendorInvoiceDetail) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const [confirmOpen, setConfirmOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)

  /** Which of the two document commands is being confirmed, if either. */
  const [command, setCommand] = useState<'submit' | 'delete' | null>(null)
  const [commandReason, setCommandReason] = useState('')
  const [commandTouched, setCommandTouched] = useState(false)

  // UX only. The API authorises the approval itself (master rules 38).
  const canApprove = can(P.VENDOR_INVOICE_APPROVE)
  /**
   * Amend, submit and delete - one right for all three.
   *
   * `vendor_invoice.create` is what the API declares on the PATCH, the submit
   * and the DELETE routes alike. It is not `vendor_invoice.approve`, which is a
   * different person's job by design.
   */
  const canRecord = can(P.VENDOR_INVOICE_CREATE)
  /** The header's cost centre is named from a list that is its own right. */
  const canViewCostCentres = can(P.COST_CENTRE_VIEW)

  const invoiceQuery = useQuery({
    queryKey: financeKeys.vendorInvoice(invoiceId ?? ''),
    queryFn: () => api.get<VendorInvoiceDetail>(`/finance/vendor-invoices/${invoiceId}`),
    enabled: Boolean(invoiceId),
    staleTime: staleTimes.transactional,
  })

  const matchQuery = useQuery({
    queryKey: financeKeys.vendorInvoiceMatch(invoiceId ?? ''),
    queryFn: () => api.get<MatchResult>(`/finance/vendor-invoices/${invoiceId}/match`),
    enabled: Boolean(invoiceId),
    // The match is recomputed against live GRN data, so it is never served from
    // a cache that could predate this morning's receipt.
    staleTime: 0,
  })

  const invoice = invoiceQuery.data
  const match = matchQuery.data
  const currency = invoice?.currency
  /** A job-work bill is matched against its order, not against a PO and GRN. */
  const isJobWork = invoice?.invoice_type === 'JOB_WORK'

  const approve = useMutation({
    mutationFn: (overrideReason: string) =>
      api.post<VendorInvoiceDetail>(`/finance/vendor-invoices/${invoiceId}/approve`, {
        override_reason: overrideReason === '' ? null : overrideReason,
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      toast.success(
        success.approved('Invoice'),
        `${updated.vendor_invoice_number} is now ${humanise(updated.status).toLowerCase()}.`,
      )
      setConfirmOpen(false)
      setReason('')
      setReasonTouched(false)
      onClose()
    },
  })

  const closeCommand = () => {
    setCommand(null)
    setCommandReason('')
    setCommandTouched(false)
  }

  const submit = useMutation({
    mutationFn: (note: string) => {
      // `ReasonRequest` is optional on this route and is 3..500 whenever it is
      // present, so a blank note sends NO body rather than an empty string.
      const payload: InvoiceReasonPayload | undefined = note === '' ? undefined : { reason: note }
      return api.post<VendorInvoiceDetail>(`/finance/vendor-invoices/${invoiceId}/submit`, payload)
    },
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      toast.success(
        success.submitted('Invoice'),
        `${updated.vendor_invoice_number} is now ${humanise(updated.status).toLowerCase()}. The three-way match was run on the way in.`,
      )
      closeCommand()
      // Deliberately left open: the match was just recomputed, and it is the
      // thing the submitter most wants to see.
    },
  })

  const deleteDraft = useMutation({
    mutationFn: (why: string) =>
      // The reason is a QUERY PARAMETER on this route (`ReasonQuery` in the
      // finance router), not a body. The codebase has both spellings and the
      // wrong one is a 422 at the moment of use.
      api.delete(`/finance/vendor-invoices/${invoiceId}`, { params: { reason: why } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      toast.success(
        success.deleted('Draft invoice'),
        'The bill was removed and the reason recorded in the audit log.',
      )
      closeCommand()
      onClose()
    },
  })

  /**
   * `matched` is the whole rule. The endpoint refuses an unmatched invoice
   * unless a reason is supplied, so the two flags the screen used to read
   * separately are one question here.
   */
  const blocked = match ? !match.matched : false
  // Optional on the wire, and empty exactly when `matched` is true.
  const blockingReasons = match?.blocking_reasons ?? []
  const needsOverride = blocked
  const trimmedReason = reason.trim()
  /**
   * Whether the approval box holds something the endpoint would refuse.
   *
   * Over a variance the reason is the override and has to be a real sentence.
   * On a clean match the box is an optional note - but it still travels as
   * `override_reason`, which is 3..500 whenever it is present, so a two-letter
   * note would go out, come back a 422 with a field error, and vanish: the
   * mutation cache stays quiet for field errors on the understanding that a
   * form shows them, and this dialog has no form to do so.
   */
  const reasonUnusable = needsOverride
    ? trimmedReason.length < MIN_OVERRIDE_REASON
    : trimmedReason !== '' && trimmedReason.length < MIN_INVOICE_REASON_LENGTH
  const decided = invoice ? !APPROVABLE_STATUSES.has(invoice.status) : true

  /**
   * The reason the command dialog is holding, judged against its own rule.
   *
   * Delete requires one; submit's note is optional. Either way, a reason that
   * is present must clear three characters, because that is the bound both the
   * `reason` query parameter and `ReasonRequest.reason` declare - a two-letter
   * note would be refused with a 422 after the click.
   */
  const trimmedCommandReason = commandReason.trim()
  const commandReasonUnusable =
    command === 'delete'
      ? trimmedCommandReason.length < MIN_INVOICE_REASON_LENGTH
      : trimmedCommandReason !== '' && trimmedCommandReason.length < MIN_INVOICE_REASON_LENGTH
  const commandPending = submit.isPending || deleteDraft.isPending

  const columns = useMemo<ColumnDef<MatchLineVariance, unknown>[]>(
    () => [
      {
        id: 'line_number',
        header: '#',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.line_number}</span>,
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[16rem]">
            {row.original.item_id ? (
              <Link
                to={`/items/${row.original.item_id}`}
                className="block truncate font-mono text-xs hover:underline"
              >
                {row.original.item_code ?? row.original.item_id}
              </Link>
            ) : (
              <span className="block truncate font-mono text-xs">
                {row.original.item_code ?? '-'}
              </span>
            )}
            {row.original.description && (
              <span className="block truncate text-2xs text-muted-foreground">
                {row.original.description}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'ordered_unit_price',
        header: isJobWork ? 'Agreed rate' : 'PO price',
        enableSorting: false,
        meta: { align: 'right' },
        // The contract reports every optional variance figure as "absent or
        // null", so the check has to cover both. `=== null` alone let an
        // omitted field fall through and render as a confident zero.
        cell: ({ row }) =>
          row.original.ordered_unit_price == null ? (
            <span className="text-muted-foreground">{isJobWork ? 'No rate' : 'No PO line'}</span>
          ) : (
            formatMoney(row.original.ordered_unit_price, { currency })
          ),
      },
      {
        id: 'invoiced_unit_price',
        header: 'Invoice price',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.invoiced_unit_price, { currency }),
      },
      {
        id: 'price_variance',
        header: 'Price variance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <VarianceCell
            raw={row.original.price_variance ?? null}
            display={
              row.original.price_variance == null
                ? null
                : formatMoney(row.original.price_variance, { currency })
            }
            ratio={row.original.price_variance_ratio ?? null}
            outOfTolerance={!row.original.within_tolerance}
          />
        ),
      },
      {
        id: 'accepted_quantity',
        header: isJobWork ? 'Good output' : 'GRN accepted',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.accepted_quantity == null ? (
            <span className="text-muted-foreground">
              {isJobWork ? 'No receipt line' : 'No receipt'}
            </span>
          ) : (
            formatQuantity(row.original.accepted_quantity)
          ),
      },
      {
        id: 'invoiced_quantity',
        header: 'Invoice qty',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.invoiced_quantity),
      },
      {
        id: 'quantity_variance',
        header: 'Qty variance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <VarianceCell
            raw={row.original.quantity_variance ?? null}
            display={
              row.original.quantity_variance == null
                ? null
                : formatQuantity(row.original.quantity_variance)
            }
            ratio={row.original.quantity_variance_ratio ?? null}
            outOfTolerance={!row.original.within_tolerance}
          />
        ),
      },
      {
        id: 'amount_variance',
        header: 'Amount variance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <VarianceCell
            raw={row.original.amount_variance ?? null}
            display={
              row.original.amount_variance == null
                ? null
                : formatMoney(row.original.amount_variance, { currency })
            }
            ratio={null}
            outOfTolerance={!row.original.within_tolerance}
          />
        ),
      },
      {
        id: 'within_tolerance',
        header: 'Result',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="space-y-1">
            <Badge variant={row.original.within_tolerance ? 'success' : 'destructive'}>
              {row.original.within_tolerance ? 'Within tolerance' : 'Out of tolerance'}
            </Badge>
            {(row.original.findings ?? []).map((finding) => (
              <span key={finding} className="block text-2xs text-muted-foreground">
                {finding}
              </span>
            ))}
          </div>
        ),
      },
    ],
    [currency, isJobWork],
  )

  return (
    <>
      <Dialog
        open={Boolean(invoiceId)}
        onOpenChange={(open) => {
          if (!open) onClose()
        }}
      >
        <DialogContent size="xl">
          <DialogHeader>
            <DialogTitle>
              {invoice ? `Invoice ${invoice.vendor_invoice_number}` : 'Vendor invoice'}
            </DialogTitle>
            <DialogDescription>
              {invoice
                ? isJobWork
                  ? `${invoice.code} - job-work match against the order rate and receipt lines.`
                  : `${invoice.code} - three-way match against the purchase order and goods receipt.`
                : 'Loading the invoice and its three-way match.'}
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="space-y-4">
            {invoiceQuery.isLoading && (
              <div className="space-y-3">
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-40 w-full" />
              </div>
            )}

            {invoiceQuery.error && !invoiceQuery.isLoading && (
              <Alert variant="destructive">
                <AlertTitle>Could not load this invoice</AlertTitle>
                <AlertDescription>{invoiceQuery.error.message}</AlertDescription>
              </Alert>
            )}

            {invoice && (
              <>
                <DescriptionList
                  columns={4}
                  items={[
                    { label: 'Document', value: invoice.code },
                    { label: 'Status', value: <StatusBadge status={invoice.status} /> },
                    { label: 'Match', value: <MatchBadge status={invoice.match_status} /> },
                    { label: 'Type', value: humanise(invoice.invoice_type) },
                    { label: 'Currency', value: invoice.currency },
                    { label: 'Invoice date', value: formatDate(invoice.invoice_date) },
                    { label: 'Due', value: formatDate(invoice.due_date) },
                    isJobWork
                      ? {
                          label: 'Job-work order',
                          value: invoice.job_work_order_id ? (
                            <Link to="/job-work" className="hover:underline">
                              View orders
                            </Link>
                          ) : (
                            '-'
                          ),
                        }
                      : {
                          label: 'Purchase order',
                          value: invoice.purchase_order_id ? (
                            <Link
                              to={`/procurement/purchase-orders/${invoice.purchase_order_id}`}
                              className="hover:underline"
                            >
                              View order
                            </Link>
                          ) : (
                            '-'
                          ),
                        },
                    {
                      label: 'Goods receipt',
                      value: invoice.goods_receipt_id ? 'Receipted' : '-',
                    },
                    // Lines may name their own; this is where the rest go.
                    ...(canViewCostCentres
                      ? [
                          {
                            label: 'Cost centre',
                            value: (
                              <CostCentreName
                                id={invoice.cost_centre_id}
                                enabled={Boolean(invoiceId)}
                              />
                            ),
                          },
                        ]
                      : []),
                    {
                      label: 'Taxable',
                      value: formatMoney(invoice.subtotal, { currency: invoice.currency }),
                    },
                    {
                      label: 'Tax',
                      value: formatMoney(invoice.tax_amount, { currency: invoice.currency }),
                    },
                    {
                      label: 'Total',
                      value: formatMoney(invoice.total_amount, { currency: invoice.currency }),
                    },
                    {
                      label: 'Outstanding',
                      value: formatMoney(invoice.outstanding_amount, {
                        currency: invoice.currency,
                      }),
                    },
                    // The excess-loss recovery posted beside a job-work bill:
                    // the vendor's net payable is the total less this.
                    ...(isJobWork && !toDecimal(invoice.recovered_amount).isZero()
                      ? [
                          {
                            label: 'Recovered',
                            value: formatMoney(invoice.recovered_amount, {
                              currency: invoice.currency,
                            }),
                          },
                        ]
                      : []),
                    ...(!toDecimal(invoice.debit_allocated_amount).isZero()
                      ? [
                          {
                            label: 'Vendor return credits',
                            value: formatMoney(invoice.debit_allocated_amount, {
                              currency: invoice.currency,
                            }),
                          },
                        ]
                      : []),
                  ]}
                />

                {invoice.variance_override_reason && (
                  <Alert variant="warning">
                    <AlertTitle>Approved over a variance</AlertTitle>
                    <AlertDescription>
                      {invoice.variance_override_reason}
                      {invoice.approved_at ? ` - ${formatDate(invoice.approved_at)}` : ''}
                    </AlertDescription>
                  </Alert>
                )}

                {invoice.dispute_reason && (
                  <Alert variant="warning">
                    <AlertTitle>Disputed</AlertTitle>
                    <AlertDescription>{invoice.dispute_reason}</AlertDescription>
                  </Alert>
                )}
              </>
            )}

            {matchQuery.isLoading && <Skeleton className="h-16 w-full" />}

            {matchQuery.error && !matchQuery.isLoading && (
              <Alert variant="destructive">
                <AlertTitle>The match could not be read</AlertTitle>
                <AlertDescription>
                  {matchQuery.error.message} Approval stays unavailable until the match is known.
                </AlertDescription>
              </Alert>
            )}

            {match && (
              <Alert variant={blocked ? 'destructive' : 'info'}>
                <div className="flex items-start gap-3">
                  {blocked ? (
                    <ShieldAlert className="mt-0.5 shrink-0 text-destructive" aria-hidden />
                  ) : match.match_status === 'OVERRIDDEN' ? (
                    <TriangleAlert className="mt-0.5 shrink-0 text-warning" aria-hidden />
                  ) : (
                    <CheckCircle2 className="mt-0.5 shrink-0 text-success" aria-hidden />
                  )}
                  <div className="min-w-0 space-y-1.5">
                    <AlertTitle>
                      {blocked
                        ? match.has_overbilling
                          ? 'Over-billed - this cannot be overridden'
                          : 'Out of tolerance - approval needs an override reason'
                        : `Match result: ${MATCH_BADGE[match.match_status]?.label ?? humanise(match.match_status)}`}
                    </AlertTitle>
                    <AlertDescription>
                      {blockingReasons.length > 0 ? (
                        <ul className="list-disc space-y-0.5 pl-4">
                          {blockingReasons.map((entry) => (
                            <li key={entry}>{entry}</li>
                          ))}
                        </ul>
                      ) : (
                        <span>
                          Tolerances: price {ratioAsPercent(match.price_tolerance)}, quantity{' '}
                          {ratioAsPercent(match.quantity_tolerance)}.
                        </span>
                      )}
                    </AlertDescription>
                    <p className="text-2xs text-muted-foreground">
                      Invoiced {formatMoney(match.total_invoiced, { currency })} against an expected{' '}
                      {formatMoney(match.total_expected, { currency })} - a variance of{' '}
                      {formatMoney(match.total_variance, { currency })}, worst single line{' '}
                      {formatMoney(match.worst_variance, { currency })}.
                    </p>
                  </div>
                </div>
              </Alert>
            )}

            <DataTable
              columns={columns}
              data={match?.lines ?? []}
              isLoading={matchQuery.isLoading}
              isFetching={matchQuery.isFetching}
              error={matchQuery.error}
              getRowId={(row) => row.invoice_line_id ?? `line-${row.line_number}`}
              stickyHeader={false}
              emptyTitle="No matched lines"
              emptyDescription="This invoice has no lines that could be matched to a purchase order and receipt."
            />
          </DialogBody>

          <DialogFooter>
            {/* The document commands. Each one is offered only in the status
                its state machine permits - the predicates live in
                `finance-types.ts` so no status is compared inline here. */}
            {invoice && canRecord && isInvoiceDeletable(invoice.status) && (
              <Button
                variant="ghost"
                onClick={() => {
                  setCommandReason('')
                  setCommandTouched(false)
                  setCommand('delete')
                }}
              >
                <Trash2 className="text-destructive" />
                Delete draft
              </Button>
            )}

            {invoice && canRecord && onAmend && isInvoiceEditable(invoice.status) && (
              <Button variant="outline" onClick={() => onAmend(invoice)}>
                <Pencil />
                Amend
              </Button>
            )}

            {invoice && canRecord && isInvoiceSubmittable(invoice.status) && (
              <Button
                variant="outline"
                onClick={() => {
                  setCommandReason('')
                  setCommandTouched(false)
                  setCommand('submit')
                }}
              >
                <SendHorizontal />
                Submit for approval
              </Button>
            )}

            <Button variant="outline" onClick={onClose}>
              Close
            </Button>

            {!decided && canApprove && (
              <div className="flex flex-col items-stretch gap-1 sm:items-end">
                <Button
                  onClick={() => setConfirmOpen(true)}
                  // The match must be known before it can be overridden: an
                  // approval sent while the verdict is unread is a guess.
                  disabled={matchQuery.isLoading || Boolean(matchQuery.error)}
                >
                  <CheckCircle2 />
                  Approve invoice
                </Button>
                {blocked && !match?.has_overbilling && (
                  <span className="text-2xs text-muted-foreground">
                    Approving overrides the variance and needs a reason.
                  </span>
                )}
                {blocked && match?.has_overbilling && (
                  <span className="text-2xs text-muted-foreground">
                    Over-billing already-billed charges cannot be overridden - amend the bill.
                  </span>
                )}
              </div>
            )}

            {!decided && !canApprove && blocked && (
              <span className="self-center text-2xs text-muted-foreground">
                This variance can only be cleared by someone who may approve vendor invoices.
              </span>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={(open) => {
          setConfirmOpen(open)
          if (!open) {
            setReason('')
            setReasonTouched(false)
          }
        }}
        title={blocked ? 'Approve over a match variance' : 'Approve this invoice'}
        description={
          blocked
            ? 'The three-way match is outside tolerance. Approving it here overrides that, and the reason is recorded against the invoice and in the audit log.'
            : 'The invoice becomes payable once approved.'
        }
        confirmLabel="Approve"
        destructive={blocked}
        loading={approve.isPending}
        onConfirm={() => {
          if (reasonUnusable) {
            setReasonTouched(true)
            return
          }
          approve.mutate(trimmedReason)
        }}
      >
        <div>
          <Label htmlFor="invoice-override-reason" required={needsOverride}>
            {needsOverride ? 'Override reason' : 'Note'}
          </Label>
          <Textarea
            id="invoice-override-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="mt-1.5"
            maxLength={MAX_INVOICE_REASON_LENGTH}
            placeholder={
              needsOverride
                ? 'Why is this variance acceptable? Recorded against the invoice.'
                : 'Optional'
            }
            aria-invalid={reasonTouched && reasonUnusable}
          />
          {reasonTouched && reasonUnusable && (
            <FieldError
              message={
                needsOverride
                  ? `Record at least ${MIN_OVERRIDE_REASON} characters explaining the override`
                  : `A note is optional, but one that is written must run to at least ${MIN_INVOICE_REASON_LENGTH} characters`
              }
            />
          )}
        </div>
      </ConfirmDialog>

      {/* Submit and delete share one confirmation, because they ask the same
          question of the operator - a sentence saying why - and differ only in
          whether that sentence is required and how badly it matters. */}
      <ConfirmDialog
        open={command !== null}
        onOpenChange={(open) => {
          if (!open) closeCommand()
        }}
        title={command === 'delete' ? 'Delete this draft bill' : 'Send this bill for approval'}
        description={
          command === 'delete'
            ? 'The draft is removed for good. Nothing of it reached the ledger, so there is nothing to reverse - but the deletion and your reason are recorded in the audit log.'
            : 'The invoice moves to the approval queue, and the three-way match is run on the way in so the approver sees any variance before opening the document.'
        }
        confirmLabel={command === 'delete' ? 'Delete the draft' : 'Submit'}
        destructive={command === 'delete'}
        loading={commandPending}
        onConfirm={() => {
          if (commandReasonUnusable) {
            setCommandTouched(true)
            return
          }
          if (command === 'delete') deleteDraft.mutate(trimmedCommandReason)
          else submit.mutate(trimmedCommandReason)
        }}
      >
        <div>
          <Label htmlFor="invoice-command-reason" required={command === 'delete'}>
            {command === 'delete' ? 'Reason' : 'Note for the approver'}
          </Label>
          <Textarea
            id="invoice-command-reason"
            value={commandReason}
            onChange={(event) => setCommandReason(event.target.value)}
            className="mt-1.5"
            maxLength={MAX_INVOICE_REASON_LENGTH}
            placeholder={
              command === 'delete'
                ? 'Why is this draft being removed? Recorded in the audit log.'
                : 'Optional - anything the approver should know about this bill.'
            }
            aria-invalid={commandTouched && commandReasonUnusable}
          />
          {commandTouched && commandReasonUnusable && (
            <FieldError
              message={
                command === 'delete'
                  ? `Record at least ${MIN_INVOICE_REASON_LENGTH} characters of reason`
                  : `A note is optional, but one that is written must run to at least ${MIN_INVOICE_REASON_LENGTH} characters`
              }
            />
          )}
        </div>
      </ConfirmDialog>
    </>
  )
}

/**
 * A variance figure with its proportion, toned by whether the line cleared.
 *
 * `raw` decides the tone and `display` is what the reader sees - the comparison
 * runs on the decimal string, never on the formatted text. `ratio` arrives as a
 * proportion and is scaled to a percentage on a Decimal.
 */
function VarianceCell({
  raw,
  display,
  ratio,
  outOfTolerance,
}: {
  raw: DecimalString | null
  display: string | null
  ratio: DecimalString | null
  outOfTolerance: boolean
}) {
  if (raw === null || display === null) return <span className="text-muted-foreground">-</span>

  const isZero = toDecimal(raw).isZero()

  return (
    <div
      className={cn(
        outOfTolerance ? 'text-destructive' : isZero ? 'text-muted-foreground' : 'text-warning',
      )}
    >
      <span className="block font-medium">{display}</span>
      {ratio !== null && <span className="block text-2xs">{ratioAsPercent(ratio)}</span>}
    </div>
  )
}

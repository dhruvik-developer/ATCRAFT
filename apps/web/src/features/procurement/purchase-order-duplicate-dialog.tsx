/**
 * The duplicate purchase order conversation.
 *
 * When the API answers a create with 409 `DUPLICATE_DOCUMENT`, it has found
 * open order lines that already cover the requirement being raised. A toast
 * would be the wrong response: the buyer needs to see *which* lines, on which
 * orders, and how sure the matcher is - that is what stops the same material
 * being bought twice.
 *
 * Only one of the four resolutions in master rules section 16 has a create-time
 * endpoint. `CREATE_NEW_WITH_APPROVAL` is a resubmit carrying
 * `acknowledge_duplicates` and a recorded reason, and the API refuses even that
 * without `purchase_order.approve`. Merging into the existing order, topping its
 * quantity up and cancelling it are all edits to *that* document and have no
 * path from here, so they are presented as what to go and do, with a link,
 * rather than as buttons that would move the refusal one click later.
 */

import { AlertTriangle, ExternalLink, ShieldAlert, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { FieldError, StatusBadge } from '@/components/shared'
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
  Textarea,
} from '@/components/ui/primitives'
import { formatDate, formatQuantity } from '@/lib/format'
import { messages } from '@/lib/messages'

import {
  DUPLICATE_ACTION_COPY,
  DUPLICATE_CONFIDENCE_COPY,
  type DuplicateActionCode,
  type DuplicateConflict,
} from './procurement-types'

/**
 * The bounds on the acknowledgement reason.
 *
 * `PurchaseOrderCreate.duplicate_acknowledgement_reason` is capped at 1000 -
 * not the 255 every other reason in procurement stops at - and the API refuses
 * the flag without one. The control stops at the cap rather than letting a
 * pasted justification earn a 422 for a field this dialog owns but the form's
 * `applyFieldErrors` has no control to land on.
 */
const MIN_ACKNOWLEDGEMENT_REASON = 3
const MAX_ACKNOWLEDGEMENT_REASON = 1000

/**
 * What the dialog says about the document that was refused.
 *
 * One order raised by hand by default. A requisition conversion raises several
 * orders in one transaction and is refused as a whole, so it says so in the
 * plural rather than telling the buyer "the order" was not created.
 */
export interface DuplicateDialogCopy {
  notCreated: string
  dismissLabel: string
  confirmLabel: string
}

const SINGLE_ORDER_COPY: DuplicateDialogCopy = {
  notCreated: 'The order was not created.',
  dismissLabel: 'Cancel, do not raise this order',
  confirmLabel: 'Raise it anyway',
}

export function PurchaseOrderDuplicateDialog({
  conflict,
  open,
  onOpenChange,
  onAcknowledge,
  isResolving,
  canAcknowledge,
  copy = SINGLE_ORDER_COPY,
}: {
  conflict: DuplicateConflict
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Re-submits the same order acknowledging the duplicates, with this reason. */
  onAcknowledge: (reason: string) => void
  isResolving: boolean
  /** The API refuses an acknowledgement without purchase order approval. */
  canAcknowledge: boolean
  copy?: DuplicateDialogCopy
}) {
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  const reasonTooShort = reason.trim().length < MIN_ACKNOWLEDGEMENT_REASON
  const reasonTooLong = reason.trim().length > MAX_ACKNOWLEDGEMENT_REASON
  const reasonInvalid = reasonTooShort || reasonTooLong

  // The union of what the matcher says is possible across every candidate,
  // recommended ones first, so the buyer reads the cheapest fix at the top.
  const recommended = new Set(conflict.candidates.map((candidate) => candidate.recommended_action))
  const offered = [...new Set(conflict.candidates.flatMap((c) => c.available_actions))].sort(
    (a, b) => Number(recommended.has(b)) - Number(recommended.has(a)),
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>This requirement may already be on order</DialogTitle>
          <DialogDescription>
            {copy.notCreated} {conflict.candidate_count}{' '}
            {conflict.candidate_count === 1 ? 'open order line' : 'open order lines'} already cover
            what you are raising.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <Alert variant="warning">
            <TriangleAlert aria-hidden />
            <AlertTitle>Why this stopped</AlertTitle>
            <AlertDescription>{conflict.message}</AlertDescription>
          </Alert>

          <div>
            <h3 className="text-sm font-medium">Lines that already cover this</h3>
            <ul className="mt-2 divide-y rounded-lg border">
              {conflict.candidates.map((candidate) => {
                const confidence = DUPLICATE_CONFIDENCE_COPY[candidate.confidence]
                return (
                  <li key={candidate.purchase_order_line_id} className="space-y-2 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        to={`/procurement/purchase-orders/${candidate.purchase_order_id}`}
                        target="_blank"
                        rel="noreferrer noopener"
                        // Opened in a new tab so the order being raised, and
                        // everything typed into it, survives the comparison.
                        className="inline-flex items-center gap-1 font-medium hover:underline"
                      >
                        {candidate.purchase_order_code}
                        <ExternalLink className="h-3 w-3" aria-hidden />
                        <span className="sr-only">(opens in a new tab)</span>
                      </Link>
                      <StatusBadge status={candidate.status} />
                      <Badge variant={confidence.variant}>{confidence.label}</Badge>
                    </div>

                    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
                      <div>
                        <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                          Item
                        </dt>
                        <dd className="truncate">
                          {candidate.item_id ? (
                            <Link
                              to={`/items/${candidate.item_id}`}
                              target="_blank"
                              rel="noreferrer noopener"
                              className="hover:underline"
                            >
                              Open the item
                            </Link>
                          ) : (
                            '-'
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                          Still expected
                        </dt>
                        <dd className="tabular">{formatQuantity(candidate.open_quantity)}</dd>
                      </div>
                      <div>
                        <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                          Required by
                        </dt>
                        <dd>{formatDate(candidate.required_date)}</dd>
                      </div>
                    </dl>

                    {candidate.match_reasons.length > 0 && (
                      <ul className="flex flex-wrap gap-1">
                        {candidate.match_reasons.map((match) => (
                          <li key={match}>
                            <Badge variant="muted">{match}</Badge>
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-medium">How would you like to proceed?</h3>
            <ul className="mt-2 space-y-2">
              {offered.map((action: DuplicateActionCode) => {
                const copy = DUPLICATE_ACTION_COPY[action]
                return (
                  <li key={action} className="rounded-lg border p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{copy.label}</span>
                      {recommended.has(action) && <Badge variant="default">Recommended</Badge>}
                      {copy.actionable && (
                        <Badge variant="warning">
                          <ShieldAlert className="h-3 w-3" aria-hidden />
                          Needs approval rights
                        </Badge>
                      )}
                    </div>
                    <p className="mt-0.5 text-sm text-muted-foreground">{copy.description}</p>
                  </li>
                )
              })}
            </ul>
          </div>

          {canAcknowledge ? (
            <div>
              <Label htmlFor="po-duplicate-reason" required>
                Why is this order needed as well?
              </Label>
              <Textarea
                id="po-duplicate-reason"
                value={reason}
                maxLength={MAX_ACKNOWLEDGEMENT_REASON}
                onChange={(event) => setReason(event.target.value)}
                className="mt-1.5"
                aria-invalid={reasonTouched && reasonInvalid}
              />
              {reasonTouched && reasonTooShort ? (
                <FieldError message={messages.errors.reasonTooShort} />
              ) : reasonTouched && reasonTooLong ? (
                <FieldError message={`At most ${MAX_ACKNOWLEDGEMENT_REASON} characters`} />
              ) : (
                <p className="mt-1 text-xs text-muted-foreground">
                  Stored on the order and shown to anybody who opens it later.
                </p>
              )}
            </div>
          ) : (
            <Alert variant="info">
              <AlertTriangle aria-hidden />
              <AlertTitle>You cannot override this yourself</AlertTitle>
              <AlertDescription>
                Raising an order over a duplicate warning needs purchase order approval rights. Fix
                one of the existing orders above, or ask an approver to raise this one.
              </AlertDescription>
            </Alert>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isResolving}>
            {copy.dismissLabel}
          </Button>
          {canAcknowledge && (
            <Button
              loading={isResolving}
              onClick={() => {
                if (reasonInvalid) {
                  setReasonTouched(true)
                  return
                }
                onAcknowledge(reason.trim())
              }}
            >
              {copy.confirmLabel}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

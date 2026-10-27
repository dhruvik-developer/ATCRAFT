import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, RefreshCw, TriangleAlert } from 'lucide-react'
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
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { REASON_MIN_LENGTH } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { formatDate, formatQuantity } from '@/lib/format'
import { describeError, messages } from '@/lib/messages'
import { toast } from '@/lib/toast'

import {
  MAX_DUPLICATE_REASON_LENGTH,
  canResolveDuplicateCandidate,
  duplicateReasonLimit,
  duplicateResolutionPermission,
  duplicateResolutionSuccess,
  invalidatePurchaseOrderChanges,
  isPoAmendable,
  type DuplicateResolutionAction,
  type DuplicateResolutionDraft,
  type DuplicateResolutionRequest,
  type DuplicateResolutionResult,
  type DuplicateResolutionScope,
} from './purchase-order-change-model'
import {
  DUPLICATE_CONFIDENCE_COPY,
  parseDuplicateConflict,
  type DuplicateCandidate,
  type DuplicateConflict,
} from './procurement-types'

/** Real actions retain the refused draft and name the demand affected by each command. */
export function PurchaseOrderDuplicateDialog({
  conflict,
  draft,
  scopes,
  onConflict,
  onOpenChange,
  onResolved,
  onRefresh,
  isPreparing = false,
}: {
  conflict: DuplicateConflict
  draft: DuplicateResolutionDraft
  scopes?: DuplicateResolutionScope[]
  onConflict: (conflict: DuplicateConflict) => void
  onOpenChange: (open: boolean) => void
  onResolved: (result: DuplicateResolutionResult, request: DuplicateResolutionRequest) => void
  onRefresh: () => Promise<void>
  isPreparing?: boolean
}) {
  const { can } = useAuth()
  const client = useQueryClient()
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)
  const resolve = useMutation({
    mutationFn: async (request: DuplicateResolutionRequest) => {
      try {
        return {
          result: await api.post<DuplicateResolutionResult>(
            '/procurement/purchase-orders/duplicate-resolution',
            request,
          ),
        }
      } catch (error) {
        const next = parseDuplicateConflict(error)
        if (next) return { conflict: next }
        throw error
      }
    },
    onSuccess: (outcome, request) => {
      if ('conflict' in outcome && outcome.conflict) {
        onConflict(outcome.conflict)
        return
      }
      if (!outcome.result) return
      invalidatePurchaseOrderChanges(client)
      const copy = duplicateResolutionSuccess(outcome.result)
      toast.success(copy.title, copy.description)
      onResolved(outcome.result, request)
    },
  })
  const refresh = useMutation({
    mutationFn: onRefresh,
    onSuccess: () => {
      resolve.reset()
      setReasonError(null)
    },
  })
  const busy = resolve.isPending || refresh.isPending || isPreparing
  const proposedContexts: DuplicateResolutionScope[] = scopes ?? [
    {
      id: 'manual',
      title: 'This purchase order draft',
      draft,
      candidateLineIds: conflict.candidates.map((candidate) => candidate.purchase_order_line_id),
      itemIds:
        draft.kind === 'MANUAL' ? draft.purchase_order.lines.map((line) => line.item_id) : [],
    },
  ]
  const coveredCandidates = new Set(proposedContexts.flatMap((scope) => scope.candidateLineIds))
  const unmatchedCandidates = conflict.candidates.filter(
    (candidate) => !coveredCandidates.has(candidate.purchase_order_line_id),
  )
  const contexts = unmatchedCandidates.length
    ? [
        ...proposedContexts,
        {
          id: 'unmatched',
          title: 'Additional matches - refresh the comparison to choose a proposal group',
          draft,
          candidateLineIds: unmatchedCandidates.map(
            (candidate) => candidate.purchase_order_line_id,
          ),
          itemIds: [],
          readOnly: true,
        },
      ]
    : proposedContexts

  function execute(
    action: DuplicateResolutionAction,
    context = draft,
    candidate?: DuplicateCandidate,
  ) {
    if (busy || !can(duplicateResolutionPermission(action))) return
    const text = reason.trim()
    const limit = duplicateReasonLimit(action)
    if (text.length < REASON_MIN_LENGTH) {
      setReasonError(messages.errors.reasonTooShort)
      return
    }
    if (text.length > limit) {
      setReasonError(`At most ${limit} characters for this action`)
      return
    }
    setReasonError(null)
    resolve.mutate({
      action,
      draft: context,
      reason: text,
      ...(candidate
        ? {
            candidate_purchase_order_id: candidate.purchase_order_id,
            expected_candidate_version: candidate.purchase_order_version,
            ...(action === 'INCREASE'
              ? { candidate_line_id: candidate.purchase_order_line_id }
              : {}),
          }
        : {}),
    })
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!busy) onOpenChange(open)
      }}
    >
      <DialogContent
        size="lg"
        className="[&_button]:h-auto [&_button]:min-h-10 [&_button]:whitespace-normal"
      >
        <DialogHeader>
          <DialogTitle>This requirement may already be on order</DialogTitle>
          <DialogDescription>
            No purchase order was created. {conflict.candidate_count} open order line(s) may cover
            this demand. Review a candidate and choose an action.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-5">
          <Alert variant="warning">
            <TriangleAlert aria-hidden />
            <AlertTitle>Why this stopped</AlertTitle>
            <AlertDescription>{conflict.message}</AlertDescription>
          </Alert>
          {draft.kind === 'REQUISITION' && contexts.length > 1 && (
            <p className="text-sm text-muted-foreground">
              Merge and increase affect only the named proposal group. Other groups stay in the
              proposal.
            </p>
          )}
          {contexts.map((scope) => {
            const candidates = conflict.candidates.filter((candidate) =>
              scope.candidateLineIds.includes(candidate.purchase_order_line_id),
            )
            if (!candidates.length) return null
            return (
              <section key={scope.id} className="space-y-3" aria-label={scope.title}>
                <h3 className="font-medium">{scope.title}</h3>
                <ul className="divide-y rounded-lg border">
                  {candidates.map((candidate) => {
                    const confidence = DUPLICATE_CONFIDENCE_COPY[candidate.confidence]
                    const amendment = isPoAmendable(candidate)
                    const canChange = !scope.readOnly && can(duplicateResolutionPermission('MERGE'))
                    return (
                      <li key={candidate.purchase_order_line_id} className="min-w-0 space-y-3 p-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <Link
                            to={`/procurement/purchase-orders/${candidate.purchase_order_id}`}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="inline-flex items-center gap-1 font-medium hover:underline"
                          >
                            {candidate.purchase_order_code}
                            <ExternalLink className="h-3 w-3" aria-hidden />
                            <span className="sr-only">(opens in a new tab)</span>
                          </Link>
                          <StatusBadge status={candidate.status} />
                          <Badge variant={confidence.variant}>{confidence.label}</Badge>
                        </div>
                        <dl className="grid gap-3 text-sm sm:grid-cols-2">
                          <div>
                            <dt className="text-muted-foreground">Still expected</dt>
                            <dd>{formatQuantity(candidate.open_quantity)}</dd>
                          </div>
                          <div>
                            <dt className="text-muted-foreground">Required by</dt>
                            <dd>{formatDate(candidate.required_date)}</dd>
                          </div>
                        </dl>
                        <div className="flex flex-wrap gap-1">
                          {candidate.match_reasons.map((match) => (
                            <Badge key={match} variant="muted">
                              {match}
                            </Badge>
                          ))}
                        </div>
                        {candidate.has_receipt_history && (
                          <p className="text-sm text-muted-foreground">
                            Receipt history prevents changes to this order. Create a separate order
                            for additional demand.
                          </p>
                        )}
                        <div className="flex flex-wrap gap-2">
                          {canChange &&
                            canResolveDuplicateCandidate(candidate, 'MERGE', scope.itemIds) && (
                              <Button
                                type="button"
                                variant="outline"
                                disabled={busy}
                                onClick={() => execute('MERGE', scope.draft, candidate)}
                              >
                                {amendment
                                  ? `Amend ${candidate.purchase_order_code} (vendor re-accepts)`
                                  : `Merge into ${candidate.purchase_order_code}`}
                              </Button>
                            )}
                          {canChange &&
                            canResolveDuplicateCandidate(candidate, 'INCREASE', scope.itemIds) && (
                              <Button
                                type="button"
                                variant="outline"
                                disabled={busy}
                                onClick={() => execute('INCREASE', scope.draft, candidate)}
                              >
                                {amendment
                                  ? `Amend ${candidate.purchase_order_code} line qty (vendor re-accepts)`
                                  : `Increase ${candidate.purchase_order_code} line qty`}
                              </Button>
                            )}
                        </div>
                      </li>
                    )
                  })}
                </ul>
              </section>
            )
          })}
          <div className="space-y-2">
            <Label htmlFor="po-duplicate-reason" required>
              Reason for this action
            </Label>
            <Textarea
              id="po-duplicate-reason"
              value={reason}
              disabled={busy}
              maxLength={MAX_DUPLICATE_REASON_LENGTH}
              onChange={(event) => {
                setReason(event.target.value)
                setReasonError(null)
                resolve.reset()
              }}
              aria-invalid={Boolean(reasonError)}
            />
            <FieldError message={reasonError ?? undefined} />
            <p className="text-sm text-muted-foreground">
              Your reason is recorded. A separate order still needs independent approval and
              explicit confirmation of the duplicate demand.
            </p>
          </div>
          {resolve.error && (
            <FieldError
              message={fieldErrorSummary(resolve.error) || describeError(resolve.error)}
            />
          )}
          {refresh.error && (
            <FieldError
              message={fieldErrorSummary(refresh.error) || describeError(refresh.error)}
            />
          )}
          <Button type="button" variant="outline" disabled={busy} onClick={() => refresh.mutate()}>
            <RefreshCw aria-hidden /> Refresh comparison
          </Button>
        </DialogBody>
        <DialogFooter className="flex-wrap">
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            Back to the draft
          </Button>
          {can(duplicateResolutionPermission('CANCEL_REQUEST')) && (
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              onClick={() => execute('CANCEL_REQUEST')}
            >
              {draft.kind === 'REQUISITION' ? 'Cancel this request' : 'Discard this draft'}
            </Button>
          )}
          {can(duplicateResolutionPermission('CREATE_WITH_APPROVAL')) && (
            <Button
              type="button"
              loading={resolve.isPending}
              disabled={busy}
              onClick={() => execute('CREATE_WITH_APPROVAL')}
            >
              Create separate PO (needs approval)
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

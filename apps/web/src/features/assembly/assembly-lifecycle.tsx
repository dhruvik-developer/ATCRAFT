/**
 * The small commands on an assembly order: one reason, one verdict.
 *
 * Approve, release, start, close and cancel each carry a mandatory audit
 * reason (`ReasonRequest.reason`, min 3 / max 255), so they share
 * `ReasonDialog`: a title, an optional explanation, and a reason box that
 * refuses to submit until the reason is long enough. The caller owns the
 * mutation; this module only collects the reason.
 *
 * `AssemblyVerdictDialog` is the older verdict path: the inspection verdict on
 * held output for a hold NO quality inspection covers. When the order names an
 * inspection, that inspection is the one release path and the caller opens it
 * (the shared `InspectionDialog` from quality) instead of this. The two
 * quantities must account for everything on hold; the server enforces the sum.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { ConfirmDialog, FieldError } from '@/components/shared'
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
import { Alert, AlertDescription, Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { formatQuantity } from '@/lib/format'
import { toast } from '@/lib/toast'
import type { ReactNode } from 'react'

import { assemblyKeys, type AssemblyOrderDetail } from './assembly-model'

/** `Quantity` is NUMERIC(18,4): at most 14 integer digits and 4 decimals. */
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const OPTIONAL_QUANTITY = /^(\d{1,14}(\.\d{1,4})?)?$/

const asDecimal = (value: string): string => (value.trim() === '' ? '0' : value.trim())

// ------------------------------------------------------------ reason ---------

export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  destructive = false,
  pending = false,
  error,
  onConfirm,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  confirmLabel: string
  destructive?: boolean
  pending?: boolean
  error?: { message: string } | null
  onConfirm: (reason: string) => void
  children?: ReactNode
}) {
  const [reason, setReason] = useState('')
  const [complaint, setComplaint] = useState<string | null>(null)

  const submit = () => {
    const trimmed = reason.trim()
    if (trimmed.length < REASON_MIN_LENGTH) {
      setComplaint(
        `A reason of at least ${REASON_MIN_LENGTH} characters is recorded with every hand-over`,
      )
      return
    }
    setComplaint(null)
    onConfirm(trimmed)
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        // A refused reason must not greet the next opening: the box and its
        // complaint are cleared whenever the dialog closes.
        if (!next) {
          setReason('')
          setComplaint(null)
        }
        onOpenChange(next)
      }}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      destructive={destructive}
      loading={pending}
      onConfirm={submit}
    >
      <div className="px-6">
        {children}
        <div className="pb-1">
          <Label htmlFor="assembly-reason" required>
            Reason
          </Label>
          <Textarea
            id="assembly-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={2}
            maxLength={REASON_MAX_LENGTH}
            autoComplete="off"
            className="mt-1.5"
            aria-invalid={Boolean(complaint)}
            placeholder="Recorded on the audit trail with this change"
          />
          <FieldError message={complaint ?? undefined} />
          {error && (
            <Alert variant="destructive" className="mt-2">
              <AlertDescription>{error.message}</AlertDescription>
            </Alert>
          )}
        </div>
      </div>
    </ConfirmDialog>
  )
}

// ------------------------------------------------------------ verdict --------

/**
 * The direct verdict on held output, for a hold no inspection covers.
 *
 * Accepted goes back to the available pool, rejected to the damaged pool, and
 * the two must account for everything held. Defaults to accepting everything
 * the order assembled; rejecting is the deliberate edit.
 */
export function AssemblyVerdictDialog({
  open,
  onOpenChange,
  order,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  order: AssemblyOrderDetail
}) {
  const queryClient = useQueryClient()
  const [accepted, setAccepted] = useState(order.assembled_quantity)
  const [rejected, setRejected] = useState('0')
  const [reason, setReason] = useState('')
  const [complaint, setComplaint] = useState<string | null>(null)

  const record = useMutation({
    mutationFn: (values: {
      accepted_quantity: string
      rejected_quantity: string
      reason: string
    }) => api.post<AssemblyOrderDetail>(`/assembly/${order.id}/qc-result`, values),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
      // Accepted output leaves QC hold and becomes promisable, so every stock
      // figure on every other screen has moved.
      toast.success(
        'Inspection recorded',
        `${updated.code}: ${formatQuantity(updated.assembled_quantity)} accepted, ${formatQuantity(updated.rejected_quantity)} rejected.`,
      )
      onOpenChange(false)
    },
    onError: (error) => {
      // This form owns no field the server names, so the refusal is shown
      // where the operator is looking: the toast layer stays silent on a
      // mutation the form owns.
      setComplaint(error.message)
    },
  })

  const submit = () => {
    if (!QUANTITY.test(accepted.trim()) || !OPTIONAL_QUANTITY.test(rejected.trim())) {
      setComplaint('Enter quantities with up to 4 decimal places')
      return
    }
    const trimmedReason = reason.trim()
    if (trimmedReason.length < REASON_MIN_LENGTH) {
      setComplaint(
        `A reason of at least ${REASON_MIN_LENGTH} characters is recorded with this verdict`,
      )
      return
    }
    setComplaint(null)
    record.mutate({
      accepted_quantity: asDecimal(accepted),
      rejected_quantity: asDecimal(rejected),
      reason: trimmedReason,
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Record verdict for {order.code}</DialogTitle>
          <DialogDescription>
            No inspection covers this hold, so the verdict is recorded here. Accepted output returns
            to the available pool; rejected output moves to the damaged pool. The two must account
            for everything on hold.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="assembly-verdict-accepted" required>
                Accepted
              </Label>
              <Input
                id="assembly-verdict-accepted"
                value={accepted}
                onChange={(event) => setAccepted(event.target.value)}
                inputMode="decimal"
                autoComplete="off"
                className="mt-1.5 text-right tabular"
              />
            </div>
            <div>
              <Label htmlFor="assembly-verdict-rejected">Rejected</Label>
              <Input
                id="assembly-verdict-rejected"
                value={rejected}
                onChange={(event) => setRejected(event.target.value)}
                inputMode="decimal"
                autoComplete="off"
                className="mt-1.5 text-right tabular"
              />
            </div>
          </div>

          <div>
            <Label htmlFor="assembly-verdict-reason" required>
              Reason
            </Label>
            <Textarea
              id="assembly-verdict-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              maxLength={REASON_MAX_LENGTH}
              autoComplete="off"
              className="mt-1.5"
              placeholder="Recorded on the audit trail with this verdict"
            />
          </div>

          {(complaint || record.error) && (
            <Alert variant="destructive">
              <AlertDescription>{complaint ?? record.error?.message}</AlertDescription>
            </Alert>
          )}
        </DialogBody>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={record.isPending}
          >
            Cancel
          </Button>
          <Button onClick={submit} loading={record.isPending}>
            Record verdict
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

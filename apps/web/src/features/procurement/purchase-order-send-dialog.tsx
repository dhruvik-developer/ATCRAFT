/**
 * Sending an approved purchase order to its vendor, and to one person there.
 *
 * Every order has an owner on the vendor's side: the vendor user who manages its
 * item category, chosen by the assignment rule when the order was raised or,
 * failing that, here. The picker lists exactly the people `POST .../send`
 * accepts - `GET .../vendor-assignees`, active users of this vendor who may act
 * on purchase orders and whose category scope covers the order's - so it can
 * never offer somebody the send would then refuse. It is served by procurement
 * because the buyer sending the order holds `purchase_order.send`, not
 * `user.view`.
 *
 * What is preselected follows what the API would do with no choice at all: the
 * person already on the order if they can still take it, else the rule's
 * suggestion. "Leave it unassigned" is offered only when that is genuinely what
 * an empty choice means - nobody suggested, and nobody on the order who can
 * still take it - because otherwise the API fills the gap itself and the option
 * would say something the send does not do.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Send } from 'lucide-react'
import { useEffect, useState } from 'react'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, SELECT_NONE } from '@/lib/constants'
import { success } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  purchaseOrderKeys,
  type PurchaseOrderDetail,
  type PurchaseOrderRow,
  type PurchaseOrderVendorAssignee,
  type SendOrderPayload,
} from './procurement-types'
import { serverFieldMessage } from './server-field-errors'

/** Who to preselect: the order's own assignee if still eligible, else the rule's choice. */
function initialChoice(assignees: PurchaseOrderVendorAssignee[], current: string | null): string {
  if (current && assignees.some((assignee) => assignee.id === current)) return current
  return assignees.find((assignee) => assignee.is_suggested)?.id ?? SELECT_NONE
}

function describe(assignee: PurchaseOrderVendorAssignee): string {
  const scope =
    assignee.allowed_category_codes.length > 0
      ? assignee.allowed_category_codes.join(', ')
      : 'every category'
  return `${assignee.email} - ${scope}`
}

export function PurchaseOrderSendDialog({
  order,
  open,
  onOpenChange,
}: {
  order: PurchaseOrderDetail
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [choice, setChoice] = useState<string>(SELECT_NONE)
  const [note, setNote] = useState('')
  const [serverNote, setServerNote] = useState<string | null>(null)

  const assignees = useQuery({
    queryKey: purchaseOrderKeys.vendorAssignees(order.id),
    queryFn: () =>
      api.get<PurchaseOrderVendorAssignee[]>(
        `/procurement/purchase-orders/${order.id}/vendor-assignees`,
      ),
    enabled: open,
    // Who can take the order changes when somebody at the vendor is added,
    // deactivated or re-scoped - read it fresh each time the dialog opens.
    staleTime: staleTimes.live,
  })
  const eligible = assignees.data ?? []
  const current = order.assigned_vendor_user_id

  useEffect(() => {
    if (!open) return
    setNote('')
    setServerNote(null)
  }, [open])

  // Seeded when the list arrives. Structural sharing keeps `assignees.data` the
  // same object across a refetch that changed nothing, so a choice the buyer has
  // already made is not overwritten by one.
  useEffect(() => {
    if (open && assignees.data) setChoice(initialChoice(assignees.data, current))
  }, [open, assignees.data, current])

  const currentStillEligible = current
    ? eligible.some((assignee) => assignee.id === current)
    : false
  const suggested = eligible.find((assignee) => assignee.is_suggested) ?? null
  // An empty choice sends `null`, and the API then keeps the order's assignee if
  // they can still take it, or applies the rule. "Unassigned" is honest only
  // when both of those give nobody.
  const mayLeaveUnassigned = !currentStillEligible && !suggested
  // Somebody was on the order and can no longer take it - deactivated or
  // re-scoped since it was raised. The send replaces them rather than refusing.
  const replacedAssignee = Boolean(current) && !currentStillEligible
  const ambiguous = !suggested && eligible.length > 1

  const send = useMutation({
    mutationFn: () => {
      const body: SendOrderPayload = {
        assigned_vendor_user_id: choice === SELECT_NONE ? null : choice,
        reason: note.trim() === '' ? null : note.trim(),
      }
      return api.post<PurchaseOrderRow>(`/procurement/purchase-orders/${order.id}/send`, body)
    },
    onSuccess: (updated) => {
      // Sending changes what MRP nets as incoming supply's timing, so planning
      // goes with procurement.
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.mrp.all })
      const person = eligible.find((assignee) => assignee.id === choice)?.full_name
      toast.success(
        success.sent('Purchase order'),
        person
          ? `${updated.code} is with the vendor, assigned to ${person}.`
          : `${updated.code} is with the vendor.`,
      )
      onOpenChange(false)
    },
    onError: (error) => setServerNote(serverFieldMessage(error, 'reason')),
  })

  const noteTooLong = note.trim().length > REASON_MAX_LENGTH
  const blocked = assignees.isLoading || Boolean(assignees.error) || noteTooLong

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Send {order.code} to the vendor?</DialogTitle>
          <DialogDescription>
            The order is issued to the vendor and the delivery clock starts against the expected
            date. The person chosen below owns it on the vendor portal.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div>
            <Label htmlFor="po-send-assignee">Vendor user responsible</Label>
            {assignees.isLoading ? (
              <Skeleton className="mt-1.5 h-9 w-full" />
            ) : assignees.error ? (
              <Alert variant="destructive" className="mt-1.5">
                <AlertTitle>Could not load the vendor&apos;s users</AlertTitle>
                <AlertDescription>{assignees.error.message}</AlertDescription>
              </Alert>
            ) : eligible.length === 0 ? (
              <p className="mt-1.5 rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                Nobody at this vendor can take the order on the portal yet, so it goes unassigned.
                Anyone at the vendor whose category scope covers it can still see it there.
              </p>
            ) : (
              <Select value={choice} onValueChange={setChoice}>
                <SelectTrigger id="po-send-assignee" className="mt-1.5">
                  <SelectValue placeholder="Choose who owns it" />
                </SelectTrigger>
                <SelectContent>
                  {mayLeaveUnassigned && (
                    <SelectItem value={SELECT_NONE}>Leave it unassigned</SelectItem>
                  )}
                  {eligible.map((assignee) => (
                    <SelectItem key={assignee.id} value={assignee.id}>
                      {assignee.full_name}
                      {assignee.is_suggested ? ' (suggested)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {eligible.length > 0 && choice !== SELECT_NONE && (
              <p className="mt-1 text-xs text-muted-foreground">
                {describe(eligible.find((assignee) => assignee.id === choice) ?? eligible[0]!)}
              </p>
            )}
            {replacedAssignee && (
              <p className="mt-1 text-xs text-warning">
                {order.assigned_vendor_user_name ?? 'The person on this order'} can no longer take
                it, so somebody else has to.
              </p>
            )}
            {ambiguous && choice === SELECT_NONE && (
              <p className="mt-1 text-xs text-muted-foreground">
                Several people at the vendor cover this category, so nobody was chosen for you.
              </p>
            )}
          </div>

          <div>
            <Label htmlFor="po-send-note">Note</Label>
            <Textarea
              id="po-send-note"
              value={note}
              maxLength={REASON_MAX_LENGTH}
              onChange={(event) => {
                setNote(event.target.value)
                setServerNote(null)
              }}
              className="mt-1.5"
              aria-invalid={noteTooLong || serverNote !== null}
            />
            {noteTooLong ? (
              <FieldError message={`At most ${REASON_MAX_LENGTH} characters`} />
            ) : serverNote !== null ? (
              <FieldError message={serverNote} />
            ) : (
              <p className="mt-1 text-xs text-muted-foreground">
                Optional. Recorded on the status change.
              </p>
            )}
          </div>
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={send.isPending}>
            Cancel
          </Button>
          <Button onClick={() => send.mutate()} loading={send.isPending} disabled={blocked}>
            <Send />
            Send order
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

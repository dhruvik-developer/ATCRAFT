/**
 * Retiring a customer account.
 *
 * `DELETE /customers/{id}` is a soft delete, and every part of that word
 * matters here. The row is kept, because the orders and invoices that quote it
 * still have to render; its addresses and contacts are retired in the same
 * transaction, because they have no life of their own; and the account then
 * vanishes from every list this application asks for, none of which send
 * `include_deleted`. Nothing on this screen brings it back - restoring is a
 * separate endpoint behind `customer.update` that no screen offers yet - so the
 * dialog says so rather than implying an undo that is not there.
 *
 * Two properties of the endpoint shape what follows.
 *
 * 1. **The reason travels in the query string, not in a body.** A DELETE body
 *    is legal HTTP and several proxies drop it, so the API declares `reason` as
 *    a required query parameter of at least three characters. Sending it as a
 *    body produces a 422 at the moment of use, which is why the mutation below
 *    passes it through `params` exactly as the address and contact removals in
 *    `customer-relations.tsx` do.
 * 2. **An account that still owes money cannot be retired at all.** That
 *    refusal is pre-empted on the list, where `outstanding_amount` is already
 *    in hand - see `hasOutstandingBalance`. This dialog therefore only opens
 *    for an account the API will accept, and the inline alert exists for the
 *    race: a payment or an invoice posted between the page load and the click.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { ConfirmDialog, FieldError } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle, Input, Label } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { messages, success } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { CustomerRow } from './customer-types'
import { dealerKeys } from './dealer-model'

export function RetireCustomerDialog({
  customer,
  open,
  onOpenChange,
}: {
  /** The row being retired; null while the dialog is closed. */
  customer: CustomerRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)
  const tooShort = reason.trim().length < REASON_MIN_LENGTH

  const retire = useMutation({
    // `reason` is a query parameter, not a body. See the note above - a DELETE
    // body would be dropped by a proxy and the API declares it in the query
    // string for exactly that reason.
    mutationFn: ({ target, reason: why }: { target: CustomerRow; reason: string }) =>
      api.delete(`/customers/${target.id}`, { params: { reason: why } }),
    onSuccess: (_result, { target }) => {
      // One prefix invalidation covers every customer list and detail at once.
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      // The dealer screen reads the same rows through `/customers/dealers` under
      // its own key, so the customers prefix does not reach it. A retired dealer
      // left in that cache is a row whose every action now answers 404.
      void queryClient.invalidateQueries({ queryKey: dealerKeys.all })
      toast.success(success.retired('Customer'), `${target.code} - ${target.name}`)
      onOpenChange(false)
    },
  })

  // Reset on close rather than on open: the failure message and what was typed
  // stay put while the dialog is up, so a refused attempt can be read and
  // corrected instead of vanishing under a toast.
  const { reset: clearResult } = retire
  useEffect(() => {
    if (!open) {
      setReason('')
      setTouched(false)
      clearResult()
    }
  }, [open, clearResult])

  if (!customer) return null

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Retire ${customer.code}?`}
      description={
        <>
          <strong className="font-medium text-foreground">{customer.name}</strong> stops appearing
          on every customer list and picker, and its addresses and contacts are retired with it.
          Orders and invoices already issued keep their reference to it - nothing is erased.
        </>
      }
      confirmLabel="Retire customer"
      destructive
      loading={retire.isPending}
      onConfirm={() => {
        if (tooShort) {
          setTouched(true)
          return
        }
        retire.mutate({ target: customer, reason: reason.trim() })
      }}
    >
      <div className="space-y-3">
        {retire.error && (
          <Alert variant="destructive">
            <AlertTitle>The account was not retired</AlertTitle>
            {/* The API's own sentence. It names the balance or the state that
                stood in the way, which is more use than "request failed". */}
            <AlertDescription>{retire.error.message}</AlertDescription>
          </Alert>
        )}

        <div>
          <Label htmlFor="customer-retire-reason" required>
            Reason
          </Label>
          <Input
            id="customer-retire-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={REASON_MAX_LENGTH}
            autoComplete="off"
            className="mt-1.5"
            aria-invalid={touched && tooShort}
          />
          {touched && tooShort ? (
            <FieldError message={messages.errors.reasonTooShort} />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              Recorded on the audit trail against this account. Bringing it back is a separate,
              permissioned action that this screen does not offer.
            </p>
          )}
        </div>
      </div>
    </ConfirmDialog>
  )
}

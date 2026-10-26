import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

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
} from '@/components/ui/overlays'
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { InboundDocumentDialog } from '@/features/procurement/inbound-document-dialog'
import { InboundShipmentDialog } from '@/features/procurement/inbound-shipment-dialog'

import type { VendorOrder } from './vendor-model'

type ActionKind = 'accept' | 'decline' | 'delivery-date' | 'ready-for-dispatch'
const titles: Record<ActionKind, string> = {
  accept: 'Accept purchase order',
  decline: 'Decline purchase order',
  'delivery-date': 'Change delivery date',
  'ready-for-dispatch': 'Mark ready for dispatch',
}
const actionSchema = z
  .object({
    kind: z.enum(['accept', 'decline', 'delivery-date', 'ready-for-dispatch']),
    committed_delivery_date: z.string(),
    vendor_reference: z.string().trim().max(64),
    date: z.string(),
    reason: z.string().trim().max(REASON_MAX_LENGTH),
    packages: z
      .string()
      .refine(
        (v): boolean =>
          v === '' || (/^\d+$/.test(v) && Number.isSafeInteger(Number(v)) && Number(v) >= 1),
        'Enter a whole number of at least 1',
      ),
    note: z.string().trim().max(2000),
  })
  .superRefine((value, ctx) => {
    if (
      (value.kind === 'decline' || value.kind === 'delivery-date') &&
      value.reason.length < REASON_MIN_LENGTH
    )
      ctx.addIssue({
        code: 'custom',
        path: ['reason'],
        message: `Enter at least ${REASON_MIN_LENGTH} characters`,
      })
    if (value.kind === 'delivery-date' && !/^\d{4}-\d{2}-\d{2}$/.test(value.date))
      ctx.addIssue({
        code: 'custom',
        path: ['date'],
        message: 'Choose the committed delivery date',
      })
    if (value.kind === 'accept' && value.note.length > REASON_MAX_LENGTH)
      ctx.addIssue({
        code: 'custom',
        path: ['note'],
        message: `At most ${REASON_MAX_LENGTH} characters`,
      })
  })

export function VendorActionDialog({
  order,
  kind,
  onClose,
}: {
  order: VendorOrder
  kind: ActionKind
  onClose: () => void
}) {
  const client = useQueryClient()
  const form = useForm<z.infer<typeof actionSchema>>({
    resolver: zodResolver(actionSchema),
    defaultValues: {
      kind,
      committed_delivery_date: order.vendor_committed_date ?? '',
      vendor_reference: order.vendor_reference ?? '',
      date: order.vendor_committed_date ?? '',
      reason: '',
      packages: order.ready_packages?.toString() ?? '',
      note: '',
    },
  })
  const mutation = useMutation({
    mutationFn: (values: z.infer<typeof actionSchema>) => {
      const body =
        kind === 'accept'
          ? {
              committed_delivery_date: values.committed_delivery_date || null,
              vendor_reference: values.vendor_reference || null,
              note: values.note || null,
            }
          : kind === 'decline'
            ? { reason: values.reason }
            : kind === 'delivery-date'
              ? { date: values.date, reason: values.reason }
              : {
                  packages: values.packages ? Number(values.packages) : null,
                  note: values.note || null,
                }
      return api.post(`/vendor-portal/purchase-orders/${order.id}/${kind}`, body)
    },
    onError: (error) => applyFieldErrors(error, form.setError),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.vendorPortal.all })
      toast.success(
        kind === 'accept'
          ? `${order.code} accepted`
          : kind === 'decline'
            ? `${order.code} declined`
            : kind === 'delivery-date'
              ? `Committed delivery date of ${order.code} updated`
              : `${order.code} marked ready for dispatch`,
      )
      onClose()
    },
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose()
      }}
    >
      <DialogContent className="max-h-[90svh] max-sm:bottom-0 max-sm:top-auto max-sm:translate-y-0 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{titles[kind]}</DialogTitle>
          <DialogDescription>
            {order.code}. Your committed date is recorded separately from the buyer’s planning date.
          </DialogDescription>
        </DialogHeader>
        <form
          className="[&_input]:min-h-11 [&_select]:min-h-11"
          onSubmit={form.handleSubmit((values) => mutation.mutate(values))}
        >
          <DialogBody className="space-y-4">
            {kind === 'accept' && (
              <>
                <div>
                  <Label htmlFor="vendor-committed">Committed delivery date</Label>
                  <Input
                    id="vendor-committed"
                    type="date"
                    min={order.po_date}
                    {...form.register('committed_delivery_date')}
                  />
                  <FieldError message={form.formState.errors.committed_delivery_date?.message} />
                </div>
                <div>
                  <Label htmlFor="vendor-reference">Your reference</Label>
                  <Input
                    id="vendor-reference"
                    maxLength={64}
                    {...form.register('vendor_reference')}
                  />
                  <FieldError message={form.formState.errors.vendor_reference?.message} />
                </div>
              </>
            )}
            {kind === 'delivery-date' && (
              <div>
                <Label htmlFor="vendor-date">New committed delivery date</Label>
                <Input
                  id="vendor-date"
                  type="date"
                  min={order.po_date}
                  {...form.register('date')}
                />
                <FieldError message={form.formState.errors.date?.message} />
              </div>
            )}
            {(kind === 'decline' || kind === 'delivery-date') && (
              <div>
                <Label htmlFor="vendor-reason">Reason</Label>
                <Textarea
                  id="vendor-reason"
                  maxLength={REASON_MAX_LENGTH}
                  {...form.register('reason')}
                />
                <FieldError message={form.formState.errors.reason?.message} />
              </div>
            )}
            {kind === 'ready-for-dispatch' && (
              <div>
                <Label htmlFor="vendor-packages">Packages</Label>
                <Input id="vendor-packages" inputMode="numeric" {...form.register('packages')} />
                <FieldError message={form.formState.errors.packages?.message} />
              </div>
            )}
            {(kind === 'accept' || kind === 'ready-for-dispatch') && (
              <div>
                <Label htmlFor="vendor-note">Note</Label>
                <Textarea
                  id="vendor-note"
                  maxLength={kind === 'accept' ? REASON_MAX_LENGTH : 2000}
                  {...form.register('note')}
                />
                <FieldError message={form.formState.errors.note?.message} />
              </div>
            )}
            <FieldError message={form.formState.errors.root?.message} />
          </DialogBody>
          <DialogFooter className="[&>button]:min-h-11">
            <Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Saving…' : titles[kind]}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function VendorShipmentDialog({
  order,
  onClose,
}: {
  order: VendorOrder
  onClose: () => void
}) {
  return <InboundShipmentDialog order={order} scope="vendor" onClose={onClose} />
}

export function VendorUploadDialog({
  order,
  canShip,
  canInvoice,
  onClose,
}: {
  order: VendorOrder
  canShip: boolean
  canInvoice: boolean
  onClose: () => void
}) {
  return (
    <InboundDocumentDialog
      order={order}
      scope="vendor"
      canShip={canShip}
      canInvoice={canInvoice}
      onClose={onClose}
    />
  )
}

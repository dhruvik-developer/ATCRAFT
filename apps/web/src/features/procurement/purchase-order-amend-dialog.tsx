import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'

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
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { ApiError, api } from '@/lib/api-client'
import { REASON_MAX_LENGTH } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { toast } from '@/lib/toast'

import {
  MAX_PURCHASE_ORDER_LINES,
  amendmentLineDrafts,
  buildPurchaseOrderAmendment,
  invalidatePurchaseOrderChanges,
  type AmendmentLineDraft,
  type PurchaseOrderAmendPayload,
} from './purchase-order-change-model'
import type { PurchaseOrderDetail } from './procurement-types'

export default function PurchaseOrderAmendDialog({
  order: openingOrder,
  onClose,
}: {
  order: PurchaseOrderDetail
  onClose: () => void
}) {
  // A background refetch must not pair old edited lines with a newer version.
  // Closing and reopening the editor is the explicit way to adopt new terms.
  const [order] = useState(openingOrder)
  const { can } = useAuth()
  const client = useQueryClient()
  const [lines, setLines] = useState(() => amendmentLineDrafts(order))
  const [reason, setReason] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const amend = useMutation({
    mutationFn: (payload: PurchaseOrderAmendPayload) =>
      api.post<PurchaseOrderDetail>(`/procurement/purchase-orders/${order.id}/amend`, payload),
    onSuccess: (saved) => {
      invalidatePurchaseOrderChanges(client)
      toast.success(
        success.updated('Purchase order'),
        `${saved.code} revision ${saved.amendment_number} is awaiting approval.`,
      )
      onClose()
    },
    onError: (error) => {
      if (error instanceof ApiError)
        setErrors(
          Object.fromEntries(
            Object.entries(error.fieldErrors).map(([path, messages]) => [path, messages.join(' ')]),
          ),
        )
    },
  })

  function changeLine(index: number, change: Partial<AmendmentLineDraft>) {
    setLines((current) =>
      current.map((line, row) => (row === index ? { ...line, ...change } : line)),
    )
    setErrors({})
    amend.reset()
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !amend.isPending) onClose()
      }}
    >
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Amend {order.code}</DialogTitle>
          <DialogDescription>
            Save revision {(order.amendment_number ?? 0) + 1} for fresh approval. The vendor must
            accept the revised order after it is sent again.
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (amend.isPending || !can(P.PURCHASE_ORDER_UPDATE)) return
            const result = buildPurchaseOrderAmendment(reason, lines, order.version)
            setErrors(result.errors)
            if (result.valid) amend.mutate(result.payload)
          }}
        >
          <DialogBody>
            <fieldset disabled={amend.isPending} className="min-w-0 space-y-5">
              <Alert variant="warning">
                <AlertTitle>This revision needs approval and vendor acceptance</AlertTitle>
                <AlertDescription>
                  The previous acknowledgement and committed delivery date are cleared. Planning
                  stops counting this order as incoming supply until it is approved again.
                </AlertDescription>
              </Alert>
              <div className="space-y-2">
                <Label htmlFor="po-amend-reason" required>
                  Reason for amendment
                </Label>
                <Textarea
                  id="po-amend-reason"
                  value={reason}
                  maxLength={REASON_MAX_LENGTH}
                  onChange={(event) => {
                    setReason(event.target.value)
                    setErrors((current) => ({ ...current, reason: '' }))
                    amend.reset()
                  }}
                  aria-invalid={Boolean(errors.reason)}
                />
                <FieldError message={errors.reason} />
              </div>
              <div className="space-y-4">
                {lines.map((line, index) => (
                  <section
                    key={line.key}
                    className="min-w-0 space-y-4 rounded-lg border p-4"
                    aria-label={`Order line ${index + 1}`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h3 className="min-w-0 break-words font-medium">
                        {line.lineId ? line.label : `New line ${index + 1}`}
                      </h3>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={`Remove order line ${index + 1}`}
                        onClick={() => {
                          setLines((current) => current.filter((_, row) => row !== index))
                          setErrors({})
                          amend.reset()
                        }}
                      >
                        <Trash2 aria-hidden /> Remove
                      </Button>
                    </div>
                    {!line.lineId && (
                      <div className="space-y-2">
                        <ItemPicker
                          id={`po-amend-item-${line.key}`}
                          label={`Item for line ${index + 1}`}
                          value={line.itemId || null}
                          filters={{ can_purchase: true }}
                          disabled={!can(P.ITEM_VIEW)}
                          onChange={(itemId, item) =>
                            changeLine(index, { itemId: itemId ?? '', label: item?.name ?? '' })
                          }
                          invalid={Boolean(errors[`lines.${index}.item_id`])}
                        />
                        <FieldError message={errors[`lines.${index}.item_id`]} />
                      </div>
                    )}
                    <div className="grid gap-4 sm:grid-cols-2">
                      <div className="space-y-2">
                        <Label htmlFor={`po-amend-quantity-${line.key}`} required>
                          Quantity for line {index + 1}
                        </Label>
                        <Input
                          id={`po-amend-quantity-${line.key}`}
                          inputMode="decimal"
                          value={line.quantity}
                          onChange={(event) => changeLine(index, { quantity: event.target.value })}
                          aria-invalid={Boolean(errors[`lines.${index}.quantity`])}
                        />
                        <FieldError message={errors[`lines.${index}.quantity`]} />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor={`po-amend-price-${line.key}`} required>
                          Unit price for line {index + 1}
                        </Label>
                        <Input
                          id={`po-amend-price-${line.key}`}
                          inputMode="decimal"
                          value={line.unitPrice}
                          onChange={(event) => changeLine(index, { unitPrice: event.target.value })}
                          aria-invalid={Boolean(errors[`lines.${index}.unit_price`])}
                        />
                        <FieldError message={errors[`lines.${index}.unit_price`]} />
                      </div>
                    </div>
                    {line.allocations.length > 0 && (
                      <div className="space-y-3 border-t pt-4">
                        <p className="text-sm text-muted-foreground">
                          Choose how much remains ordered for each requisition. Zero returns that
                          allocation for purchasing. Removing the whole line returns all of its
                          allocations.
                        </p>
                        <div className="grid gap-3 sm:grid-cols-2">
                          {line.allocations.map((allocation, allocationIndex) => (
                            <div key={allocation.requisitionLineId} className="min-w-0 space-y-2">
                              <Label htmlFor={`po-amend-allocation-${line.key}-${allocationIndex}`}>
                                {allocation.label}
                              </Label>
                              <Input
                                id={`po-amend-allocation-${line.key}-${allocationIndex}`}
                                inputMode="decimal"
                                value={allocation.quantity}
                                onChange={(event) =>
                                  changeLine(index, {
                                    allocations: line.allocations.map((current, row) =>
                                      row === allocationIndex
                                        ? { ...current, quantity: event.target.value }
                                        : current,
                                    ),
                                  })
                                }
                                aria-invalid={Boolean(
                                  errors[
                                    `lines.${index}.source_allocations.${allocationIndex}.quantity`
                                  ],
                                )}
                              />
                              <FieldError
                                message={
                                  errors[
                                    `lines.${index}.source_allocations.${allocationIndex}.quantity`
                                  ]
                                }
                              />
                            </div>
                          ))}
                        </div>
                        <FieldError message={errors[`lines.${index}.source_allocations`]} />
                      </div>
                    )}
                  </section>
                ))}
              </div>
              <FieldError message={errors.lines} />
              <Button
                type="button"
                variant="outline"
                disabled={lines.length >= MAX_PURCHASE_ORDER_LINES || !can(P.ITEM_VIEW)}
                onClick={() => {
                  setLines((current) => [
                    ...current,
                    {
                      key: crypto.randomUUID(),
                      lineId: null,
                      itemId: '',
                      label: '',
                      quantity: '',
                      unitPrice: '',
                      allocations: [],
                    },
                  ])
                  setErrors({})
                  amend.reset()
                }}
              >
                <Plus aria-hidden /> Add order line
              </Button>
              {!can(P.ITEM_VIEW) && (
                <p className="text-sm text-muted-foreground">
                  Item viewing permission is needed to choose new items. Existing lines can still be
                  amended.
                </p>
              )}
              {amend.error && (
                <FieldError
                  message={fieldErrorSummary(amend.error) || describeError(amend.error)}
                />
              )}
              {amend.error instanceof ApiError && amend.error.status === 409 && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    onClose()
                    invalidatePurchaseOrderChanges(client)
                  }}
                >
                  Close and refresh order
                </Button>
              )}
            </fieldset>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" disabled={amend.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={amend.isPending}
              disabled={!can(P.PURCHASE_ORDER_UPDATE)}
            >
              Save amendment
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

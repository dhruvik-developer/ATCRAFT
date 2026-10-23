import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { format } from 'date-fns'
import { Plus, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'

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
import { Alert, AlertDescription, Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { ItemPicker } from '@/features/inventory/pickers'
import { WarehouseSelect } from '@/features/inventory/pickers'
import type { ItemDetail } from '@/features/items/item-model'
import type { SalesOrderDetail } from '@/features/sales/sales-types'
import { api } from '@/lib/api-client'
import { formatQuantity, toDecimal } from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { packingKeys, type PackingOrderDetail } from './packing-types'

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/

type DraftLine = {
  key: number
  item_id: string
  quantity_to_pack: string
  uom_id: string
  sales_order_line_id?: string
  maximum_quantity?: string
}

function emptyLine(key: number): DraftLine {
  return { key, item_id: '', quantity_to_pack: '', uom_id: '' }
}

function outstandingOrderLines(order: SalesOrderDetail, nextKey: () => number): DraftLine[] {
  return order.lines.flatMap((line) => {
    const remaining = toDecimal(line.quantity).minus(line.dispatched_quantity)
    if (line.status === 'CANCELLED' || !remaining.greaterThan(0)) return []
    const quantity = remaining.toFixed()
    return [
      {
        key: nextKey(),
        item_id: line.item_id,
        quantity_to_pack: quantity,
        uom_id: line.uom_id,
        sales_order_line_id: line.id,
        maximum_quantity: quantity,
      },
    ]
  })
}

export function PackingOrderCreateDialog({
  open,
  onOpenChange,
  salesOrderId = null,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  salesOrderId?: string | null
  onCreated: (order: PackingOrderDetail) => void
}) {
  const queryClient = useQueryClient()
  const { can } = useAuth()
  const nextKey = useRef(1)
  const initializedSalesOrder = useRef<string | null>(null)
  const [customerId, setCustomerId] = useState('')
  const [warehouseId, setWarehouseId] = useState('')
  const [plannedDate, setPlannedDate] = useState('')
  const [notes, setNotes] = useState('')
  const [lines, setLines] = useState<DraftLine[]>([])
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  const salesOrderQuery = useQuery({
    queryKey: queryKeys.sales.detail(salesOrderId ?? ''),
    queryFn: () => api.get<SalesOrderDetail>(`/sales/${salesOrderId}`),
    enabled: open && Boolean(salesOrderId),
    staleTime: staleTimes.transactional,
  })
  const salesOrder = salesOrderQuery.data?.id === salesOrderId ? salesOrderQuery.data : undefined

  const itemFacts = useQueries({
    queries: lines.map((line) => ({
      queryKey: queryKeys.items.detail(line.item_id),
      queryFn: () => api.get<ItemDetail>(`/products/items/${line.item_id}`),
      enabled: open && !salesOrderId && Boolean(line.item_id),
      staleTime: staleTimes.master,
    })),
  })

  useEffect(() => {
    if (!open) {
      initializedSalesOrder.current = null
      return
    }
    setCustomerId('')
    setWarehouseId('')
    setPlannedDate(format(new Date(), 'yyyy-MM-dd'))
    setNotes('')
    setLines(salesOrderId ? [] : [emptyLine(nextKey.current++)])
    setFieldErrors({})
  }, [open, salesOrderId])

  useEffect(() => {
    if (!open || !salesOrder || initializedSalesOrder.current === salesOrder.id) return
    initializedSalesOrder.current = salesOrder.id
    setCustomerId(salesOrder.customer_id)
    setWarehouseId(salesOrder.warehouse_id)
    setLines(outstandingOrderLines(salesOrder, () => nextKey.current++))
  }, [open, salesOrder])

  useEffect(() => {
    if (salesOrderId) return
    setLines((current) => {
      let changed = false
      const next = current.map((line, index) => {
        const baseUomId = itemFacts[index]?.data?.base_uom_id
        if (line.item_id && !line.uom_id && baseUomId) {
          changed = true
          return { ...line, uom_id: baseUomId }
        }
        return line
      })
      return changed ? next : current
    })
  }, [itemFacts, lines, salesOrderId])

  const create = useMutation({
    mutationFn: (payload: SchemaOf<'PackingOrderCreate'>) =>
      api.post<PackingOrderDetail>('/packing', payload),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: packingKeys.all })
      toast.success(success.created('Packing order'), created.code)
      onCreated(created)
      onOpenChange(false)
    },
  })

  const updateLine = (key: number, patch: Partial<DraftLine>) => {
    setLines((current) => current.map((line) => (line.key === key ? { ...line, ...patch } : line)))
    setFieldErrors((current) => {
      const next = { ...current }
      delete next[`line-${key}`]
      return next
    })
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const errors: Record<string, string> = {}
    if (!customerId) errors.customer = 'Choose a customer.'
    if (!warehouseId) errors.warehouse = 'Choose a warehouse.'
    if (!/^\d{4}-\d{2}-\d{2}$/.test(plannedDate)) errors.planned_date = 'Choose a planned date.'
    if (lines.length === 0) errors.lines = 'Add at least one line with quantity to pack.'

    for (const line of lines) {
      if (!line.item_id) {
        errors[`line-${line.key}`] = 'Choose an item.'
      } else if (!line.uom_id) {
        const detailError = itemFacts[lines.indexOf(line)]?.error
        errors[`line-${line.key}`] =
          detailError?.message ?? 'The item unit is still loading; retry after it appears.'
      } else if (
        !QUANTITY.test(line.quantity_to_pack) ||
        !toDecimal(line.quantity_to_pack).greaterThan(0)
      ) {
        errors[`line-${line.key}`] =
          'Enter a quantity greater than zero with up to 4 decimal places.'
      } else if (
        line.maximum_quantity &&
        toDecimal(line.quantity_to_pack).greaterThan(line.maximum_quantity)
      ) {
        errors[`line-${line.key}`] =
          `Cannot exceed ${formatQuantity(line.maximum_quantity)} still undelivered.`
      }
    }

    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return

    const payload: SchemaOf<'PackingOrderCreate'> = {
      customer_id: customerId,
      warehouse_id: warehouseId,
      planned_date: plannedDate,
      ...(salesOrderId ? { sales_order_id: salesOrderId } : {}),
      ...(notes.trim() ? { notes: notes.trim() } : {}),
      lines: lines.map((line) => ({
        item_id: line.item_id,
        quantity_to_pack: line.quantity_to_pack,
        uom_id: line.uom_id,
        ...(line.sales_order_line_id ? { sales_order_line_id: line.sales_order_line_id } : {}),
      })),
    }
    create.reset()
    create.mutate(payload)
  }

  const missingMasterViews = [
    !can(P.ITEM_VIEW) && 'item.view',
    !can(P.CUSTOMER_VIEW) && 'customer.view',
    !can(P.WAREHOUSE_VIEW) && 'warehouse.view',
  ].filter((permission): permission is string => Boolean(permission))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {salesOrderId ? 'Raise packing order from sales order' : 'Raise packing order'}
          </DialogTitle>
          <DialogDescription>
            {salesOrderId
              ? 'Create a draft packing order for the remaining undelivered sales-order lines. Review quantities before raising it.'
              : 'Create a draft packing order for a customer shipment, sample or replacement.'}
          </DialogDescription>
        </DialogHeader>

        {missingMasterViews.length > 0 ? (
          <Alert variant="destructive">
            <AlertDescription>
              Creating a packing order also needs {missingMasterViews.join(', ')} permission to
              choose and verify its customer, warehouse and items.
            </AlertDescription>
          </Alert>
        ) : (
          <form onSubmit={submit} className="flex min-h-0 flex-col gap-4" noValidate>
            <DialogBody className="space-y-4">
              {salesOrderId && salesOrderQuery.isLoading && (
                <p className="text-sm text-muted-foreground">Loading sales order...</p>
              )}
              {salesOrderQuery.error && (
                <Alert variant="destructive">
                  <AlertDescription>{describeError(salesOrderQuery.error)}</AlertDescription>
                </Alert>
              )}
              {salesOrder && (
                <Alert>
                  <AlertDescription>
                    {salesOrder.code}: only lines with an undelivered balance are included. Existing
                    open packing orders should be reviewed before raising another.
                  </AlertDescription>
                </Alert>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <CustomerPicker
                    id="packing-create-customer"
                    label="Customer"
                    value={customerId || null}
                    onChange={(value) => {
                      setCustomerId(value ?? '')
                      setFieldErrors((current) => ({ ...current, customer: '' }))
                    }}
                    placeholder="Choose a customer"
                    disabled={Boolean(salesOrderId) || create.isPending}
                  />
                  <FieldError message={fieldErrors.customer} />
                </div>
                <div>
                  <WarehouseSelect
                    id="packing-create-warehouse"
                    label="Warehouse"
                    value={warehouseId}
                    onChange={(value) => {
                      setWarehouseId(value)
                      setFieldErrors((current) => ({ ...current, warehouse: '' }))
                    }}
                    disabled={Boolean(salesOrderId) || create.isPending}
                  />
                  <FieldError message={fieldErrors.warehouse} />
                </div>
                <div>
                  <Label htmlFor="packing-create-planned-date">Planned date</Label>
                  <Input
                    id="packing-create-planned-date"
                    type="date"
                    value={plannedDate}
                    onChange={(event) => setPlannedDate(event.target.value)}
                    aria-invalid={Boolean(fieldErrors.planned_date)}
                    className="mt-1.5"
                    disabled={create.isPending}
                  />
                  <FieldError message={fieldErrors.planned_date} />
                </div>
              </div>

              <section aria-labelledby="packing-create-lines-heading" className="space-y-3">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <h3 id="packing-create-lines-heading" className="font-medium">
                      Items to pack
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      Item units come from the item master; sales-order lines keep their order unit.
                    </p>
                  </div>
                  {!salesOrderId && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setLines((current) => [...current, emptyLine(nextKey.current++)])
                      }
                      disabled={create.isPending}
                    >
                      <Plus />
                      Add item
                    </Button>
                  )}
                </div>

                {lines.length === 0 && !salesOrderQuery.isLoading && (
                  <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                    {salesOrderId
                      ? 'There are no undelivered sales-order lines to pack.'
                      : 'Add an item to this packing order.'}
                  </p>
                )}

                <div className="space-y-3">
                  {lines.map((line, index) => {
                    const itemError = itemFacts[index]?.error
                    return (
                      <div
                        key={line.key}
                        className="grid items-end gap-3 rounded-md border p-3 sm:grid-cols-[minmax(0,1fr)_minmax(9rem,0.45fr)_auto]"
                      >
                        {salesOrderId ? (
                          <div>
                            <ItemPicker
                              id={`packing-create-item-${line.key}`}
                              label={`Sales order line ${index + 1}`}
                              value={line.item_id}
                              onChange={() => undefined}
                              disabled
                            />
                            {line.sales_order_line_id && (
                              <p className="mt-1 text-xs text-muted-foreground">
                                Linked to sales order line {index + 1}
                              </p>
                            )}
                          </div>
                        ) : (
                          <div>
                            <ItemPicker
                              id={`packing-create-item-${line.key}`}
                              label={`Item ${index + 1}`}
                              value={line.item_id || null}
                              onChange={(itemId) =>
                                updateLine(line.key, {
                                  item_id: itemId ?? '',
                                  uom_id: '',
                                })
                              }
                              disabled={create.isPending}
                            />
                            {itemError && (
                              <FieldError
                                message={`Could not load item details: ${itemError.message}`}
                              />
                            )}
                          </div>
                        )}
                        <div>
                          <Label htmlFor={`packing-create-quantity-${line.key}`}>
                            Quantity to pack
                          </Label>
                          <Input
                            id={`packing-create-quantity-${line.key}`}
                            value={line.quantity_to_pack}
                            onChange={(event) =>
                              updateLine(line.key, { quantity_to_pack: event.target.value })
                            }
                            inputMode="decimal"
                            autoComplete="off"
                            max={line.maximum_quantity}
                            aria-invalid={Boolean(fieldErrors[`line-${line.key}`])}
                            className="mt-1.5 text-right tabular"
                            disabled={create.isPending}
                          />
                          <FieldError message={fieldErrors[`line-${line.key}`]} />
                          {!salesOrderId && line.item_id && (
                            <p className="mt-1 text-xs text-muted-foreground">
                              {line.uom_id
                                ? 'Base unit'
                                : itemError
                                  ? 'Item unit unavailable'
                                  : 'Loading item unit...'}
                            </p>
                          )}
                          {line.maximum_quantity && (
                            <p className="mt-1 text-xs text-muted-foreground">
                              Up to {formatQuantity(line.maximum_quantity)} undelivered.
                            </p>
                          )}
                        </div>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          aria-label={`Remove ${salesOrderId ? 'sales order line' : 'item'} ${index + 1}`}
                          onClick={() =>
                            setLines((current) => current.filter((row) => row.key !== line.key))
                          }
                          disabled={create.isPending}
                        >
                          <Trash2 />
                        </Button>
                      </div>
                    )
                  })}
                </div>
                <FieldError message={fieldErrors.lines} />
              </section>

              <div>
                <Label htmlFor="packing-create-notes">Notes</Label>
                <Textarea
                  id="packing-create-notes"
                  value={notes}
                  onChange={(event) => setNotes(event.target.value)}
                  rows={2}
                  maxLength={2000}
                  className="mt-1.5"
                  disabled={create.isPending}
                />
              </div>

              {create.error && (
                <Alert variant="destructive">
                  <AlertDescription>{describeError(create.error)}</AlertDescription>
                </Alert>
              )}
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={create.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                loading={create.isPending}
                disabled={
                  Boolean(salesOrderId) &&
                  (salesOrderQuery.isLoading || Boolean(salesOrderQuery.error) || !salesOrder)
                }
              >
                Raise packing order
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

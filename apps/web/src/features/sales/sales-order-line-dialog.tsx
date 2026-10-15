/**
 * Add or amend one line of a sales order.
 *
 * This dialog does not talk to the API. Sales order lines are not addressable -
 * there is no `/sales/{id}/lines` endpoint - so a line change is a change to the
 * order's whole line set, sent by whoever owns that set: the create dialog
 * before the order exists, the detail screen afterwards. The dialog therefore
 * collects one line's values and hands them back.
 *
 * The net value shown here is a preview computed with decimal.js so the figure
 * moves as the operator types. It is never sent: the API prices the line, works
 * out the tax split from the place of supply and returns the authoritative
 * amounts, which is what the screen then displays. Tax is not previewed at all,
 * because the rate comes from the item and the place of supply and guessing it
 * here would put a number on screen that the invoice then contradicts.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { Controller, useForm } from 'react-hook-form'
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { useUomOptions, type ItemVariant } from '@/features/items/item-model'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, SELECT_NONE } from '@/lib/constants'
import { formatMoney, toDateInput, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import type { SalesOrderLineIn } from './sales-types'

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const PRICE = /^$|^\d{1,12}(\.\d{1,6})?$/
const RATE = /^(\d{1,3}(\.\d{1,4})?)?$/

/** Radix Select forbids an empty option value, so "not stated" needs a sentinel. */
const ITEM_DEFAULT_UOM = '__item_default'

const schema = z.object({
  item_id: z.string().min(1, 'Choose an item'),
  variant_id: z.string(),
  description: z.string().trim().max(500, 'At most 500 characters'),
  quantity: z
    .string()
    .trim()
    .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => toDecimal(value).greaterThan(0), 'Quantity must be more than zero'),
  uom_id: z.string(),
  unit_price: z.string().trim().regex(PRICE, 'Enter a price with up to 6 decimal places'),
  discount_percentage: z
    .string()
    .trim()
    .regex(RATE, 'Enter a percentage')
    .refine((value) => toDecimal(value).lessThanOrEqualTo(100), 'A discount cannot exceed 100%'),
  required_date: z.string(),
  notes: z.string().trim().max(2000, 'At most 2000 characters'),
})

type LineFormValues = z.infer<typeof schema>

function emptyValues(): LineFormValues {
  return {
    item_id: '',
    variant_id: SELECT_NONE,
    description: '',
    quantity: '1',
    uom_id: ITEM_DEFAULT_UOM,
    unit_price: '',
    discount_percentage: '0',
    required_date: '',
    notes: '',
  }
}

function valuesFrom(line: SalesOrderLineIn): LineFormValues {
  return {
    item_id: line.item_id,
    variant_id: line.item_variant_id ?? SELECT_NONE,
    description: line.description ?? '',
    quantity: line.quantity,
    uom_id: line.uom_id ?? ITEM_DEFAULT_UOM,
    unit_price: line.unit_price ?? '',
    discount_percentage: line.discount_percentage ?? '0',
    required_date: toDateInput(line.required_date),
    notes: line.notes ?? '',
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

export function SalesOrderLineDialog({
  open,
  onOpenChange,
  line,
  lineNumber,
  currency,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present when amending a line, absent when adding one. */
  line?: SalesOrderLineIn | null
  /** Position this line will hold in the set. */
  lineNumber: number
  currency?: string
  /** Handed the line as the API's `SalesOrderLineIn` wants it. */
  onSubmit: (line: SalesOrderLineIn) => void
}) {
  const { can } = useAuth()
  const isEdit = Boolean(line)
  const uoms = useUomOptions(can(P.UOM_VIEW) && open)
  const canOverrideDiscount = can(P.SALES_ORDER_DISCOUNT_OVERRIDE)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setValue,
    watch,
    formState: { errors },
  } = useForm<LineFormValues>({ resolver: zodResolver(schema), defaultValues: emptyValues() })

  const editingKey = line ? `${line.line_number}:${line.item_id}` : null
  useEffect(() => {
    if (open) reset(line ? valuesFrom(line) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingKey, reset])

  const selectedItemId = watch('item_id')

  // The variants of the chosen item, for the picker below. Listing variants
  // needs `item.view`, which the order screen already required to be here.
  // Active and inactive alike: a kept line may carry a variant since
  // deactivated, and the picker must name what the line holds rather than
  // show "the item itself" for a line that has a variant. Ordering an
  // inactive variant is refused by the API with the variant's code.
  const variants = useQuery({
    queryKey: [...queryKeys.items.detail(selectedItemId), 'variant-options'],
    queryFn: () =>
      api.getPage<ItemVariant>(`/products/items/${selectedItemId}/variants`, {
        params: { limit: LOOKUP_LIST_LIMIT },
      }),
    enabled: open && Boolean(selectedItemId),
    staleTime: staleTimes.master,
  })
  const variantRows = variants.data?.data ?? []

  // A variant belongs to its item: picking another item drops the choice
  // rather than sending a variant the new item does not have. The ref records
  // the item the form was seeded with, so reopening the dialog on a kept line
  // does not clear the variant it already carries.
  const seededItemId = useRef<string | null>(null)
  useEffect(() => {
    if (open) seededItemId.current = line?.item_id ?? ''
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingKey])
  useEffect(() => {
    if (open && seededItemId.current !== null && selectedItemId !== seededItemId.current) {
      seededItemId.current = selectedItemId
      setValue('variant_id', SELECT_NONE)
    }
  }, [open, selectedItemId, setValue])

  const [quantity, unitPrice, discountPercentage] = watch([
    'quantity',
    'unit_price',
    'discount_percentage',
  ])

  // decimal.js throughout: 0.1 + 0.2 on a line total is a real invoice defect.
  const gross = toDecimal(quantity).times(toDecimal(unitPrice))
  const discount = gross.times(toDecimal(discountPercentage)).dividedBy(100)
  const net = gross.minus(discount)

  const submit = (values: LineFormValues) => {
    onSubmit({
      line_number: lineNumber,
      item_id: values.item_id,
      // The API prices the variant's delta onto the line; null is the item
      // itself, sold in its one form.
      item_variant_id: values.variant_id === SELECT_NONE ? null : values.variant_id,
      description: orNull(values.description),
      // Decimals stay strings all the way to the wire - never parsed to a double.
      quantity: values.quantity.trim(),
      uom_id: values.uom_id === ITEM_DEFAULT_UOM ? null : values.uom_id,
      unit_price: orNull(values.unit_price),
      discount_percentage: orNull(values.discount_percentage),
      required_date: orNull(values.required_date),
      notes: orNull(values.notes),
    })
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit line ${line?.line_number}` : 'Add a line'}</DialogTitle>
          <DialogDescription>
            The line is saved with the order. On a confirmed order the API adjusts the reserved
            quantity to match; on a draft it simply replaces the line set.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(submit)} className="flex min-h-0 flex-col gap-4" noValidate>
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <Controller
                  control={control}
                  name="item_id"
                  render={({ field }) => (
                    <ItemPicker
                      id="line-item"
                      label="Item"
                      value={field.value || null}
                      onChange={(itemId) => field.onChange(itemId ?? '')}
                      placeholder="Search the item master"
                      invalid={Boolean(errors.item_id)}
                      filters={{ can_sell: true }}
                    />
                  )}
                />
                <FieldError message={errors.item_id?.message} />
              </div>

              {/* Offered only when the chosen item actually has variants: most
                  items are stocked and sold in one form, and a picker that can
                  only ever say "the item itself" is noise on every line. */}
              {variantRows.length > 0 && (
                <div className="sm:col-span-2">
                  <Label htmlFor="line-variant">Variant</Label>
                  <Controller
                    control={control}
                    name="variant_id"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger id="line-variant" className="mt-1.5">
                          <SelectValue placeholder="The item itself" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={SELECT_NONE}>The item itself</SelectItem>
                          {variantRows.map((variant) => (
                            <SelectItem key={variant.id} value={variant.id}>
                              {variant.code} - {variant.name}
                              {!variant.is_active && ' (inactive)'}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError message={errors.variant_id?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Priced with the variant&apos;s difference against the item. A variant-specific
                    price row is already that variant&apos;s price; otherwise the difference is
                    added to the item-wide price.
                  </p>
                </div>
              )}

              <div className="sm:col-span-2">
                <Label htmlFor="line-description">Description</Label>
                <Input
                  id="line-description"
                  {...register('description')}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.description)}
                />
                <FieldError message={errors.description?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  What the customer called it. Left empty, the item&apos;s own name is printed.
                </p>
              </div>

              <div>
                <Label htmlFor="line-quantity" required>
                  Quantity
                </Label>
                <Input
                  id="line-quantity"
                  {...register('quantity')}
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.quantity)}
                />
                <FieldError message={errors.quantity?.message} />
              </div>

              <div>
                <Label htmlFor="line-uom">Unit</Label>
                <Controller
                  control={control}
                  name="uom_id"
                  render={({ field }) => (
                    <Select
                      value={field.value}
                      onValueChange={field.onChange}
                      disabled={!can(P.UOM_VIEW)}
                    >
                      <SelectTrigger id="line-uom" className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={ITEM_DEFAULT_UOM}>The item&apos;s sale unit</SelectItem>
                        {(uoms.data ?? []).map((uom) => (
                          <SelectItem key={uom.id} value={uom.id}>
                            {uom.code} - {uom.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="line-unit-price">Unit price</Label>
                <Input
                  id="line-unit-price"
                  {...register('unit_price')}
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.unit_price)}
                />
                <FieldError message={errors.unit_price?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Left empty, the API prices the line from the customer&apos;s price list. A price
                  below cost needs an approval before the order can be confirmed.
                </p>
              </div>

              <div>
                <Label htmlFor="line-discount">Discount (%)</Label>
                <Input
                  id="line-discount"
                  {...register('discount_percentage')}
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.discount_percentage)}
                />
                <FieldError message={errors.discount_percentage?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  {canOverrideDiscount
                    ? 'A discount above the company threshold is allowed for your account, but the order will wait for a pricing approval before it can be confirmed.'
                    : 'A discount above the company threshold will be refused when the order is saved - your account does not hold the discount-override right - and an allowed one still puts the order through a pricing approval.'}
                </p>
              </div>

              <div>
                <Label htmlFor="line-required-date">Required date</Label>
                <Input
                  id="line-required-date"
                  type="date"
                  {...register('required_date')}
                  className="mt-1.5"
                />
                <FieldError message={errors.required_date?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Left empty, the line follows the order&apos;s required date.
                </p>
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="line-notes">Notes</Label>
                <Textarea id="line-notes" {...register('notes')} className="mt-1.5" />
                <FieldError message={errors.notes?.message} />
              </div>
            </div>

            <div className="rounded-lg border bg-muted/40 p-4">
              <dl className="grid gap-2 text-sm sm:grid-cols-3">
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">Gross</dt>
                  <dd className="tabular">
                    {formatMoney(gross.toFixed(2), { currency, showSymbol: Boolean(currency) })}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                    Discount
                  </dt>
                  <dd className="tabular">
                    {formatMoney(discount.toFixed(2), { currency, showSymbol: Boolean(currency) })}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                    Net of discount
                  </dt>
                  <dd className="font-medium tabular">
                    {formatMoney(net.toFixed(2), { currency, showSymbol: Boolean(currency) })}
                  </dd>
                </div>
              </dl>
              <p className="mt-2 text-xs text-muted-foreground">
                A preview before tax. The API prices the line and splits the tax into CGST + SGST or
                IGST from the place of supply.
                {!currency && ' Amounts use the company currency when the order is saved.'}
              </p>
            </div>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit">{isEdit ? 'Save line' : 'Add line'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The dated price list for one item.
 *
 * A price row is a channel (`SALE` / `DEALER` / `DISTRIBUTOR`), an optional
 * variant, a quantity slab and a validity window. The server refuses two active
 * rows that overlap on all four at once, which is what makes "the price of this
 * item today" a single unambiguous answer rather than a guess.
 *
 * Three contract facts shape these dialogs:
 *
 * 1. **`PriceUpdate` carries no `price_type`, `currency` or `variant_id`.**
 *    Those three are what a row *is*; changing them would silently re-point a
 *    price at a different channel or a different variant, and quotations
 *    already raised on it would no longer be explicable. They are shown as read
 *    only on an edit, with the reason.
 * 2. **A PATCH ignores nulls.** `update_price` applies only non-null fields, so
 *    an end date that has been set cannot be removed through this endpoint.
 *    Rather than let an emptied field look like it worked, the form keeps it
 *    required once it has a value and says why.
 * 3. **Money is a decimal string end to end.** `price` and `min_quantity` are
 *    typed as text, validated against the contract's own patterns and posted
 *    verbatim. Nothing here parses either into a number.
 *
 * Every write is gated on `price.manage`; the list behind them needs
 * `price.view`, and naming the variants needs `item.view`.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  Checkbox,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { formatMoney, humanise } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  applyFieldErrors,
  mastersKeys,
  PRICE_PATTERN,
  PRICE_TYPES,
  QUANTITY_PATTERN,
  type PriceRow,
  type VariantRow,
} from './masters-model'

/** Radix Select forbids an empty option value, so "every variant" needs a sentinel. */
const ALL_VARIANTS = '__all_variants'

const priceSchema = z
  .object({
    price_type: z.enum(PRICE_TYPES),
    variant_id: z.string(),
    currency: z
      .string()
      .trim()
      .regex(/^[A-Za-z]{3}$/, 'A three-letter ISO currency code, such as INR'),
    price: z
      .string()
      .trim()
      .min(1, 'A price is required')
      .regex(PRICE_PATTERN, 'Up to 12 digits and 6 decimal places, no sign'),
    min_quantity: z
      .string()
      .trim()
      .regex(QUANTITY_PATTERN, 'Up to 14 digits and 4 decimal places, no sign'),
    valid_from: z.string().min(1, 'A start date is required'),
    valid_to: z.string(),
    is_active: z.boolean(),
    notes: z.string(),
  })
  .refine((values) => values.valid_to === '' || values.valid_to >= values.valid_from, {
    path: ['valid_to'],
    message: 'The end date cannot be before the start date',
  })

type PriceFormValues = z.infer<typeof priceSchema>

const PRICE_FIELDS = new Set([
  'price_type',
  'variant_id',
  'currency',
  'price',
  'min_quantity',
  'valid_from',
  'valid_to',
  'is_active',
  'notes',
])

/** Today in the yyyy-MM-dd shape `<input type="date">` and the API both use. */
function today(): string {
  const now = new Date()
  const month = `${now.getMonth() + 1}`.padStart(2, '0')
  const day = `${now.getDate()}`.padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

function emptyValues(): PriceFormValues {
  return {
    price_type: 'SALE',
    variant_id: ALL_VARIANTS,
    // `PriceCreate.currency` defaults to INR server-side; stating it here keeps
    // the field from looking optional when it is stored on every row.
    currency: 'INR',
    price: '',
    min_quantity: '0',
    valid_from: today(),
    valid_to: '',
    is_active: true,
    notes: '',
  }
}

function valuesFrom(price: PriceRow): PriceFormValues {
  return {
    price_type: price.price_type,
    variant_id: price.variant_id ?? ALL_VARIANTS,
    currency: price.currency,
    price: price.price,
    min_quantity: price.min_quantity,
    valid_from: price.valid_from,
    valid_to: price.valid_to ?? '',
    is_active: price.is_active,
    notes: price.notes ?? '',
  }
}

export function PriceFormDialog({
  open,
  onOpenChange,
  itemId,
  price,
  variants,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The item the price list belongs to; the create endpoint is under it. */
  itemId: string
  /** Present for an edit, absent for a create. */
  price?: PriceRow | null
  /** Variants of this item, empty when the account cannot read them. */
  variants: readonly VariantRow[]
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(price)
  /** Once an end date exists it cannot be removed - see the module note. */
  const endDateIsSticky = Boolean(price?.valid_to)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<PriceFormValues>({
    resolver: zodResolver(priceSchema),
    defaultValues: emptyValues(),
  })

  const editingId = price?.id ?? null
  useEffect(() => {
    if (open) reset(price ? valuesFrom(price) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: PriceFormValues) => {
      if (price) {
        return api.patch<PriceRow>(`/products/prices/${price.id}`, {
          price: values.price.trim(),
          min_quantity: values.min_quantity.trim() === '' ? '0' : values.min_quantity.trim(),
          valid_from: values.valid_from,
          // Omitted rather than nulled when empty: a null would be dropped by
          // the service anyway, and sending one would suggest it had cleared.
          ...(values.valid_to ? { valid_to: values.valid_to } : {}),
          is_active: values.is_active,
          // An empty string, not null - `""` is the only way to clear a note.
          notes: values.notes.trim(),
        })
      }

      return api.post<PriceRow>(`/products/items/${itemId}/prices`, {
        price_type: values.price_type,
        variant_id: values.variant_id === ALL_VARIANTS ? null : values.variant_id,
        currency: values.currency.trim().toUpperCase(),
        price: values.price.trim(),
        min_quantity: values.min_quantity.trim() === '' ? '0' : values.min_quantity.trim(),
        valid_from: values.valid_from,
        valid_to: values.valid_to || null,
        notes: values.notes.trim() === '' ? null : values.notes.trim(),
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      // The item detail screen keeps its own Prices tab under the item's key,
      // so a price added here must not leave that tab showing the old list.
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.detail(itemId) })
      toast.success(
        isEdit ? 'Price updated' : 'Price added',
        `${humanise(saved.price_type)} - ${formatMoney(saved.price, { currency: saved.currency })}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError, PRICE_FIELDS),
  })

  const submit = handleSubmit((values) => {
    if (endDateIsSticky && values.valid_to === '') {
      setError('valid_to', {
        type: 'manual',
        message:
          'This endpoint cannot reopen a closed price. Leave the end date, or add a new row for the period that follows it.',
      })
      return
    }
    mutation.mutate(values)
  })

  const currency = watch('currency')
  const amount = watch('price')

  const variantLabel = (variantId: string | null): string => {
    if (variantId === null) return 'Every variant of this item'
    const match = variants.find((variant) => variant.id === variantId)
    return match ? `${match.code} - ${match.name}` : 'One variant'
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit price' : 'New price'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The channel, currency and variant define which price this is and cannot be changed. Close this row and add another to move a price between channels.'
              : 'Two active prices may not overlap on channel, variant, quantity slab and dates at once.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex min-h-0 flex-col gap-4" noValidate>
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="price-type" required>
                  Channel
                </Label>
                <Controller
                  control={control}
                  name="price_type"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange} disabled={isEdit}>
                      <SelectTrigger id="price-type" className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {PRICE_TYPES.map((entry) => (
                          <SelectItem key={entry} value={entry}>
                            {humanise(entry)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.price_type?.message} />
              </div>

              <div>
                <Label htmlFor="price-variant">Applies to</Label>
                {isEdit ? (
                  <p
                    id="price-variant"
                    className="mt-1.5 flex h-9 items-center text-sm text-muted-foreground"
                  >
                    {variantLabel(price?.variant_id ?? null)}
                  </p>
                ) : (
                  <Controller
                    control={control}
                    name="variant_id"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger id="price-variant" className="mt-1.5">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={ALL_VARIANTS}>Every variant of this item</SelectItem>
                          {variants.map((variant) => (
                            <SelectItem key={variant.id} value={variant.id}>
                              {variant.code} - {variant.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                )}
                <FieldError message={errors.variant_id?.message} />
              </div>

              <div>
                <Label htmlFor="price-currency" required>
                  Currency
                </Label>
                <Input
                  id="price-currency"
                  {...register('currency')}
                  disabled={isEdit}
                  maxLength={3}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.currency)}
                  className="mt-1.5 uppercase"
                />
                <FieldError message={errors.currency?.message} />
              </div>

              <div>
                <Label htmlFor="price-amount" required>
                  Price
                </Label>
                <Input
                  id="price-amount"
                  {...register('price')}
                  inputMode="decimal"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.price)}
                  className="mt-1.5 tabular"
                />
                <FieldError message={errors.price?.message} />
                {!errors.price && amount.trim() !== '' && PRICE_PATTERN.test(amount.trim()) && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {formatMoney(amount.trim(), {
                      currency: /^[A-Za-z]{3}$/.test(currency.trim())
                        ? currency.trim().toUpperCase()
                        : undefined,
                    })}
                  </p>
                )}
              </div>

              <div>
                <Label htmlFor="price-min-quantity">Applies from quantity</Label>
                <Input
                  id="price-min-quantity"
                  {...register('min_quantity')}
                  inputMode="decimal"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.min_quantity)}
                  className="mt-1.5 tabular"
                />
                <FieldError message={errors.min_quantity?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  The bottom of the quantity slab. Nought means this price applies at any quantity.
                </p>
              </div>

              <div>
                <Label htmlFor="price-valid-from" required>
                  Valid from
                </Label>
                <Input
                  id="price-valid-from"
                  type="date"
                  {...register('valid_from')}
                  aria-invalid={Boolean(errors.valid_from)}
                  className="mt-1.5"
                />
                <FieldError message={errors.valid_from?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="price-valid-to" required={endDateIsSticky}>
                  Valid to
                </Label>
                <Input
                  id="price-valid-to"
                  type="date"
                  {...register('valid_to')}
                  aria-invalid={Boolean(errors.valid_to)}
                  aria-describedby="price-valid-to-help"
                  className="mt-1.5"
                />
                <FieldError message={errors.valid_to?.message} />
                <p id="price-valid-to-help" className="mt-1 text-xs text-muted-foreground">
                  {endDateIsSticky
                    ? 'An end date, once set, cannot be removed through this endpoint. Add a new row for the period that follows instead.'
                    : 'Leave empty for a price that runs until it is superseded.'}
                </p>
              </div>
            </div>

            <div>
              <Label htmlFor="price-notes">Notes</Label>
              <Textarea id="price-notes" {...register('notes')} rows={2} className="mt-1.5" />
              <FieldError message={errors.notes?.message} />
            </div>

            {isEdit && (
              <div className="flex items-start gap-2.5">
                <Controller
                  control={control}
                  name="is_active"
                  render={({ field }) => (
                    <Checkbox
                      id="price-active"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <div>
                  <Label htmlFor="price-active" className="cursor-pointer font-normal">
                    Active
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    An inactive row is ignored when a price is resolved, and stops blocking another
                    row that would otherwise overlap it.
                  </p>
                </div>
              </div>
            )}

            {!isEdit && (
              <Alert>
                <AlertDescription>
                  If an active price already covers part of this window for the same channel,
                  variant and slab, the save is refused and names the row it clashes with. Close
                  that one first.
                </AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Add price'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Remove a price row.
 *
 * A soft delete server-side, but from the operator's point of view the row is
 * gone from the list and from price resolution. Deactivating it instead keeps
 * it visible and auditable, so that alternative is offered in the wording
 * rather than assumed.
 */
export function PriceDeleteDialog({
  price,
  itemId,
  onOpenChange,
}: {
  price: PriceRow | null
  itemId: string
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: (target: PriceRow) => api.delete(`/products/prices/${target.id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.detail(itemId) })
      toast.success('Price removed')
      onOpenChange(false)
    },
  })

  return (
    <ConfirmDialog
      open={Boolean(price)}
      onOpenChange={onOpenChange}
      title="Remove this price?"
      description={
        price
          ? `${humanise(price.price_type)} at ${formatMoney(price.price, { currency: price.currency })} from ${price.valid_from}. It disappears from the list and from price resolution. To keep it on the record, edit it and turn Active off instead.`
          : undefined
      }
      confirmLabel="Remove price"
      destructive
      loading={mutation.isPending}
      onConfirm={() => price && mutation.mutate(price)}
    />
  )
}

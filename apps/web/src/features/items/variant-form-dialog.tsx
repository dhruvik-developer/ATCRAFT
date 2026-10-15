/**
 * Create / edit dialog for an item variant.
 *
 * A variant is one finish/size/colour of the *same* item: it shares the
 * parent's bill of materials, planning figures and all nine capability flags,
 * and differs only in identity, in the two deltas and in its barcode
 * (docs/assumptions.md §E10-1). Anything that needs its own bill of materials
 * is a new item, not a variant.
 *
 * The API splits the rights: `POST /products/items/{id}/variants` needs
 * `item.create` and `PATCH /products/variants/{id}` needs `item.update`, so
 * the parent offers Create and Edit on exactly those rights and this dialog
 * assumes it was opened by someone holding them.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
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
import { Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { mastersKeys } from '@/features/masters/masters-model'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'
import { useItemAttributeOptions } from '@/hooks/use-item-attribute-options'

import type { ItemVariant } from './item-model'

/** Deltas are signed: a variant is routinely cheaper than its parent. */
const SIGNED_6 = /^(-?\d{1,14}(\.\d{1,6})?)?$/
const SIGNED_4 = /^(-?\d{1,14}(\.\d{1,4})?)?$/

const schema = z.object({
  code: z.string().trim().min(1, 'Code is required').max(64, 'Code is at most 64 characters'),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  finish_id: z.string(),
  size_id: z.string(),
  colour_id: z.string(),
  price_delta: z.string().trim().regex(SIGNED_6, 'Enter a number with up to 6 decimal places'),
  cost_delta: z.string().trim().regex(SIGNED_4, 'Enter a number with up to 4 decimal places'),
  barcode: z.string().trim().max(64, 'Barcode is at most 64 characters'),
  is_active: z.boolean(),
})

type VariantFormValues = z.infer<typeof schema>

function emptyValues(): VariantFormValues {
  return {
    code: '',
    name: '',
    finish_id: SELECT_NONE,
    size_id: SELECT_NONE,
    colour_id: SELECT_NONE,
    price_delta: '0',
    cost_delta: '0',
    barcode: '',
    is_active: true,
  }
}

function valuesFrom(variant: ItemVariant): VariantFormValues {
  return {
    code: variant.code,
    name: variant.name,
    finish_id: variant.finish_id ?? SELECT_NONE,
    size_id: variant.size_id ?? SELECT_NONE,
    colour_id: variant.colour_id ?? SELECT_NONE,
    price_delta: String(variant.price_delta),
    cost_delta: String(variant.cost_delta),
    barcode: variant.barcode ?? '',
    is_active: variant.is_active,
  }
}

/** Decimals stay strings all the way to the wire - never parsed into a double. */
const asDelta = (value: string): string => (value.trim() === '' ? '0' : value.trim())

function buildPayload(
  values: VariantFormValues,
  { includeCode, includeCost }: { includeCode: boolean; includeCost: boolean },
): Record<string, unknown> {
  return {
    ...(includeCode ? { code: values.code.trim() } : {}),
    name: values.name.trim(),
    // An explicit null clears the attribute; the update endpoint is the one
    // place in the products module where null means "clear it" rather than
    // "leave it alone".
    finish_id: asId(values.finish_id),
    size_id: asId(values.size_id),
    colour_id: asId(values.colour_id),
    price_delta: asDelta(values.price_delta),
    ...(includeCost ? { cost_delta: asDelta(values.cost_delta) } : {}),
    // A blank barcode posts null, never an empty string.
    barcode: asText(values.barcode),
    is_active: values.is_active,
  }
}

export function VariantFormDialog({
  open,
  onOpenChange,
  itemId,
  itemCode,
  variant,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  itemId: string
  itemCode: string
  /** Present for an edit, absent for a create. */
  variant?: ItemVariant | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(variant)

  // Affordance only - the API answers `VariantOut` with the cost delta
  // regardless, but writing it is a costing decision, so the field is offered
  // on the same right that shows the item's own standard cost.
  const showCost = can(P.ITEM_COST_VIEW)

  const mayReadAttributes = open && can(P.ITEM_ATTRIBUTE_VIEW)
  const finishes = useItemAttributeOptions('FINISH', mayReadAttributes)
  const sizes = useItemAttributeOptions('SIZE', mayReadAttributes)
  const colours = useItemAttributeOptions('COLOUR', mayReadAttributes)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    clearErrors,
    formState: { errors },
  } = useForm<VariantFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  // Seeded once per opening, keyed on the variant's id rather than its
  // identity: a background refetch of the list would otherwise resolve mid-edit
  // and wipe what the user is typing.
  const editingId = variant?.id ?? null
  useEffect(() => {
    if (open) reset(variant ? valuesFrom(variant) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: VariantFormValues) =>
      variant
        ? api.patch<ItemVariant>(
            `/products/variants/${variant.id}`,
            buildPayload(values, { includeCode: false, includeCost: showCost }),
          )
        : api.post<ItemVariant>(
            `/products/items/${itemId}/variants`,
            buildPayload(values, { includeCode: true, includeCost: showCost }),
          ),
    onSuccess: (saved) => {
      // One prefix invalidation covers the item read and every panel on it;
      // the price screen keeps its own variant key for its picker.
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.detail(itemId) })
      void queryClient.invalidateQueries({ queryKey: mastersKeys.variants(itemId) })
      toast.success(
        isEdit ? success.updated('Variant') : success.created('Variant'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const submit = (values: VariantFormValues) => {
    // The one rule the API enforces on creation: a variant with no
    // distinguishing attribute is a duplicate of its parent.
    clearErrors('finish_id')
    if (
      !isEdit &&
      values.finish_id === SELECT_NONE &&
      values.size_id === SELECT_NONE &&
      values.colour_id === SELECT_NONE
    ) {
      setError('finish_id', {
        type: 'manual',
        message: 'A variant must differ in at least one of finish, size or colour.',
      })
      return
    }
    mutation.mutate(values)
  }

  const attributeSelect = (
    id: string,
    label: string,
    name: 'finish_id' | 'size_id' | 'colour_id',
    options: ReadonlyArray<{ id: string; name: string }> | undefined,
  ) => (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Controller
        control={control}
        name={name}
        render={({ field }) => (
          <Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger id={id} className="mt-1.5">
              <SelectValue placeholder={`No ${label.toLowerCase()}`} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_NONE}>No {label.toLowerCase()}</SelectItem>
              {(options ?? []).map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      />
    </div>
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? `Edit variant ${variant?.code}` : `New variant of ${itemCode}`}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - it is printed beside the parent on price lists.'
              : 'A finish, size or colour of this same item. It shares the parent stock, bill of materials and planning figures.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(submit)} className="flex min-h-0 flex-col gap-4" noValidate>
          <DialogBody className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="variant-code" required>
                  Code
                </Label>
                <Input
                  id="variant-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="variant-name" required>
                  Name
                </Label>
                <Input
                  id="variant-name"
                  {...register('name')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>

              {attributeSelect('variant-finish', 'Finish', 'finish_id', finishes.data)}
              {attributeSelect('variant-size', 'Size', 'size_id', sizes.data)}
              {attributeSelect('variant-colour', 'Colour', 'colour_id', colours.data)}
              <FieldError message={errors.finish_id?.message} />

              <div>
                <Label htmlFor="variant-price-delta">Price delta</Label>
                <Input
                  id="variant-price-delta"
                  {...register('price_delta')}
                  inputMode="decimal"
                  aria-invalid={Boolean(errors.price_delta)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.price_delta?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Added to the item-wide price when no variant-specific price row exists. May be
                  negative.
                </p>
              </div>

              {showCost && (
                <div>
                  <Label htmlFor="variant-cost-delta">Cost delta</Label>
                  <Input
                    id="variant-cost-delta"
                    {...register('cost_delta')}
                    inputMode="decimal"
                    aria-invalid={Boolean(errors.cost_delta)}
                    className="mt-1.5 text-right tabular"
                  />
                  <FieldError message={errors.cost_delta?.message} />
                </div>
              )}

              <div>
                <Label htmlFor="variant-barcode">Barcode</Label>
                <Input
                  id="variant-barcode"
                  {...register('barcode')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.barcode)}
                  className="mt-1.5"
                />
                <FieldError message={errors.barcode?.message} />
              </div>

              <div className="flex items-center gap-2.5 pt-6">
                <Controller
                  control={control}
                  name="is_active"
                  render={({ field }) => (
                    <Checkbox
                      id="variant-is-active"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <Label htmlFor="variant-is-active" className="font-normal">
                  Active
                </Label>
              </div>
            </div>

            <FieldError message={errors.root?.message} />
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
              {isEdit ? 'Save changes' : 'Create variant'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

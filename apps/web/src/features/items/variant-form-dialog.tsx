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
import {
  attributeLabeller,
  useItemAttributeOptions,
  type ItemAttributeOption,
} from '@/hooks/use-item-attribute-options'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'

import type { ItemVariant } from './item-model'

const PRICE_DELTA = /^(-?\d{1,12}(\.\d{1,6})?)?$/
const COST_DELTA = /^(-?\d{1,14}(\.\d{1,4})?)?$/

const schema = z
  .object({
    code: z
      .string()
      .trim()
      .min(1, validation.required('Code'))
      .max(64, 'Code is at most 64 characters'),
    name: z
      .string()
      .trim()
      .min(1, validation.required('Name'))
      .max(255, 'Name is at most 255 characters'),
    finish_id: z.string(),
    size_id: z.string(),
    colour_id: z.string(),
    price_delta: z
      .string()
      .trim()
      .regex(PRICE_DELTA, 'Enter a signed number with up to 6 decimals'),
    cost_delta: z.string().trim().regex(COST_DELTA, 'Enter a signed number with up to 4 decimals'),
    barcode: z.string().trim().max(64, 'Barcode is at most 64 characters'),
    is_active: z.boolean(),
  })
  .refine(
    (values) =>
      values.finish_id !== SELECT_NONE ||
      values.size_id !== SELECT_NONE ||
      values.colour_id !== SELECT_NONE,
    {
      message: 'A variant must differ in at least one of finish, size or colour.',
      path: ['finish_id'],
    },
  )

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
    price_delta: variant.price_delta,
    cost_delta: variant.cost_delta ?? '0',
    barcode: variant.barcode ?? '',
    is_active: variant.is_active,
  }
}

/** Decimal deltas stay strings; an emptied optional delta returns to zero. */
const asDelta = (value: string): string => (value.trim() === '' ? '0' : value.trim())

function attributeOptions(
  options: ItemAttributeOption[] | undefined,
  selectedId: string,
  label: (id: string | null | undefined) => string,
): { id: string; name: string }[] {
  if (selectedId === SELECT_NONE || options?.some((option) => option.id === selectedId)) {
    return options ?? []
  }
  return [{ id: selectedId, name: label(selectedId) }, ...(options ?? [])]
}

export function VariantFormDialog({
  itemId,
  itemCode,
  variant,
  open,
  onOpenChange,
}: {
  itemId: string
  itemCode: string
  variant?: ItemVariant | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(variant)
  const canViewCost = can(P.ITEM_COST_VIEW)
  const canViewAttributes = can(P.ITEM_ATTRIBUTE_VIEW)
  const finishes = useItemAttributeOptions('FINISH', open && canViewAttributes)
  const sizes = useItemAttributeOptions('SIZE', open && canViewAttributes)
  const colours = useItemAttributeOptions('COLOUR', open && canViewAttributes)
  const finishLabel = attributeLabeller(finishes.data)
  const sizeLabel = attributeLabeller(sizes.data)
  const colourLabel = attributeLabeller(colours.data)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<VariantFormValues>({ resolver: zodResolver(schema), defaultValues: emptyValues() })

  const variantId = variant?.id ?? null
  useEffect(() => {
    if (open) reset(variant ? valuesFrom(variant) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, itemId, variantId, reset])

  const mutation = useMutation({
    mutationFn: (values: VariantFormValues) => {
      const body = {
        name: values.name.trim(),
        finish_id: asId(values.finish_id),
        size_id: asId(values.size_id),
        colour_id: asId(values.colour_id),
        price_delta: asDelta(values.price_delta),
        barcode: asText(values.barcode),
        ...(canViewCost ? { cost_delta: asDelta(values.cost_delta) } : {}),
        is_active: values.is_active,
      }

      if (variant) {
        return api.patch<ItemVariant>(`/products/variants/${variant.id}`, body)
      }
      return api.post<ItemVariant>(`/products/items/${itemId}/variants`, {
        code: values.code.trim(),
        ...body,
        is_active: values.is_active,
      })
    },
    onSuccess: (saved) => {
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

  const [finishId, sizeId, colourId] = watch(['finish_id', 'size_id', 'colour_id'])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? `Edit variant ${variant?.code}` : `New variant of ${itemCode}`}
          </DialogTitle>
          <DialogDescription>
            A variant shares its item&apos;s stock, BOM and planning settings. Use a separate item
            when the product needs its own BOM.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="variant-code" required>
                  Code
                </Label>
                <Input
                  id="variant-code"
                  disabled={isEdit}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.code)}
                  {...register('code')}
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="variant-name" required>
                  Name
                </Label>
                <Input
                  id="variant-name"
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.name)}
                  {...register('name')}
                />
                <FieldError message={errors.name?.message} />
              </div>

              {(
                [
                  ['finish_id', 'Finish', finishes.data, finishId, finishLabel],
                  ['size_id', 'Size', sizes.data, sizeId, sizeLabel],
                  ['colour_id', 'Colour', colours.data, colourId, colourLabel],
                ] as const
              ).map(([fieldName, label, options, selectedId, labelFor]) => (
                <div key={fieldName}>
                  <Label htmlFor={`variant-${fieldName}`}>{label}</Label>
                  <Controller
                    control={control}
                    name={fieldName}
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={field.onChange}
                        disabled={!canViewAttributes}
                      >
                        <SelectTrigger id={`variant-${fieldName}`} className="mt-1.5">
                          <SelectValue placeholder={`Choose ${label.toLowerCase()}`} />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={SELECT_NONE}>No {label.toLowerCase()}</SelectItem>
                          {attributeOptions(options, selectedId, labelFor).map((option) => (
                            <SelectItem key={option.id} value={option.id}>
                              {option.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError
                    message={fieldName === 'finish_id' ? errors.finish_id?.message : undefined}
                  />
                </div>
              ))}

              {!canViewAttributes && (
                <p className="sm:col-span-2 text-sm text-muted-foreground">
                  Finish, size and colour choices are unavailable because your role cannot view item
                  attributes.
                </p>
              )}

              <div>
                <Label htmlFor="variant-price-delta">Price delta</Label>
                <Input
                  id="variant-price-delta"
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.price_delta)}
                  {...register('price_delta')}
                />
                <FieldError message={errors.price_delta?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Added to the item-wide price when no variant-specific price row exists. May be
                  negative.
                </p>
              </div>

              {canViewCost && (
                <div>
                  <Label htmlFor="variant-cost-delta">Cost delta</Label>
                  <Input
                    id="variant-cost-delta"
                    inputMode="decimal"
                    className="mt-1.5 text-right tabular"
                    aria-invalid={Boolean(errors.cost_delta)}
                    {...register('cost_delta')}
                  />
                  <FieldError message={errors.cost_delta?.message} />
                </div>
              )}

              <div>
                <Label htmlFor="variant-barcode">Barcode</Label>
                <Input
                  id="variant-barcode"
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.barcode)}
                  {...register('barcode')}
                />
                <FieldError message={errors.barcode?.message} />
              </div>

              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <label className="flex items-center gap-2 self-end pb-2 text-sm">
                    <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                    Active
                  </label>
                )}
              />
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

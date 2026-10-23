/**
 * The client's own finish, size and colour vocabulary.
 *
 * These three were free text on the item until recently. Nothing in the
 * application branched on their values - which is precisely why they can be
 * handed to an administrator - but nothing governed them either, so "Brushed",
 * "brushed" and "Brushed Steel" were three different finishes and no report
 * could group by finish.
 *
 * Two things the contract deliberately does not let this dialog do:
 *
 * - **The kind cannot be changed after creation.** `ItemAttributeUpdate` does
 *   not carry `attribute`. Moving a value from Finish to Colour would silently
 *   retarget every item pointing at it, and the code is unique per kind, so the
 *   move could also collide. Retire it and make a new one.
 * - **There is no delete.** Retirement is the `is_active` flag, and the service
 *   refuses even that while an item or a variant still carries the value -
 *   otherwise an item would display a value its own edit form could no longer
 *   offer, and saving that form would silently drop it.
 *
 * Renaming, by contrast, is free and is the point: items reference the row, so
 * correcting a spelling here corrects it everywhere at once.
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
import { itemAttributeOptionKeys } from '@/hooks/use-item-attribute-options'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { success, validation } from '@/lib/messages'
import { toast } from '@/lib/toast'

import { mastersKeys, type ItemAttributeRow } from './masters-model'

/** The three kinds, with the words a person uses for them. */
export const ATTRIBUTE_KINDS = [
  { value: 'FINISH', label: 'Finish', hint: 'Antique Brass, Satin Stainless, Black Nickel' },
  { value: 'SIZE', label: 'Size', hint: '52mm, 100 x 75 x 3mm' },
  { value: 'COLOUR', label: 'Colour', hint: 'Matt Black, White' },
] as const

const attributeSchema = z.object({
  // Annotated `boolean` rather than left to inference: since TypeScript 5.5 a
  // bare predicate of this shape is inferred as a type guard, which narrows the
  // field and makes the empty string the select starts on unassignable.
  attribute: z
    .string()
    .refine((value): boolean => ATTRIBUTE_KINDS.some((kind) => kind.value === value), {
      message: 'Choose which attribute this value belongs to.',
    }),
  code: z.string().trim().min(1, validation.required('Code')).max(32),
  name: z.string().trim().min(1, validation.required('Name')).max(64),
  sort_order: z.string().trim(),
  is_active: z.boolean(),
})

type AttributeFormValues = z.infer<typeof attributeSchema>

function emptyAttribute(attribute: string): AttributeFormValues {
  return { attribute, code: '', name: '', sort_order: '0', is_active: true }
}

function valuesFrom(row: ItemAttributeRow): AttributeFormValues {
  return {
    attribute: row.attribute,
    code: row.code,
    name: row.name,
    sort_order: String(row.sort_order),
    is_active: row.is_active,
  }
}

export function ItemAttributeFormDialog({
  open,
  onOpenChange,
  attribute,
  defaultKind = 'FINISH',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  attribute?: ItemAttributeRow | null
  /** Which kind a new value starts on - the one the tab is filtered to. */
  defaultKind?: string
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(attribute)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<AttributeFormValues>({
    resolver: zodResolver(attributeSchema),
    defaultValues: emptyAttribute(defaultKind),
  })

  const editingId = attribute?.id ?? null
  useEffect(() => {
    if (open) reset(attribute ? valuesFrom(attribute) : emptyAttribute(defaultKind))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, defaultKind, reset])

  const mutation = useMutation({
    mutationFn: (values: AttributeFormValues) => {
      const sortOrder = Number.parseInt(values.sort_order || '0', 10)

      if (attribute) {
        return api.patch<ItemAttributeRow>(`/products/attributes/${attribute.id}`, {
          name: values.name.trim(),
          sort_order: sortOrder,
          is_active: values.is_active,
        })
      }

      return api.post<ItemAttributeRow>('/products/attributes', {
        attribute: values.attribute,
        code: values.code.trim(),
        name: values.name.trim(),
        sort_order: sortOrder,
        is_active: values.is_active,
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      // The item and variant forms read the shared branch; a finish added here
      // must not stay invisible to them for the reference-data stale window.
      void queryClient.invalidateQueries({ queryKey: itemAttributeOptionKeys.all })
      toast.success(
        isEdit ? success.updated('Attribute') : success.created('Attribute'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit value' : 'New value'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Renaming is safe: items reference this row, not the words in it, so the new name appears everywhere at once.'
              : 'A permitted finish, size or colour. Items and variants choose from these instead of typing.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit((values) => mutation.mutate(values))}>
          <DialogBody className="space-y-4">
            <div>
              <Label htmlFor="attribute-kind">Attribute</Label>
              <Controller
                control={control}
                name="attribute"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange} disabled={isEdit}>
                    <SelectTrigger id="attribute-kind" className="mt-1.5">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {ATTRIBUTE_KINDS.map((kind) => (
                        <SelectItem key={kind.value} value={kind.value}>
                          {kind.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError message={errors.attribute?.message} />
              {isEdit && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Fixed once created. Moving a value between attributes would retarget every item
                  using it.
                </p>
              )}
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="attribute-code">Code</Label>
                <Input
                  id="attribute-code"
                  className="mt-1.5"
                  disabled={isEdit}
                  placeholder="ANTIQUE-BRASS"
                  {...register('code')}
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="attribute-name">Name</Label>
                <Input
                  id="attribute-name"
                  className="mt-1.5"
                  placeholder="Antique Brass"
                  {...register('name')}
                />
                <FieldError message={errors.name?.message} />
              </div>
            </div>

            <div>
              <Label htmlFor="attribute-sort">Position in the list</Label>
              <Input
                id="attribute-sort"
                className="mt-1.5"
                inputMode="numeric"
                {...register('sort_order')}
              />
              <FieldError message={errors.sort_order?.message} />
              {/* Sizes are the reason this field exists: 12mm, 25mm and 50mm is
                  the order a person expects and is not the order they sort in. */}
              <p className="mt-1 text-xs text-muted-foreground">
                Lower comes first. Values sharing a position fall back to their code.
              </p>
            </div>

            <Controller
              control={control}
              name="is_active"
              render={({ field }) => (
                <div className="flex items-start gap-2.5">
                  <Checkbox
                    id="attribute-active"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                  <div>
                    <Label htmlFor="attribute-active">Offered on forms</Label>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      Clear this to retire the value. Items already using it keep it; it simply
                      stops being offered. Retiring is refused while anything still uses it.
                    </p>
                  </div>
                </div>
              )}
            />
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create value'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

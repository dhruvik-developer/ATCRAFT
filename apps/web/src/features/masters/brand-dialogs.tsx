/**
 * Create, edit and retire a brand - the name a product is sold under.
 *
 * What the contract does not let these dialogs do, and why:
 *
 * - **The code cannot change after creation.** `BrandUpdate` forbids it (a PATCH
 *   carrying `code` is a 422, not a silent no-op): the code identifies the row
 *   on every document and report that quotes it. Renaming, by contrast, is free
 *   - items reference the row, so a corrected name reaches every item at once.
 * - **A brand live items still carry cannot be deactivated or retired.** The
 *   server refuses it and names the reason; the retire dialog shows that
 *   sentence verbatim, because it says exactly what has to change first.
 * - **Nothing branches on a brand.** It is classification, which is why the
 *   client may edit the list freely.
 *
 * Every successful write invalidates both the masters tab and the shared brand
 * options, so the item form's Brand picker sees a new brand at once rather than
 * after the reference-data stale window.
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
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { brandOptionKeys } from '@/hooks/use-brand-options'
import { useManufacturerOptions } from '@/hooks/use-manufacturer-options'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'

import { mastersKeys, type BrandRow } from './masters-model'
import { RetireWithReasonDialog } from './masters-shared'

/** `BrandCreate.sort_order` is bounded 0..9999 by the contract and a CHECK. */
const MAX_POSITION = 9999

const brandSchema = z.object({
  code: z.string().trim().min(1, validation.required('Code')).max(32, 'At most 32 characters.'),
  name: z.string().trim().min(1, validation.required('Name')).max(120, 'At most 120 characters.'),
  manufacturer_id: z.string(),
  // Annotated `boolean`: since TypeScript 5.5 a bare predicate of this shape is
  // inferred as a type guard, which would narrow the field.
  sort_order: z
    .string()
    .trim()
    .refine(
      (value): boolean =>
        value === '' || (/^\d{1,4}$/.test(value) && Number(value) <= MAX_POSITION),
      { message: `A whole number from 0 to ${MAX_POSITION}.` },
    ),
  description: z.string(),
  is_active: z.boolean(),
})

type BrandFormValues = z.infer<typeof brandSchema>

function emptyBrand(): BrandFormValues {
  return {
    code: '',
    name: '',
    manufacturer_id: SELECT_NONE,
    sort_order: '0',
    description: '',
    is_active: true,
  }
}

function valuesFrom(row: BrandRow): BrandFormValues {
  return {
    code: row.code,
    name: row.name,
    manufacturer_id: row.manufacturer_id ?? SELECT_NONE,
    sort_order: String(row.sort_order),
    description: row.description ?? '',
    is_active: row.is_active,
  }
}

export function BrandFormDialog({
  open,
  onOpenChange,
  brand,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  brand?: BrandRow | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(brand)

  // The owner list is a separate right. Without it the select offers only "No
  // owner"; an edit still posts the owner the brand already has, untouched.
  const makers = useManufacturerOptions(open && can(P.MANUFACTURER_VIEW))

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<BrandFormValues>({
    resolver: zodResolver(brandSchema),
    defaultValues: emptyBrand(),
  })

  const editingId = brand?.id ?? null
  useEffect(() => {
    if (open) reset(brand ? valuesFrom(brand) : emptyBrand())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: BrandFormValues) => {
      const body = {
        name: values.name.trim(),
        // "No owner" posts null - an empty string would be a foreign key the
        // server has to reject, and an absent field would leave a stale owner.
        manufacturer_id: asId(values.manufacturer_id),
        sort_order: Number.parseInt(values.sort_order || '0', 10),
        description: asText(values.description),
        is_active: values.is_active,
      }
      if (brand) return api.patch<BrandRow>(`/products/brands/${brand.id}`, body)
      return api.post<BrandRow>('/products/brands', { code: values.code.trim(), ...body })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: brandOptionKeys.all })
      toast.success(
        isEdit ? success.updated('Brand') : success.created('Brand'),
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
          <DialogTitle>{isEdit ? `Edit ${brand?.code}` : 'New brand'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Renaming is safe: items reference this brand, not its name, so the new name appears on every item at once.'
              : 'The name products are sold under. Items choose from this list on their form.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit((values) => mutation.mutate(values))} noValidate>
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="brand-code" required>
                  Code
                </Label>
                <Input
                  id="brand-code"
                  className="mt-1.5"
                  disabled={isEdit}
                  placeholder="ATCRAFT-PRO"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  {...register('code')}
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="brand-name" required>
                  Name
                </Label>
                <Input
                  id="brand-name"
                  className="mt-1.5"
                  placeholder="ATCRAFT PRO"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  {...register('name')}
                />
                <FieldError message={errors.name?.message} />
              </div>

              <div>
                <Label htmlFor="brand-owner">Owner (manufacturer)</Label>
                <Controller
                  control={control}
                  name="manufacturer_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="brand-owner" className="mt-1.5">
                        <SelectValue placeholder="No owner" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>No owner</SelectItem>
                        {(makers.data ?? []).map((maker) => (
                          <SelectItem key={maker.id} value={maker.id}>
                            {maker.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.manufacturer_id?.message} />
              </div>

              <div>
                <Label htmlFor="brand-position">Position in the list</Label>
                <Input
                  id="brand-position"
                  className="mt-1.5"
                  inputMode="numeric"
                  aria-invalid={Boolean(errors.sort_order)}
                  {...register('sort_order')}
                />
                <FieldError message={errors.sort_order?.message} />
              </div>
            </div>

            <div>
              <Label htmlFor="brand-description">Description</Label>
              <Textarea id="brand-description" className="mt-1.5" {...register('description')} />
            </div>

            <Controller
              control={control}
              name="is_active"
              render={({ field }) => (
                <div className="flex items-start gap-2.5">
                  <Checkbox
                    id="brand-active"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                  <div>
                    <Label htmlFor="brand-active">Offered on item forms</Label>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      Clear this to stop offering the brand. Refused while any item still carries it
                      - move those items first.
                    </p>
                  </div>
                </div>
              )}
            />

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
              {isEdit ? 'Save changes' : 'Create brand'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Retire (archive) a brand. The reason travels in the query string, as the API declares it. */
export function BrandRetireDialog({
  brand,
  onOpenChange,
}: {
  /** The brand being retired; null while the dialog is closed. */
  brand: BrandRow | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const open = brand !== null

  const retire = useMutation({
    mutationFn: ({ target, reason }: { target: BrandRow; reason: string }) =>
      api.delete(`/products/brands/${target.id}`, { params: { reason } }),
    onSuccess: (_result, { target }) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: brandOptionKeys.all })
      toast.success(success.retired('Brand'), `${target.code} - ${target.name}`)
      onOpenChange(false)
    },
  })

  const { reset: clearResult } = retire
  useEffect(() => {
    if (!open) clearResult()
  }, [open, clearResult])

  return (
    <RetireWithReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      title={brand ? `Retire ${brand.code}?` : 'Retire brand?'}
      description={
        <>
          <strong className="font-medium text-foreground">{brand?.name}</strong> leaves the brand
          list and every picker. Its code is never reused. Refused while any item still carries it.
        </>
      }
      confirmLabel="Retire brand"
      inputId="brand-retire-reason"
      pending={retire.isPending}
      error={retire.error}
      errorHeadline="The brand was not retired"
      onConfirm={(reason) => {
        if (brand) retire.mutate({ target: brand, reason })
      }}
    />
  )
}

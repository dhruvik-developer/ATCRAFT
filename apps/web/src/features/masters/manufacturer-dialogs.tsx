/**
 * Create, edit and retire a manufacturer - who actually makes a part.
 *
 * A manufacturer is deliberately not a vendor: a maker's cylinder is routinely
 * bought through a distributor, and a vendor carries payables, approval and a
 * portal login that a maker does not. The item records its maker; each
 * item-vendor link records which maker that source supplies.
 *
 * The same contract rules as the brand dialogs: the code is fixed once created
 * (a PATCH carrying it is refused), renaming is free, and a maker that live
 * items, item-vendor links or brands still name can be neither deactivated nor
 * retired - the server says which of the three is in the way, and the retire
 * dialog shows that sentence verbatim.
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
} from '@/components/ui/overlays'
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { manufacturerOptionKeys } from '@/hooks/use-manufacturer-options'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { success, validation } from '@/lib/messages'
import { toast } from '@/lib/toast'
import { asText } from '@/lib/utils'

import { mastersKeys, type ManufacturerRow } from './masters-model'
import { RetireWithReasonDialog } from './masters-shared'

/** The server's own default for a maker nobody placed (`ManufacturerCreate.country`). */
const DEFAULT_COUNTRY = 'India'

const manufacturerSchema = z.object({
  code: z.string().trim().min(1, validation.required('Code')).max(32, 'At most 32 characters.'),
  name: z.string().trim().min(1, validation.required('Name')).max(255, 'At most 255 characters.'),
  legal_name: z.string().trim().max(255, 'At most 255 characters.'),
  country: z
    .string()
    .trim()
    .min(1, validation.required('Country'))
    .max(80, 'At most 80 characters.'),
  website: z.string().trim().max(255, 'At most 255 characters.'),
  notes: z.string(),
  is_active: z.boolean(),
})

type ManufacturerFormValues = z.infer<typeof manufacturerSchema>

function emptyManufacturer(): ManufacturerFormValues {
  return {
    code: '',
    name: '',
    legal_name: '',
    country: DEFAULT_COUNTRY,
    website: '',
    notes: '',
    is_active: true,
  }
}

function valuesFrom(row: ManufacturerRow): ManufacturerFormValues {
  return {
    code: row.code,
    name: row.name,
    legal_name: row.legal_name ?? '',
    country: row.country,
    website: row.website ?? '',
    notes: row.notes ?? '',
    is_active: row.is_active,
  }
}

export function ManufacturerFormDialog({
  open,
  onOpenChange,
  manufacturer,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  manufacturer?: ManufacturerRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(manufacturer)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<ManufacturerFormValues>({
    resolver: zodResolver(manufacturerSchema),
    defaultValues: emptyManufacturer(),
  })

  const editingId = manufacturer?.id ?? null
  useEffect(() => {
    if (open) reset(manufacturer ? valuesFrom(manufacturer) : emptyManufacturer())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: ManufacturerFormValues) => {
      // Blank optional text posts null: on an edit that clears the field, and
      // the API stores "no website" one way rather than two.
      const body = {
        name: values.name.trim(),
        legal_name: asText(values.legal_name),
        country: values.country.trim(),
        website: asText(values.website),
        notes: asText(values.notes),
        is_active: values.is_active,
      }
      if (manufacturer) {
        return api.patch<ManufacturerRow>(`/products/manufacturers/${manufacturer.id}`, body)
      }
      return api.post<ManufacturerRow>('/products/manufacturers', {
        code: values.code.trim(),
        ...body,
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: manufacturerOptionKeys.all })
      toast.success(
        isEdit ? success.updated('Manufacturer') : success.created('Manufacturer'),
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
          <DialogTitle>{isEdit ? `Edit ${manufacturer?.code}` : 'New manufacturer'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Renaming is safe: items, brands and vendor links reference this maker, not its name.'
              : 'Who makes a part. Not a vendor - a maker is recorded even when its parts arrive through a distributor.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit((values) => mutation.mutate(values))} noValidate>
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="manufacturer-code" required>
                  Code
                </Label>
                <Input
                  id="manufacturer-code"
                  className="mt-1.5"
                  disabled={isEdit}
                  placeholder="CYLINDER-WORKS"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  {...register('code')}
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="manufacturer-name" required>
                  Name
                </Label>
                <Input
                  id="manufacturer-name"
                  className="mt-1.5"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  {...register('name')}
                />
                <FieldError message={errors.name?.message} />
              </div>

              <div>
                <Label htmlFor="manufacturer-legal-name">Legal name</Label>
                <Input
                  id="manufacturer-legal-name"
                  className="mt-1.5"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.legal_name)}
                  {...register('legal_name')}
                />
                <FieldError message={errors.legal_name?.message} />
              </div>

              <div>
                <Label htmlFor="manufacturer-country" required>
                  Country
                </Label>
                <Input
                  id="manufacturer-country"
                  className="mt-1.5"
                  autoComplete="off"
                  aria-invalid={Boolean(errors.country)}
                  {...register('country')}
                />
                <FieldError message={errors.country?.message} />
              </div>
            </div>

            <div>
              <Label htmlFor="manufacturer-website">Website</Label>
              <Input
                id="manufacturer-website"
                className="mt-1.5"
                inputMode="url"
                autoComplete="off"
                aria-invalid={Boolean(errors.website)}
                {...register('website')}
              />
              <FieldError message={errors.website?.message} />
            </div>

            <div>
              <Label htmlFor="manufacturer-notes">Notes</Label>
              <Textarea id="manufacturer-notes" className="mt-1.5" {...register('notes')} />
            </div>

            <Controller
              control={control}
              name="is_active"
              render={({ field }) => (
                <div className="flex items-start gap-2.5">
                  <Checkbox
                    id="manufacturer-active"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                  <div>
                    <Label htmlFor="manufacturer-active">Offered on forms</Label>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      Clear this to stop offering the maker. Refused while items, vendor links or
                      brands still name it.
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
              {isEdit ? 'Save changes' : 'Create manufacturer'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Retire (archive) a manufacturer, with the reason the audit trail keeps. */
export function ManufacturerRetireDialog({
  manufacturer,
  onOpenChange,
}: {
  /** The maker being retired; null while the dialog is closed. */
  manufacturer: ManufacturerRow | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const open = manufacturer !== null

  const retire = useMutation({
    mutationFn: ({ target, reason }: { target: ManufacturerRow; reason: string }) =>
      api.delete(`/products/manufacturers/${target.id}`, { params: { reason } }),
    onSuccess: (_result, { target }) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: manufacturerOptionKeys.all })
      toast.success(success.retired('Manufacturer'), `${target.code} - ${target.name}`)
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
      title={manufacturer ? `Retire ${manufacturer.code}?` : 'Retire manufacturer?'}
      description={
        <>
          <strong className="font-medium text-foreground">{manufacturer?.name}</strong> leaves the
          manufacturer list and every picker. Its code is never reused. Refused while items, vendor
          links or brands still name it.
        </>
      }
      confirmLabel="Retire manufacturer"
      inputId="manufacturer-retire-reason"
      pending={retire.isPending}
      error={retire.error}
      errorHeadline="The manufacturer was not retired"
      onConfirm={(reason) => {
        if (manufacturer) retire.mutate({ target: manufacturer, reason })
      }}
    />
  )
}

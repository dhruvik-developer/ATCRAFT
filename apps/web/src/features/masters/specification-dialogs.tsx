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
import { useAuth } from '@/features/auth/auth-context'
import {
  invalidateProductDescriptions,
  SPECIFICATION_TYPES,
  type SpecificationAttribute,
} from '@/features/product-descriptions/specification-model'
import { api } from '@/lib/api-client'
import { PRODUCT_DESCRIPTION_POSITION_MAX, SPECIFICATION_OPTION_MAX } from '@/lib/constants'
import { applyFieldErrors, fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { RetireWithReasonDialog } from './masters-shared'

const formSchema = z
  .object({
    code: z.string().trim().min(1, validation.required('Code')).max(32),
    name: z.string().trim().min(1, validation.required('Name')).max(120),
    value_type: z.enum(SPECIFICATION_TYPES),
    unit: z.string().trim().max(16),
    options_text: z.string(),
    sort_order: z
      .string()
      .trim()
      .regex(/^\d{1,4}$/, `Enter a whole number from 0 to ${PRODUCT_DESCRIPTION_POSITION_MAX}.`),
    is_filterable: z.boolean(),
    is_active: z.boolean(),
  })
  .superRefine((value, context) => {
    if (value.value_type !== 'OPTION') return
    const options = value.options_text
      .split('\n')
      .map((option) => option.trim())
      .filter(Boolean)
    if (!options.length)
      context.addIssue({
        code: 'custom',
        path: ['options_text'],
        message: 'Enter at least one option, one per line.',
      })
    if (options.length > SPECIFICATION_OPTION_MAX)
      context.addIssue({
        code: 'custom',
        path: ['options_text'],
        message: `Use at most ${SPECIFICATION_OPTION_MAX} options.`,
      })
    if (options.some((option) => option.length > 64))
      context.addIssue({
        code: 'custom',
        path: ['options_text'],
        message: 'Each option may contain at most 64 characters.',
      })
    if (new Set(options).size !== options.length)
      context.addIssue({
        code: 'custom',
        path: ['options_text'],
        message: 'Each option must appear only once.',
      })
  })
type FormValues = z.infer<typeof formSchema>

function formValues(attribute?: SpecificationAttribute | null): FormValues {
  return {
    code: attribute?.code ?? '',
    name: attribute?.name ?? '',
    value_type: attribute?.value_type ?? 'TEXT',
    unit: attribute?.unit ?? '',
    options_text: attribute?.options.join('\n') ?? '',
    sort_order: String(attribute?.sort_order ?? 0),
    is_filterable: attribute?.is_filterable ?? false,
    is_active: attribute?.is_active ?? true,
  }
}

export function SpecificationFormDialog({
  open,
  onOpenChange,
  attribute,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  attribute?: SpecificationAttribute | null
}) {
  const { can } = useAuth()
  const client = useQueryClient()
  const canManage = can(P.ITEM_ATTRIBUTE_MANAGE)
  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: formValues(attribute),
  })
  const editingId = attribute?.id
  useEffect(() => {
    if (open) reset(formValues(attribute))
    // A background list refresh must not replace an explanation being edited.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])
  const valueType = watch('value_type')
  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = {
        name: values.name,
        value_type: values.value_type,
        unit: values.unit || null,
        options:
          values.value_type === 'OPTION'
            ? values.options_text
                .split('\n')
                .map((option) => option.trim())
                .filter(Boolean)
            : [],
        is_filterable: values.is_filterable,
        sort_order: Number(values.sort_order),
        is_active: values.is_active,
      } satisfies SchemaOf<'SpecificationAttributeUpdate'>
      return attribute
        ? api.patch<SpecificationAttribute>(
            `/products/specification-attributes/${attribute.id}`,
            body,
          )
        : api.post<SpecificationAttribute>('/products/specification-attributes', {
            ...body,
            code: values.code,
          } satisfies SchemaOf<'SpecificationAttributeCreate'>)
    },
    onSuccess: () => {
      invalidateProductDescriptions(client)
      toast.success(attribute ? success.updated('Specification') : success.created('Specification'))
      onOpenChange(false)
    },
    onError: (error) =>
      applyFieldErrors(error, setError, (field) =>
        field.startsWith('options') ? 'options_text' : field,
      ),
  })
  const pending = mutation.isPending
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!pending) onOpenChange(next)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{attribute ? `Edit ${attribute.code}` : 'New specification'}</DialogTitle>
          <DialogDescription>
            Technical descriptions shared by item editors and the dealer catalog. Existing values
            protect their type and options from incompatible changes.
          </DialogDescription>
        </DialogHeader>
        <form
          noValidate
          onSubmit={handleSubmit((values) => {
            if (canManage && !pending) mutation.mutate(values)
          })}
        >
          <DialogBody className="space-y-4">
            <fieldset disabled={!canManage || pending} className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="specification-code" required>
                    Code
                  </Label>
                  <Input
                    id="specification-code"
                    disabled={Boolean(attribute)}
                    {...register('code')}
                    aria-invalid={Boolean(errors.code)}
                  />
                  <FieldError message={errors.code?.message} />
                </div>
                <div>
                  <Label htmlFor="specification-name" required>
                    Name
                  </Label>
                  <Input
                    id="specification-name"
                    {...register('name')}
                    aria-invalid={Boolean(errors.name)}
                  />
                  <FieldError message={errors.name?.message} />
                </div>
                <div>
                  <Label htmlFor="specification-type">Value type</Label>
                  <select
                    id="specification-type"
                    {...register('value_type')}
                    className="h-11 w-full rounded-md border border-input bg-background px-3 text-sm"
                  >
                    {SPECIFICATION_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type.charAt(0) + type.slice(1).toLowerCase()}
                      </option>
                    ))}
                  </select>
                  <FieldError message={errors.value_type?.message} />
                </div>
                <div>
                  <Label htmlFor="specification-unit">Unit</Label>
                  <Input
                    id="specification-unit"
                    placeholder="mm, kg, years"
                    {...register('unit')}
                    aria-invalid={Boolean(errors.unit)}
                  />
                  <FieldError message={errors.unit?.message} />
                </div>
              </div>
              {valueType === 'OPTION' && (
                <div>
                  <Label htmlFor="specification-options" required>
                    Permitted options
                  </Label>
                  <Textarea
                    id="specification-options"
                    rows={5}
                    placeholder={'Brass\nStainless Steel 304'}
                    {...register('options_text')}
                    aria-invalid={Boolean(errors.options_text)}
                  />
                  <p className="text-xs text-muted-foreground">
                    One option per line. Options currently recorded on items cannot be removed.
                  </p>
                  <FieldError message={errors.options_text?.message} />
                </div>
              )}
              <div>
                <Label htmlFor="specification-position">Position in the list</Label>
                <Input
                  id="specification-position"
                  inputMode="numeric"
                  {...register('sort_order')}
                  aria-invalid={Boolean(errors.sort_order)}
                />
                <FieldError message={errors.sort_order?.message} />
              </div>
              <Controller
                control={control}
                name="is_filterable"
                render={({ field }) => (
                  <div className="flex items-start gap-2">
                    <Checkbox
                      id="specification-filterable"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                    <div>
                      <Label htmlFor="specification-filterable">
                        Available as a catalog filter
                      </Label>
                      <p className="text-xs text-muted-foreground">
                        Catalog filtering supports Option and Boolean specifications.
                      </p>
                    </div>
                  </div>
                )}
              />
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <div className="flex items-start gap-2">
                    <Checkbox
                      id="specification-active"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                    <Label htmlFor="specification-active">Offered on item forms</Label>
                  </div>
                )}
              />
            </fieldset>
            <FieldError message={errors.root?.message} />
            {mutation.error && (
              <FieldError
                message={fieldErrorSummary(mutation.error) || describeError(mutation.error)}
              />
            )}
          </DialogBody>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={pending}
            >
              Cancel
            </Button>
            {canManage && (
              <Button type="submit" loading={pending}>
                {attribute ? 'Save specification' : 'Create specification'}
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function SpecificationRetireDialog({
  attribute,
  onOpenChange,
}: {
  attribute: SpecificationAttribute | null
  onOpenChange: (open: boolean) => void
}) {
  const client = useQueryClient()
  const { can } = useAuth()
  const mutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      api.delete(`/products/specification-attributes/${id}`, { params: { reason } }),
    onSuccess: () => {
      invalidateProductDescriptions(client)
      toast.success(success.retired('Specification'))
      onOpenChange(false)
    },
  })
  const { reset } = mutation
  useEffect(() => {
    if (!attribute) reset()
  }, [attribute, reset])
  return (
    <RetireWithReasonDialog
      open={Boolean(attribute)}
      onOpenChange={(next) => {
        if (!mutation.isPending) onOpenChange(next)
      }}
      title={`Retire ${attribute?.code ?? 'specification'}?`}
      description="Remove this specification from pickers. First remove its item values and category templates; its code is retained in the audit history."
      confirmLabel="Retire specification"
      inputId="specification-retire-reason"
      pending={mutation.isPending}
      error={mutation.error}
      errorHeadline="The specification was not retired"
      onConfirm={(reason) => {
        if (attribute && can(P.ITEM_ATTRIBUTE_MANAGE) && !mutation.isPending)
          mutation.mutate({ id: attribute.id, reason })
      }}
    />
  )
}

/**
 * The two BOM dialogs: create a new bill, and copy an existing one forward as a
 * new version.
 *
 * They are one file because they share a field vocabulary - a BOM version is
 * identified by (item, version, revision) in both - and keeping them together is
 * what stops the copy dialog quietly accepting a shape the create dialog
 * rejects.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useEffect } from 'react'
import {
  Controller,
  useFieldArray,
  useForm,
  type FieldValues,
  type UseFormSetError,
} from 'react-hook-form'
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
import { ItemPicker } from '@/features/inventory/pickers'
import { useUomOptions } from '@/features/items/item-model'
import { api, ApiError } from '@/lib/api-client'
import { humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { BOM_TYPES, type BomDetail } from './bom-model'

/** Radix forbids an empty option value, so "not set" needs a sentinel. */
const NONE = '__none'

const DECIMAL = /^\d{1,14}(\.\d{1,4})?$/

/**
 * Puts a 422 back on the control that caused it instead of in a toast.
 *
 * FastAPI reports the location as `body.version`; the form knows the field as
 * `version`, so only the last segment is used.
 */
function applyFieldErrors<T extends FieldValues>(
  error: unknown,
  known: ReadonlySet<string>,
  setError: UseFormSetError<T>,
): void {
  if (!(error instanceof ApiError) || error.status !== 422) return
  for (const [path, messages] of Object.entries(error.fieldErrors)) {
    const field = path.split('.').pop() ?? path
    if (!known.has(field)) continue
    setError(field as never, {
      type: 'server',
      message: Array.isArray(messages) ? messages.join('. ') : String(messages),
    })
  }
}

// ------------------------------------------------------------- new BOM ------

/**
 * One component line.
 *
 * `BomCreate.lines` has `minItems: 1` - the API will not accept a bill with no
 * components at all - so the dialog collects at least one here rather than
 * creating an empty header and earning a 422 the user cannot read.
 */
const lineSchema = z.object({
  component_item_id: z.string().min(1, 'Choose a component'),
  quantity_per: z
    .string()
    .trim()
    .regex(DECIMAL, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => value !== '0', 'Quantity per must be more than zero'),
  scrap_percentage: z.string().trim().regex(DECIMAL, 'Enter a percentage with up to 4 decimals'),
  is_optional: z.boolean(),
})

const createSchema = z.object({
  item_id: z.string().min(1, 'Choose the item this BOM produces'),
  bom_type: z.enum(BOM_TYPES),
  // The version number is minted by the API under a per-item advisory lock
  // (`service.next_version`); a client that sends one is ignored at best and
  // racing at worst, so the field is gone.
  revision: z.string().trim().max(16, 'Revision is at most 16 characters'),
  output_quantity: z
    .string()
    .trim()
    .regex(DECIMAL, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => value !== '0', 'Output quantity must be more than zero'),
  base_uom_id: z.string().min(1, 'Base unit is required'),
  effective_from: z.string().min(1, 'An effective-from date is required'),
  effective_to: z.string(),
  is_default: z.boolean(),
  notes: z.string().trim().max(1000, 'Notes are at most 1000 characters'),
  lines: z.array(lineSchema).min(1, 'A bill needs at least one component line'),
})

type CreateValues = z.infer<typeof createSchema>

const CREATE_FIELDS: ReadonlySet<string> = new Set(Object.keys(createSchema.shape))

function todayInput(): string {
  return new Date().toISOString().slice(0, 10)
}

function emptyLine(): z.infer<typeof lineSchema> {
  return { component_item_id: '', quantity_per: '1', scrap_percentage: '0', is_optional: false }
}

function emptyCreateValues(): CreateValues {
  return {
    item_id: '',
    bom_type: 'PRODUCTION',
    revision: 'A',
    output_quantity: '1',
    base_uom_id: '',
    effective_from: todayInput(),
    effective_to: '',
    is_default: false,
    notes: '',
    lines: [emptyLine()],
  }
}

export function BomFormDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const uoms = useUomOptions(open && can(P.UOM_VIEW))

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: emptyCreateValues(),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (open) reset(emptyCreateValues())
  }, [open, reset])

  const mutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post<BomDetail>('/bom', {
        item_id: values.item_id,
        bom_type: values.bom_type,
        revision: values.revision.trim() || null,
        // Quantities stay decimal strings all the way to the wire.
        output_quantity: values.output_quantity.trim(),
        base_uom_id: values.base_uom_id,
        effective_from: values.effective_from,
        effective_to: values.effective_to || null,
        is_default: values.is_default,
        notes: values.notes.trim() || null,
        // Line numbers are the API's key for a line, and a substitute points at
        // one, so they are assigned here in display order rather than left out.
        lines: values.lines.map((line, index) => ({
          line_number: index + 1,
          component_item_id: line.component_item_id,
          quantity_per: line.quantity_per.trim(),
          scrap_percentage: line.scrap_percentage.trim(),
          is_optional: line.is_optional,
        })),
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.bom.all })
      toast.success('BOM created', `${created.code} starts as a draft.`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, CREATE_FIELDS, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>New bill of materials</DialogTitle>
          <DialogDescription>
            The bill is created as a draft, and the version number is minted by the server. It
            becomes usable for planning and production once it is approved.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <Controller
              control={control}
              name="item_id"
              render={({ field }) => (
                <ItemPicker
                  id="bom-item"
                  label="Produced item"
                  value={field.value || null}
                  onChange={(itemId) => field.onChange(itemId ?? '')}
                  placeholder="Search the item master"
                  invalid={Boolean(errors.item_id)}
                />
              )}
            />
            <FieldError message={errors.item_id?.message} />

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="bom-type" required>
                  Type
                </Label>
                <Controller
                  control={control}
                  name="bom_type"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="bom-type" className="mt-1.5" aria-label="BOM type">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {BOM_TYPES.map((type) => (
                          <SelectItem key={type} value={type}>
                            {humanise(type)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.bom_type?.message} />
              </div>

              <div>
                <Label htmlFor="bom-revision">Revision</Label>
                <Input
                  id="bom-revision"
                  {...register('revision')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.revision)}
                  className="mt-1.5"
                />
                <FieldError message={errors.revision?.message} />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="bom-output" required>
                  Output quantity
                </Label>
                <Input
                  id="bom-output"
                  inputMode="decimal"
                  {...register('output_quantity')}
                  aria-invalid={Boolean(errors.output_quantity)}
                  aria-describedby="bom-output-hint"
                  className="mt-1.5"
                />
                <p id="bom-output-hint" className="mt-1 text-xs text-muted-foreground">
                  How many units one run of this bill yields.
                </p>
                <FieldError message={errors.output_quantity?.message} />
              </div>

              <div>
                <Label htmlFor="bom-uom" required>
                  Base unit
                </Label>
                <Controller
                  control={control}
                  name="base_uom_id"
                  render={({ field }) => (
                    <Select
                      value={field.value || NONE}
                      onValueChange={(value) => field.onChange(value === NONE ? '' : value)}
                    >
                      <SelectTrigger
                        id="bom-uom"
                        className="mt-1.5"
                        aria-label="Base unit"
                        aria-invalid={Boolean(errors.base_uom_id)}
                      >
                        <SelectValue
                          placeholder={uoms.isLoading ? 'Loading...' : 'Select a unit'}
                        />
                      </SelectTrigger>
                      <SelectContent>
                        {(uoms.data ?? []).map((uom) => (
                          <SelectItem key={uom.id} value={uom.id}>
                            {uom.code} - {uom.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.base_uom_id?.message} />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="bom-from" required>
                  Effective from
                </Label>
                <Input
                  id="bom-from"
                  type="date"
                  {...register('effective_from')}
                  aria-invalid={Boolean(errors.effective_from)}
                  className="mt-1.5"
                />
                <FieldError message={errors.effective_from?.message} />
              </div>

              <div>
                <Label htmlFor="bom-to">Effective to</Label>
                <Input
                  id="bom-to"
                  type="date"
                  {...register('effective_to')}
                  aria-invalid={Boolean(errors.effective_to)}
                  aria-describedby="bom-to-hint"
                  className="mt-1.5"
                />
                <p id="bom-to-hint" className="mt-1 text-xs text-muted-foreground">
                  Leave empty for an open-ended version.
                </p>
                <FieldError message={errors.effective_to?.message} />
              </div>
            </div>

            <div className="flex items-start gap-2.5 rounded-md border p-3">
              <Controller
                control={control}
                name="is_default"
                render={({ field }) => (
                  <Checkbox
                    id="bom-default"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                    className="mt-0.5"
                  />
                )}
              />
              <div>
                <Label htmlFor="bom-default">Make this the default bill for the item</Label>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Only one approved bill per item may be the default; the API rejects a second.
                </p>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Components</Label>
                <Button type="button" variant="outline" size="sm" onClick={() => append(emptyLine())}>
                  <Plus />
                  Add line
                </Button>
              </div>

              {fields.map((field, index) => (
                <div
                  key={field.id}
                  className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-[minmax(0,1fr)_7rem_6rem_auto]"
                >
                  <div className="min-w-0">
                    <Controller
                      control={control}
                      name={`lines.${index}.component_item_id`}
                      render={({ field: pick }) => (
                        <ItemPicker
                          id={`bom-line-item-${index}`}
                          label={`Component ${index + 1}`}
                          hideLabel
                          value={pick.value || null}
                          onChange={(itemId) => pick.onChange(itemId ?? '')}
                          placeholder="Search the item master"
                          invalid={Boolean(errors.lines?.[index]?.component_item_id)}
                        />
                      )}
                    />
                    <FieldError message={errors.lines?.[index]?.component_item_id?.message} />
                  </div>

                  <div>
                    <Label htmlFor={`bom-line-qty-${index}`} className="text-xs">
                      Qty per
                    </Label>
                    <Input
                      id={`bom-line-qty-${index}`}
                      inputMode="decimal"
                      {...register(`lines.${index}.quantity_per`)}
                      aria-invalid={Boolean(errors.lines?.[index]?.quantity_per)}
                      className="mt-1 text-right tabular"
                    />
                    <FieldError message={errors.lines?.[index]?.quantity_per?.message} />
                  </div>

                  <div>
                    <Label htmlFor={`bom-line-scrap-${index}`} className="text-xs">
                      Scrap %
                    </Label>
                    <Input
                      id={`bom-line-scrap-${index}`}
                      inputMode="decimal"
                      {...register(`lines.${index}.scrap_percentage`)}
                      aria-invalid={Boolean(errors.lines?.[index]?.scrap_percentage)}
                      className="mt-1 text-right tabular"
                    />
                    <FieldError message={errors.lines?.[index]?.scrap_percentage?.message} />
                  </div>

                  <div className="flex items-center gap-2 pt-6">
                    <Controller
                      control={control}
                      name={`lines.${index}.is_optional`}
                      render={({ field: flag }) => (
                        <Checkbox
                          id={`bom-line-optional-${index}`}
                          checked={flag.value}
                          onCheckedChange={(checked) => flag.onChange(checked === true)}
                          aria-label={`Component ${index + 1} is optional`}
                        />
                      )}
                    />
                    <Label htmlFor={`bom-line-optional-${index}`} className="text-xs font-normal">
                      Optional
                    </Label>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      // The API will not take a bill with no lines at all, so the
                      // last one cannot be removed.
                      disabled={fields.length === 1}
                      aria-label={`Remove component ${index + 1}`}
                      onClick={() => remove(index)}
                    >
                      <Trash2 />
                    </Button>
                  </div>
                </div>
              ))}

              <FieldError message={errors.lines?.message ?? errors.lines?.root?.message} />
            </div>

            <div>
              <Label htmlFor="bom-notes">Notes</Label>
              <Textarea
                id="bom-notes"
                {...register('notes')}
                aria-invalid={Boolean(errors.notes)}
                className="mt-1.5"
              />
              <FieldError message={errors.notes?.message} />
            </div>
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
              Create BOM
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------- copy as version ---

const copySchema = z.object({
  // No version field: the API mints the next one under the per-item advisory
  // lock, which is the only place two people copying at once can be serialised.
  revision: z.string().trim().max(16, 'Revision is at most 16 characters'),
  effective_from: z.string().min(1, 'An effective-from date is required'),
  effective_to: z.string(),
  copy_operations: z.boolean(),
  reason: z
    .string()
    .trim()
    .min(1, 'A new version is recorded with the reason it was raised')
    .max(500, 'At most 500 characters'),
  notes: z.string().trim().max(1000, 'Notes are at most 1000 characters'),
})

type CopyValues = z.infer<typeof copySchema>

const COPY_FIELDS: ReadonlySet<string> = new Set(Object.keys(copySchema.shape))

function copyDefaults(): CopyValues {
  return {
    revision: '',
    effective_from: todayInput(),
    effective_to: '',
    copy_operations: true,
    reason: '',
    notes: '',
  }
}

export function CopyBomDialog({
  open,
  onOpenChange,
  bom,
  onCopied,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  bom: BomDetail
  /** Navigates to the new version once the server has created it. */
  onCopied: (created: BomDetail) => void
}) {
  const queryClient = useQueryClient()

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<CopyValues>({
    resolver: zodResolver(copySchema),
    defaultValues: copyDefaults(),
  })

  // Reseeded per opening and keyed on the source BOM, so a background refetch of
  // the record cannot rewrite what the user is typing.
  const sourceId = bom.id
  useEffect(() => {
    if (!open) return
    reset(copyDefaults())
  }, [open, sourceId, reset])

  const mutation = useMutation({
    mutationFn: (values: CopyValues) =>
      // `/copy-as-new-version`, not `/copy`: the shorter path does not exist and
      // every copy from this dialog was a 404.
      api.post<BomDetail>(`/bom/${bom.id}/copy-as-new-version`, {
        reason: values.reason.trim(),
        revision: values.revision.trim() || null,
        effective_from: values.effective_from || null,
        effective_to: values.effective_to || null,
        copy_operations: values.copy_operations,
        notes: values.notes.trim() || null,
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.bom.all })
      toast.success('New version created', `${created.code} is a draft copy of ${bom.code}.`)
      onOpenChange(false)
      onCopied(created)
    },
    onError: (error) => applyFieldErrors(error, COPY_FIELDS, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Copy {bom.code} as a new version</DialogTitle>
          <DialogDescription>
            Every line and operation is copied into a fresh draft. The version you are looking at is
            left untouched, because it may already be quoted on open orders.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div>
              <Label htmlFor="copy-reason" required>
                Reason
              </Label>
              <Input
                id="copy-reason"
                {...register('reason')}
                autoComplete="off"
                placeholder="Why is a new version needed?"
                aria-invalid={Boolean(errors.reason)}
                className="mt-1.5"
              />
              <FieldError message={errors.reason?.message} />
              <p className="mt-1 text-xs text-muted-foreground">
                Recorded on the audit trail against both versions. The new version number is
                assigned by the server.
              </p>
            </div>

            <div>
              <Label htmlFor="copy-revision">Revision</Label>
              <Input
                id="copy-revision"
                {...register('revision')}
                autoComplete="off"
                aria-invalid={Boolean(errors.revision)}
                className="mt-1.5"
              />
              <FieldError message={errors.revision?.message} />
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="copy-from" required>
                  Effective from
                </Label>
                <Input
                  id="copy-from"
                  type="date"
                  {...register('effective_from')}
                  aria-invalid={Boolean(errors.effective_from)}
                  className="mt-1.5"
                />
                <FieldError message={errors.effective_from?.message} />
              </div>

              <div>
                <Label htmlFor="copy-to">Effective to</Label>
                <Input
                  id="copy-to"
                  type="date"
                  {...register('effective_to')}
                  aria-invalid={Boolean(errors.effective_to)}
                  className="mt-1.5"
                />
                <FieldError message={errors.effective_to?.message} />
              </div>
            </div>

            <div className="flex items-start gap-2.5 rounded-md border p-3">
              <Controller
                control={control}
                name="copy_operations"
                render={({ field }) => (
                  <Checkbox
                    id="copy-operations"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                    className="mt-0.5"
                  />
                )}
              />
              <div>
                <Label htmlFor="copy-operations">Copy the operations too</Label>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Untick to carry only the component lines forward and re-plan the operations.
                </p>
              </div>
            </div>

            <div>
              <Label htmlFor="copy-notes">Change note</Label>
              <Textarea
                id="copy-notes"
                {...register('notes')}
                placeholder="What changed in this revision?"
                aria-invalid={Boolean(errors.notes)}
                className="mt-1.5"
              />
              <FieldError message={errors.notes?.message} />
            </div>
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
              Create copy
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

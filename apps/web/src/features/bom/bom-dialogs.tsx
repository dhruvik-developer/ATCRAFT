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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useEffect } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
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
import { optionLabel, useWorkCentreOptions } from '@/features/production/work-order-model'
import { ApiError, api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { BOM_TYPES, type BomDetail, type BomRow } from './bom-model'

const DECIMAL = /^\d{1,14}(\.\d{1,4})?$/

// ------------------------------------------------------------- new BOM ------

/**
 * One component line.
 *
 * `BomCreate.lines` has `minItems: 1` - the API will not accept a bill with no
 * components at all - so the dialog collects at least one here rather than
 * creating an empty header and earning a 422 the user cannot read.
 */
const lineSchema = z.object({
  component_item_id: z.string().min(1, validation.choose('a component')),
  quantity_per: z
    .string()
    .trim()
    .regex(DECIMAL, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => value !== '0', 'Quantity per must be more than zero'),
  scrap_percentage: z.string().trim().regex(DECIMAL, 'Enter a percentage with up to 4 decimals'),
  is_optional: z.boolean(),
})

const createSchema = z
  .object({
    item_id: z.string().min(1, validation.choose('the item this BOM produces')),
    bom_type: z.enum(BOM_TYPES),
    // Only an ALTERNATE names the bill it stands in for. The API refuses an
    // ALTERNATE without one (`BomCreate._check`), and the form used to offer
    // the type with no way to say which bill - so every alternate was a 422.
    parent_bom_id: z.string(),
    // The version number is minted by the API under a per-item advisory lock
    // (`service.next_version`); a client that sends one is ignored at best and
    // racing at worst, so the field is gone.
    revision: z.string().trim().max(16, 'Revision is at most 16 characters'),
    output_quantity: z
      .string()
      .trim()
      .regex(DECIMAL, 'Enter a quantity with up to 4 decimal places')
      .refine((value) => value !== '0', 'Output quantity must be more than zero'),
    base_uom_id: z.string().min(1, validation.required('Base unit')),
    effective_from: z.string().min(1, validation.required('Effective-from date')),
    effective_to: z.string(),
    is_default: z.boolean(),
    notes: z.string().trim().max(1000, 'Notes are at most 1000 characters'),
    lines: z.array(lineSchema).min(1, 'A bill needs at least one component line'),
  })
  .superRefine((values, context) => {
    if (values.bom_type === 'ALTERNATE' && values.parent_bom_id === '') {
      context.addIssue({
        code: 'custom',
        path: ['parent_bom_id'],
        message: 'Name the bill this alternate stands in for',
      })
    }
  })

type CreateValues = z.infer<typeof createSchema>

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
    parent_bom_id: '',
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
    setValue,
    watch,
    formState: { errors },
  } = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: emptyCreateValues(),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (open) reset(emptyCreateValues())
  }, [open, reset])

  const itemId = watch('item_id')
  const bomType = watch('bom_type')

  // The bills an alternate may stand in for: every version of the same item.
  // `create_bom` requires only that the parent is for `item_id` - not that it
  // is approved - so the list is not narrowed by status.
  const alternativeParams = {
    item_id: itemId,
    limit: LOOKUP_LIST_LIMIT,
    with_total: false,
    sort_by: 'code',
    sort_dir: 'asc',
  }
  const alternatives = useQuery({
    queryKey: queryKeys.bom.list(alternativeParams),
    queryFn: () => api.getPage<BomRow>('/bom', { params: alternativeParams }),
    enabled: open && bomType === 'ALTERNATE' && Boolean(itemId),
    staleTime: staleTimes.master,
  })

  // A parent belongs to the produced item, so changing the item clears it.
  useEffect(() => {
    setValue('parent_bom_id', '')
  }, [itemId, setValue])

  const mutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post<BomDetail>('/bom', {
        item_id: values.item_id,
        bom_type: values.bom_type,
        parent_bom_id: values.bom_type === 'ALTERNATE' ? values.parent_bom_id : null,
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
      toast.success(success.created('BOM'), `${created.code} starts as a draft.`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
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

            {bomType === 'ALTERNATE' && (
              <div>
                <Label htmlFor="bom-parent" required>
                  Replaces
                </Label>
                <Controller
                  control={control}
                  name="parent_bom_id"
                  render={({ field }) => (
                    <Select
                      value={field.value || SELECT_NONE}
                      onValueChange={(value) => field.onChange(value === SELECT_NONE ? '' : value)}
                      disabled={!itemId || alternatives.isLoading}
                    >
                      <SelectTrigger
                        id="bom-parent"
                        className="mt-1.5"
                        aria-label="Replaces"
                        aria-invalid={Boolean(errors.parent_bom_id)}
                      >
                        <SelectValue
                          placeholder={
                            !itemId
                              ? 'Choose the produced item first'
                              : alternatives.isLoading
                                ? 'Loading...'
                                : 'Select the bill this one stands in for'
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        {(alternatives.data?.data ?? []).map((candidate) => (
                          <SelectItem key={candidate.id} value={candidate.id}>
                            {candidate.code} v{candidate.version} ({humanise(candidate.status)})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.parent_bom_id?.message} />
                {alternatives.error && (
                  <FieldError
                    message={"The item's bills could not be read: " + alternatives.error.message}
                  />
                )}
                {itemId && alternatives.data && alternatives.data.data.length === 0 && (
                  <p className="mt-1 text-xs text-warning">
                    This item has no bill yet. An alternate stands in for an existing one, so create
                    that first.
                  </p>
                )}
                <p className="mt-1 text-xs text-muted-foreground">
                  Any version of the same item qualifies; the API refuses a bill made for a
                  different item.
                </p>
              </div>
            )}

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
                      value={field.value || SELECT_NONE}
                      onValueChange={(value) => field.onChange(value === SELECT_NONE ? '' : value)}
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
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => append(emptyLine())}
                >
                  <Plus />
                  Add line
                </Button>
              </div>

              {fields.map((field, index) => (
                <div
                  key={field.id}
                  className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-2 md:grid-cols-[minmax(0,1fr)_7rem_6rem_auto]"
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

                  {/* A rule about the line as a whole - a substitute naming
                      itself, say - lands on the row rather than on a field,
                      and would otherwise render nowhere. */}
                  {errors.lines?.[index]?.message && (
                    <div className="sm:col-span-2 md:col-span-4">
                      <FieldError message={errors.lines?.[index]?.message} />
                    </div>
                  )}
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
              Create BOM
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------ edit draft ----

/**
 * One component line on the edit form.
 *
 * The grid carries every field `PATCH /bom/{id}` accepts on a line -
 * substitute and operation staging included, which the create dialog never
 * collected - because an edit that cannot see them would silently keep values
 * the engineer did not review. `substitute_for_index` is the target row's
 * position, translated to the API's line numbers on submit; both travel
 * together, so the numbering cannot drift between them.
 */
const editLineSchema = z.object({
  component_item_id: z.string().min(1, validation.choose('a component')),
  quantity_per: z
    .string()
    .trim()
    .regex(DECIMAL, 'Enter a quantity with up to 4 decimal places')
    .refine((value) => value !== '0', 'Quantity per must be more than zero'),
  scrap_percentage: z.string().trim().regex(DECIMAL, 'Enter a percentage with up to 4 decimals'),
  is_optional: z.boolean(),
  substitute_for_index: z.number().int().min(0).nullable(),
  operation_sequence: z
    .string()
    .trim()
    .regex(/^(\d{1,5})?$/, 'Enter the operation step number, if any'),
  notes: z.string().trim().max(2000, 'Notes are at most 2000 characters'),
})

/**
 * One operation on the edit form.
 *
 * `sequence` is the step number the lines stage against, carried rather than
 * edited: renumbering steps would orphan the staging the lines name. A new row
 * takes the next free step above the highest one on submit.
 */
const editOperationSchema = z.object({
  sequence: z.number().int().positive().nullable(),
  name: z.string().trim().min(1, validation.required('Operation name')).max(255),
  work_center_id: z.string().nullable(),
  setup_time_minutes: z.string().trim().regex(DECIMAL, 'Enter minutes, up to 4 decimals'),
  run_time_minutes_per_unit: z.string().trim().regex(DECIMAL, 'Enter minutes, up to 4 decimals'),
  cost_per_hour: z.string().trim().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),
  fixed_cost: z.string().trim().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),
  is_outsourced: z.boolean(),
  notes: z.string().trim().max(2000, 'Notes are at most 2000 characters'),
})

const editSchema = z
  .object({
    lines: z.array(editLineSchema).min(1, 'A bill needs at least one component line'),
    operations: z.array(editOperationSchema),
  })
  .superRefine((values, context) => {
    values.lines.forEach((line, index) => {
      if (line.substitute_for_index === index) {
        context.addIssue({
          code: 'custom',
          path: ['lines', index, 'substitute_for_index'],
          message: 'A line cannot substitute for itself.',
        })
      }
    })
  })

type EditValues = z.infer<typeof editSchema>

function emptyEditLine(): EditValues['lines'][number] {
  return {
    component_item_id: '',
    quantity_per: '1',
    scrap_percentage: '0',
    is_optional: false,
    substitute_for_index: null,
    operation_sequence: '',
    notes: '',
  }
}

function emptyEditOperation(): EditValues['operations'][number] {
  return {
    sequence: null,
    name: '',
    work_center_id: null,
    setup_time_minutes: '0',
    run_time_minutes_per_unit: '0',
    cost_per_hour: '0',
    fixed_cost: '0',
    is_outsourced: false,
    notes: '',
  }
}

function editDefaults(bom: BomDetail): EditValues {
  const lines = [...(bom.lines ?? [])].sort((a, b) => a.line_number - b.line_number)
  const idToIndex = new Map(lines.map((line, index) => [line.id, index]))
  return {
    lines: lines.map((line) => ({
      component_item_id: line.component_item_id,
      quantity_per: line.quantity_per,
      scrap_percentage: line.scrap_percentage,
      is_optional: line.is_optional,
      substitute_for_index:
        line.substitute_for_line_id == null
          ? null
          : (idToIndex.get(line.substitute_for_line_id) ?? null),
      operation_sequence: line.operation_sequence == null ? '' : String(line.operation_sequence),
      notes: line.notes ?? '',
    })),
    operations: [...(bom.operations ?? [])]
      .sort((a, b) => a.operation_sequence - b.operation_sequence)
      .map((operation) => ({
        sequence: operation.operation_sequence,
        name: operation.name,
        work_center_id: operation.work_center_id,
        setup_time_minutes: operation.setup_time_minutes,
        run_time_minutes_per_unit: operation.run_time_minutes_per_unit,
        cost_per_hour: operation.cost_per_hour,
        fixed_cost: operation.fixed_cost,
        is_outsourced: operation.is_outsourced,
        notes: operation.notes ?? '',
      })),
  }
}

export function BomEditDialog({
  bom,
  open,
  onOpenChange,
}: {
  /** The draft being edited, with its lines and operations already loaded. */
  bom: BomDetail | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const workCentres = useWorkCentreOptions(open)
  const centreRows = workCentres.data ?? []

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    defaultValues: bom ? editDefaults(bom) : { lines: [emptyEditLine()], operations: [] },
  })

  const {
    fields: lineFields,
    append: appendLine,
    remove: removeLine,
  } = useFieldArray({ control, name: 'lines' })
  const {
    fields: operationFields,
    append: appendOperation,
    remove: removeOperation,
  } = useFieldArray({ control, name: 'operations' })

  // Seeded from the loaded bill on every opening: a background refetch must
  // not rewrite what the engineer is editing.
  useEffect(() => {
    if (open && bom) reset(editDefaults(bom))
  }, [open, bom, reset])

  const mutation = useMutation({
    mutationFn: (values: EditValues) => {
      // Sequences for brand-new operations continue past the highest kept
      // step, so they cannot collide with one the lines already stage at.
      let nextSequence =
        values.operations.reduce<number>(
          (highest, operation) => Math.max(highest, operation.sequence ?? 0),
          0,
        ) + 10
      const operations = values.operations.map((operation) => {
        const sequence = operation.sequence ?? nextSequence
        if (operation.sequence == null) nextSequence += 10
        return {
          operation_sequence: sequence,
          name: operation.name.trim(),
          work_center_id: operation.work_center_id,
          setup_time_minutes: operation.setup_time_minutes.trim(),
          run_time_minutes_per_unit: operation.run_time_minutes_per_unit.trim(),
          cost_per_hour: operation.cost_per_hour.trim(),
          fixed_cost: operation.fixed_cost.trim(),
          is_outsourced: operation.is_outsourced,
          notes: operation.notes.trim() || null,
        }
      })
      return api.patch<BomDetail>(`/bom/${bom?.id}`, {
        lines: values.lines.map((line, index) => ({
          line_number: index + 1,
          component_item_id: line.component_item_id,
          quantity_per: line.quantity_per.trim(),
          scrap_percentage: line.scrap_percentage.trim(),
          is_optional: line.is_optional,
          substitute_for_line_number:
            line.substitute_for_index == null ? null : line.substitute_for_index + 1,
          operation_sequence:
            line.operation_sequence === '' ? null : Number(line.operation_sequence),
          notes: line.notes.trim() || null,
        })),
        operations,
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.bom.all })
      toast.success(success.updated('BOM'), `${saved.code} v${saved.version} updated.`)
      onOpenChange(false)
    },
    onError: (error) => {
      // The bill carries an optimistic lock: a second engineer saving first
      // answers this save with a 409, which has no field behind it. It lands
      // on the form's root slot (and the mutation toast) rather than vanishing
      // into the field-error machinery, which only speaks 422.
      if (error instanceof ApiError && error.status === 409) {
        setError('root', { type: 'server', message: error.message })
        return
      }
      applyFieldErrors(error, setError)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Edit {bom?.code ?? 'draft'}</DialogTitle>
          <DialogDescription>
            Lines and operations replace the draft wholesale. A bill that someone else saved first
            refuses with a conflict - reload it and re-apply the change.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Components</Label>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => appendLine(emptyEditLine())}
                >
                  <Plus />
                  Add line
                </Button>
              </div>

              {lineFields.map((field, index) => (
                <div
                  key={field.id}
                  className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_6rem_5rem_7rem_6rem_auto]"
                >
                  <div className="min-w-0">
                    <Controller
                      control={control}
                      name={`lines.${index}.component_item_id`}
                      render={({ field: pick }) => (
                        <ItemPicker
                          id={`bom-edit-line-item-${index}`}
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
                    <Label htmlFor={`bom-edit-line-qty-${index}`} className="text-xs">
                      Qty per
                    </Label>
                    <Input
                      id={`bom-edit-line-qty-${index}`}
                      inputMode="decimal"
                      {...register(`lines.${index}.quantity_per`)}
                      aria-invalid={Boolean(errors.lines?.[index]?.quantity_per)}
                      className="mt-1 text-right tabular"
                    />
                    <FieldError message={errors.lines?.[index]?.quantity_per?.message} />
                  </div>

                  <div>
                    <Label htmlFor={`bom-edit-line-scrap-${index}`} className="text-xs">
                      Scrap %
                    </Label>
                    <Input
                      id={`bom-edit-line-scrap-${index}`}
                      inputMode="decimal"
                      {...register(`lines.${index}.scrap_percentage`)}
                      aria-invalid={Boolean(errors.lines?.[index]?.scrap_percentage)}
                      className="mt-1 text-right tabular"
                    />
                    <FieldError message={errors.lines?.[index]?.scrap_percentage?.message} />
                  </div>

                  <div>
                    <Label htmlFor={`bom-edit-line-opseq-${index}`} className="text-xs">
                      At operation
                    </Label>
                    <Input
                      id={`bom-edit-line-opseq-${index}`}
                      inputMode="numeric"
                      placeholder="Any"
                      {...register(`lines.${index}.operation_sequence`)}
                      aria-invalid={Boolean(errors.lines?.[index]?.operation_sequence)}
                      className="mt-1 text-right tabular"
                    />
                    <FieldError message={errors.lines?.[index]?.operation_sequence?.message} />
                  </div>

                  <div>
                    <Label htmlFor={`bom-edit-line-sub-${index}`} className="text-xs">
                      Substitutes
                    </Label>
                    <Controller
                      control={control}
                      name={`lines.${index}.substitute_for_index`}
                      render={({ field: sub }) => (
                        <Select
                          value={sub.value == null ? SELECT_NONE : String(sub.value)}
                          onValueChange={(value) =>
                            sub.onChange(value === SELECT_NONE ? null : Number(value))
                          }
                        >
                          <SelectTrigger
                            id={`bom-edit-line-sub-${index}`}
                            className="mt-1"
                            aria-label={`Line ${index + 1} substitutes for`}
                          >
                            <SelectValue placeholder="Primary" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>Primary line</SelectItem>
                            {lineFields.map((_, other) =>
                              other === index ? null : (
                                <SelectItem key={other} value={String(other)}>
                                  Line {other + 1}
                                </SelectItem>
                              ),
                            )}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    <FieldError message={errors.lines?.[index]?.substitute_for_index?.message} />
                  </div>

                  <div className="flex items-center gap-2 pt-6">
                    <Controller
                      control={control}
                      name={`lines.${index}.is_optional`}
                      render={({ field: flag }) => (
                        <Checkbox
                          id={`bom-edit-line-optional-${index}`}
                          checked={flag.value}
                          onCheckedChange={(checked) => flag.onChange(checked === true)}
                          aria-label={`Component ${index + 1} is optional`}
                        />
                      )}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      disabled={lineFields.length === 1}
                      aria-label={`Remove component ${index + 1}`}
                      onClick={() => removeLine(index)}
                    >
                      <Trash2 />
                    </Button>
                  </div>

                  <div className="sm:col-span-2 lg:col-span-6">
                    <Label htmlFor={`bom-edit-line-notes-${index}`} className="text-xs">
                      Line notes
                    </Label>
                    <Input
                      id={`bom-edit-line-notes-${index}`}
                      {...register(`lines.${index}.notes`)}
                      autoComplete="off"
                      className="mt-1"
                    />
                    <FieldError message={errors.lines?.[index]?.notes?.message} />
                  </div>

                  {errors.lines?.[index]?.message && (
                    <div className="sm:col-span-2 lg:col-span-6">
                      <FieldError message={errors.lines?.[index]?.message} />
                    </div>
                  )}
                </div>
              ))}

              <FieldError message={errors.lines?.message ?? errors.lines?.root?.message} />
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Operations</Label>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => appendOperation(emptyEditOperation())}
                >
                  <Plus />
                  Add operation
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                The labour steps this bill costs. A draft with no operations takes the item&apos;s
                default routing when it is approved.
              </p>

              {operationFields.map((field, index) => (
                <div
                  key={field.id}
                  className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-2 lg:grid-cols-[5rem_minmax(0,1fr)_minmax(0,1fr)_auto]"
                >
                  <div>
                    <Label htmlFor={`bom-edit-op-name-${index}`} className="text-xs">
                      Step {field.sequence ?? 'new'}
                    </Label>
                    <Input
                      id={`bom-edit-op-name-${index}`}
                      {...register(`operations.${index}.name`)}
                      autoComplete="off"
                      placeholder="Operation name"
                      aria-invalid={Boolean(errors.operations?.[index]?.name)}
                      className="mt-1"
                    />
                    <FieldError message={errors.operations?.[index]?.name?.message} />
                  </div>

                  <div>
                    <Label htmlFor={`bom-edit-op-wc-${index}`} className="text-xs">
                      Work centre
                    </Label>
                    <Controller
                      control={control}
                      name={`operations.${index}.work_center_id`}
                      render={({ field: centre }) => (
                        <Select
                          value={centre.value ?? SELECT_NONE}
                          onValueChange={(value) =>
                            centre.onChange(value === SELECT_NONE ? null : value)
                          }
                        >
                          <SelectTrigger
                            id={`bom-edit-op-wc-${index}`}
                            className="mt-1"
                            aria-label="Operation work centre"
                          >
                            <SelectValue
                              placeholder={workCentres.isLoading ? 'Loading...' : 'No centre'}
                            />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>No centre</SelectItem>
                            {centreRows.map((row) => (
                              <SelectItem key={row.id} value={row.id}>
                                {optionLabel(row)}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    <FieldError message={errors.operations?.[index]?.work_center_id?.message} />
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <Label htmlFor={`bom-edit-op-setup-${index}`} className="text-xs">
                        Setup (min)
                      </Label>
                      <Input
                        id={`bom-edit-op-setup-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.setup_time_minutes`)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError
                        message={errors.operations?.[index]?.setup_time_minutes?.message}
                      />
                    </div>
                    <div>
                      <Label htmlFor={`bom-edit-op-run-${index}`} className="text-xs">
                        Run / unit
                      </Label>
                      <Input
                        id={`bom-edit-op-run-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.run_time_minutes_per_unit`)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError
                        message={errors.operations?.[index]?.run_time_minutes_per_unit?.message}
                      />
                    </div>
                    <div>
                      <Label htmlFor={`bom-edit-op-rate-${index}`} className="text-xs">
                        Rate / hour
                      </Label>
                      <Input
                        id={`bom-edit-op-rate-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.cost_per_hour`)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError message={errors.operations?.[index]?.cost_per_hour?.message} />
                    </div>
                    <div>
                      <Label htmlFor={`bom-edit-op-fixed-${index}`} className="text-xs">
                        Fixed charge
                      </Label>
                      <Input
                        id={`bom-edit-op-fixed-${index}`}
                        inputMode="decimal"
                        {...register(`operations.${index}.fixed_cost`)}
                        className="mt-1 text-right tabular"
                      />
                      <FieldError message={errors.operations?.[index]?.fixed_cost?.message} />
                    </div>
                  </div>

                  <div className="flex items-center gap-2 pt-6">
                    <Controller
                      control={control}
                      name={`operations.${index}.is_outsourced`}
                      render={({ field: flag }) => (
                        <Checkbox
                          id={`bom-edit-op-outsourced-${index}`}
                          checked={flag.value}
                          onCheckedChange={(checked) => flag.onChange(checked === true)}
                          aria-label={`Operation ${index + 1} is outsourced`}
                        />
                      )}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove operation ${index + 1}`}
                      onClick={() => removeOperation(index)}
                    >
                      <Trash2 />
                    </Button>
                  </div>

                  {errors.operations?.[index]?.message && (
                    <div className="sm:col-span-2 lg:col-span-4">
                      <FieldError message={errors.operations?.[index]?.message} />
                    </div>
                  )}
                </div>
              ))}

              <FieldError
                message={errors.operations?.message ?? errors.operations?.root?.message}
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
              Save changes
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
  effective_from: z.string().min(1, validation.required('Effective-from date')),
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
      toast.success(
        success.created('New version'),
        `${created.code} is a draft copy of ${bom.code}.`,
      )
      onOpenChange(false)
      onCopied(created)
    },
    onError: (error) => applyFieldErrors(error, setError),
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
              Create copy
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

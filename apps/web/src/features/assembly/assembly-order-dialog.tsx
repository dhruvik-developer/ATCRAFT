/**
 * Raise an assembly order, and amend one while it is still a draft.
 *
 * The register could approve, release, start and complete an order it had no
 * way of creating, so this is the missing front of the lifecycle. Both
 * endpoints sit behind `assembly_order.manage`; the caller gates the
 * affordance and this module only builds the payloads.
 *
 * Three properties of the contract shape the form and are not preferences:
 *
 * 1. **The item is narrowed server-side.** The API refuses an inactive item and
 *    one not flagged `can_manufacture`, so the picker searches with
 *    `can_manufacture=true` rather than offering the whole master and failing
 *    at save.
 * 2. **The BOM is optional.** Omitting it builds from the item's default
 *    approved structure, which must be an ASSEMBLY or PRODUCTION one. Naming
 *    one explicitly picks between several approved structures of those two
 *    types; any other type is refused, so the select lists only those two.
 * 3. **An edit cannot change what is being built.** `PATCH` carries no
 *    `item_id` and no `bom_id` - changing either is a different order, not an
 *    edit, because the exploded component set would no longer match the
 *    header. Changing the planned quantity re-explodes from the same
 *    structure, so the item and BOM are shown and locked when amending.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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
import {
  Alert,
  AlertDescription,
  Checkbox,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import {
  ASSEMBLY_LINE_MAX_LENGTH,
  ASSEMBLY_NOTES_MAX_LENGTH,
  ASSEMBLY_PRIORITY_MAX,
  DEFAULT_ASSEMBLY_PRIORITY,
} from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise, toDateInput } from '@/lib/format'
import { success, validation } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { BomRow } from '@/features/bom/bom-model'

import { assemblyKeys, type AssemblyOrderDetail } from './assembly-model'

/** `Quantity` is NUMERIC(18,4): at most 14 integer digits and 4 decimals. */
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
/** A priority is a small whole number; lower is more urgent. */
const PRIORITY = /^\d{1,4}$/

/** Radix Select forbids an empty item value, so "default recipe" needs a sentinel. */
const DEFAULT_BOM = '__default__'

const schema = z
  .object({
    item_id: z.string().min(1, validation.choose('the item to assemble')),
    bom_id: z.string(),
    planned_quantity: z
      .string()
      .trim()
      .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    warehouse_id: z
      .string()
      .min(1, validation.choose('the warehouse the components are drawn from')),
    output_warehouse_id: z.string(),
    assembly_line: z.string().trim().max(64, 'At most 64 characters'),
    planned_start: z.string(),
    planned_end: z.string(),
    priority: z
      .string()
      .trim()
      .regex(PRIORITY, 'Enter a whole number')
      .refine((value) => Number(value) <= 9999, 'At most 9999'),
    requires_qc: z.boolean(),
    notes: z.string().trim().max(2000, 'At most 2000 characters'),
  })
  .refine(
    (values) =>
      values.planned_start === '' ||
      values.planned_end === '' ||
      values.planned_end >= values.planned_start,
    { message: 'The planned end cannot be before the planned start', path: ['planned_end'] },
  )

type FormValues = z.infer<typeof schema>

function defaultsFor(order?: AssemblyOrderDetail | null): FormValues {
  return {
    item_id: order?.item_id ?? '',
    bom_id: '',
    planned_quantity: order ? String(order.planned_quantity) : '',
    warehouse_id: order?.warehouse_id ?? '',
    output_warehouse_id: order?.output_warehouse_id ?? '',
    assembly_line: order?.assembly_line ?? '',
    planned_start: toDateInput(order?.planned_start),
    planned_end: toDateInput(order?.planned_end),
    priority: String(order?.priority ?? DEFAULT_ASSEMBLY_PRIORITY),
    requires_qc: order?.requires_qc ?? false,
    notes: order?.notes ?? '',
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

export function AssemblyOrderFormDialog({
  open,
  onOpenChange,
  order,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present when amending a draft; absent when raising one. */
  order?: AssemblyOrderDetail | null
  /** The saved order, so the caller can open its panel without a second read. */
  onSaved?: (order: AssemblyOrderDetail) => void
}) {
  const queryClient = useQueryClient()
  const editing = Boolean(order)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: defaultsFor(order),
  })

  const itemId = watch('item_id')

  useEffect(() => {
    if (open) reset(defaultsFor(order))
    // Re-read on every open so an amendment never starts from a stale header.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reset])

  // The approved structures the item may be built from. Filtered to the two
  // types the order endpoint accepts; anything else is refused at save, so it
  // is not offered. Read whole rather than paged: one item holds a handful of
  // structures, never a catalogue.
  const boms = useQuery({
    queryKey: [...queryKeys.bom.all, 'for-item', itemId] as const,
    queryFn: () =>
      api.getPage<BomRow>('/bom', {
        params: { item_id: itemId, status: 'APPROVED', limit: 50, with_total: false },
      }),
    enabled: open && !editing && Boolean(itemId),
    staleTime: staleTimes.master,
  })
  const structures = (boms.data?.data ?? []).filter(
    (bom) => bom.bom_type === 'ASSEMBLY' || bom.bom_type === 'PRODUCTION',
  )

  useEffect(() => {
    // A new item has no BOM choice yet: fall back to its default recipe rather
    // than holding the previous item's structure.
    if (!editing) setValue('bom_id', DEFAULT_BOM)
  }, [editing, itemId, setValue])

  const save = useMutation({
    mutationFn: (values: FormValues) => {
      const plannedStart = orNull(values.planned_start)
      const plannedEnd = orNull(values.planned_end)
      if (editing && order) {
        // No item and no BOM: changing what is being built is a different
        // order, not an edit. An explicit null clears a nullable column; the
        // output warehouse falls back to the component warehouse server-side.
        return api.patch<AssemblyOrderDetail>(`/assembly/${order.id}`, {
          planned_quantity: values.planned_quantity.trim(),
          warehouse_id: values.warehouse_id,
          output_warehouse_id: values.output_warehouse_id || null,
          assembly_line: orNull(values.assembly_line),
          planned_start: plannedStart,
          planned_end: plannedEnd,
          priority: Number(values.priority),
          requires_qc: values.requires_qc,
          notes: orNull(values.notes),
        })
      }
      return api.post<AssemblyOrderDetail>('/assembly', {
        item_id: values.item_id,
        // Omitted for the item's default approved structure.
        ...(values.bom_id && values.bom_id !== DEFAULT_BOM ? { bom_id: values.bom_id } : {}),
        planned_quantity: values.planned_quantity.trim(),
        warehouse_id: values.warehouse_id,
        ...(values.output_warehouse_id ? { output_warehouse_id: values.output_warehouse_id } : {}),
        ...(orNull(values.assembly_line) ? { assembly_line: orNull(values.assembly_line) } : {}),
        ...(plannedStart ? { planned_start: plannedStart } : {}),
        ...(plannedEnd ? { planned_end: plannedEnd } : {}),
        priority: Number(values.priority),
        requires_qc: values.requires_qc,
        ...(orNull(values.notes) ? { notes: orNull(values.notes) } : {}),
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
      toast.success(
        editing ? success.updated('Assembly order') : success.created('Assembly order'),
        `${saved.code}: assemble ${saved.planned_quantity} from ${saved.warehouse_id.slice(0, 8)}.`,
      )
      onSaved?.(saved)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{editing ? `Amend ${order?.code}` : 'New assembly order'}</DialogTitle>
          <DialogDescription>
            {editing
              ? 'Only a draft may be amended. Changing the planned quantity rebuilds the kitting list from the same structure.'
              : 'Explodes one level of the approved recipe and freezes the kitting list onto the order. A draft reserves and consumes nothing.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <ItemPicker
                  id="assembly-order-item"
                  label="Item to assemble"
                  value={itemId || null}
                  onChange={(value) => setValue('item_id', value ?? '', { shouldValidate: true })}
                  placeholder="Search manufacturable items..."
                  invalid={Boolean(errors.item_id)}
                  disabled={editing}
                  filters={{ can_manufacture: true }}
                  emptyMessage="Nothing manufacturable matches. The item must be active and flagged manufacturable."
                />
                <FieldError message={errors.item_id?.message} />
                {editing && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    The item and its recipe are fixed once raised.
                  </p>
                )}
              </div>

              {!editing && (
                <div className="space-y-1.5">
                  <Label htmlFor="assembly-order-bom">Recipe</Label>
                  <Controller
                    control={control}
                    name="bom_id"
                    render={({ field }) => (
                      <Select
                        value={field.value || DEFAULT_BOM}
                        onValueChange={field.onChange}
                        disabled={!itemId || boms.isLoading}
                      >
                        <SelectTrigger
                          id="assembly-order-bom"
                          aria-label="Recipe"
                          className="min-w-[180px]"
                        >
                          <SelectValue
                            placeholder={
                              !itemId
                                ? 'Choose an item first'
                                : boms.isLoading
                                  ? 'Loading recipes...'
                                  : 'Default recipe'
                            }
                          />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={DEFAULT_BOM}>Default recipe for this item</SelectItem>
                          {structures.map((bom) => (
                            <SelectItem key={bom.id} value={bom.id}>
                              {bom.code} · v{bom.version} · {humanise(bom.bom_type)}
                              {bom.is_default ? ' · default' : ''}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  {itemId && !boms.isLoading && structures.length === 0 && (
                    <p className="text-xs text-muted-foreground">
                      No approved assembly or production recipe for this item; the default recipe is
                      used, or approve one on the BOM screen first.
                    </p>
                  )}
                </div>
              )}

              <div>
                <Label htmlFor="assembly-order-quantity" required>
                  Planned quantity
                </Label>
                <Input
                  id="assembly-order-quantity"
                  {...register('planned_quantity')}
                  inputMode="decimal"
                  autoComplete="off"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.planned_quantity)}
                />
                <FieldError message={errors.planned_quantity?.message} />
              </div>

              <div>
                <Label htmlFor="assembly-order-priority" required>
                  Priority
                </Label>
                <Input
                  id="assembly-order-priority"
                  {...register('priority')}
                  inputMode="numeric"
                  autoComplete="off"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.priority)}
                />
                <FieldError message={errors.priority?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Lower is more urgent; carried onto every component reservation at release. At most{' '}
                  {ASSEMBLY_PRIORITY_MAX}.
                </p>
              </div>

              <WarehouseSelect
                id="assembly-order-warehouse"
                label="Component warehouse"
                value={watch('warehouse_id')}
                onChange={(value) => setValue('warehouse_id', value, { shouldValidate: true })}
                invalid={Boolean(errors.warehouse_id)}
              />
              <FieldError message={errors.warehouse_id?.message} />

              <WarehouseSelect
                id="assembly-order-output-warehouse"
                label="Output warehouse (same when empty)"
                value={watch('output_warehouse_id')}
                onChange={(value) => setValue('output_warehouse_id', value)}
              />

              <div>
                <Label htmlFor="assembly-order-line">Assembly line</Label>
                <Input
                  id="assembly-order-line"
                  {...register('assembly_line')}
                  autoComplete="off"
                  maxLength={ASSEMBLY_LINE_MAX_LENGTH}
                  className="mt-1.5"
                  placeholder="Bench or line, e.g. Line 1"
                  aria-invalid={Boolean(errors.assembly_line)}
                />
                <FieldError message={errors.assembly_line?.message} />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <Label htmlFor="assembly-order-planned-start">Planned start</Label>
                  <Input
                    id="assembly-order-planned-start"
                    type="date"
                    {...register('planned_start')}
                    className="mt-1.5"
                  />
                </div>
                <div>
                  <Label htmlFor="assembly-order-planned-end">Planned end</Label>
                  <Input
                    id="assembly-order-planned-end"
                    type="date"
                    {...register('planned_end')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.planned_end)}
                  />
                  <FieldError message={errors.planned_end?.message} />
                </div>
              </div>
              <p className="-mt-2 text-xs text-muted-foreground sm:col-span-2">
                The planned start becomes the required date on every component reservation at
                release.
              </p>
            </div>

            <div className="flex items-start gap-2">
              <Controller
                control={control}
                name="requires_qc"
                render={({ field }) => (
                  <Checkbox
                    id="assembly-order-requires-qc"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <div>
                <Label htmlFor="assembly-order-requires-qc" className="font-normal">
                  Inspect the finished output before it becomes saleable
                </Label>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Completion receives the output onto QC hold and raises an inspection with it.
                </p>
              </div>
            </div>

            <div>
              <Label htmlFor="assembly-order-notes">Notes</Label>
              <Textarea
                id="assembly-order-notes"
                {...register('notes')}
                rows={2}
                maxLength={ASSEMBLY_NOTES_MAX_LENGTH}
                className="mt-1.5"
                aria-invalid={Boolean(errors.notes)}
              />
              <FieldError message={errors.notes?.message} />
            </div>

            {save.error && (
              <Alert variant="destructive">
                <AlertDescription>{save.error.message}</AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={save.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending}>
              {editing ? 'Save changes' : 'Raise order'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

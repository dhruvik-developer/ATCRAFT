/**
 * The dialogs behind the stock movement screen: one to raise each of the three
 * documents, and one to confirm a count - the step that turns counted stock
 * into real stock.
 *
 * Quantities are typed and submitted as strings. An `<input type="number">`
 * hands back a value the browser has already normalised through a double, which
 * is precisely the round-trip docs/api.md forbids for a 4-decimal quantity - so
 * these are text inputs with a decimal input mode, validated by pattern and
 * compared with decimal.js.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Loader2, Plus, Trash2 } from 'lucide-react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import {
  Controller,
  useFieldArray,
  useForm,
  useWatch,
  type Control,
  type FieldErrors,
} from 'react-hook-form'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
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
  AlertTitle,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { CostCentreSelect } from '@/features/admin/cost-centre-select'
import BatchSelect from './movement-batch-picker'
import { useAuth } from '@/features/auth/auth-context'
import { api, ApiError } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE, LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
  toDateInput,
  toDecimal,
} from '@/lib/format'
import { joinValidationMessages, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'
import type { SchemaOf } from '@/types/api'

import { ItemPicker, WarehouseSelect } from './pickers'
import { LocationPicker } from './location-picker'
import { SerialNumbersField } from './serial-numbers-field'
import { SERIAL_BULK_MAX, splitSerialNumbers } from './serial-numbers'
import { ADJUSTMENT_REASON_CODES, type PhysicalCountLineRow, type PhysicalCountRow } from './types'

// --------------------------------------------------------- validation ------

const QUANTITY_PATTERN = /^\d+(\.\d{1,4})?$/
const SIGNED_QUANTITY_PATTERN = /^-?\d+(\.\d{1,4})?$/

function isPositiveQuantity(value: string): boolean {
  const trimmed = value.trim()
  return QUANTITY_PATTERN.test(trimmed) && toDecimal(trimmed).greaterThan(0)
}

function isNonZeroQuantity(value: string): boolean {
  const trimmed = value.trim()
  return SIGNED_QUANTITY_PATTERN.test(trimmed) && !toDecimal(trimmed).isZero()
}

const today = () => toDateInput(new Date())

// ------------------------------------------------------- line editor -------

interface LineFields {
  lines: {
    item_id: string
    quantity: string
    batch_id?: string
    location_id?: string
    from_location_id?: string
    to_location_id?: string
    serial_numbers?: string
    unit_cost?: string
  }[]
}

/**
 * The item/quantity grid shared by transfers and adjustments. From the sm
 * breakpoint the labels are shown on the first row and kept for screen readers
 * on the rest, so the grid reads as a table without repeating "Item" down the
 * page. On a phone the rows stack into one column, so every row keeps its
 * labels and a rule beneath it - there the labels are the only row boundary.
 */
function LineEditor<T extends LineFields>({
  control,
  errors,
  idPrefix,
  quantityLabel,
  quantityHint,
  signed = false,
  itemFilters,
  itemEmptyMessage,
  warehouseId,
  toWarehouseId,
  canEditCost = false,
}: {
  control: Control<T>
  errors: FieldErrors<LineFields>
  idPrefix: string
  quantityLabel: string
  quantityHint?: string
  signed?: boolean
  /** Narrowing applied by the item search, for a document that cannot take
   *  every item in the master. */
  itemFilters?: Record<string, unknown>
  itemEmptyMessage?: string
  warehouseId: string
  toWarehouseId?: string
  canEditCost?: boolean
}) {
  // `useFieldArray` is generic over the form shape; the cast narrows T to the
  // part this component actually edits.
  const { fields, append, remove } = useFieldArray({
    control: control as unknown as Control<LineFields>,
    name: 'lines',
  })
  const lineControl = control as unknown as Control<LineFields>
  const values = useWatch({ control: lineControl, name: 'lines' })

  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-sm font-medium">Lines</legend>

      {fields.map((field, index) => (
        <div
          key={field.id}
          className="grid gap-2 border-b pb-3 last:border-b-0 last:pb-0 sm:grid-cols-[minmax(0,1fr)_10rem_auto] sm:items-end sm:border-b-0 sm:pb-0"
        >
          <Controller
            control={control as unknown as Control<LineFields>}
            name={`lines.${index}.item_id`}
            render={({ field: itemField }) => (
              <ItemPicker
                id={idPrefix + '-item-' + index}
                label={'Item, line ' + (index + 1)}
                className={index > 0 ? 'sm:[&>label]:sr-only' : undefined}
                placeholder="Choose an item"
                value={itemField.value || null}
                onChange={(itemId) => itemField.onChange(itemId ?? '')}
                invalid={Boolean(errors.lines?.[index]?.item_id)}
                filters={itemFilters}
                emptyMessage={itemEmptyMessage}
              />
            )}
          />

          <Controller
            control={control as unknown as Control<LineFields>}
            name={`lines.${index}.quantity`}
            render={({ field: quantityField }) => (
              <div className="space-y-1.5">
                <Label
                  htmlFor={idPrefix + '-quantity-' + index}
                  className={index > 0 ? 'sm:sr-only' : undefined}
                >
                  {quantityLabel}
                </Label>
                <Input
                  id={idPrefix + '-quantity-' + index}
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder={signed ? '-12.5' : '0'}
                  className="text-right tabular"
                  aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                  value={quantityField.value}
                  onChange={quantityField.onChange}
                  onBlur={quantityField.onBlur}
                  name={quantityField.name}
                  ref={quantityField.ref}
                />
              </div>
            )}
          />

          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={'Remove line ' + (index + 1)}
            disabled={fields.length === 1}
            onClick={() => remove(index)}
          >
            <Trash2 />
          </Button>

          <div className="sm:col-span-3">
            <FieldError message={errors.lines?.[index]?.item_id?.message} />
            <FieldError message={errors.lines?.[index]?.quantity?.message} />
          </div>
          <div className="grid gap-3 sm:col-span-3 sm:grid-cols-3">
            {values?.[index]?.item_id && (
              <Controller
                control={lineControl}
                name={`lines.${index}.batch_id`}
                render={({ field: batch }) => (
                  <BatchSelect
                    id={`${idPrefix}-batch-${index}`}
                    itemId={values?.[index]?.item_id ?? ''}
                    value={batch.value ?? ''}
                    onChange={batch.onChange}
                  />
                )}
              />
            )}
            <Controller
              control={lineControl}
              name={`lines.${index}.${toWarehouseId !== undefined ? 'from_location_id' : 'location_id'}`}
              render={({ field: bin }) => (
                <LocationPicker
                  id={`${idPrefix}-bin-${index}`}
                  label={toWarehouseId !== undefined ? 'From bin' : 'Bin'}
                  warehouseId={warehouseId}
                  itemId={values?.[index]?.item_id ?? ''}
                  value={bin.value || null}
                  onChange={(value) => bin.onChange(value ?? '')}
                />
              )}
            />
            {toWarehouseId !== undefined && (
              <Controller
                control={lineControl}
                name={`lines.${index}.to_location_id`}
                render={({ field: bin }) => (
                  <LocationPicker
                    id={`${idPrefix}-to-bin-${index}`}
                    label="To bin"
                    warehouseId={toWarehouseId}
                    value={bin.value || null}
                    onChange={(value) => bin.onChange(value ?? '')}
                  />
                )}
              />
            )}
            {canEditCost && (
              <Controller
                control={lineControl}
                name={`lines.${index}.unit_cost`}
                render={({ field: cost }) => (
                  <div className="space-y-1.5">
                    <Label htmlFor={`${idPrefix}-cost-${index}`}>Unit cost for write-up</Label>
                    <Input
                      id={`${idPrefix}-cost-${index}`}
                      inputMode="decimal"
                      {...cost}
                      value={cost.value ?? ''}
                      placeholder="Use item cost"
                    />
                  </div>
                )}
              />
            )}
          </div>
          {values?.[index]?.item_id && (
            <div className="sm:col-span-3">
              <Controller
                control={lineControl}
                name={`lines.${index}.serial_numbers`}
                render={({ field: serials }) => (
                  <SerialNumbersField
                    id={`${idPrefix}-serials-${index}`}
                    value={serials.value ?? ''}
                    onChange={serials.onChange}
                    expected={values?.[index]?.quantity}
                    label="Serial numbers"
                    hint="One serial per unit moved. Leave empty for an untracked item."
                    error={errors.lines?.[index]?.serial_numbers?.message}
                    rows={2}
                  />
                )}
              />
            </div>
          )}
        </div>
      ))}

      <FieldError message={errors.lines?.message} />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => append({ item_id: '', quantity: '' })}
        >
          <Plus />
          Add line
        </Button>
        {quantityHint && <p className="text-xs text-muted-foreground">{quantityHint}</p>}
      </div>
    </fieldset>
  )
}

// ------------------------------------------------------------ transfer -----

/**
 * A stock transfer now carries its units: the API has a serial list on each
 * line and parks the named units in transit. A line may carry labels for a
 * serial-tracked item; an untracked item simply sends none.
 */
const transferSchema = z
  .object({
    from_warehouse_id: z.string().min(1, validation.choose('the warehouse the stock leaves')),
    to_warehouse_id: z.string().min(1, validation.choose('the warehouse the stock arrives at')),
    transfer_date: z.string().min(1, validation.required('Transfer date')),
    reason: z.string().trim().min(5, 'Say why the stock is moving.'),
    lines: z
      .array(
        z.object({
          item_id: z.string().min(1, validation.choose('an item')),
          batch_id: z.string().optional(),
          serial_numbers: z.string().optional(),
          from_location_id: z.string().optional(),
          to_location_id: z.string().optional(),
          quantity: z
            .string()
            .refine(isPositiveQuantity, 'Enter a quantity greater than zero, to 4 decimal places.'),
        }),
      )
      .min(1, 'A transfer needs at least one line.'),
  })
  .refine((values) => values.from_warehouse_id !== values.to_warehouse_id, {
    message: 'The source and destination warehouses must differ.',
    path: ['to_warehouse_id'],
  })
  .superRefine((values, ctx) => {
    values.lines.forEach((line, index) => {
      const serials = splitSerialNumbers(line.serial_numbers ?? '')
      if (serials.length === 0) return
      if (serials.length > SERIAL_BULK_MAX) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'serial_numbers'],
          message: `The API accepts at most ${SERIAL_BULK_MAX} serials at a time.`,
        })
        return
      }
      if (!toDecimal(line.quantity).equals(serials.length)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'serial_numbers'],
          message: `Name one serial per unit moved - ${line.quantity || '0'} moved, ${serials.length} captured`,
        })
      }
    })
  })

type TransferForm = z.infer<typeof transferSchema>

/**
 * Pre-fill for a transfer raised from another document (WP E1b section 4).
 *
 * The sales order panel knows the item, the quantity it is short of, the
 * warehouse that can give it up and the order line the stock is for. Asking the
 * user to retype any of that would be an invitation to move the wrong units or
 * lose the peg back to the order.
 */
export interface TransferSeed {
  fromWarehouseId?: string
  toWarehouseId?: string
  itemId?: string
  quantity?: string
  reason?: string
  /** MANUAL for a typed transfer; SALES_ORDER/REALLOCATION names the source. */
  sourceType?: 'MANUAL' | 'SALES_ORDER' | 'REALLOCATION'
  sourceId?: string | null
  /** The source document's line this transfer answers, when it has one. */
  sourceLineId?: string | null
}

export function CreateTransferDialog({
  open,
  onOpenChange,
  initial,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  initial?: TransferSeed
}) {
  const queryClient = useQueryClient()
  const defaults = useMemo<TransferForm>(
    () => ({
      from_warehouse_id: initial?.fromWarehouseId ?? '',
      to_warehouse_id: initial?.toWarehouseId ?? '',
      transfer_date: today(),
      reason: initial?.reason ?? '',
      lines: [{ item_id: initial?.itemId ?? '', quantity: initial?.quantity ?? '' }],
    }),
    [initial],
  )
  const form = useForm<TransferForm>({
    resolver: zodResolver(transferSchema),
    defaultValues: defaults,
  })
  const sourceType = initial?.sourceType ?? 'MANUAL'

  const create = useMutation({
    mutationFn: (values: TransferForm) =>
      api.post<{ code: string }>('/inventory/transfers', {
        from_warehouse_id: values.from_warehouse_id,
        to_warehouse_id: values.to_warehouse_id,
        transfer_date: values.transfer_date,
        reason: values.reason.trim(),
        source_type: sourceType,
        // The contract refuses a manual transfer that names a source and a
        // typed one that omits it, so the pair is sent together or not at all.
        ...(sourceType !== 'MANUAL' ? { source_id: initial?.sourceId ?? null } : {}),
        lines: values.lines.map((line) => ({
          item_id: line.item_id,
          quantity: line.quantity.trim(),
          ...(line.batch_id ? { batch_id: line.batch_id } : {}),
          ...(splitSerialNumbers(line.serial_numbers ?? '').length
            ? { serial_numbers: splitSerialNumbers(line.serial_numbers ?? '') }
            : {}),
          ...(line.from_location_id ? { from_location_id: line.from_location_id } : {}),
          ...(line.to_location_id ? { to_location_id: line.to_location_id } : {}),
          ...(initial?.sourceLineId && line.item_id === initial.itemId
            ? { source_line_id: initial.sourceLineId }
            : {}),
        })),
      }),
    onSuccess: (result) => {
      toast.success(success.created('Transfer'), result.code + ' is waiting for approval.')
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      form.reset()
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  const errors = form.formState.errors

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>New stock transfer</DialogTitle>
          <DialogDescription>
            Stock leaves the source warehouse when the transfer is dispatched and arrives when it is
            received. Nothing moves on creation.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Controller
                control={form.control}
                name="from_warehouse_id"
                render={({ field }) => (
                  <WarehouseSelect
                    id="transfer-from"
                    label="From warehouse"
                    value={field.value}
                    onChange={field.onChange}
                    invalid={Boolean(errors.from_warehouse_id)}
                  />
                )}
              />
              <FieldError message={errors.from_warehouse_id?.message} />
            </div>

            <div>
              <Controller
                control={form.control}
                name="to_warehouse_id"
                render={({ field }) => (
                  <WarehouseSelect
                    id="transfer-to"
                    label="To warehouse"
                    value={field.value}
                    onChange={field.onChange}
                    invalid={Boolean(errors.to_warehouse_id)}
                  />
                )}
              />
              <FieldError message={errors.to_warehouse_id?.message} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="transfer-date" required>
                Transfer date
              </Label>
              <Input
                id="transfer-date"
                type="date"
                aria-invalid={Boolean(errors.transfer_date)}
                {...form.register('transfer_date')}
              />
              <FieldError message={errors.transfer_date?.message} />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="transfer-reason" required>
              Reason
            </Label>
            <Textarea
              id="transfer-reason"
              placeholder="e.g. Rebalancing stock ahead of the September assembly plan"
              aria-invalid={Boolean(errors.reason)}
              {...form.register('reason')}
            />
            <FieldError message={errors.reason?.message} />
          </div>

          <LineEditor
            control={form.control}
            errors={errors}
            idPrefix="transfer"
            warehouseId={form.watch('from_warehouse_id')}
            toWarehouseId={form.watch('to_warehouse_id')}
            quantityLabel="Quantity"
          />

          <p className="text-xs text-muted-foreground">
            A serial-tracked item moves by naming its units on the line. The units stay attributed
            to the source warehouse while in transit and are relocated when the transfer is
            received.
          </p>

          <FieldError message={errors.root?.message} />
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            onClick={form.handleSubmit((values) => create.mutate(values))}
            loading={create.isPending}
          >
            Create transfer
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------- adjustment -----

const adjustmentSchema = z
  .object({
    warehouse_id: z.string().min(1, validation.choose('the warehouse being adjusted')),
    adjustment_date: z.string().min(1, validation.required('Adjustment date')),
    reason_code: z.string().min(1, 'Classify the cause of the adjustment.'),
    // Master rules 83: no stock change without a recorded reason. The code says
    // what kind of loss it was; this says what actually happened.
    reason: z.string().trim().min(10, 'Describe what happened in enough detail to audit later.'),
    /** Empty means unallocated: the write-off is charged to no cost centre. */
    cost_centre_id: z.string(),
    lines: z
      .array(
        z.object({
          item_id: z.string().min(1, validation.choose('an item')),
          batch_id: z.string().optional(),
          serial_numbers: z.string().optional(),
          location_id: z.string().optional(),
          unit_cost: z
            .string()
            .optional()
            .refine(
              (value) => !value || /^\d+(\.\d{1,6})?$/.test(value.trim()),
              'Enter a non-negative cost, to 6 decimal places.',
            ),
          quantity: z
            .string()
            .refine(
              isNonZeroQuantity,
              'Enter a signed quantity, e.g. -12 to write stock off or 12 to add it.',
            ),
        }),
      )
      .min(1, 'An adjustment needs at least one line.'),
  })
  .superRefine((values, ctx) => {
    values.lines.forEach((line, index) => {
      const serials = splitSerialNumbers(line.serial_numbers ?? '')
      if (serials.length === 0) return
      if (serials.length > SERIAL_BULK_MAX) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'serial_numbers'],
          message: `The API accepts at most ${SERIAL_BULK_MAX} serials at a time.`,
        })
        return
      }
      if (!toDecimal(line.quantity).abs().equals(serials.length)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'serial_numbers'],
          message: `Name one serial per unit moved - ${line.quantity || '0'} moved, ${serials.length} captured`,
        })
      }
    })
  })

type AdjustmentForm = z.infer<typeof adjustmentSchema>

export function CreateAdjustmentDialog({
  open,
  onOpenChange,
  adjustment,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  adjustment?: SchemaOf<'StockAdjustmentRead'>
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  // A storekeeper raising a write-off usually holds no `cost_centre.view`; the
  // adjustment is then raised unallocated rather than behind a refused picker.
  const canPickCostCentre = can(P.COST_CENTRE_VIEW)
  const canEditCost = can(P.INVENTORY_VALUATION_VIEW) || can(P.ITEM_COST_VIEW)
  const form = useForm<AdjustmentForm>({
    resolver: zodResolver(adjustmentSchema),
    defaultValues: {
      warehouse_id: '',
      adjustment_date: today(),
      reason_code: '',
      reason: '',
      cost_centre_id: '',
      lines: [{ item_id: '', quantity: '' }],
    },
  })
  const editingId = adjustment?.id
  useEffect(() => {
    if (open && adjustment)
      form.reset({
        warehouse_id: adjustment.warehouse_id,
        adjustment_date: adjustment.adjustment_date,
        reason_code: adjustment.reason_code,
        reason: adjustment.reason,
        cost_centre_id: adjustment.cost_centre_id ?? '',
        lines: (adjustment.lines ?? []).map((line) => ({
          item_id: line.item_id,
          quantity: line.quantity,
          batch_id: line.batch_id ?? '',
          location_id: line.location_id ?? '',
          unit_cost: line.unit_cost,
        })),
      })
    // Reset only when opening a different document; background data never replaces edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId])

  const create = useMutation({
    mutationFn: (values: AdjustmentForm) => {
      const payload = {
        warehouse_id: values.warehouse_id,
        adjustment_date: values.adjustment_date,
        reason_code: values.reason_code,
        reason: values.reason.trim(),
        // Who bears the write-off or the write-on. The movements it posts reach
        // the centre through the adjustment they reference.
        cost_centre_id: values.cost_centre_id || null,
        lines: values.lines.map((line) => ({
          item_id: line.item_id,
          quantity: line.quantity.trim(),
          ...(line.batch_id ? { batch_id: line.batch_id } : {}),
          ...(splitSerialNumbers(line.serial_numbers ?? '').length
            ? { serial_numbers: splitSerialNumbers(line.serial_numbers ?? '') }
            : {}),
          ...(line.location_id ? { location_id: line.location_id } : {}),
          ...(canEditCost && line.unit_cost ? { unit_cost: line.unit_cost.trim() } : {}),
        })),
        ...(adjustment ? { notes: adjustment.notes } : {}),
      }
      return adjustment
        ? api.patch<{ code: string }>(`/inventory/adjustments/${adjustment.id}`, payload)
        : api.post<{ code: string }>('/inventory/adjustments', payload)
    },
    onSuccess: (result) => {
      toast.success(
        adjustment ? success.updated('Adjustment') : 'Adjustment raised',
        result.code + ' is saved as a draft.',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      form.reset()
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  const errors = form.formState.errors

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {adjustment ? `Edit ${adjustment.code}` : 'New stock adjustment'}
          </DialogTitle>
          <DialogDescription>
            An adjustment changes stock without a document behind it, so it is approved separately
            and the reason is kept on the ledger forever.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Controller
                control={form.control}
                name="warehouse_id"
                render={({ field }) => (
                  <WarehouseSelect
                    id="adjustment-warehouse"
                    label="Warehouse"
                    value={field.value}
                    onChange={field.onChange}
                    invalid={Boolean(errors.warehouse_id)}
                  />
                )}
              />
              <FieldError message={errors.warehouse_id?.message} />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="adjustment-date" required>
                Adjustment date
              </Label>
              <Input
                id="adjustment-date"
                type="date"
                aria-invalid={Boolean(errors.adjustment_date)}
                {...form.register('adjustment_date')}
              />
              <FieldError message={errors.adjustment_date?.message} />
            </div>

            <div>
              <Controller
                control={form.control}
                name="reason_code"
                render={({ field }) => (
                  <div className="space-y-1.5">
                    <Label htmlFor="adjustment-reason-code" required>
                      Reason code
                    </Label>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="adjustment-reason-code"
                        aria-label="Reason code"
                        aria-invalid={Boolean(errors.reason_code)}
                      >
                        <SelectValue placeholder="Classify the cause" />
                      </SelectTrigger>
                      <SelectContent>
                        {ADJUSTMENT_REASON_CODES.map((code) => (
                          <SelectItem key={code} value={code}>
                            {humanise(code)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}
              />
              <FieldError message={errors.reason_code?.message} />
            </div>

            {canPickCostCentre && (
              <Controller
                control={form.control}
                name="cost_centre_id"
                render={({ field }) => (
                  <CostCentreSelect
                    id="adjustment-cost-centre"
                    value={field.value || null}
                    onChange={(next) => field.onChange(next ?? '')}
                    enabled={open}
                    hint="Who bears the loss, or the gain."
                    error={errors.cost_centre_id?.message}
                  />
                )}
              />
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="adjustment-reason" required>
              Reason
            </Label>
            <Textarea
              id="adjustment-reason"
              placeholder="e.g. Pallet damaged by a forklift in aisle B on 28 Aug; photographs filed with the QC report"
              aria-invalid={Boolean(errors.reason)}
              {...form.register('reason')}
            />
            <FieldError message={errors.reason?.message} />
          </div>

          <LineEditor
            control={form.control}
            errors={errors}
            idPrefix="adjustment"
            warehouseId={form.watch('warehouse_id')}
            canEditCost={canEditCost}
            quantityLabel="Quantity"
            quantityHint="Negative writes stock off, positive adds it."
            signed
          />
          {!canEditCost && (
            <p className="text-xs text-muted-foreground">
              Write-ups use the item's cost basis. Cost entry requires permission to view costs.
            </p>
          )}

          <FieldError message={errors.root?.message} />
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            onClick={form.handleSubmit((values) => create.mutate(values))}
            loading={create.isPending}
          >
            {adjustment ? 'Save changes' : 'Raise adjustment'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------ physical count -----

const physicalCountSchema = z.object({
  location_id: z.string().optional(),
  warehouse_id: z.string().min(1, validation.choose('the warehouse being counted')),
  count_date: z.string().min(1, validation.required('Count date')),
  // Required by the API, not optional decoration. It was missing entirely, so
  // every attempt to open a count sheet came back a validation error naming a
  // field the form did not have.
  reason: z.string().trim().min(5, 'Say why the count is being run.'),
  notes: z.string().trim().max(500, 'Keep the scope note under 500 characters.').optional(),
})

type PhysicalCountForm = z.infer<typeof physicalCountSchema>

export function CreatePhysicalCountDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const form = useForm<PhysicalCountForm>({
    resolver: zodResolver(physicalCountSchema),
    defaultValues: { warehouse_id: '', count_date: today(), reason: '', notes: '' },
  })

  const create = useMutation({
    mutationFn: (values: PhysicalCountForm) =>
      api.post<{ code: string }>('/inventory/physical-counts', {
        warehouse_id: values.warehouse_id,
        count_date: values.count_date,
        reason: values.reason.trim(),
        notes: values.notes?.trim() || null,
        ...(values.location_id ? { location_id: values.location_id } : {}),
      }),
    onSuccess: (result) => {
      toast.success(success.opened('Count sheet'), result.code + ' is ready to be counted.')
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      form.reset()
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  const errors = form.formState.errors

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New physical count</DialogTitle>
          <DialogDescription>
            Opening a count snapshots the warehouse&apos;s balances. Confirming it later posts the
            variance as an adjustment.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div>
            <Controller
              control={form.control}
              name="warehouse_id"
              render={({ field }) => (
                <WarehouseSelect
                  id="count-warehouse"
                  label="Warehouse"
                  value={field.value}
                  onChange={field.onChange}
                  invalid={Boolean(errors.warehouse_id)}
                />
              )}
            />
            <FieldError message={errors.warehouse_id?.message} />
          </div>
          <Controller
            control={form.control}
            name="location_id"
            render={({ field }) => (
              <LocationPicker
                id="count-bin"
                label="Bin (leave empty for the whole warehouse)"
                warehouseId={form.watch('warehouse_id')}
                value={field.value || null}
                onChange={(value) => field.onChange(value ?? '')}
              />
            )}
          />

          <div className="space-y-1.5">
            <Label htmlFor="count-date" required>
              Count date
            </Label>
            <Input
              id="count-date"
              type="date"
              aria-invalid={Boolean(errors.count_date)}
              {...form.register('count_date')}
            />
            <FieldError message={errors.count_date?.message} />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="count-reason" required>
              Reason
            </Label>
            <Textarea
              id="count-reason"
              placeholder="e.g. Quarterly cycle count for the finished-goods store"
              aria-invalid={Boolean(errors.reason)}
              {...form.register('reason')}
            />
            <FieldError message={errors.reason?.message} />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="count-notes">Scope note</Label>
            <Textarea
              id="count-notes"
              placeholder="e.g. Aisles A to C only, night shift"
              aria-invalid={Boolean(errors.notes)}
              {...form.register('notes')}
            />
            <FieldError message={errors.notes?.message} />
          </div>

          <FieldError message={errors.root?.message} />
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            onClick={form.handleSubmit((values) => create.mutate(values))}
            loading={create.isPending}
          >
            Open count sheet
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------- confirming a count -----

/**
 * How much of the sheet the preview reads: one page at the list endpoint's cap
 * (`LOOKUP_LIST_LIMIT`). A count sheet can be longer than that, so the per-line
 * breakdown is a sample. The headline figures stay the sheet's own
 * `variance_line_count` and `variance_value`, which the server totalled across
 * every line: a total summed from whatever fitted on one page would understate
 * a write-off, and understating a write-off is the one direction this dialog
 * must not err in.
 */
const COUNT_LINE_PREVIEW_LIMIT = LOOKUP_LIST_LIMIT

/**
 * Confirm a count sheet and post the variance it implies.
 *
 * This is the end of the count flow and there is no way back: CONFIRMED is
 * terminal in `COUNT_FLOW`, and what confirmation writes is ledger rows, which
 * are never soft-deleted (master rules 83). A mistake is corrected by a further
 * adjustment, not by undoing this. So the variance is put in front of the
 * operator *before* the question is asked, rather than described afterwards in
 * a toast.
 *
 * The line figures are measured against the snapshot taken when the sheet was
 * opened. Confirmation re-reads each position under the same row lock the
 * posting uses and moves `counted - current`, so a line whose stock legitimately
 * moved while the count was running posts a different amount - deliberately,
 * because posting against the snapshot would silently reverse every receipt and
 * issue made in the meantime. The dialog says so rather than presenting the
 * preview as the settled number.
 *
 * Gated by the caller on `P.INVENTORY_RECONCILE`, which is not the
 * `P.INVENTORY_PHYSICAL_COUNT` that opens and counts the sheet: counting stock
 * and accepting the count are separated on purpose.
 */
export function ConfirmPhysicalCountDialog({
  count,
  onClose,
}: {
  count: PhysicalCountRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()

  const params = { limit: COUNT_LINE_PREVIEW_LIMIT, with_total: true }
  const lines = useQuery({
    queryKey: ['inventory', 'physical-counts', count.id, 'lines', params],
    queryFn: () =>
      api.getPage<PhysicalCountLineRow>('/inventory/physical-counts/' + count.id + '/lines', {
        params,
      }),
    // What is about to be posted against live stock (master rules 34).
    staleTime: staleTimes.live,
  })

  const preview = useMemo(() => {
    // A line nobody counted carries a null counted quantity and a null variance;
    // `toDecimal(null)` is 0, so those drop out here - which matches the
    // service, where an uncounted line is skipped rather than written to zero.
    const varied = (lines.data?.data ?? []).filter(
      (line) => !toDecimal(line.variance_quantity).isZero(),
    )

    // decimal.js, not `+=` on a number: these are the two figures somebody signs
    // a write-off against.
    let short = toDecimal(0)
    let over = toDecimal(0)
    for (const line of varied) {
      const value = toDecimal(line.variance_value)
      if (toDecimal(line.variance_quantity).lessThan(0)) short = short.plus(value)
      else over = over.plus(value)
    }
    return { varied, short, over }
  }, [lines.data])

  const netVariance = toDecimal(count.variance_value)
  /** The sheet has more lines than one page, so the list below is a sample. */
  const sampled = lines.data?.pagination.has_next ?? false

  const confirm = useMutation({
    mutationFn: () =>
      api.post<PhysicalCountRow>('/inventory/physical-counts/' + count.id + '/confirm'),
    onSuccess: (result) => {
      // The posted figure, not the preview: the service restates the sheet's
      // variance from the ledger rows it actually wrote, which under FIFO is a
      // different number from quantity x snapshot average.
      toast.success(
        success.confirmed('Count'),
        result.code +
          ' posted ' +
          formatMoney(result.variance_value) +
          ' of variance across ' +
          formatNumber(result.variance_line_count) +
          ' line(s).',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      // Stock that just changed changes what can be promised from it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      onClose()
    },
  })

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      destructive
      title={'Confirm ' + count.code + '?'}
      description={
        <>
          The counted figures become the system quantities at{' '}
          {count.warehouse_code ?? 'this warehouse'}, and the difference posts to the stock ledger
          straight away. A ledger row cannot be deleted, so this cannot be undone - only corrected
          by a further adjustment.
        </>
      }
      confirmLabel="Confirm and post"
      // Blocked until the variance is on screen. Asking for an irreversible
      // posting while the figures are still loading is the thing this dialog
      // exists to prevent.
      loading={confirm.isPending || lines.isLoading}
      onConfirm={() => confirm.mutate()}
    >
      <div className="space-y-3">
        <div className="rounded-md border p-3">
          <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
            <div>
              <dt className="text-xs text-muted-foreground">Lines on the sheet</dt>
              <dd className="tabular text-sm font-medium">{formatNumber(count.line_count)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Lines differing</dt>
              <dd
                className={cn(
                  'tabular text-sm font-medium',
                  count.variance_line_count > 0 && 'text-warning',
                )}
              >
                {formatNumber(count.variance_line_count)}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-xs text-muted-foreground">Net variance to post</dt>
              <dd
                className={cn(
                  'tabular text-base font-semibold',
                  netVariance.greaterThan(0) && 'text-success',
                  netVariance.lessThan(0) && 'text-destructive',
                )}
              >
                {netVariance.greaterThan(0) ? '+' : ''}
                {formatMoney(count.variance_value)}
              </dd>
            </div>
          </dl>

          {preview.varied.length > 0 && (
            <p className="mt-2 border-t pt-2 text-xs text-muted-foreground">
              {formatMoney(preview.short.toFixed())} written off,{' '}
              {formatMoney(preview.over.toFixed())} written on
              {sampled ? ', across the lines listed below' : ''}.
            </p>
          )}
        </div>

        {lines.isLoading && (
          <p className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Reading the counted lines...
          </p>
        )}

        {lines.error && (
          <Alert variant="warning">
            <AlertTitle>The line-by-line breakdown could not be read</AlertTitle>
            <AlertDescription>
              {lines.error.message} The sheet totals above come from the count itself and still
              stand; only the detail behind them is missing.
            </AlertDescription>
          </Alert>
        )}

        {!lines.isLoading && !lines.error && preview.varied.length > 0 && (
          <div>
            <p className="mb-1.5 text-xs font-medium">
              What the posting moves ({formatNumber(preview.varied.length)}
              {sampled ? ' of ' + formatNumber(count.variance_line_count) : ''} line
              {preview.varied.length === 1 ? '' : 's'})
            </p>
            <ul className="max-h-56 space-y-1.5 overflow-y-auto pr-1">
              {preview.varied.map((line) => {
                const variance = toDecimal(line.variance_quantity)
                return (
                  <li
                    key={line.id}
                    className="flex items-start justify-between gap-3 rounded-md border p-2 text-xs"
                  >
                    <div className="min-w-0">
                      <p className="truncate font-mono">{line.item_code ?? line.item_id}</p>
                      <p className="truncate text-muted-foreground">
                        Book {formatQuantity(line.system_quantity)}, counted{' '}
                        {formatQuantity(line.counted_quantity)}
                      </p>
                    </div>
                    <div className="shrink-0 text-right">
                      <p
                        className={cn(
                          'tabular font-medium',
                          variance.greaterThan(0) && 'text-success',
                          variance.lessThan(0) && 'text-destructive',
                        )}
                      >
                        {variance.greaterThan(0) ? '+' : ''}
                        {formatQuantity(line.variance_quantity)}
                      </p>
                      <p className="tabular text-muted-foreground">
                        {formatMoney(line.variance_value)}
                      </p>
                    </div>
                  </li>
                )
              })}
            </ul>
          </div>
        )}

        {!lines.isLoading && !lines.error && preview.varied.length === 0 && (
          <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
            Nothing on the sheet differs from the book. Confirming still closes the count and posts
            whatever the re-read finds at the moment it runs.
          </p>
        )}

        <p className="text-xs text-muted-foreground">
          These figures are measured against the snapshot taken when the sheet was opened.
          Confirmation re-reads each position under a lock and posts counted minus current, so a
          line whose stock moved while the count was running posts a different amount - that is
          deliberate, and it is why confirming does not reverse a receipt or an issue made
          mid-count. A line nobody counted is left alone; it is not treated as zero stock.
        </p>
      </div>
    </ConfirmDialog>
  )
}

// ------------------------------------------------------ keying a count -----

/** What `PhysicalCountEntry.counted_quantity` accepts: nought or more, to 4 places. */
const COUNTED_QUANTITY_PATTERN = /^\d{1,14}(\.\d{1,4})?$/

/**
 * `PhysicalCountEntryRequest` takes at most this many entries per request
 * (`MAX_DOCUMENT_LINES` in inventory/schemas.py). Figures keyed across several
 * pages are held until saved, so the cap is enforced here rather than earned as
 * a 422 after the walk round the aisles.
 */
const MAX_ENTRIES_PER_SAVE = 500

/** What the counted-quantity cell needs from the dialog around it. */
interface CountEntryState {
  /** Figures keyed so far, by line id. */
  drafts: Record<string, string>
  /** A 422 the server addressed to a line, by line id. */
  errorsByLine: Record<string, string>
  setDraft: (lineId: string, value: string) => void
}

/**
 * Handed to the cell through a context rather than closed over in the column
 * definitions. `flexRender` mounts a function cell as a React component, so a
 * `cell` recreated whenever the drafts change would be a new component type
 * on every keystroke - React would remount it and the input would lose focus
 * after the first character. Module-level columns cannot go stale that way.
 */
const CountEntryContext = createContext<CountEntryState | null>(null)

function CountedQuantityCell({ line }: { line: PhysicalCountLineRow }) {
  const entry = useContext(CountEntryContext)
  if (!entry) return null
  const draft = entry.drafts[line.id]
  const value = draft ?? line.counted_quantity ?? ''
  const message =
    draft !== undefined && draft.trim() !== '' && !COUNTED_QUANTITY_PATTERN.test(draft.trim())
      ? 'A quantity of nought or more, to 4 decimal places.'
      : entry.errorsByLine[line.id]
  return (
    <div className="flex flex-col items-end">
      <Input
        inputMode="decimal"
        autoComplete="off"
        aria-label={'Counted quantity for ' + (line.item_code ?? line.item_id)}
        aria-invalid={Boolean(message)}
        value={value}
        onChange={(event) => entry.setDraft(line.id, event.target.value)}
        className="h-8 w-28 text-right tabular"
      />
      <FieldError message={message} />
    </div>
  )
}

const COUNT_ENTRY_COLUMNS: ColumnDef<PhysicalCountLineRow, unknown>[] = [
  {
    id: 'line_number',
    header: '#',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => <span className="text-muted-foreground">{row.original.line_number}</span>,
  },
  {
    id: 'item',
    header: 'Item',
    enableSorting: false,
    cell: ({ row }) => (
      <span>
        <span className="block font-mono text-xs">
          {row.original.item_code ?? row.original.item_id}
        </span>
        {row.original.item_name && (
          <span className="block max-w-[16rem] truncate text-xs text-muted-foreground">
            {row.original.item_name}
          </span>
        )}
      </span>
    ),
  },
  {
    id: 'system_quantity',
    header: 'Book',
    enableSorting: false,
    meta: { align: 'right' },
    cell: ({ row }) => formatQuantity(row.original.system_quantity),
  },
  {
    id: 'counted_quantity',
    header: 'Counted',
    enableSorting: false,
    enableHiding: false,
    meta: { align: 'right' },
    cell: ({ row }) => <CountedQuantityCell line={row.original} />,
  },
  {
    id: 'variance_quantity',
    header: 'Variance',
    enableSorting: false,
    meta: { align: 'right' },
    // The server's figure for a saved count, against the snapshot. Blank until
    // the line has been keyed and saved - never a guess computed here.
    cell: ({ row }) => {
      const line = row.original
      if (line.counted_quantity === null) {
        return <span className="text-muted-foreground">-</span>
      }
      const variance = toDecimal(line.variance_quantity)
      return (
        <span
          className={cn(
            variance.greaterThan(0) && 'text-success',
            variance.lessThan(0) && 'text-destructive',
          )}
        >
          {variance.greaterThan(0) ? '+' : ''}
          {formatQuantity(line.variance_quantity)}
        </span>
      )
    },
  },
]

/**
 * Key counted quantities onto an IN_PROGRESS sheet.
 *
 * This is the step that was missing: Start moved a sheet to IN_PROGRESS and
 * nothing on any screen could move it on, so the snapshot aged and the variance
 * could never be posted. The lines are read page by page - the endpoint is
 * paginated and a sheet may run to `MAX_COUNT_LINES` - and a figure typed on
 * one page survives a visit to the next, keyed by line id.
 *
 * Saving is partial by design: `enter_counts` accepts any subset and only moves
 * the sheet to COUNTED once no line is left uncounted, so a counter can save
 * after every aisle. A line left blank is left uncounted, never written to zero.
 *
 * Quantities are posted as decimal strings. Gated by the caller on
 * `P.INVENTORY_PHYSICAL_COUNT`, which is what `POST .../entries` enforces.
 */
export function EnterCountsDialog({
  count,
  onClose,
}: {
  count: PhysicalCountRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState<number>(DEFAULT_PAGE_SIZE)
  /** Figures keyed so far, by line id. Only a typed value is ever sent. */
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  const params = { page, limit, with_total: true }
  const lines = useQuery({
    queryKey: ['inventory', 'physical-counts', count.id, 'lines', params],
    queryFn: () =>
      api.getPage<PhysicalCountLineRow>('/inventory/physical-counts/' + count.id + '/lines', {
        params,
      }),
    placeholderData: keepPreviousData,
    // A figure a colleague saved a moment ago must show (master rules 34).
    staleTime: staleTimes.live,
  })

  const save = useMutation({
    mutationFn: (entries: { line_id: string; counted_quantity: string }[]) =>
      api.post<PhysicalCountRow>('/inventory/physical-counts/' + count.id + '/entries', {
        entries,
      }),
    onSuccess: (result, entries) => {
      toast.success(
        success.saved('Counts'),
        formatNumber(entries.length) +
          ' line(s) recorded on ' +
          result.code +
          (result.status === 'COUNTED'
            ? '. Every line is counted, so the sheet is ready to confirm.'
            : '.'),
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      setDrafts({})
      // Nothing is left to key once the sheet is COUNTED; otherwise it stays
      // open for the next aisle.
      if (result.status === 'COUNTED') onClose()
    },
  })

  const pending = useMemo(
    () => Object.entries(drafts).filter(([, value]) => value.trim() !== ''),
    [drafts],
  )
  const malformed = pending.filter(([, value]) => !COUNTED_QUANTITY_PATTERN.test(value.trim()))
  const overCap = pending.length > MAX_ENTRIES_PER_SAVE

  /**
   * A 422 addresses an entry by its position in the request
   * (`entries.3.counted_quantity`). The request was built from `pending` in
   * order, so the position maps back to the line it came from; anything the
   * server says about the request as a whole lands under the table instead.
   */
  const serverErrors = useMemo(() => {
    const byLine: Record<string, string> = {}
    const general: string[] = []
    if (save.error instanceof ApiError && save.variables) {
      for (const [path, messages] of Object.entries(save.error.fieldErrors)) {
        const position = /^entries\.(\d+)/.exec(path)
        const lineId = position ? save.variables[Number(position[1])]?.line_id : undefined
        if (lineId) byLine[lineId] = joinValidationMessages(messages)
        else general.push(...messages)
      }
    }
    return { byLine, general: general.length > 0 ? joinValidationMessages(general) : undefined }
  }, [save.error, save.variables])

  const hasServerError = save.error !== null
  const resetSave = save.reset
  const setDraft = useCallback(
    (lineId: string, value: string) => {
      setDrafts((current) => ({ ...current, [lineId]: value }))
      // A corrected figure clears the refusal it earned.
      if (hasServerError) resetSave()
    },
    [hasServerError, resetSave],
  )

  const entryState = useMemo<CountEntryState>(
    () => ({ drafts, errorsByLine: serverErrors.byLine, setDraft }),
    [drafts, serverErrors, setDraft],
  )

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Key counts for {count.code}</DialogTitle>
          <DialogDescription>
            What was found on the shelf at {count.warehouse_code ?? 'this warehouse'}, line by line.
            Save as often as you like: a line left blank stays uncounted, and the sheet moves on to
            counted only once every line has a figure.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-3">
          <CountEntryContext.Provider value={entryState}>
            <DataTable
              columns={COUNT_ENTRY_COLUMNS}
              data={lines.data?.data ?? []}
              pagination={lines.data?.pagination}
              isLoading={lines.isLoading}
              isFetching={lines.isFetching}
              error={lines.error}
              onPageChange={setPage}
              onLimitChange={(next) => {
                setLimit(next)
                setPage(1)
              }}
              onRefresh={() => void lines.refetch()}
              getRowId={(row) => row.id}
              emptyTitle="No lines on this sheet"
              emptyDescription="The scope held no positions to count when the sheet was opened."
            />
          </CountEntryContext.Provider>

          <FieldError message={serverErrors.general} />

          {overCap && (
            <FieldError
              message={
                'Save in batches: at most ' +
                formatNumber(MAX_ENTRIES_PER_SAVE) +
                ' lines can be recorded at once, and ' +
                formatNumber(pending.length) +
                ' are keyed.'
              }
            />
          )}

          <p className="text-xs text-muted-foreground" role="status">
            {pending.length === 0
              ? 'Nothing keyed yet.'
              : formatNumber(pending.length) + ' figure(s) keyed and not yet saved.'}
          </p>
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>
            Close
          </Button>
          <Button
            onClick={() =>
              save.mutate(
                pending.map(([line_id, value]) => ({ line_id, counted_quantity: value.trim() })),
              )
            }
            loading={save.isPending}
            disabled={pending.length === 0 || malformed.length > 0 || overCap}
          >
            Save counts
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

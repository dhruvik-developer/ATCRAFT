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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Plus, Trash2 } from 'lucide-react'
import { useMemo } from 'react'
import { Controller, useFieldArray, useForm, type Control, type FieldErrors } from 'react-hook-form'
import { z } from 'zod'

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
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
  toDateInput,
  toDecimal,
} from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import { ItemPicker, WarehouseSelect } from './pickers'
import {
  ADJUSTMENT_REASON_CODES,
  type PhysicalCountLineRow,
  type PhysicalCountRow,
} from './types'

/**
 * What a stock transfer may carry. See docs/assumptions.md 25: a serial-tracked
 * item is refused at create, not at dispatch, so the picker must not offer one.
 *
 * Batch-tracked items are still offered even though the API requires the line to
 * name its batch: hiding them would be wrong - they are transferable - and the
 * missing control is a batch picker on the line, noted rather than faked.
 */
const TRANSFERABLE_ITEM_FILTERS = { is_serial_tracked: false }

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
  lines: { item_id: string; quantity: string }[]
}

/**
 * The item/quantity grid shared by transfers and adjustments. Labels are shown
 * on the first row and kept for screen readers on the rest, so the grid reads as
 * a table without repeating "Item" down the page.
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
}) {
  // `useFieldArray` is generic over the form shape; the cast narrows T to the
  // part this component actually edits.
  const { fields, append, remove } = useFieldArray({
    control: control as unknown as Control<LineFields>,
    name: 'lines',
  })

  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-sm font-medium">Lines</legend>

      {fields.map((field, index) => (
        <div
          key={field.id}
          className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_10rem_auto] sm:items-end"
        >
          <Controller
            control={control as unknown as Control<LineFields>}
            name={`lines.${index}.item_id`}
            render={({ field: itemField }) => (
              <ItemPicker
                id={idPrefix + '-item-' + index}
                label={'Item, line ' + (index + 1)}
                hideLabel={index > 0}
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
                  className={index > 0 ? 'sr-only' : undefined}
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
        </div>
      ))}

      <FieldError message={errors.lines?.message} />

      <div className="flex items-center justify-between gap-3">
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

const transferSchema = z
  .object({
    from_warehouse_id: z.string().min(1, 'Choose the warehouse the stock leaves.'),
    to_warehouse_id: z.string().min(1, 'Choose the warehouse the stock arrives at.'),
    transfer_date: z.string().min(1, 'A transfer date is required.'),
    reason: z.string().trim().min(5, 'Say why the stock is moving.'),
    lines: z
      .array(
        z.object({
          item_id: z.string().min(1, 'Choose an item.'),
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

type TransferForm = z.infer<typeof transferSchema>

export function CreateTransferDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const form = useForm<TransferForm>({
    resolver: zodResolver(transferSchema),
    defaultValues: {
      from_warehouse_id: '',
      to_warehouse_id: '',
      transfer_date: today(),
      reason: '',
      lines: [{ item_id: '', quantity: '' }],
    },
  })

  const create = useMutation({
    mutationFn: (values: TransferForm) =>
      api.post<{ code: string }>('/inventory/transfers', {
        from_warehouse_id: values.from_warehouse_id,
        to_warehouse_id: values.to_warehouse_id,
        transfer_date: values.transfer_date,
        reason: values.reason.trim(),
        lines: values.lines.map((line) => ({
          item_id: line.item_id,
          quantity: line.quantity.trim(),
        })),
      }),
    onSuccess: (result) => {
      toast.success('Transfer created', result.code + ' is waiting for approval.')
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
            quantityLabel="Quantity"
            // docs/assumptions.md 25: a stock transfer refuses a serial-tracked
            // item outright, because `stock_transfer_lines` has nowhere to name
            // the units being shipped. The search is narrowed server-side so the
            // refusal is never reached, rather than offering a choice that
            // always comes back an error at submit.
            itemFilters={TRANSFERABLE_ITEM_FILTERS}
            itemEmptyMessage="No transferable item matches that search. Serial-tracked items cannot move on a transfer."
          />

          <p className="text-xs text-muted-foreground">
            Serial-tracked items are not listed: a transfer line carries no unit list, so there is
            nowhere to record which units shipped. Move them with a goods issue and a receipt
            instead.
          </p>
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

const adjustmentSchema = z.object({
  warehouse_id: z.string().min(1, 'Choose the warehouse being adjusted.'),
  adjustment_date: z.string().min(1, 'An adjustment date is required.'),
  reason_code: z.string().min(1, 'Classify the cause of the adjustment.'),
  // Master rules 83: no stock change without a recorded reason. The code says
  // what kind of loss it was; this says what actually happened.
  reason: z.string().trim().min(10, 'Describe what happened in enough detail to audit later.'),
  lines: z
    .array(
      z.object({
        item_id: z.string().min(1, 'Choose an item.'),
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

type AdjustmentForm = z.infer<typeof adjustmentSchema>

export function CreateAdjustmentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const form = useForm<AdjustmentForm>({
    resolver: zodResolver(adjustmentSchema),
    defaultValues: {
      warehouse_id: '',
      adjustment_date: today(),
      reason_code: '',
      reason: '',
      lines: [{ item_id: '', quantity: '' }],
    },
  })

  const create = useMutation({
    mutationFn: (values: AdjustmentForm) =>
      api.post<{ code: string }>('/inventory/adjustments', {
        warehouse_id: values.warehouse_id,
        adjustment_date: values.adjustment_date,
        reason_code: values.reason_code,
        reason: values.reason.trim(),
        lines: values.lines.map((line) => ({
          item_id: line.item_id,
          quantity: line.quantity.trim(),
        })),
      }),
    onSuccess: (result) => {
      toast.success('Adjustment raised', result.code + ' has been created as a draft.')
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
          <DialogTitle>New stock adjustment</DialogTitle>
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
            quantityLabel="Quantity"
            quantityHint="Negative writes stock off, positive adds it."
            signed
          />
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            onClick={form.handleSubmit((values) => create.mutate(values))}
            loading={create.isPending}
          >
            Raise adjustment
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------ physical count -----

const physicalCountSchema = z.object({
  warehouse_id: z.string().min(1, 'Choose the warehouse being counted.'),
  count_date: z.string().min(1, 'A count date is required.'),
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
      }),
    onSuccess: (result) => {
      toast.success('Count sheet opened', result.code + ' is ready to be counted.')
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
 * How much of the sheet the preview reads.
 *
 * `limit` is capped at 200 by the list endpoint and a count sheet can be longer
 * than that, so the per-line breakdown is a sample. The headline figures stay
 * the sheet's own `variance_line_count` and `variance_value`, which the server
 * totalled across every line: a total summed from whatever fitted on one page
 * would understate a write-off, and understating a write-off is the one
 * direction this dialog must not err in.
 */
const COUNT_LINE_PREVIEW_LIMIT = 200

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
        'Count confirmed',
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
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
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
            <div className="col-span-2">
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

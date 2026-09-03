/**
 * The write paths of the production module: raising an order, issuing material
 * against it, taking the unused balance back, and booking what came off the
 * machine.
 *
 * All of them keep decimals as strings the whole way to the wire. The API is the
 * authority on what an issue costs and on how much of the order is now done;
 * nothing here recomputes a stock figure and posts it back.
 *
 * Issue and return are deliberately two dialogs rather than one with a sign.
 * They are two endpoints, two permissions and two different judgements - the
 * quantity a storekeeper reaches for is the outstanding requirement one way and
 * what the job is still holding the other - and a single form with a direction
 * toggle is how a return gets keyed as an issue.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
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
import { Alert, AlertDescription, Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatQuantity, humanise, toDateInput, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import {
  PRIORITY_BANDS,
  PRIORITY_DEFAULT,
  outstandingQuantity,
  returnableQuantity,
  useMachineOptions,
  useOperatorOptions,
  type MaterialIssueDetail,
  type MaterialIssuePostResult,
  type ProductionComponent,
  type ProductionEntryResult,
  type ProductionOrderRow,
} from './production-model'

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const WHOLE = /^\d{1,6}$/
const OPTIONAL_QUANTITY = /^(\d{1,14}(\.\d{1,4})?)?$/
/** Minutes are a measured value on the wire, so decimals are legitimate. */
const MINUTES = /^(\d{1,6}(\.\d{1,4})?)?$/

/** Radix Select forbids an empty option value, so "not chosen" needs a sentinel. */
const NONE = '__none'

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())
/** Decimals stay strings all the way to the wire - never parsed into a double. */
const asDecimal = (value: string): string => (value.trim() === '' ? '0' : value.trim())

function today(): string {
  return toDateInput(new Date())
}

// ------------------------------------------------- raise a production order --

const orderSchema = z
  .object({
    item_id: z.string().min(1, 'Choose the item to be made'),
    bom_id: z.string(),
    planned_quantity: z
      .string()
      .trim()
      .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
      .refine((value) => toDecimal(value).greaterThan(0), 'Quantity must be more than zero'),
    planned_start_date: z.string().min(1, 'A planned start is required'),
    // Required by the API, not optional: `ProductionOrderCreate` names both
    // dates and the output warehouse without defaults.
    planned_end_date: z.string().min(1, 'A planned end is required'),
    // A plain integer, lower being more urgent. There is no priority enum.
    priority: z.string().trim().regex(WHOLE, 'Priority is a whole number'),
    warehouse_id: z.string().min(1, 'Choose the warehouse the output is received into'),
    notes: z.string().trim().max(500, 'At most 500 characters'),
  })
  // ISO dates compare correctly as strings, so no Date object is needed here.
  .refine((values) => values.planned_end_date >= values.planned_start_date, {
    message: 'The planned end cannot be before the planned start',
    path: ['planned_end_date'],
  })

type OrderFormValues = z.infer<typeof orderSchema>

interface BomChoice {
  id: string
  code: string
  version: number
  is_default: boolean
}

function emptyOrder(): OrderFormValues {
  return {
    item_id: '',
    bom_id: '',
    planned_quantity: '1',
    planned_start_date: today(),
    planned_end_date: today(),
    priority: String(PRIORITY_DEFAULT),
    warehouse_id: '',
    notes: '',
  }
}

export function ProductionOrderFormDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<OrderFormValues>({ resolver: zodResolver(orderSchema), defaultValues: emptyOrder() })

  useEffect(() => {
    if (open) reset(emptyOrder())
  }, [open, reset])

  const itemId = watch('item_id')

  // Only approved BOMs may drive an order, so the list asks for those. Leaving
  // the choice empty lets the API pick the item's default BOM, which is what a
  // planner wants almost every time.
  const bomParams = {
    item_id: itemId,
    status: 'APPROVED',
    limit: 50,
    with_total: false,
    sort_by: 'version',
    sort_dir: 'desc',
  }
  const boms = useQuery({
    queryKey: queryKeys.bom.list(bomParams),
    queryFn: () => api.getPage<BomChoice>('/bom', { params: bomParams }),
    enabled: open && Boolean(itemId),
    staleTime: staleTimes.master,
  })
  const bomRows = boms.data?.data ?? []

  const create = useMutation({
    mutationFn: (values: OrderFormValues) =>
      api.post<ProductionOrderRow>('/production', {
        item_id: values.item_id,
        bom_id: orNull(values.bom_id),
        planned_quantity: asDecimal(values.planned_quantity),
        // The API takes both dates and the warehouse as required values; the
        // form now insists on them rather than sending null and earning a 422.
        planned_start_date: values.planned_start_date,
        planned_end_date: values.planned_end_date,
        priority: Number.parseInt(values.priority, 10),
        warehouse_id: values.warehouse_id,
        notes: orNull(values.notes),
      }),
    onSuccess: (order) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      toast.success('Production order raised', `${order.code} is a draft awaiting approval.`)
      onOpenChange(false)
      navigate(`/production/${order.id}`)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Raise a production order</DialogTitle>
          <DialogDescription>
            The order starts as a draft. Approving and releasing it is what reserves components and
            makes it visible to the shop floor.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => create.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <Controller
                  control={control}
                  name="item_id"
                  render={({ field }) => (
                    <ItemPicker
                      id="production-item"
                      label="Item to make"
                      value={field.value || null}
                      onChange={(value) => {
                        field.onChange(value ?? '')
                        // A BOM belongs to one item; keeping the old choice
                        // would silently build the wrong thing.
                        setValue('bom_id', '')
                      }}
                      placeholder="Search the item master"
                      invalid={Boolean(errors.item_id)}
                    />
                  )}
                />
                <FieldError message={errors.item_id?.message} />
              </div>

              <div>
                <Label htmlFor="production-quantity" required>
                  Planned quantity
                </Label>
                <Input
                  id="production-quantity"
                  {...register('planned_quantity')}
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.planned_quantity)}
                />
                <FieldError message={errors.planned_quantity?.message} />
              </div>

              <div>
                <Label htmlFor="production-bom">Bill of materials</Label>
                <Controller
                  control={control}
                  name="bom_id"
                  render={({ field }) => (
                    <Select
                      value={field.value || NONE}
                      onValueChange={(value) => field.onChange(value === NONE ? '' : value)}
                      disabled={!itemId || bomRows.length === 0}
                    >
                      <SelectTrigger
                        id="production-bom"
                        className="mt-1.5"
                        aria-label="Bill of materials"
                      >
                        <SelectValue
                          placeholder={boms.isLoading ? 'Loading...' : "The item's default BOM"}
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NONE}>The item&apos;s default BOM</SelectItem>
                        {bomRows.map((bom) => (
                          <SelectItem key={bom.id} value={bom.id}>
                            {bom.code} - v{bom.version}
                            {bom.is_default ? ' (default)' : ''}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  {itemId && !boms.isLoading && bomRows.length === 0
                    ? 'This item has no approved BOM. The order can still be raised, but it will carry no components.'
                    : 'Only approved BOMs can drive an order.'}
                </p>
              </div>

              <div>
                <Label htmlFor="production-start" required>
                  Planned start
                </Label>
                <Input
                  id="production-start"
                  type="date"
                  {...register('planned_start_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.planned_start_date)}
                />
                <FieldError message={errors.planned_start_date?.message} />
              </div>

              <div>
                <Label htmlFor="production-end" required>
                  Planned end
                </Label>
                <Input
                  id="production-end"
                  type="date"
                  {...register('planned_end_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.planned_end_date)}
                />
                <FieldError message={errors.planned_end_date?.message} />
              </div>

              <div>
                <Label htmlFor="production-priority">Priority</Label>
                {/* An integer on the wire, lower being more urgent. The named
                    bands are presets over that number, not a separate vocabulary
                    - a planner who wants 35 can type 35. */}
                <Controller
                  control={control}
                  name="priority"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="production-priority"
                        className="mt-1.5"
                        aria-label="Priority"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {PRIORITY_BANDS.map((band) => (
                          <SelectItem key={band.value} value={String(band.value)}>
                            {band.label} ({band.value})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Lower runs first. The default is {PRIORITY_DEFAULT}.
                </p>
                <FieldError message={errors.priority?.message} />
              </div>

              <div>
                <Controller
                  control={control}
                  name="warehouse_id"
                  render={({ field }) => (
                    <WarehouseSelect
                      id="production-warehouse"
                      label="Receive output into"
                      value={field.value}
                      onChange={field.onChange}
                    />
                  )}
                />
                <FieldError message={errors.warehouse_id?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="production-notes">Notes</Label>
                <Textarea id="production-notes" {...register('notes')} className="mt-1.5" />
                <FieldError message={errors.notes?.message} />
              </div>
            </div>
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Raise order
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------ issue material --

const issueSchema = z.object({
  warehouse_id: z.string(),
  reason: z
    .string()
    .trim()
    // `MaterialIssueCreate.reason` is min 3 / max 255. A shorter one is a 422,
    // not a shrug.
    .min(3, 'Every stock movement is recorded with a reason of at least 3 characters')
    .max(255, 'At most 255 characters'),
  lines: z.array(
    z.object({
      component_id: z.string(),
      // The API keys a line by its own number, not by the item: a kit list can
      // legitimately carry the same part on two lines.
      line_number: z.number(),
      label: z.string(),
      quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    }),
  ),
})

type IssueFormValues = z.infer<typeof issueSchema>

function issueDefaults(components: ProductionComponent[]): IssueFormValues {
  return {
    warehouse_id: '',
    reason: 'Material issue to production order',
    lines: components.map((component) => ({
      component_id: component.id,
      line_number: component.line_number,
      label: component.item_code,
      // Pre-filled with what is still outstanding: the common case is issuing
      // the balance of the requirement, and retyping it invites a slip.
      quantity: outstandingQuantity(component).toFixed(),
    })),
  }
}

export function IssueMaterialDialog({
  open,
  onOpenChange,
  orderId,
  orderCode,
  components,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  orderCode: string
  components: ProductionComponent[]
}) {
  const queryClient = useQueryClient()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<IssueFormValues>({
    resolver: zodResolver(issueSchema),
    defaultValues: issueDefaults(components),
  })

  const { fields } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (open) {
      reset(issueDefaults(components))
      setStrandedDraft(null)
    }
    // Components are re-read whenever the dialog opens, so the outstanding
    // quantities are never a snapshot from an earlier visit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reset])

  /**
   * Set when the picking document was created but the posting that moves the
   * stock then failed. The draft is a real document at that point and has to be
   * named, or a storekeeper posts the same kit twice.
   */
  const [strandedDraft, setStrandedDraft] = useState<string | null>(null)

  const issue = useMutation({
    mutationFn: async (values: IssueFormValues) => {
      setStrandedDraft(null)
      // Two calls, because issuing material is two steps and the first one moves
      // nothing: `/material-issues` only "creates the picking document. Nothing
      // moves until it is posted", which is what lets a storekeeper review the
      // kit. Stopping after it left the dialog claiming stock had left the store
      // while the ledger was untouched.
      const draft = await api.post<MaterialIssueDetail>(
        `/production/${orderId}/material-issues`,
        {
          warehouse_id: orNull(values.warehouse_id),
          reason: values.reason.trim(),
          lines: values.lines
            .filter((line) => toDecimal(line.quantity).greaterThan(0))
            .map((line) => ({
              component_id: line.component_id,
              line_number: line.line_number,
              quantity: asDecimal(line.quantity),
            })),
        },
      )

      try {
        // The body is required even though there is nothing to say in it.
        // `allow_partial_issue` is deliberately left at its default: advancing
        // an order over an incomplete kit needs `production_order.approve` and
        // is a supervisor's decision, not a side effect of this dialog.
        return await api.post<MaterialIssuePostResult>(
          `/production/material-issues/${draft.id}/post`,
          {},
        )
      } catch (error) {
        setStrandedDraft(draft.code)
        throw error
      }
    },
    onSuccess: (result) => {
      // An issue moves stock and consumes reservations, so every cache that
      // reads a balance is dropped alongside the order itself.
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      // The API says what the posting actually wrote and where the order stands
      // afterwards; a fixed "Material issued" would be a claim the screen has no
      // grounds for on a partial kit.
      toast.success(
        `Material issued on ${orderCode}`,
        `${result.issue.code} posted ${formatQuantity(result.issue.total_quantity)} across ${
          result.issue.line_count
        } line(s). The order is now ${humanise(result.order_status).toLowerCase()}.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Issue material to {orderCode}</DialogTitle>
          <DialogDescription>
            The picking document is drafted and posted in one go, so stock really does leave the
            store here. Quantities are pre-filled with what is still outstanding; clear a line to
            leave it for later.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            // Checked here rather than in the schema: a resolver error on the
            // `lines` array itself lands under a key the field rows do not
            // render, so the operator would see a form that refuses to submit
            // with nothing explaining why.
            if (!values.lines.some((line) => toDecimal(line.quantity).greaterThan(0))) {
              setError('root', {
                type: 'manual',
                message: 'Enter a quantity against at least one component',
              })
              return
            }
            issue.mutate(values)
          })}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            {strandedDraft && (
              <Alert variant="warning">
                <AlertDescription>
                  Picking document {strandedDraft} was created, but posting it failed and no stock
                  moved. Post or cancel {strandedDraft} from the material issues register rather
                  than submitting this form again, or the same kit is drafted twice.
                </AlertDescription>
              </Alert>
            )}

            <Controller
              control={control}
              name="warehouse_id"
              render={({ field }) => (
                <WarehouseSelect
                  id="issue-warehouse"
                  label="Issue from"
                  value={field.value}
                  onChange={field.onChange}
                  includeAll
                  allLabel="The component's own warehouse"
                />
              )}
            />

            <div className="space-y-2">
              <p className="text-sm font-medium">Components</p>
              {fields.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  This order carries no components, so there is nothing to issue.
                </p>
              )}
              {fields.map((field, index) => {
                const component = components[index]
                // Only judged when the API actually reported free stock: an
                // absent figure is unknown, and treating it as zero would paint
                // every line as short.
                const shortOfStock =
                  component?.available_quantity !== undefined &&
                  outstandingQuantity(component).greaterThan(
                    toDecimal(component.available_quantity),
                  )
                return (
                  <div
                    key={field.id}
                    className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-[1fr_10rem]"
                  >
                    <div className="min-w-0">
                      <Label htmlFor={`issue-line-${index}`} className="block truncate">
                        {field.label}
                      </Label>
                      {component && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          Required {formatQuantity(component.required_quantity)}
                          {' - issued '}
                          {formatQuantity(component.net_issued_quantity)}
                          {' - free stock '}
                          <span className={shortOfStock ? 'font-medium text-warning' : undefined}>
                            {component.available_quantity === undefined
                              ? 'not reported'
                              : formatQuantity(component.available_quantity)}
                          </span>
                        </p>
                      )}
                    </div>
                    <div>
                      <Input
                        id={`issue-line-${index}`}
                        {...register(`lines.${index}.quantity`)}
                        inputMode="decimal"
                        className="text-right tabular"
                        aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                      />
                      <FieldError message={errors.lines?.[index]?.quantity?.message} />
                    </div>
                  </div>
                )
              })}
              <FieldError message={errors.root?.message} />
            </div>

            <div>
              <Label htmlFor="issue-reason" required>
                Reason
              </Label>
              <Input
                id="issue-reason"
                {...register('reason')}
                autoComplete="off"
                className="mt-1.5"
                aria-invalid={Boolean(errors.reason)}
              />
              <FieldError message={errors.reason?.message} />
              <p className="mt-1 text-xs text-muted-foreground">
                Recorded on every ledger row this issue writes.
              </p>
            </div>
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={issue.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={issue.isPending} disabled={fields.length === 0}>
              Issue material
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------- return material --

/**
 * Handing the unused balance back to the store.
 *
 * The mirror of the issue above, and the reason it exists: a job that is still
 * holding material cannot be cancelled at all, and completing one strands the
 * leftovers on the floor. This is the document that puts them back.
 *
 * It is a different right from issuing - `material_return.create` - because
 * taking stock off the shelf and putting it back are different decisions, and
 * the API checks the return right on both the draft and the posting.
 */
const returnSchema = z.object({
  warehouse_id: z.string(),
  reason: z
    .string()
    .trim()
    // The same `MaterialIssueCreate` body the issue posts, so the same
    // min 3 / max 255. A shorter one is a 422.
    .min(3, 'Every stock movement is recorded with a reason of at least 3 characters')
    .max(255, 'At most 255 characters'),
  lines: z.array(
    z
      .object({
        component_id: z.string(),
        // Keyed by line number like the issue: a kit list can legitimately
        // carry the same part twice.
        line_number: z.number(),
        label: z.string(),
        /** This line's own ceiling, carried so the rule can name the row. */
        returnable: z.string(),
        quantity: z
          .string()
          .trim()
          .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
      })
      .superRefine((line, ctx) => {
        // Held here rather than left to the API. The service refuses the whole
        // document on the first over-return, so one mistyped row would throw
        // away every other line the storekeeper had just counted.
        if (toDecimal(line.quantity).greaterThan(toDecimal(line.returnable))) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: `At most ${formatQuantity(line.returnable)} can be handed back`,
            path: ['quantity'],
          })
        }
      }),
  ),
})

type ReturnFormValues = z.infer<typeof returnSchema>

function returnDefaults(holding: ProductionComponent[]): ReturnFormValues {
  return {
    warehouse_id: '',
    reason: 'Unused material returned to store',
    lines: holding.map((component) => ({
      component_id: component.id,
      line_number: component.line_number,
      label: component.item_code,
      returnable: returnableQuantity(component).toFixed(),
      // Pre-filled with the whole unused balance, because a return is normally
      // the end-of-job sweep of everything left over. Clear a line to leave that
      // component out on the floor.
      quantity: returnableQuantity(component).toFixed(),
    })),
  }
}

export function ReturnMaterialDialog({
  open,
  onOpenChange,
  orderId,
  orderCode,
  components,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  orderCode: string
  components: ProductionComponent[]
}) {
  const queryClient = useQueryClient()

  // Only components the job is actually holding something of. A row that can
  // return nothing is a row that can only be got wrong, and the same filtered
  // list backs both the defaults and the rendered rows, so an index into it can
  // never drift from the field array.
  const holding = useMemo(
    () => components.filter((component) => returnableQuantity(component).greaterThan(0)),
    [components],
  )

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<ReturnFormValues>({
    resolver: zodResolver(returnSchema),
    defaultValues: returnDefaults(holding),
  })

  const { fields } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (open) {
      reset(returnDefaults(holding))
      setStrandedDraft(null)
      setPending(null)
    }
    // The holding is re-read whenever the dialog opens, so the balances are
    // never a snapshot from an earlier visit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reset])

  /**
   * Set when the return document was created but the posting that moves the
   * stock then failed - the same trap the issue has. The draft is a real
   * document at that point and has to be named, or the same material is put
   * back twice.
   */
  const [strandedDraft, setStrandedDraft] = useState<string | null>(null)

  /**
   * The validated form, held while the confirmation is up. A return writes a
   * ledger row and a ledger row is never deleted, so the last step is a plain
   * statement of what is about to move rather than a submit button that posts.
   */
  const [pending, setPending] = useState<ReturnFormValues | null>(null)

  const pendingLines = pending
    ? pending.lines.filter((line) => toDecimal(line.quantity).greaterThan(0))
    : []
  // decimal.js throughout; the total is only ever displayed, never sent back.
  const pendingTotal = pendingLines.reduce(
    (sum, line) => sum.plus(toDecimal(line.quantity)),
    toDecimal(0),
  )

  const postReturn = useMutation({
    mutationFn: async (values: ReturnFormValues) => {
      setStrandedDraft(null)
      // Two calls, for the same reason the issue takes two: `/material-returns`
      // only drafts the document - it "puts unused material back on the shelf
      // when it is posted" - and the stock moves at
      // `/material-issues/{id}/post`, the one endpoint that posts both
      // directions and asks for the right matching the document it loads.
      const payload: SchemaOf<'MaterialIssueCreate'> = {
        warehouse_id: orNull(values.warehouse_id),
        reason: values.reason.trim(),
        lines: values.lines
          .filter((line) => toDecimal(line.quantity).greaterThan(0))
          .map((line) => ({
            component_id: line.component_id,
            line_number: line.line_number,
            quantity: asDecimal(line.quantity),
          })),
      }
      const draft = await api.post<MaterialIssueDetail>(
        `/production/${orderId}/material-returns`,
        payload,
      )

      try {
        // The body is required even though a return has nothing to say in it:
        // `allow_partial_issue` governs whether an *issue* advances the order
        // and is ignored in this direction.
        return await api.post<MaterialIssuePostResult>(
          `/production/material-issues/${draft.id}/post`,
          {},
        )
      } catch (error) {
        setStrandedDraft(draft.code)
        throw error
      }
    },
    onSuccess: (result) => {
      // The material is back in free stock, so the order, the balances and what
      // can be promised from them all move together. Reservations themselves do
      // not - a return posts a movement and does not re-reserve - but
      // availability does, and that is what the allocation screens read.
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      // What the posting actually wrote, from the API. A return does not move
      // the order on, so nothing here claims it did.
      toast.success(
        `Material returned from ${orderCode}`,
        `${result.issue.code} put ${formatQuantity(result.issue.total_quantity)} back into store across ${
          result.issue.line_count
        } line(s).`,
      )
      setPending(null)
      onOpenChange(false)
    },
    onError: (error) => {
      // Drop the confirmation so the form comes back with the message on it -
      // and, when the draft was stranded, with its code.
      setPending(null)
      applyFieldErrors(error, setError)
    },
  })

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>Return material from {orderCode}</DialogTitle>
            <DialogDescription>
              Unused material goes back to the store. Quantities are pre-filled with what the job is
              still holding, less what it has already consumed; clear a line to leave that component
              on the floor.
            </DialogDescription>
          </DialogHeader>

          <form
            onSubmit={handleSubmit((values) => {
              // Checked here rather than in the schema for the same reason as
              // the issue: an error on the `lines` array itself lands under a
              // key no row renders, so the form would refuse to submit with
              // nothing on screen explaining why.
              if (!values.lines.some((line) => toDecimal(line.quantity).greaterThan(0))) {
                setError('root', {
                  type: 'manual',
                  message: 'Enter a quantity against at least one component',
                })
                return
              }
              setPending(values)
            })}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-4">
              {strandedDraft && (
                <Alert variant="warning">
                  <AlertDescription>
                    Return document {strandedDraft} was created, but posting it failed and no stock
                    moved. Post or cancel {strandedDraft} from the material issues register rather
                    than submitting this form again, or the same material is drafted back twice.
                  </AlertDescription>
                </Alert>
              )}

              <Controller
                control={control}
                name="warehouse_id"
                render={({ field }) => (
                  <WarehouseSelect
                    id="return-warehouse"
                    label="Return into"
                    value={field.value}
                    onChange={field.onChange}
                    includeAll
                    allLabel="The component's own warehouse"
                  />
                )}
              />

              <div className="space-y-2">
                <p className="text-sm font-medium">Material on the floor</p>
                {fields.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    This order is not holding any material, so there is nothing to return.
                  </p>
                )}
                {fields.map((field, index) => {
                  const component = holding[index]
                  return (
                    <div
                      key={field.id}
                      className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-[1fr_10rem]"
                    >
                      <div className="min-w-0">
                        <Label htmlFor={`return-line-${index}`} className="block truncate">
                          {field.label}
                        </Label>
                        {component && (
                          <p className="mt-1 text-xs text-muted-foreground">
                            Holding {formatQuantity(component.net_issued_quantity)}
                            {' - consumed '}
                            {formatQuantity(component.consumed_quantity)}
                            {' - returnable '}
                            <span className="font-medium">{formatQuantity(field.returnable)}</span>
                          </p>
                        )}
                      </div>
                      <div>
                        <Input
                          id={`return-line-${index}`}
                          {...register(`lines.${index}.quantity`)}
                          inputMode="decimal"
                          className="text-right tabular"
                          aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                        />
                        <FieldError message={errors.lines?.[index]?.quantity?.message} />
                      </div>
                    </div>
                  )
                })}
                <FieldError message={errors.root?.message} />
              </div>

              <div>
                <Label htmlFor="return-reason" required>
                  Reason
                </Label>
                <Input
                  id="return-reason"
                  {...register('reason')}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.reason)}
                />
                <FieldError message={errors.reason?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Recorded on every ledger row this return writes.
                </p>
              </div>

              <Alert variant="info">
                <AlertDescription>
                  Material has to come back before the order is completed or cancelled: a job still
                  holding stock cannot be cancelled at all, and neither status accepts a return
                  afterwards.
                </AlertDescription>
              </Alert>
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={postReturn.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={fields.length === 0}>
                Return material
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(next) => {
          // Never dismissed out from under a posting in flight. The form stays
          // open behind this either way, so cancelling comes straight back to
          // the quantities rather than losing them.
          if (!next && !postReturn.isPending) setPending(null)
        }}
        destructive
        title={`Return material to store from ${orderCode}?`}
        description={
          <>
            This posts the material back into stock straight away, valued at what it left the store
            at. A ledger row cannot be deleted, so it cannot be undone - only reversed by issuing
            the material out again.
          </>
        }
        confirmLabel="Post return"
        cancelLabel="Back"
        loading={postReturn.isPending}
        onConfirm={() => pending && postReturn.mutate(pending)}
      >
        <div className="rounded-md border p-3">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
            <div>
              <dt className="text-xs text-muted-foreground">Lines</dt>
              <dd className="tabular text-sm font-medium">{pendingLines.length}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Going back to store</dt>
              <dd className="tabular text-sm font-medium">
                {formatQuantity(pendingTotal.toFixed())}
              </dd>
            </div>
          </dl>
        </div>
      </ConfirmDialog>
    </>
  )
}

// -------------------------------------------------------- record production --

const entrySchema = z
  .object({
    entry_date: z.string().min(1, 'A booking needs a date'),
    shift: z.string().trim().max(32, 'At most 32 characters'),
    operator_user_id: z.string(),
    machine_id: z.string(),
    produced_quantity: z
      .string()
      .trim()
      .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    rejected_quantity: z
      .string()
      .trim()
      .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    rejection_reason: z.string().trim().max(255, 'At most 255 characters'),
    downtime_minutes: z.string().trim().regex(MINUTES, 'Enter minutes, up to 4 decimals'),
  })
  .superRefine((values, ctx) => {
    const produced = toDecimal(values.produced_quantity)
    const rejected = toDecimal(values.rejected_quantity)
    if (produced.isZero() && rejected.isZero()) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Book either produced or rejected quantity',
        path: ['produced_quantity'],
      })
    }
    // A rejection with no recorded cause is worthless to the quality team, and
    // it is the only chance to capture it while the operator still remembers.
    if (rejected.greaterThan(0) && values.rejection_reason === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'A rejection needs a reason',
        path: ['rejection_reason'],
      })
    }
  })

type EntryFormValues = z.infer<typeof entrySchema>

function emptyEntry(): EntryFormValues {
  return {
    entry_date: today(),
    shift: '',
    operator_user_id: '',
    machine_id: '',
    produced_quantity: '',
    rejected_quantity: '',
    rejection_reason: '',
    downtime_minutes: '',
  }
}

export function RecordProductionDialog({
  open,
  onOpenChange,
  orderId,
  orderCode,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  orderCode: string
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const machines = useMachineOptions(open && can(P.MACHINE_VIEW))
  const operators = useOperatorOptions(open && can(P.USER_VIEW))
  const machineRows = machines.data ?? []
  const operatorRows = operators.data ?? []

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<EntryFormValues>({ resolver: zodResolver(entrySchema), defaultValues: emptyEntry() })

  useEffect(() => {
    if (open) reset(emptyEntry())
  }, [open, reset])

  const record = useMutation({
    mutationFn: (values: EntryFormValues) =>
      api.post<ProductionEntryResult>(`/production/${orderId}/entries`, {
        entry_date: values.entry_date,
        shift: orNull(values.shift),
        operator_user_id: orNull(values.operator_user_id),
        machine_id: orNull(values.machine_id),
        produced_quantity: asDecimal(values.produced_quantity),
        rejected_quantity: asDecimal(values.rejected_quantity),
        rejection_reason: orNull(values.rejection_reason),
        // Downtime is a measured value like everything else here, so it stays a
        // decimal string rather than being parsed into a double.
        downtime_minutes: asDecimal(values.downtime_minutes),
      }),
    onSuccess: (result) => {
      // Output is received into stock, so the balances move with the order.
      void queryClient.invalidateQueries({ queryKey: queryKeys.production.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      // The API says whether the booking actually posted a receipt; a booking of
      // rejects alone does not, and claiming it did would be a lie a store
      // keeper reconciles against later.
      toast.success(
        'Production booked',
        result.output_movement
          ? `${formatQuantity(result.output_movement.quantity)} received into stock against ${orderCode}.`
          : `The entry was recorded against ${orderCode}. Nothing was received into stock.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Record production on {orderCode}</DialogTitle>
          <DialogDescription>
            One booking per shift. Good output is received into stock; rejected quantity is not, and
            its reason is what the quality team reads later.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => record.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="entry-date" required>
                  Date
                </Label>
                <Input
                  id="entry-date"
                  type="date"
                  {...register('entry_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.entry_date)}
                />
                <FieldError message={errors.entry_date?.message} />
              </div>

              <div>
                <Label htmlFor="entry-shift">Shift</Label>
                <Input
                  id="entry-shift"
                  {...register('shift')}
                  autoComplete="off"
                  placeholder="A, B, General..."
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.shift)}
                />
                <FieldError message={errors.shift?.message} />
              </div>

              <div>
                <Label htmlFor="entry-operator">Operator</Label>
                <Controller
                  control={control}
                  name="operator_user_id"
                  render={({ field }) => (
                    <Select
                      value={field.value || NONE}
                      onValueChange={(value) => field.onChange(value === NONE ? '' : value)}
                      disabled={operatorRows.length === 0}
                    >
                      <SelectTrigger id="entry-operator" className="mt-1.5" aria-label="Operator">
                        <SelectValue
                          placeholder={
                            operators.isLoading
                              ? 'Loading...'
                              : operatorRows.length === 0
                                ? 'Not available to your account'
                                : 'Not recorded'
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NONE}>Not recorded</SelectItem>
                        {operatorRows.map((operator) => (
                          <SelectItem key={operator.id} value={operator.id}>
                            {operator.full_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="entry-machine">Machine</Label>
                <Controller
                  control={control}
                  name="machine_id"
                  render={({ field }) => (
                    <Select
                      value={field.value || NONE}
                      onValueChange={(value) => field.onChange(value === NONE ? '' : value)}
                      disabled={machineRows.length === 0}
                    >
                      <SelectTrigger id="entry-machine" className="mt-1.5" aria-label="Machine">
                        <SelectValue
                          placeholder={
                            machines.isLoading
                              ? 'Loading...'
                              : machineRows.length === 0
                                ? 'Not available to your account'
                                : 'Not recorded'
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NONE}>Not recorded</SelectItem>
                        {machineRows.map((machine) => (
                          <SelectItem key={machine.id} value={machine.id}>
                            {machine.code} - {machine.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="entry-produced">Produced</Label>
                <Input
                  id="entry-produced"
                  {...register('produced_quantity')}
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.produced_quantity)}
                />
                <FieldError message={errors.produced_quantity?.message} />
              </div>

              <div>
                <Label htmlFor="entry-rejected">Rejected</Label>
                <Input
                  id="entry-rejected"
                  {...register('rejected_quantity')}
                  inputMode="decimal"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.rejected_quantity)}
                />
                <FieldError message={errors.rejected_quantity?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="entry-rejection-reason">Rejection reason</Label>
                <Input
                  id="entry-rejection-reason"
                  {...register('rejection_reason')}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.rejection_reason)}
                />
                <FieldError message={errors.rejection_reason?.message} />
              </div>

              <div>
                <Label htmlFor="entry-downtime">Downtime (minutes)</Label>
                <Input
                  id="entry-downtime"
                  {...register('downtime_minutes')}
                  inputMode="numeric"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.downtime_minutes)}
                />
                <FieldError message={errors.downtime_minutes?.message} />
              </div>
            </div>

            <Alert variant="info">
              <AlertDescription>
                Booking output receives it into stock against this order. The first booking also
                moves the order from material issued to in process.
              </AlertDescription>
            </Alert>
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={record.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={record.isPending}>
              Record production
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

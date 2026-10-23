/**
 * Raise a purchase requisition, or amend a draft one.
 *
 * A requisition is where demand enters procurement, so this is the one place a
 * requisition can be brought into existence by hand. Lines are collected here
 * rather than on a later screen: `RequisitionCreate` declares `lines` as
 * required, and a header with nothing on it is not a demand signal.
 *
 * `requisitionId` null raises a new one; an id amends that draft. Only a DRAFT
 * can be amended - `update_requisition` refuses anything further along, because
 * a document somebody is already deciding on must not change underneath them.
 *
 * Two contract facts shape the amend path and are easy to get wrong:
 *
 *  - `RequisitionUpdate.lines` replaces the whole set. Anything not resent is
 *    gone, which is why the fields this form does not show are still carried
 *    through the form state and sent back untouched.
 *  - `update_requisition` ignores every null it is given for the scalar fields,
 *    so this can change a purpose but cannot clear one. The same is true of the
 *    RFQ editor; it is the API's rule, not a shortcut taken here.
 *
 * Money and quantities stay decimal strings the whole way. The indicative value
 * is computed with decimal.js purely so a fat-fingered quantity is obvious
 * before the requisition is raised - the estimate is not a price commitment and
 * nothing downstream reads it as one.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, RefreshCw, Trash2 } from 'lucide-react'
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
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { useDepartmentOptions } from '@/hooks/use-organisation-options'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatMoney, toDateInput, toDecimal } from '@/lib/format'
import { describeError, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  DEFAULT_PRIORITY,
  MAX_PRIORITY,
  MIN_PRIORITY,
  isRequisitionAmendable,
  requisitionKeys,
  type RequisitionCreatePayload,
  type RequisitionDetail,
  type RequisitionLinePayload,
  type RequisitionUpdatePayload,
} from './procurement-types'

// The API's own precision: a requisition quantity is NUMERIC(18,4) and an
// estimated price NUMERIC(18,6). Refusing a seventh decimal here is kinder than
// letting the 422 arrive after the whole form has been filled in.
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const OPTIONAL_PRICE = /^(\d{1,12}(\.\d{1,6})?)?$/

const schema = z
  .object({
    requisition_date: z.string().min(1, validation.required('Raised date')),
    required_date: z.string().min(1, validation.required('Required-by date')),
    // Required by the contract: a requisition has to say where the material is
    // wanted, because that is what an order raised against it delivers to.
    warehouse_id: z.string().min(1, validation.choose('where the material is needed')),
    department_id: z.string(),
    priority: z.coerce
      .number()
      .int(validation.integer('Priority'))
      .min(MIN_PRIORITY, `Between ${MIN_PRIORITY} and ${MAX_PRIORITY}`)
      .max(MAX_PRIORITY, `Between ${MIN_PRIORITY} and ${MAX_PRIORITY}`),
    purpose: z.string().trim().max(255, 'At most 255 characters'),
    notes: z.string().trim().max(4000, 'At most 4000 characters'),
    lines: z
      .array(
        z.object({
          item_id: z.string().min(1, validation.choose('an item')),
          quantity: z
            .string()
            .trim()
            .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
            .refine((v) => toDecimal(v).greaterThan(0), validation.positive('Quantity')),
          estimated_unit_price: z
            .string()
            .trim()
            .regex(OPTIONAL_PRICE, 'Enter a price with up to 6 decimal places, or leave it blank'),
          // Recorded on the line but not edited here. They are held in the form
          // so that an amend - which replaces the whole line set - sends them
          // back rather than silently dropping them.
          description: z.string(),
          uom_id: z.string(),
          required_date: z.string(),
          suggested_vendor_id: z.string(),
          notes: z.string(),
        }),
      )
      .min(1, 'A requisition needs at least one line'),
  })
  // The API refuses a required date before the requisition date outright, and
  // it is nearly always a mis-typed year rather than a real instruction.
  .superRefine((values, ctx) => {
    if (
      values.required_date &&
      values.requisition_date &&
      values.required_date < values.requisition_date
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['required_date'],
        message: 'The material cannot be needed before the requisition is raised',
      })
    }
  })

type RequisitionFormValues = z.infer<typeof schema>
type RequisitionLineValues = RequisitionFormValues['lines'][number]

function emptyLine(): RequisitionLineValues {
  return {
    item_id: '',
    quantity: '',
    estimated_unit_price: '',
    description: '',
    uom_id: '',
    required_date: '',
    suggested_vendor_id: '',
    notes: '',
  }
}

function emptyValues(): RequisitionFormValues {
  return {
    requisition_date: toDateInput(new Date()),
    required_date: '',
    warehouse_id: '',
    department_id: '',
    priority: DEFAULT_PRIORITY,
    purpose: '',
    notes: '',
    lines: [emptyLine()],
  }
}

/**
 * The loaded draft as form state.
 *
 * Lines are ordered by `line_number` rather than trusted in arrival order: the
 * payload renumbers them from their position on submit, so a reordered read
 * would quietly renumber the document.
 */
function valuesFrom(detail: RequisitionDetail): RequisitionFormValues {
  const lines = [...(detail.lines ?? [])].sort((a, b) => a.line_number - b.line_number)
  return {
    requisition_date: detail.requisition_date,
    required_date: detail.required_date,
    warehouse_id: detail.warehouse_id,
    department_id: detail.department_id ?? '',
    priority: detail.priority,
    purpose: detail.purpose ?? '',
    notes: detail.notes ?? '',
    lines:
      lines.length > 0
        ? lines.map((line) => ({
            item_id: line.item_id,
            quantity: line.quantity,
            estimated_unit_price: line.estimated_unit_price ?? '',
            description: line.description ?? '',
            uom_id: line.uom_id,
            required_date: line.required_date ?? '',
            suggested_vendor_id: line.suggested_vendor_id ?? '',
            notes: line.notes ?? '',
          }))
        : [emptyLine()],
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/**
 * Lines exactly as `RequisitionLineIn` declares them.
 *
 * `line_number` is required and comes from the position in the editor, so the
 * numbering always matches what the requester sees. A null `uom_id` is the
 * honest answer for a new line - the API fills in the item's own order unit,
 * which is a better answer than one this form could invent.
 */
function buildLines(lines: RequisitionLineValues[]): RequisitionLinePayload[] {
  return lines.map((line, index) => ({
    line_number: index + 1,
    item_id: line.item_id,
    // Quantities and prices stay strings on the wire; the API parses Decimals.
    quantity: line.quantity.trim(),
    estimated_unit_price: orNull(line.estimated_unit_price),
    description: orNull(line.description),
    uom_id: orNull(line.uom_id),
    required_date: orNull(line.required_date),
    suggested_vendor_id: orNull(line.suggested_vendor_id),
    notes: orNull(line.notes),
  }))
}

export function RequisitionFormDialog({
  open,
  onOpenChange,
  requisitionId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** `null` raises a new requisition; an id amends that draft. */
  requisitionId: string | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isAmend = Boolean(requisitionId)

  const detail = useQuery({
    queryKey: requisitionKeys.detail(requisitionId ?? ''),
    queryFn: () =>
      api.get<RequisitionDetail>(`/procurement/purchase-requisitions/${requisitionId}`),
    enabled: open && Boolean(requisitionId),
    staleTime: staleTimes.transactional,
  })

  // Departments resolve to an empty list without company.view, and the field is
  // optional on the contract - so its absence narrows the form rather than
  // blocking it, unlike the warehouse below.
  const departments = useDepartmentOptions(open)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<RequisitionFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'lines' })

  const loaded = detail.data
  useEffect(() => {
    if (!open) return
    reset(loaded && requisitionId ? valuesFrom(loaded) : emptyValues())
    // `loaded` is compared by presence: re-running on every background refetch
    // would throw away edits in progress.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, requisitionId, Boolean(loaded), reset])

  const save = useMutation({
    mutationFn: (values: RequisitionFormValues) => {
      if (requisitionId) {
        // Typed as the contract's own body: both models forbid unknown keys, so
        // a field this form invented would be a 422 at the moment of use rather
        // than a compile error. `requisition_date` is absent from the update
        // model on purpose - the raised date is what the document number was
        // issued against.
        const payload: RequisitionUpdatePayload = {
          warehouse_id: values.warehouse_id,
          required_date: values.required_date,
          department_id: orNull(values.department_id),
          priority: values.priority,
          purpose: orNull(values.purpose),
          notes: orNull(values.notes),
          lines: buildLines(values.lines),
        }
        return api.patch<RequisitionDetail>(
          `/procurement/purchase-requisitions/${requisitionId}`,
          payload,
        )
      }
      const payload: RequisitionCreatePayload = {
        warehouse_id: values.warehouse_id,
        requisition_date: values.requisition_date,
        required_date: values.required_date,
        department_id: orNull(values.department_id),
        priority: values.priority,
        purpose: orNull(values.purpose),
        notes: orNull(values.notes),
        // Stated rather than left to the default: this screen only ever raises
        // demand by hand. A converted MRP suggestion arrives through the planner
        // and carries its own source type and source id.
        source_type: 'MANUAL',
        lines: buildLines(values.lines),
      }
      return api.post<RequisitionDetail>('/procurement/purchase-requisitions', payload)
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      // The POST raises a DRAFT and stops there - it does not submit it. Telling
      // the requester it had gone for approval would send them looking for a
      // decision nobody has been asked to make.
      toast.success(
        isAmend ? success.updated('Requisition') : success.created('Requisition'),
        isAmend
          ? `${saved.code} is still a draft. Submit it when the lines are right.`
          : `${saved.code} is saved as a draft with ${saved.lines?.length ?? 0} line(s). Submit it for approval when the lines are right.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const lines = watch('lines')

  // Indicative only. The estimate is what an approver judges the spend against;
  // the price actually paid is settled on the purchase order.
  const indicativeTotal = lines.reduce(
    (sum, line) => sum.plus(toDecimal(line.quantity).times(toDecimal(line.estimated_unit_price))),
    toDecimal(0),
  )

  // The draft may have been submitted or decided by somebody else while this was
  // open. Saying so beats letting the save earn a refusal.
  const noLongerDraft = Boolean(loaded && requisitionId && !isRequisitionAmendable(loaded.status))
  // An amend saves `lines` as the whole set, so it is only ever offered over
  // the draft that actually loaded. A blank form after a failed load is not an
  // empty requisition - saving it would replace every line the draft has.
  const canSave =
    can(P.WAREHOUSE_VIEW) && !noLongerDraft && !detail.isLoading && (!isAmend || Boolean(loaded))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>
            {isAmend ? `Amend ${loaded?.code ?? 'requisition'}` : 'New purchase requisition'}
          </DialogTitle>
          <DialogDescription>
            The requisition is saved as a draft and goes through approval before anything can be
            sourced against it. Only items flagged as purchasable can be asked for.
          </DialogDescription>
        </DialogHeader>

        {isAmend && detail.isLoading && (
          <DialogBody className="space-y-3" aria-hidden>
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </DialogBody>
        )}

        {isAmend && !detail.isLoading && detail.error && (
          <DialogBody>
            <Alert variant="destructive">
              <AlertTitle>Could not load this requisition</AlertTitle>
              <AlertDescription>{describeError(detail.error)}</AlertDescription>
              <Button
                variant="outline"
                size="sm"
                className="mt-3"
                loading={detail.isFetching}
                onClick={() => void detail.refetch()}
              >
                <RefreshCw />
                Try again
              </Button>
            </Alert>
          </DialogBody>
        )}

        {(!isAmend || (!detail.isLoading && !detail.error)) && (
          <form
            onSubmit={handleSubmit((values) => save.mutate(values))}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-5">
              {noLongerDraft && (
                <Alert variant="warning">
                  <AlertTitle>This requisition is no longer a draft</AlertTitle>
                  <AlertDescription>
                    {loaded?.code} has moved on and can no longer be edited. Close this and raise a
                    new requisition for anything still needed.
                  </AlertDescription>
                </Alert>
              )}

              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <Label htmlFor="req-date" required>
                    Raised on
                  </Label>
                  <Input
                    id="req-date"
                    type="date"
                    {...register('requisition_date')}
                    // Absent from `RequisitionUpdate`: the raised date is what the
                    // document number was issued against and cannot move.
                    disabled={isAmend}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.requisition_date)}
                  />
                  <FieldError message={errors.requisition_date?.message} />
                  {isAmend && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      Fixed once the requisition has a number.
                    </p>
                  )}
                </div>

                <div>
                  <Label htmlFor="req-required" required>
                    Required by
                  </Label>
                  <Input
                    id="req-required"
                    type="date"
                    {...register('required_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.required_date)}
                  />
                  <FieldError message={errors.required_date?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    What the queue is worked against, and what marks the demand late.
                  </p>
                </div>

                {/* Required by the API, and only offered to a principal who can
                  read warehouses: firing the lookup regardless would earn a 403
                  they cannot act on. */}
                {can(P.WAREHOUSE_VIEW) ? (
                  <div>
                    <Controller
                      control={control}
                      name="warehouse_id"
                      render={({ field }) => (
                        <WarehouseSelect
                          id="req-warehouse"
                          label="Needed at"
                          value={field.value}
                          onChange={field.onChange}
                          invalid={Boolean(errors.warehouse_id)}
                        />
                      )}
                    />
                    <FieldError message={errors.warehouse_id?.message} />
                  </div>
                ) : (
                  // The API requires a warehouse, so without the right to read the
                  // list there is nothing to choose and the form would fail on a
                  // field that was never shown. Say so instead.
                  <div className="sm:col-span-2">
                    <Alert variant="warning">
                      <AlertTitle>You cannot save a requisition from here</AlertTitle>
                      <AlertDescription>
                        A requisition has to state the warehouse the material is needed at, and your
                        account cannot read the warehouse list. Ask somebody who can to raise it.
                      </AlertDescription>
                    </Alert>
                  </div>
                )}

                {/* Optional on the contract, and left out entirely when the lookup
                  comes back empty - which is what happens without company.view.
                  An empty picker is worse than no picker, and the API files the
                  requisition under the requester's own department anyway. */}
                {(departments.data?.length ?? 0) > 0 && (
                  <div>
                    <Controller
                      control={control}
                      name="department_id"
                      render={({ field }) => (
                        <div className="space-y-1.5">
                          <Label htmlFor="req-department">Department</Label>
                          <Select
                            value={field.value || SELECT_NONE}
                            onValueChange={(next) =>
                              field.onChange(next === SELECT_NONE ? '' : next)
                            }
                          >
                            <SelectTrigger id="req-department">
                              <SelectValue placeholder="Your own department" />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value={SELECT_NONE}>Your own department</SelectItem>
                              {(departments.data ?? []).map((department) => (
                                <SelectItem key={department.id} value={department.id}>
                                  {department.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      )}
                    />
                    <FieldError message={errors.department_id?.message} />
                  </div>
                )}

                <div>
                  <Label htmlFor="req-priority">Priority</Label>
                  <Input
                    id="req-priority"
                    type="number"
                    min={MIN_PRIORITY}
                    max={MAX_PRIORITY}
                    step={1}
                    {...register('priority')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.priority)}
                  />
                  <FieldError message={errors.priority?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    {MIN_PRIORITY} is the most urgent, {MAX_PRIORITY} the least. {DEFAULT_PRIORITY}{' '}
                    is routine.
                  </p>
                </div>

                <div className="sm:col-span-2">
                  <Label htmlFor="req-purpose">Purpose</Label>
                  <Input
                    id="req-purpose"
                    {...register('purpose')}
                    autoComplete="off"
                    maxLength={255}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.purpose)}
                  />
                  <FieldError message={errors.purpose?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    What the material is for. It is usually what decides the approval.
                  </p>
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium">Lines</h3>
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
                <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />

                <div className="mt-3 space-y-3">
                  {fields.map((field, index) => (
                    <div
                      key={field.id}
                      className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
                    >
                      <div className="sm:col-span-2 lg:col-span-1">
                        <Controller
                          control={control}
                          name={`lines.${index}.item_id`}
                          render={({ field: itemField }) => (
                            <ItemPicker
                              id={`req-line-item-${index}`}
                              label={`Line ${index + 1} item`}
                              value={itemField.value || null}
                              onChange={(itemId) => {
                                itemField.onChange(itemId ?? '')
                                // The description and unit carried on the line
                                // belong to the item that was on it. Kept across a
                                // change they would label the new item with the old
                                // one's name and buy it in the old one's unit;
                                // cleared, the API fills both from the new item.
                                setValue(`lines.${index}.description`, '')
                                setValue(`lines.${index}.uom_id`, '')
                              }}
                              placeholder="Search purchasable items"
                              // Narrowed by the API rather than here: a
                              // non-purchasable item is refused outright by
                              // `create_requisition`, so it should never be offered
                              // in the first place.
                              filters={{ can_purchase: true }}
                              emptyMessage="No purchasable item matches that search."
                              invalid={Boolean(errors.lines?.[index]?.item_id)}
                            />
                          )}
                        />
                        <FieldError message={errors.lines?.[index]?.item_id?.message} />
                      </div>

                      <div>
                        <Label htmlFor={`req-line-qty-${index}`} required>
                          Quantity
                        </Label>
                        <Input
                          id={`req-line-qty-${index}`}
                          {...register(`lines.${index}.quantity`)}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                        />
                        <FieldError message={errors.lines?.[index]?.quantity?.message} />
                      </div>

                      <div>
                        <Label htmlFor={`req-line-price-${index}`}>Estimated price</Label>
                        <Input
                          id={`req-line-price-${index}`}
                          {...register(`lines.${index}.estimated_unit_price`)}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.estimated_unit_price)}
                        />
                        <FieldError
                          message={errors.lines?.[index]?.estimated_unit_price?.message}
                        />
                      </div>

                      <div className="flex items-end sm:col-span-2 sm:justify-end lg:col-span-1 lg:justify-start">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`Remove line ${index + 1}`}
                          disabled={fields.length === 1}
                          onClick={() => remove(index)}
                        >
                          <Trash2 className="text-destructive" />
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>

                <p className="mt-3 text-sm text-muted-foreground">
                  Indicative value{' '}
                  <span className="tabular font-medium text-foreground">
                    {formatMoney(indicativeTotal.toFixed(4))}
                  </span>{' '}
                  at the estimates entered. Nothing is committed until a purchase order is raised.
                </p>
              </div>

              <div>
                <Label htmlFor="req-notes">Notes</Label>
                <Textarea id="req-notes" {...register('notes')} className="mt-1.5" />
                <FieldError message={errors.notes?.message} />
              </div>
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
              <Button type="submit" loading={save.isPending} disabled={!canSave}>
                {isAmend ? 'Save draft' : 'Raise draft requisition'}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

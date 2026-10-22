/**
 * Assembly order detail, opened over the list.
 *
 * An assembly is short-lived and narrow - a kit, a build, a finished unit - so
 * it reads better as a panel over the list than as a page of its own: the
 * operator picking through twenty orders never loses their place.
 *
 * The serial panel is the reason this screen exists at all. When a component is
 * serial tracked, the serials that went into a specific assembly are what
 * answers a warranty claim years later, and they are shown in full rather than
 * counted.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { CheckCircle2, PackageMinus, ScanBarcode, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { DescriptionList, FieldError, StatusBadge } from '@/components/shared'
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
import { Alert, AlertDescription, Badge, Input, Label, Skeleton } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { BatchSelect } from '@/features/dispatch/stock-pickers'
import MovementBatchPicker from '@/features/inventory/movement-batch-picker'
import { SerialNumbersField } from '@/features/inventory/serial-numbers-field'
import { splitSerialNumbers } from '@/features/inventory/serial-numbers'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE, REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatDate,
  formatDateTime,
  formatNumber,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { QcInspectionDetail, QcInspectionRow } from '@/features/quality/quality-types'
import { InspectionDialog } from '@/features/quality/inspection-dialog'
import { packingKeys, type PackingOrderDetail } from '@/features/packing/packing-types'

import { AssemblyVerdictDialog, ReasonDialog } from './assembly-lifecycle'

import {
  acceptsComponentIssue,
  assemblyKeys,
  hasLinkedInspection,
  isApprovable,
  isCancellable,
  isClosable,
  isCompletable,
  isDeletable,
  isEditable,
  isReleasable,
  isStartable,
  onBenchQuantity,
  outstandingQuantity,
  remainingToAssemble,
  type AssemblyComponent,
  type AssemblyComponentAvailability,
  type AssemblyCompletionResult,
  type AssemblyOrderDetail,
  type StockSerial,
} from './assembly-model'
import { AssemblyOrderFormDialog } from './assembly-order-dialog'

const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const OPTIONAL_QUANTITY = /^(\d{1,14}(\.\d{1,4})?)?$/

/** Decimals stay strings all the way to the wire - never parsed into a double. */
const asDecimal = (value: string): string => (value.trim() === '' ? '0' : value.trim())

// -------------------------------------------------------- issue components --

const issueSchema = z.object({
  reason: z
    .string()
    .trim()
    // `IssueComponentsRequest.reason` is min 3 / max 255. A shorter one is a
    // 422, not a shrug.
    .min(
      REASON_MIN_LENGTH,
      `Every hand-over is recorded with a reason of at least ${REASON_MIN_LENGTH} characters`,
    )
    .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
  lines: z.array(
    z.object({
      // The API addresses a component by its line number on the order.
      line_number: z.number(),
      item_id: z.string(),
      label: z.string(),
      batch_id: z.string(),
      quantity: z
        .string()
        .trim()
        .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    }),
  ),
})

type IssueFormValues = z.infer<typeof issueSchema>

/**
 * `issued_quantity` is the running total on the bench, not this hand-over.
 *
 * `issue_components` assigns it - `component.issued_quantity = overrides.get(...)`
 * - so sending the *outstanding balance* on a top-up replaced a line that had 40
 * of 100 issued with 60, losing 40 units of kit from the record. The field is
 * therefore pre-filled with the full requirement, which is also what the API
 * assumes for any line the request omits.
 */
function issueDefaults(components: AssemblyComponent[]): IssueFormValues {
  return {
    reason: 'Component issue to assembly order',
    // The component carries an item id and nothing else - no code, no name -
    // so the label is the line number and the id it points at.
    lines: components.map((component) => ({
      line_number: component.line_number,
      item_id: component.item_id,
      label: `Line ${component.line_number} - ${component.item_id.slice(0, 8)}`,
      batch_id: component.issued_batch_id ?? '',
      quantity: toDecimal(component.required_quantity).toFixed(),
    })),
  }
}

function IssueComponentsDialog({
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
  components: AssemblyComponent[]
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
    if (open) reset(issueDefaults(components))
    // Re-read on every open so the outstanding quantities are never a snapshot
    // from an earlier visit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reset])

  const issue = useMutation({
    mutationFn: (values: IssueFormValues) =>
      // Every line goes, including the ones left at zero. Omitting a line does
      // not leave it alone: the service issues its full `required_quantity`,
      // so the old filter quietly kitted the lines the operator had cleared.
      api.post<AssemblyOrderDetail>(`/assembly/${orderId}/issue-components`, {
        reason: values.reason.trim(),
        lines: values.lines.map((line) => ({
          line_number: line.line_number,
          issued_quantity: asDecimal(line.quantity),
          ...(line.batch_id ? { batch_id: line.batch_id } : {}),
        })),
      }),
    onSuccess: (updated) => {
      // Only the order changes. No ledger row is written here by design - the
      // parts are still owned, still in the position they were reserved at, and
      // still reserved to this order - so no balance or reservation cache is
      // stale because of it.
      void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
      toast.success(
        'Components kitted',
        `${updated.code} is now ${humanise(updated.status).toLowerCase()}. Nothing has left the store yet; the ledger is written when the assembly is completed.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Issue components to {orderCode}</DialogTitle>
          <DialogDescription>
            Records what was physically kitted to the bench. No stock moves here - the parts are
            still owned and still reserved to this order, and the ledger is written when the
            assembly is completed. Each figure is the running total handed over for that line, not
            an addition to it.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            // Checked here rather than in the schema: an error on the `lines`
            // array itself lands under a key no field row renders, so the
            // operator would face a form that refuses to submit in silence.
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
            {/* No source-warehouse control: `IssueComponentsRequest` carries a
                reason and lines and nothing else, and it forbids additional
                properties, so a warehouse chosen here could not be sent. The
                components are kitted from the order's own warehouse. */}
            <div className="space-y-2">
              <p className="text-sm font-medium">Components</p>
              {fields.map((field, index) => {
                const component = components[index]
                return (
                  <div
                    key={field.id}
                    className="grid items-start gap-3 rounded-md border p-3 sm:grid-cols-[1fr_10rem]"
                  >
                    <div className="min-w-0">
                      <Label htmlFor={`assembly-issue-${index}`} className="block truncate">
                        {field.label}
                      </Label>
                      {component && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          Required {formatQuantity(component.required_quantity)}
                          {' - issued so far '}
                          {formatQuantity(component.issued_quantity)}
                          {' - on the bench '}
                          {formatQuantity(onBenchQuantity(component).toFixed())}
                          {component.is_serial_tracked_component && ' - serial tracked'}
                        </p>
                      )}
                      {component?.is_batch_tracked_component && (
                        <div className="mt-2">
                          <Controller
                            control={control}
                            name={`lines.${index}.batch_id`}
                            render={({ field: batch }) => (
                              <BatchSelect
                                id={`assembly-issue-batch-${index}`}
                                itemId={component.item_id}
                                value={batch.value}
                                onChange={batch.onChange}
                              />
                            )}
                          />
                          <FieldError message={errors.lines?.[index]?.batch_id?.message} />
                        </div>
                      )}
                    </div>
                    <div>
                      <Input
                        id={`assembly-issue-${index}`}
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
              <Label htmlFor="assembly-issue-reason" required>
                Reason
              </Label>
              <Input
                id="assembly-issue-reason"
                {...register('reason')}
                autoComplete="off"
                className="mt-1.5"
                aria-invalid={Boolean(errors.reason)}
              />
              <FieldError message={errors.reason?.message} />
            </div>

            <Alert variant="info">
              <AlertDescription>
                A line may be issued short but never above its requirement - nothing beyond that was
                ever reserved for this order. Which serialised units were consumed is named at
                completion, not here.
              </AlertDescription>
            </Alert>
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
              Issue components
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------- serial panel ----

function SerialTraceability({ components }: { components: AssemblyComponent[] }) {
  const tracked = components.filter((component) => component.is_serial_tracked_component)

  if (tracked.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        None of these components is serial tracked, so there is nothing to trace unit by unit.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      {tracked.map((component) => {
        // The API returns the consumed serials as ids only - it carries no
        // serial numbers on this payload - so the id is what can be shown.
        const serialIds = component.consumed_serial_ids
        return (
          <div key={component.id} className="rounded-md border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-medium">
                Line {component.line_number}
                <Link
                  to={`/items/${component.item_id}`}
                  className="ml-2 font-mono text-xs font-normal hover:underline"
                >
                  {component.item_id.slice(0, 8)}
                </Link>
              </p>
              <span className="text-xs text-muted-foreground">
                {serialIds.length} serial{serialIds.length === 1 ? '' : 's'} consumed
              </span>
            </div>

            {serialIds.length === 0 ? (
              <p className="mt-2 text-xs text-muted-foreground">
                Nothing consumed yet. Serials are recorded as the assembly is completed.
              </p>
            ) : (
              <ul className="mt-2 flex flex-wrap gap-1.5">
                {serialIds.map((serialId) => (
                  <li key={serialId}>
                    <Badge variant="outline" className="font-mono">
                      <ScanBarcode className="h-3 w-3" aria-hidden />
                      {serialId.slice(0, 8)}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ------------------------------------------------------ complete assembly --

/**
 * The serials one serialised component gave up.
 *
 * `plan_consumption` refuses a completion that names the wrong number of
 * serials for a serial-tracked line ("consumes 5 serialised unit(s); 3 serial
 * number(s) were named") and refuses any serial at all on a line that is not
 * tracked. How many a line consumes is the API's own arithmetic, scaled from
 * what was produced, so this collects what the operator physically consumed and
 * lets the server be the judge of the count rather than restating the rule.
 */
function SerialPicker({
  component,
  warehouseId,
  selected,
  onChange,
  canSearch,
}: {
  component: AssemblyComponent
  warehouseId: string
  selected: StockSerial[]
  onChange: (next: StockSerial[]) => void
  canSearch: boolean
}) {
  const [term, setTerm] = useState('')

  // One request per keystroke would put a scan of the serial register behind
  // every character typed.
  const debouncedTerm = useDebouncedValue(term)

  const params = useMemo(
    () => ({
      limit: PICKER_PAGE_SIZE,
      with_total: false,
      item_id: component.item_id,
      warehouse_id: warehouseId,
      ...(debouncedTerm ? { search: debouncedTerm } : {}),
    }),
    [component.item_id, warehouseId, debouncedTerm],
  )

  const results = useQuery({
    queryKey: [...queryKeys.inventory.all, 'serials', params] as const,
    queryFn: () => api.getPage<StockSerial>('/inventory/serials', { params }),
    // A serial another document has just eaten must never be offered from cache.
    staleTime: staleTimes.live,
    enabled: canSearch,
  })

  const chosen = new Set(selected.map((serial) => serial.id))
  const rows = (results.data?.data ?? []).filter((serial) => !chosen.has(serial.id))

  return (
    <div className="rounded-md border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-medium">
          Line {component.line_number}
          <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">
            {component.item_id.slice(0, 8)}
          </span>
        </p>
        <span className="text-xs text-muted-foreground">
          {selected.length} serial{selected.length === 1 ? '' : 's'} named
        </span>
      </div>

      {selected.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {selected.map((serial) => (
            <li key={serial.id}>
              <Badge variant="outline" className="gap-1 font-mono">
                <ScanBarcode className="h-3 w-3" aria-hidden />
                {serial.serial_number}
                <button
                  type="button"
                  aria-label={`Remove serial ${serial.serial_number}`}
                  onClick={() => onChange(selected.filter((row) => row.id !== serial.id))}
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </Badge>
            </li>
          ))}
        </ul>
      )}

      {!canSearch ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Serial numbers are not readable by your account, so this completion cannot name them. Ask
          someone with inventory visibility to complete this order.
        </p>
      ) : (
        <>
          <Input
            aria-label={`Search a serial number for line ${component.line_number}`}
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            autoComplete="off"
            placeholder="Search a serial number..."
            className="mt-2"
          />
          <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto">
            {results.isLoading && <li className="text-xs text-muted-foreground">Loading...</li>}
            {!results.isLoading && rows.length === 0 && (
              <li className="text-xs text-muted-foreground">
                Nothing matches. The register holds serials in every condition; the API accepts only
                the ones actually available in this order&apos;s warehouse.
              </li>
            )}
            {rows.map((serial) => (
              <li key={serial.id}>
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-xs hover:bg-muted"
                  onClick={() => onChange([...selected, serial])}
                >
                  <span className="font-mono">{serial.serial_number}</span>
                  <span className="text-muted-foreground">{humanise(serial.status)}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

const completeSchema = z
  .object({
    assembled_quantity: z
      .string()
      .trim()
      .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    rejected_quantity: z
      .string()
      .trim()
      .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
    reason: z
      .string()
      .trim()
      .min(
        REASON_MIN_LENGTH,
        `The completion is recorded with a reason of at least ${REASON_MIN_LENGTH} characters`,
      )
      .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
    output_batch_id: z.string(),
    output_serial_numbers: z.string(),
  })
  .superRefine((values, ctx) => {
    const serials = splitSerialNumbers(values.output_serial_numbers)
    const assembled = toDecimal(values.assembled_quantity)
    if (serials.length > 0 && !assembled.equals(serials.length)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `Name one output serial per assembled unit - ${assembled.toString()} assembled, ${serials.length} captured`,
        path: ['output_serial_numbers'],
      })
    }
  })

type CompleteFormValues = z.infer<typeof completeSchema>

/**
 * Completing an assembly.
 *
 * A form rather than a confirmation, for three reasons the contract insists on:
 * `reason` is mandatory, `rejected_quantity` consumed components exactly as the
 * good output did and is scaled from the sum of the two, and a serial-tracked
 * component has to name the units it ate.
 */
function CompleteAssemblyDialog({
  open,
  onOpenChange,
  order,
  components,
  canSearchSerials,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  order: AssemblyOrderDetail
  components: AssemblyComponent[]
  canSearchSerials: boolean
}) {
  const queryClient = useQueryClient()
  const [serialsByLine, setSerialsByLine] = useState<Record<number, StockSerial[]>>({})
  const [batchesByLine, setBatchesByLine] = useState<Record<number, string>>({})

  const serialTracked = useMemo(
    () => components.filter((component) => component.is_serial_tracked_component),
    [components],
  )
  const batchTracked = useMemo(
    () => components.filter((component) => component.is_batch_tracked_component),
    [components],
  )
  const trackedComponents = useMemo(
    () =>
      components.filter(
        (component) =>
          component.is_serial_tracked_component || component.is_batch_tracked_component,
      ),
    [components],
  )

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<CompleteFormValues>({
    resolver: zodResolver(completeSchema),
    defaultValues: {
      assembled_quantity: remainingToAssemble(order).toFixed(),
      rejected_quantity: '0',
      reason: 'Assembly completed',
      output_batch_id: '',
      output_serial_numbers: '',
    },
  })

  const orderId = order.id
  useEffect(() => {
    if (open) {
      reset({
        assembled_quantity: remainingToAssemble(order).toFixed(),
        rejected_quantity: '0',
        reason: 'Assembly completed',
        output_batch_id: '',
        output_serial_numbers: '',
      })
      setSerialsByLine({})
      setBatchesByLine(
        Object.fromEntries(
          components
            .filter((component) => component.issued_batch_id)
            .map((component) => [component.line_number, component.issued_batch_id ?? '']),
        ),
      )
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, orderId, reset])

  const complete = useMutation({
    mutationFn: (values: CompleteFormValues) =>
      // `CompletionResult`, not the order: the endpoint answers with what the
      // posting did and nests the order inside it.
      api.post<AssemblyCompletionResult>(`/assembly/${orderId}/complete`, {
        assembled_quantity: asDecimal(values.assembled_quantity),
        rejected_quantity: asDecimal(values.rejected_quantity),
        reason: values.reason.trim(),
        output_batch_id: values.output_batch_id || null,
        output_serial_numbers: splitSerialNumbers(values.output_serial_numbers),
        // Only tracked lines. A line that names neither a serial nor a lot "has
        // nothing to name", and a serial on an untracked line is refused.
        components: trackedComponents.map((component) => ({
          line_number: component.line_number,
          serial_ids: (serialsByLine[component.line_number] ?? []).map((serial) => serial.id),
          ...(component.is_batch_tracked_component
            ? {
                batch_id: batchesByLine[component.line_number] || component.issued_batch_id || null,
              }
            : {}),
        })),
      }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
      // This is where the ledger is finally written: the components leave stock
      // and the finished item is received, on QC hold when the order needs it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
      toast.success(
        success.completed('Assembly order'),
        result.output_held_for_qc
          ? `${result.order.code}: ${formatQuantity(result.output_quantity)} received on QC hold${
              result.qc_inspection_created ? ' and an inspection was raised' : ''
            }; it is not available stock until it passes.`
          : `${result.order.code}: ${formatQuantity(result.output_quantity)} received into stock from ${formatNumber(
              result.consume_movement_count,
            )} consumption movement(s).`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Complete {order.code}</DialogTitle>
          <DialogDescription>
            The issued components are consumed and the finished units are received. Rejected output
            consumed its components too, so it is reported here rather than written off later. A
            completed assembly cannot be reopened.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => complete.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            {/* The mutation cache stays silent on a 422 that carries field
                errors, on the understanding that the form shows them. The API
                reports a serial-count or issued-quantity failure against no
                field this form renders, so it is surfaced here or nowhere. */}
            {complete.error && (
              <Alert variant="destructive">
                <AlertDescription>{complete.error.message}</AlertDescription>
              </Alert>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="assembly-complete-quantity" required>
                  Assembled
                </Label>
                <Input
                  id="assembly-complete-quantity"
                  {...register('assembled_quantity')}
                  inputMode="decimal"
                  autoComplete="off"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.assembled_quantity)}
                />
                <FieldError message={errors.assembled_quantity?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Balance of the plan: {formatQuantity(remainingToAssemble(order).toFixed())}{' '}
                  outstanding.
                </p>
              </div>

              <div>
                <Label htmlFor="assembly-complete-rejected">Rejected</Label>
                <Input
                  id="assembly-complete-rejected"
                  {...register('rejected_quantity')}
                  inputMode="decimal"
                  autoComplete="off"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.rejected_quantity)}
                />
                <FieldError message={errors.rejected_quantity?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Never received into stock, but it ate its components all the same.
                </p>
              </div>
            </div>

            <div>
              <Label htmlFor="assembly-complete-reason" required>
                Reason
              </Label>
              <Input
                id="assembly-complete-reason"
                {...register('reason')}
                autoComplete="off"
                className="mt-1.5"
                aria-invalid={Boolean(errors.reason)}
              />
              <FieldError message={errors.reason?.message} />
            </div>

            {serialTracked.length > 0 && (
              <div className="space-y-2">
                <p className="text-sm font-medium">Serial numbers consumed</p>
                <p className="text-xs text-muted-foreground">
                  One serial per unit consumed, for every serial-tracked line. The API works out how
                  many that is from what was produced and refuses the completion if the count does
                  not match.
                </p>
                {serialTracked.map((component) => (
                  <SerialPicker
                    key={component.id}
                    component={component}
                    warehouseId={order.warehouse_id}
                    canSearch={canSearchSerials}
                    selected={serialsByLine[component.line_number] ?? []}
                    onChange={(next) =>
                      setSerialsByLine((current) => ({
                        ...current,
                        [component.line_number]: next,
                      }))
                    }
                  />
                ))}
              </div>
            )}

            {batchTracked.length > 0 && (
              <div className="space-y-2">
                <p className="text-sm font-medium">Lots consumed</p>
                <p className="text-xs text-muted-foreground">
                  Name the lot each batch-tracked line actually consumed. The lot kitted at issue is
                  preselected; change it if the bench drew from a different one.
                </p>
                {batchTracked.map((component) => (
                  <div key={component.id} className="rounded-md border p-3">
                    <p className="mb-2 text-sm font-medium">
                      Line {component.line_number}
                      <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">
                        {component.item_id.slice(0, 8)}
                      </span>
                    </p>
                    <BatchSelect
                      id={`assembly-complete-batch-${component.line_number}`}
                      itemId={component.item_id}
                      value={
                        batchesByLine[component.line_number] ?? component.issued_batch_id ?? ''
                      }
                      onChange={(value) =>
                        setBatchesByLine((current) => ({
                          ...current,
                          [component.line_number]: value,
                        }))
                      }
                    />
                  </div>
                ))}
              </div>
            )}

            <div className="space-y-3 rounded-md border p-3">
              <p className="text-sm font-medium">Finished output</p>
              <p className="text-xs text-muted-foreground">
                Capture the lot and units received for a tracked finished item. Leave both empty for
                an untracked item.
              </p>
              <Controller
                control={control}
                name="output_batch_id"
                render={({ field }) => (
                  <MovementBatchPicker
                    id="assembly-complete-output-batch"
                    itemId={order.item_id}
                    value={field.value}
                    onChange={field.onChange}
                  />
                )}
              />
              <Controller
                control={control}
                name="output_serial_numbers"
                render={({ field }) => (
                  <SerialNumbersField
                    id="assembly-complete-output-serials"
                    label="Output serial numbers"
                    value={field.value}
                    onChange={field.onChange}
                    expected={watch('assembled_quantity')}
                    hint="One serial per good unit assembled. The rejected quantity is never received into stock."
                    error={errors.output_serial_numbers?.message}
                  />
                )}
              />
            </div>

            {order.requires_qc && (
              <Alert variant="info">
                <AlertDescription>
                  This order needs inspecting, so the output is received onto QC hold rather than
                  into the available pool, and an inspection is raised with it.
                </AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={complete.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={complete.isPending}>
              Complete assembly
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------- detail ------

/**
 * What a release would still be short of, read live.
 *
 * Release reserves an untracked component at the bin-less, batch-less position
 * and a batch-tracked one against a single ACTIVE, unexpired lot, and refuses
 * the whole order when one line is short. The shortage is shown before anything
 * is promised. Availability is never cached and never comes from the kitting
 * list, which carries no stock figure at all.
 */
function ReleaseReadiness({
  availability,
  isLoading,
  error,
  onRefresh,
}: {
  availability: AssemblyComponentAvailability[] | undefined
  isLoading: boolean
  error: Error | null
  onRefresh: () => void
}) {
  if (isLoading) {
    return (
      <div className="space-y-2 pb-3">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-2 pb-3">
        <Alert variant="destructive">
          <AlertDescription>{error.message}</AlertDescription>
        </Alert>
        <Button variant="outline" size="sm" onClick={onRefresh}>
          Retry
        </Button>
      </div>
    )
  }

  const rows = availability ?? []
  const short = rows.filter((row) => toDecimal(row.shortage_quantity).greaterThan(0))
  if (rows.length === 0) {
    return (
      <p className="pb-3 text-sm text-muted-foreground">
        The order carries no component lines, so there is nothing to reserve.
      </p>
    )
  }

  if (short.length === 0) {
    return (
      <p className="pb-3 text-sm text-success">
        Every component is covered. Untracked lines are reserved from free stock; each batch-tracked
        line is reserved against one lot with enough on hand. Releasing takes the reservations now.
      </p>
    )
  }

  return (
    <div className="space-y-2 pb-3">
      <Alert variant="warning">
        <AlertDescription>
          {short.length} line{short.length === 1 ? ' is' : 's are'} short. A release is refused
          while any line is short, so nothing below is reserved yet.
        </AlertDescription>
      </Alert>
      <ul className="space-y-1.5">
        {short.map((row) => (
          <li
            key={row.line_number}
            className="flex items-baseline justify-between gap-3 rounded-md border px-2.5 py-1.5 text-sm"
          >
            <span className="text-muted-foreground">Line {row.line_number}</span>
            <span className="font-medium tabular">
              short {formatQuantity(row.shortage_quantity)} of{' '}
              {formatQuantity(row.required_quantity)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function AssemblyDetailDialog({
  open,
  onOpenChange,
  orderId,
  canManage,
  canComplete,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  orderId: string
  /** `assembly_order.manage`. Gating an affordance only - the API authorises. */
  canManage: boolean
  /** `assembly.complete` - a different permission from the writes above. */
  canComplete: boolean
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [issueOpen, setIssueOpen] = useState(false)
  const [completeOpen, setCompleteOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [reasonFor, setReasonFor] = useState<
    'approve' | 'release' | 'start' | 'close' | 'cancel' | 'delete' | null
  >(null)
  const [directVerdictOpen, setDirectVerdictOpen] = useState(false)
  const [openingInspection, setOpeningInspection] = useState(false)
  // The linked inspection, opened in quality's own decide dialog. Only the id
  // is carried: the dialog reads the sheet itself.
  const [inspecting, setInspecting] = useState<QcInspectionRow | null>(null)

  const orderQuery = useQuery({
    queryKey: assemblyKeys.detail(orderId),
    queryFn: () => api.get<AssemblyOrderDetail>(`/assembly/${orderId}`),
    staleTime: staleTimes.transactional,
    enabled: open && Boolean(orderId),
  })

  const componentsQuery = useQuery({
    queryKey: assemblyKeys.components(orderId),
    queryFn: () => api.get<AssemblyComponent[]>(`/assembly/${orderId}/components`),
    // Availability is never served stale (master rules 34).
    staleTime: staleTimes.live,
    enabled: open && Boolean(orderId),
  })

  const order = orderQuery.data
  const components = useMemo(() => componentsQuery.data ?? [], [componentsQuery.data])

  // Live promisable stock per component, read only while a release is being
  // considered: it is never cached, and the shortage column is what the
  // release dialog shows before the bench is promised anything.
  const availabilityQuery = useQuery({
    queryKey: assemblyKeys.availability(orderId),
    queryFn: () => api.get<AssemblyComponentAvailability[]>(`/assembly/${orderId}/availability`),
    staleTime: staleTimes.live,
    enabled: open && Boolean(orderId) && reasonFor === 'release',
  })

  // The packing order that claimed this assembly's output, if one did. Read
  // for its code only; the packing screen owns everything else about it.
  const packingQuery = useQuery({
    queryKey: packingKeys.detail(order?.packing_order_id ?? ''),
    queryFn: () => api.get<PackingOrderDetail>(`/packing/${order?.packing_order_id}`),
    staleTime: staleTimes.transactional,
    enabled: open && Boolean(order?.packing_order_id),
  })

  const columns = useMemo<ColumnDef<AssemblyComponent, unknown>[]>(
    () => [
      {
        id: 'item',
        header: 'Component',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <span className="block text-2xs text-muted-foreground">
              Line {row.original.line_number}
            </span>
            <Link
              to={`/items/${row.original.item_id}`}
              className="font-mono text-xs hover:underline"
              title="Open this item"
            >
              {row.original.item_id.slice(0, 8)}
            </Link>
          </div>
        ),
      },
      {
        id: 'tracking',
        header: 'Tracking',
        enableSorting: false,
        // The kitting list flags serial tracking only; batch tracking is a
        // property of the item master, not of this row.
        cell: ({ row }) =>
          row.original.is_serial_tracked_component ? (
            <Badge variant="secondary">Serial</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'required',
        header: 'Required',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.required_quantity),
      },
      {
        id: 'issued',
        header: 'Issued',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.issued_quantity),
      },
      {
        id: 'consumed',
        header: 'Consumed',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.consumed_quantity),
      },
      {
        // Free stock and the shortage against it are gone: the kitting list no
        // longer carries an availability figure, and there is no per-component
        // availability endpoint to replace it. What the row does answer is how
        // much is still to draw and how much is sitting on the bench unused.
        id: 'outstanding',
        header: 'Still to issue',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const outstanding = outstandingQuantity(row.original)
          return outstanding.isZero() ? (
            <span className="text-success">Fully issued</span>
          ) : (
            <span className="font-medium text-warning">
              {formatQuantity(outstanding.toFixed())}
            </span>
          )
        },
      },
      {
        id: 'on_bench',
        header: 'On the bench',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(onBenchQuantity(row.original).toFixed()),
      },
    ],
    [],
  )

  const canIssue = Boolean(order && canManage && acceptsComponentIssue(order.status))
  // `POST /assembly/{id}/complete` is gated on `assembly.complete`, not on the
  // manage permission every other write on the order uses, so the two are asked
  // separately rather than one standing in for the other.
  const showComplete = Boolean(order && canComplete && isCompletable(order.status))

  // Every other write on the order declares `assembly_order.manage` - approve,
  // release, start, close and cancel alike - while the verdict declares
  // `qc.decide`. The two rights are therefore asked separately here, exactly
  // as the routes declare them.
  const showEdit = Boolean(order && canManage && isEditable(order.status))
  const showDelete = Boolean(order && canManage && isDeletable(order.status))
  const showApprove = Boolean(order && canManage && isApprovable(order.status))
  const showRelease = Boolean(order && canManage && isReleasable(order.status))
  const showStart = Boolean(order && canManage && isStartable(order.status))
  const showClose = Boolean(order && canManage && isClosable(order.status))
  const showCancel = Boolean(order && canManage && isCancellable(order.status))
  const showVerdict = Boolean(order && can(P.QC_DECIDE) && order.status === 'QC_PENDING')

  type LifecycleAction = 'approve' | 'release' | 'start' | 'close' | 'cancel' | 'delete'
  const lifecycle = useMutation({
    mutationFn: ({ action, reason }: { action: LifecycleAction; reason: string }) =>
      action === 'delete'
        ? api
            .delete<null>(`/assembly/${orderId}`, { params: { reason } })
            .then(() => null as AssemblyOrderDetail | null)
        : api.post<AssemblyOrderDetail>(`/assembly/${orderId}/${action}`, { reason }),
    onSuccess: (updated, { action }) => {
      void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
      // Release reserves every component and cancel gives them back, so the
      // allocation screens have moved with the order.
      if (action === 'release' || action === 'cancel' || action === 'close') {
        void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      }
      if (action === 'delete') {
        toast.success(success.deleted('Assembly order'), order?.code ?? '')
        // A deleted draft has no panel anymore.
        onOpenChange(false)
      } else if (updated && order) {
        const titles = {
          approve: success.approved('Assembly order'),
          release: success.released('Assembly order'),
          start: 'Assembly started',
          close: success.closed('Assembly order'),
          cancel: success.cancelled('Assembly order'),
        } satisfies Record<Exclude<LifecycleAction, 'delete'>, string>
        toast.success(
          titles[action],
          `${updated.code} is now ${humanise(updated.status).toLowerCase()}.`,
        )
      }
      setReasonFor(null)
    },
  })

  const openReason = (action: LifecycleAction) => {
    lifecycle.reset()
    setReasonFor(action)
  }

  // The linked inspection's sheet, opened with the inspection itself.
  //
  // The sheet's header reads the row it was opened with, so the inspection is
  // read first rather than passed as a bare id: an id alone renders a header
  // of `undefined`s while the readings load. `InspectionDetail` carries every
  // `InspectionSummary` field, so it opens the sheet directly.
  const openLinkedInspection = async () => {
    if (!order?.qc_inspection_id || openingInspection) return
    setOpeningInspection(true)
    try {
      setInspecting(
        await api.get<QcInspectionDetail>(`/quality/inspections/${order.qc_inspection_id}`),
      )
    } catch (error) {
      toast.failure(error, 'Could not open the inspection')
    } finally {
      setOpeningInspection(false)
    }
  }

  const reasonCopy: Record<
    LifecycleAction,
    { title: string; confirm: string; destructive?: boolean }
  > = {
    approve: {
      title: `Approve ${order?.code ?? 'order'}?`,
      confirm: 'Approve order',
    },
    release: {
      title: `Release ${order?.code ?? 'order'} to the bench?`,
      confirm: 'Release and reserve',
    },
    start: { title: `Start ${order?.code ?? 'order'}?`, confirm: 'Start assembly' },
    close: { title: `Close ${order?.code ?? 'order'}?`, confirm: 'Close order' },
    cancel: {
      title: `Cancel ${order?.code ?? 'order'}?`,
      confirm: 'Cancel order',
      destructive: true,
    },
    delete: {
      title: `Delete draft ${order?.code ?? 'order'}?`,
      confirm: 'Delete draft',
      destructive: true,
    },
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent size="xl">
          <DialogHeader>
            <DialogTitle className="flex flex-wrap items-center gap-2.5">
              {order?.code ?? 'Assembly order'}
              {order && <StatusBadge status={order.status} />}
            </DialogTitle>
            <DialogDescription>
              The kit issued against this order and what it produced.
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="space-y-5">
            {orderQuery.isLoading && (
              <div className="space-y-3">
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-40 w-full" />
              </div>
            )}

            {orderQuery.error && (
              <Alert variant="destructive">
                <AlertDescription>{orderQuery.error.message}</AlertDescription>
              </Alert>
            )}

            {order && (
              <>
                <DescriptionList
                  columns={3}
                  items={[
                    {
                      // The order carries ids only - no codes, no names.
                      label: 'Item',
                      value: (
                        <Link
                          to={`/items/${order.item_id}`}
                          className="font-mono text-xs hover:underline"
                        >
                          {order.item_id.slice(0, 8)}
                        </Link>
                      ),
                    },
                    {
                      label: 'Bill of materials',
                      value: (
                        <Link to={`/bom/${order.bom_id}`} className="hover:underline">
                          View BOM
                        </Link>
                      ),
                    },
                    {
                      // The row records what raised the order rather than a
                      // sales order link; a sales-order-sourced assembly puts
                      // the order id in `source_id`.
                      label: 'Raised by',
                      value:
                        order.source_type === 'SALES_ORDER' && order.source_id ? (
                          <Link to={`/sales-orders/${order.source_id}`} className="hover:underline">
                            Sales order
                          </Link>
                        ) : (
                          humanise(order.source_type)
                        ),
                    },
                    {
                      label: 'Planned',
                      value: formatQuantity(order.planned_quantity),
                    },
                    {
                      label: 'Assembled',
                      value: formatQuantity(order.assembled_quantity),
                    },
                    {
                      label: 'Rejected',
                      // A NUMERIC(18,4) column serialises as "0.0000", so a
                      // comparison against the literal "0" never matched and
                      // every untouched order read as having rejected output.
                      value: toDecimal(order.rejected_quantity).isZero() ? (
                        '-'
                      ) : (
                        <span className="text-destructive">
                          {formatQuantity(order.rejected_quantity)}
                        </span>
                      ),
                    },
                    { label: 'Assembly line', value: order.assembly_line ?? 'Not assigned' },
                    { label: 'Planned start', value: formatDate(order.planned_start) },
                    { label: 'Planned end', value: formatDate(order.planned_end) },
                    { label: 'Started', value: formatDateTime(order.actual_start) },
                    { label: 'Finished', value: formatDateTime(order.actual_end) },
                    {
                      // Whether QC has to clear the output before it can be
                      // packed is the difference between "done" and "done but
                      // still held", so it belongs on the header.
                      label: 'Quality',
                      value: !order.requires_qc ? (
                        'Not required'
                      ) : order.qc_inspection_id ? (
                        <Link to="/quality" className="hover:underline">
                          Inspection raised
                        </Link>
                      ) : (
                        <Badge variant="warning">Awaiting inspection</Badge>
                      ),
                    },
                    {
                      // The packing order that claimed the output, when one did:
                      // the assembly leaves the ready-for-packing list the
                      // moment it is marked packed against that order.
                      label: 'Packing order',
                      value: order.packing_order_id ? (
                        packingQuery.data ? (
                          <Link to="/packing" className="hover:underline">
                            {packingQuery.data.code}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">
                            {order.packed_at
                              ? `Packed ${formatDateTime(order.packed_at)}`
                              : 'Handed to packing'}
                          </span>
                        )
                      ) : order.packed_at ? (
                        formatDateTime(order.packed_at)
                      ) : (
                        'Not packed'
                      ),
                    },
                  ]}
                />

                {order.notes && (
                  <p className="border-t pt-4 text-sm text-muted-foreground">{order.notes}</p>
                )}

                <div>
                  <h3 className="mb-2 text-sm font-semibold">Components</h3>
                  <DataTable
                    columns={columns}
                    data={components}
                    isLoading={componentsQuery.isLoading}
                    isFetching={componentsQuery.isFetching}
                    error={componentsQuery.error as Error | null}
                    onRefresh={() => void componentsQuery.refetch()}
                    getRowId={(row) => row.id}
                    emptyTitle="No components"
                    emptyDescription="This order carries no bill of materials, so nothing has to be drawn from stock."
                  />
                </div>

                <div>
                  <h3 className="mb-2 text-sm font-semibold">Serial traceability</h3>
                  <SerialTraceability components={components} />
                </div>
              </>
            )}
          </DialogBody>

          <DialogFooter className="flex-wrap">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
            {showEdit && (
              <Button variant="outline" onClick={() => setEditOpen(true)}>
                Edit draft
              </Button>
            )}
            {showDelete && (
              <Button variant="outline" onClick={() => openReason('delete')}>
                Delete
              </Button>
            )}
            {showApprove && (
              <Button variant="outline" onClick={() => openReason('approve')}>
                Approve
              </Button>
            )}
            {showRelease && (
              <Button variant="outline" onClick={() => openReason('release')}>
                Release
              </Button>
            )}
            {showStart && (
              <Button variant="outline" onClick={() => openReason('start')}>
                Start
              </Button>
            )}
            {showClose && (
              <Button variant="outline" onClick={() => openReason('close')}>
                Close order
              </Button>
            )}
            {showCancel && (
              <Button variant="outline" onClick={() => openReason('cancel')}>
                Cancel
              </Button>
            )}
            {showVerdict && order && (
              <Button
                variant="outline"
                onClick={() =>
                  hasLinkedInspection(order)
                    ? void openLinkedInspection()
                    : setDirectVerdictOpen(true)
                }
                disabled={openingInspection}
              >
                {openingInspection ? 'Opening inspection...' : 'Record QC verdict'}
              </Button>
            )}
            {canIssue && (
              <Button variant="outline" onClick={() => setIssueOpen(true)}>
                <PackageMinus />
                Issue components
              </Button>
            )}
            {showComplete && (
              <Button onClick={() => setCompleteOpen(true)}>
                <CheckCircle2 />
                Complete
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {order && canIssue && (
        <IssueComponentsDialog
          open={issueOpen}
          onOpenChange={setIssueOpen}
          orderId={order.id}
          orderCode={order.code}
          components={components}
        />
      )}

      {order && showComplete && (
        <CompleteAssemblyDialog
          open={completeOpen}
          onOpenChange={setCompleteOpen}
          order={order}
          components={components}
          canSearchSerials={can(P.INVENTORY_VIEW)}
        />
      )}

      {order && showEdit && (
        <AssemblyOrderFormDialog open={editOpen} onOpenChange={setEditOpen} order={order} />
      )}

      {order && reasonFor && (
        <ReasonDialog
          open
          onOpenChange={(next) => {
            if (!next) setReasonFor(null)
          }}
          title={reasonCopy[reasonFor].title}
          description={
            reasonFor === 'release'
              ? 'Takes a hard reservation for every component at the position a release reserves. A component that is short refuses the whole release.'
              : reasonFor === 'cancel'
                ? 'Its component reservations are given back in the same transaction, so no part stays invisible to every other document while belonging to nobody.'
                : reasonFor === 'delete'
                  ? 'A deleted draft leaves no trail. Anything past a draft is cancelled instead, so the record survives.'
                  : undefined
          }
          confirmLabel={reasonCopy[reasonFor].confirm}
          destructive={reasonCopy[reasonFor].destructive}
          pending={lifecycle.isPending}
          error={lifecycle.error}
          onConfirm={(reason) => lifecycle.mutate({ action: reasonFor, reason })}
        >
          {reasonFor === 'release' && (
            <ReleaseReadiness
              availability={availabilityQuery.data}
              isLoading={availabilityQuery.isLoading}
              error={availabilityQuery.error as Error | null}
              onRefresh={() => void availabilityQuery.refetch()}
            />
          )}
        </ReasonDialog>
      )}

      {order && directVerdictOpen && (
        <AssemblyVerdictDialog
          open={directVerdictOpen}
          onOpenChange={setDirectVerdictOpen}
          order={order}
        />
      )}

      <InspectionDialog
        inspection={inspecting}
        open={Boolean(inspecting)}
        onOpenChange={(next) => {
          if (!next) {
            setInspecting(null)
            // Deciding the inspection completes the order through the owner
            // callback, so the panel re-reads it when the sheet closes.
            void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
          }
        }}
      />
    </>
  )
}

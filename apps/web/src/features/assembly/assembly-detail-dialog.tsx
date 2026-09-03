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
import { useFieldArray, useForm } from 'react-hook-form'
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
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import {
  formatDate,
  formatDateTime,
  formatNumber,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  acceptsComponentIssue,
  assemblyKeys,
  isCompletable,
  onBenchQuantity,
  outstandingQuantity,
  remainingToAssemble,
  type AssemblyComponent,
  type AssemblyCompletionResult,
  type AssemblyOrderDetail,
  type StockSerial,
} from './assembly-model'

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
    .min(3, 'Every hand-over is recorded with a reason of at least 3 characters')
    .max(255, 'At most 255 characters'),
  lines: z.array(
    z.object({
      // The API addresses a component by its line number on the order.
      line_number: z.number(),
      item_id: z.string(),
      label: z.string(),
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
  const [debouncedTerm, setDebouncedTerm] = useState('')

  // One request per keystroke would put a scan of the serial register behind
  // every character typed.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedTerm(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const params = useMemo(
    () => ({
      limit: 20,
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

const completeSchema = z.object({
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
    .min(3, 'The completion is recorded with a reason of at least 3 characters')
    .max(255, 'At most 255 characters'),
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

  const tracked = useMemo(
    () => components.filter((component) => component.is_serial_tracked_component),
    [components],
  )

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<CompleteFormValues>({
    resolver: zodResolver(completeSchema),
    defaultValues: {
      assembled_quantity: remainingToAssemble(order).toFixed(),
      rejected_quantity: '0',
      reason: 'Assembly completed',
    },
  })

  const orderId = order.id
  useEffect(() => {
    if (open) {
      reset({
        assembled_quantity: remainingToAssemble(order).toFixed(),
        rejected_quantity: '0',
        reason: 'Assembly completed',
      })
      setSerialsByLine({})
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
        // Only the serialised lines. A line that is not serial tracked "has
        // nothing to name" and is refused outright if it names anything.
        components: tracked.map((component) => ({
          line_number: component.line_number,
          serial_ids: (serialsByLine[component.line_number] ?? []).map((serial) => serial.id),
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
        `${result.order.code} completed`,
        result.output_held_for_qc
          ? `${formatQuantity(result.output_quantity)} received on QC hold${
              result.qc_inspection_created ? ' and an inspection was raised' : ''
            }; it is not available stock until it passes.`
          : `${formatQuantity(result.output_quantity)} received into stock from ${formatNumber(
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

            {tracked.length > 0 && (
              <div className="space-y-2">
                <p className="text-sm font-medium">Serial numbers consumed</p>
                <p className="text-xs text-muted-foreground">
                  One serial per unit consumed, for every serial-tracked line. The API works out how
                  many that is from what was produced and refuses the completion if the count does
                  not match.
                </p>
                {tracked.map((component) => (
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
  const [issueOpen, setIssueOpen] = useState(false)
  const [completeOpen, setCompleteOpen] = useState(false)

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
                          <Link
                            to={`/sales-orders/${order.source_id}`}
                            className="hover:underline"
                          >
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
                      label: 'Packed',
                      value: order.packed_at ? formatDateTime(order.packed_at) : 'Not packed',
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

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
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
    </>
  )
}

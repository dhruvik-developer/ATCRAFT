/**
 * The four things a buyer does with a purchase return: raise one, read it,
 * post it, and abandon it.
 *
 * Posting is the dialog that matters. It writes one `PURCHASE_RETURN` movement
 * per line, credits the goods receipt line and the purchase order line with the
 * returned quantity, and moves the document to a terminal POSTED. None of that
 * can be taken back - an append-only ledger is corrected by a further entry, not
 * by retracting one - so the confirmation enumerates what is about to leave
 * rather than asking "are you sure?" over a document code.
 *
 * Permissions, which are not all the same and are the thing this codebase gets
 * wrong most often:
 *
 * - reading a return and its lines: `purchase_return.view`
 * - raising, posting and cancelling one: `purchase_return.create` - the same
 *   right for all three, because the API declares it so on all three routes.
 *   Posting is *not* gated on a separate posting right the way a goods receipt
 *   is (`grn.post`); anyone who may raise a return may also move the stock.
 * - searching goods receipts to raise a return against one: `grn.view`, which
 *   is a different right from either of the above and belongs to a different
 *   module. An account with `purchase_return.create` and no `grn.view` cannot
 *   see anything to return, so the form says that instead of showing an empty
 *   picker and letting the user conclude nothing has ever been received.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  ChevronsUpDown,
  ExternalLink,
  FileDown,
  Loader2,
  PackageX,
  Search,
  Undo2,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useFieldArray, useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useUomOptions } from '@/features/items/item-model'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE, REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors, remapArrayIndices } from '@/lib/form-errors'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatPercent,
  formatQuantity,
  toDateInput,
  toDecimal,
} from '@/lib/format'
import { success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn, indexById } from '@/lib/utils'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'

import type { GoodsReceiptDetail, GoodsReceiptRow } from './procurement-types'
import {
  MAX_DEBIT_NOTE_LENGTH,
  MAX_LINE_NOTES_LENGTH,
  MAX_NOTES_LENGTH,
  goodsReceiptDetailKey,
  isReturnPostable,
  purchaseReturnKeys,
  returnableQuantity,
  type PurchaseReturnCreatePayload,
  type PurchaseReturnDetail,
  type PurchaseReturnPostResult,
  type PurchaseReturnRow,
  type ReturnReasonPayload,
} from './purchase-return-model'
import { sentLineIndices } from './server-field-errors'
import { PurchaseReturnDebitAllocation } from './purchase-return-debit-allocation'

/** NUMERIC(18,4): fourteen integer digits and four decimals, or nothing typed. */
const QUANTITY = /^$|^\d{1,14}(\.\d{1,4})?$/

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

// -------------------------------------------------------- receipt picker ----

/**
 * Posted goods receipts, searched on the server.
 *
 * `status: 'POSTED'` is sent as a filter rather than applied to a downloaded
 * page, for two reasons. It is what the API will accept - `create_purchase_return`
 * refuses anything else outright: "only material that was actually received can
 * be returned" - and narrowing in the browser would silently shorten each page
 * and hide matches further down it.
 */
function PostedReceiptPicker({
  value,
  onChange,
  invalid,
  vendorName,
  disabled,
}: {
  value: GoodsReceiptRow | null
  onChange: (receipt: GoodsReceiptRow) => void
  invalid: boolean
  vendorName: (vendorId: string) => string
  disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')

  // One request per keystroke would put a filtered scan of the receipt register
  // behind every character typed.
  const debouncedTerm = useDebouncedValue(term)

  const params = {
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    status: 'POSTED',
    sort_by: 'receipt_date',
    sort_dir: 'desc',
    ...(debouncedTerm ? { search: debouncedTerm } : {}),
  }

  const results = useQuery({
    queryKey: queryKeys.procurement.grns(params),
    queryFn: () => api.getPage<GoodsReceiptRow>('/procurement/goods-receipts', { params }),
    enabled: open && !disabled,
    staleTime: staleTimes.transactional,
  })

  const rows = results.data?.data ?? []

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id="return-receipt"
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-label="Goods receipt"
          aria-invalid={invalid}
          disabled={disabled}
          className={cn(
            'h-9 w-full justify-between font-normal',
            !value && 'text-muted-foreground',
            invalid && 'border-destructive',
          )}
        >
          <span className="truncate">
            {value ? `${value.code} - ${vendorName(value.vendor_id)}` : 'Search posted receipts'}
          </span>
          <ChevronsUpDown className="opacity-50" aria-hidden />
        </Button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        collisionPadding={8}
        className="w-[26rem] max-w-[var(--radix-popover-content-available-width)] p-0"
      >
        <div className="relative border-b p-2">
          <Search
            className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            autoFocus
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Search by receipt code, delivery note or invoice"
            aria-label="Search posted goods receipts"
            className="pl-8"
          />
        </div>

        <div
          className="max-h-72 overflow-y-auto p-1"
          role="listbox"
          aria-label="Posted goods receipts"
        >
          {results.isLoading && (
            <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              Searching...
            </p>
          )}

          {!results.isLoading && results.error && (
            <p className="px-2 py-6 text-sm text-destructive">{(results.error as Error).message}</p>
          )}

          {!results.isLoading && !results.error && rows.length === 0 && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              No posted goods receipt matches. Only a posted receipt has stock to send back.
            </p>
          )}

          {rows.map((receipt) => (
            <button
              key={receipt.id}
              type="button"
              role="option"
              aria-selected={receipt.id === value?.id}
              onClick={() => {
                onChange(receipt)
                setOpen(false)
              }}
              className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
            >
              <Check
                className={cn('mt-0.5 h-4 w-4 shrink-0', receipt.id !== value?.id && 'invisible')}
                aria-hidden
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{receipt.code}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {vendorName(receipt.vendor_id)} - received {formatDate(receipt.receipt_date)},{' '}
                  {formatQuantity(receipt.total_accepted_quantity)} accepted
                </span>
              </span>
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}

// -------------------------------------------------------------- raise one ---

const lineSchema = z.object({
  goods_receipt_line_id: z.string(),
  line_number: z.number(),
  description: z.string(),
  uom_id: z.string(),
  batch_number: z.string(),
  returnable_quantity: z.string(),
  unit_cost: z.string(),
  quantity: z.string().trim().regex(QUANTITY, 'Up to 4 decimal places'),
  reason: z.string().trim().max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
  notes: z
    .string()
    .trim()
    .max(MAX_LINE_NOTES_LENGTH, `At most ${MAX_LINE_NOTES_LENGTH} characters`),
})

const schema = z
  .object({
    return_date: z.string().min(1, validation.required('Return date')),
    reason: z
      .string()
      .trim()
      .min(REASON_MIN_LENGTH, `At least ${REASON_MIN_LENGTH} characters`)
      .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
    debit_note_number: z
      .string()
      .trim()
      .max(MAX_DEBIT_NOTE_LENGTH, `At most ${MAX_DEBIT_NOTE_LENGTH} characters`),
    notes: z.string().trim().max(MAX_NOTES_LENGTH, `At most ${MAX_NOTES_LENGTH} characters`),
    lines: z.array(lineSchema),
  })
  // Both rules mirror one the API enforces. Catching them here names the failing
  // line rather than having the whole document refused with a 422.
  .superRefine((values, ctx) => {
    let anyReturned = false

    values.lines.forEach((line, index) => {
      const quantity = toDecimal(line.quantity)
      if (quantity.lessThanOrEqualTo(0)) return
      anyReturned = true

      if (quantity.greaterThan(toDecimal(line.returnable_quantity))) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['lines', index, 'quantity'],
          message: `Only ${formatQuantity(line.returnable_quantity)} of this line is still returnable`,
        })
      }
    })

    if (!anyReturned) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['lines'],
        message: 'Enter a quantity on at least one line',
      })
    }
  })

type ReturnFormValues = z.infer<typeof schema>

/**
 * Whether a form line goes into the payload.
 *
 * One predicate for both the filter that builds the payload and the index map
 * that lands a server error back on the right form line - the two must agree,
 * or a 422 for payload line 0 is shown on a line nobody typed a quantity on.
 */
const isSentLine = (line: ReturnFormValues['lines'][number]): boolean =>
  toDecimal(line.quantity).greaterThan(0)

function emptyHeader(): Omit<ReturnFormValues, 'lines'> {
  return {
    return_date: toDateInput(new Date()),
    reason: '',
    debit_note_number: '',
    notes: '',
  }
}

function linesFrom(receipt: GoodsReceiptDetail): ReturnFormValues['lines'] {
  return (
    // `lines` is optional on `GoodsReceiptDetail` - the API declares it with a
    // default - so a reader copes with its absence rather than assuming an array.
    (receipt.lines ?? [])
      // A line with nothing left to return is not offered at all. Accepted less
      // already returned is what the API bounds against, and a line that has
      // gone back in full would only invite a second attempt that is refused.
      .filter((line) => returnableQuantity(line).greaterThan(0))
      .map((line) => ({
        goods_receipt_line_id: line.id,
        line_number: line.line_number,
        description: line.description ?? '',
        // The id, not the resolved code: the unit list may arrive after the
        // lines do, and rebuilding the form when it lands would wipe quantities
        // already typed. The label is resolved at render instead.
        uom_id: line.uom_id,
        batch_number: line.batch_number ?? '',
        returnable_quantity: returnableQuantity(line).toFixed(),
        unit_cost: line.unit_cost,
        quantity: '',
        reason: '',
        notes: '',
      }))
  )
}

export function PurchaseReturnFormDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [receipt, setReceipt] = useState<GoodsReceiptRow | null>(null)
  const [receiptTouched, setReceiptTouched] = useState(false)

  // Searching and reading goods receipts is `grn.view`, a different right from
  // the `purchase_return.create` that opened this dialog. Without it the picker
  // would 403 on every search and look like an empty receipt register.
  const canReadReceipts = can(P.GRN_VIEW)

  const vendors = useVendorOptions(open && can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const uoms = useUomOptions(open && can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])
  const warehouses = useWarehouseOptions(open && can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const detail = useQuery({
    queryKey: goodsReceiptDetailKey(receipt?.id ?? ''),
    queryFn: () => api.get<GoodsReceiptDetail>(`/procurement/goods-receipts/${receipt?.id}`),
    enabled: open && canReadReceipts && Boolean(receipt),
    staleTime: staleTimes.transactional,
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<ReturnFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { ...emptyHeader(), lines: [] },
  })

  const { fields } = useFieldArray({ control, name: 'lines' })

  useEffect(() => {
    if (!open) return
    setReceipt(null)
    setReceiptTouched(false)
    reset({ ...emptyHeader(), lines: [] })
  }, [open, reset])

  // Rebuilt when a different receipt is chosen, and only once its lines have
  // arrived - resetting on every refetch would wipe quantities already typed.
  const loadedReceiptId = detail.data?.id ?? null
  useEffect(() => {
    if (!detail.data) return
    reset({ ...emptyHeader(), lines: linesFrom(detail.data) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedReceiptId, reset])

  const create = useMutation({
    mutationFn: (values: ReturnFormValues) => {
      if (!receipt) throw new Error('No goods receipt chosen')

      const returned = values.lines.filter(isSentLine)

      // `PurchaseReturnCreate` forbids unknown keys, so every field here is one
      // it declares. Line numbers are re-issued 1..n over the lines actually
      // being returned, because the receipt's own numbering has gaps once the
      // untouched lines are dropped.
      const payload: PurchaseReturnCreatePayload = {
        goods_receipt_id: receipt.id,
        return_date: values.return_date,
        reason: values.reason.trim(),
        debit_note_number: orNull(values.debit_note_number),
        notes: orNull(values.notes),
        lines: returned.map((line, index) => ({
          line_number: index + 1,
          goods_receipt_line_id: line.goods_receipt_line_id,
          // Quantities stay strings on the wire; the API parses Decimals.
          quantity: line.quantity.trim(),
          reason: orNull(line.reason),
          notes: orNull(line.notes),
        })),
      }

      return api.post<PurchaseReturnDetail>('/procurement/purchase-returns', payload)
    },
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: purchaseReturnKeys.all })
      // Deliberately no inventory invalidation: raising a return is a document,
      // not a movement. Nothing has left the warehouse until it is posted.
      toast.success(
        success.created('Purchase return'),
        `${created.code} is a draft. The stock is still in the warehouse until the return is posted.`,
      )
      onOpenChange(false)
    },
    // Line errors are indexed by the payload, which holds only the lines with
    // a quantity - so they are re-addressed to the form line that sent them.
    onError: (error, values) =>
      applyFieldErrors(
        error,
        setError,
        remapArrayIndices('lines', sentLineIndices(values.lines, isSentLine)),
      ),
  })

  const lines = watch('lines')

  /**
   * What the goods are worth, before tax.
   *
   * Deliberately not called a debit note total: the API prices each line at the
   * receipt's unit cost and then adds tax at the *item's* GST rate, which the
   * receipt line does not carry. The taxed figure only exists once the return
   * has been created, and it is shown on the detail. Summed with decimal.js
   * because a running total of money in a double is how an invoice ends up a
   * paisa out.
   */
  const goodsValue = lines.reduce(
    (total, line) => total.plus(toDecimal(line.quantity).times(toDecimal(line.unit_cost))),
    toDecimal(0),
  )

  const source = detail.data ? warehouseById.get(detail.data.warehouse_id) : undefined

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Return goods to a vendor</DialogTitle>
          <DialogDescription>
            Choose the receipt the material came in on, then say how much of each line is going
            back. This raises a draft - posting it is what moves the stock.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => {
            if (!receipt) {
              setReceiptTouched(true)
              return
            }
            create.mutate(values)
          })}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            {!canReadReceipts && (
              <Alert variant="warning">
                <AlertTitle>Goods receipts are not visible to your account</AlertTitle>
                <AlertDescription>
                  A return is always raised against a posted receipt, and reading those needs the
                  goods receipt viewing right. Ask an administrator for it, or ask the stores team
                  to raise the return.
                </AlertDescription>
              </Alert>
            )}

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <div className="sm:col-span-2 lg:col-span-1">
                <Label htmlFor="return-receipt" required>
                  Goods receipt
                </Label>
                <div className="mt-1.5">
                  <PostedReceiptPicker
                    value={receipt}
                    onChange={(next) => {
                      setReceipt(next)
                      setReceiptTouched(true)
                    }}
                    invalid={receiptTouched && !receipt}
                    disabled={!canReadReceipts}
                    vendorName={(vendorId) => vendorById.get(vendorId)?.name ?? 'vendor'}
                  />
                </div>
                {receiptTouched && !receipt && (
                  <FieldError message="Choose the receipt the material came in on" />
                )}
                {source && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    The stock leaves {source.code} - {source.name}, where the receipt put it.
                  </p>
                )}
              </div>

              <div>
                <Label htmlFor="return-date" required>
                  Return date
                </Label>
                <Input
                  id="return-date"
                  type="date"
                  {...register('return_date')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.return_date)}
                />
                <FieldError message={errors.return_date?.message} />
              </div>

              <div>
                <Label htmlFor="return-debit-note">Debit note number</Label>
                <Input
                  id="return-debit-note"
                  {...register('debit_note_number')}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.debit_note_number)}
                  aria-describedby="return-debit-note-hint"
                />
                <FieldError message={errors.debit_note_number?.message} />
                <p id="return-debit-note-hint" className="mt-1 text-xs text-muted-foreground">
                  Your own reference for the debit raised on the vendor. Optional.
                </p>
              </div>

              <div className="sm:col-span-2 lg:col-span-3">
                <Label htmlFor="return-reason" required>
                  Reason
                </Label>
                <Input
                  id="return-reason"
                  {...register('reason')}
                  autoComplete="off"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.reason)}
                  aria-describedby="return-reason-hint"
                />
                <FieldError message={errors.reason?.message} />
                <p id="return-reason-hint" className="mt-1 text-xs text-muted-foreground">
                  Recorded on the document, on every stock movement it writes, and in the audit
                  trail. A line can override it with a reason of its own.
                </p>
              </div>
            </div>

            {!receipt && canReadReceipts && (
              <EmptyState
                icon={PackageX}
                title="Choose the receipt first"
                description="Its lines appear here with how much of each is still returnable."
              />
            )}

            {receipt && detail.isLoading && (
              <div className="space-y-2" aria-hidden>
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-20 w-full" />
              </div>
            )}

            {receipt && !detail.isLoading && detail.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load the receipt lines</AlertTitle>
                <AlertDescription>{(detail.error as Error).message}</AlertDescription>
              </Alert>
            )}

            {receipt && !detail.isLoading && !detail.error && fields.length === 0 && (
              <EmptyState
                title="Nothing on this receipt can be returned"
                description="Every line was either rejected at the gate - so it never became stock - or has already gone back in full. Choose a different receipt."
              />
            )}

            {fields.length > 0 && (
              <div className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium">Lines</h3>
                  <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />
                </div>

                {fields.map((field, index) => (
                  <div key={field.id} className="rounded-lg border p-3">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <p className="min-w-0 font-medium">
                        <span className="tabular mr-2 text-muted-foreground">
                          {field.line_number}
                        </span>
                        {field.description || 'Item'}
                      </p>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge variant="muted">
                          Returnable{' '}
                          {formatQuantity(
                            field.returnable_quantity,
                            uomById.get(field.uom_id)?.code,
                          )}
                        </Badge>
                        {/* The lot the goods arrived on. The return leaves
                            against the same one, which is what keeps a
                            batch-traced item addressable afterwards. */}
                        {field.batch_number && (
                          <Badge variant="secondary">Lot {field.batch_number}</Badge>
                        )}
                      </div>
                    </div>

                    <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                      <div>
                        <Label htmlFor={`return-quantity-${index}`}>Returning</Label>
                        <Input
                          id={`return-quantity-${index}`}
                          {...register(`lines.${index}.quantity`)}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                        />
                        <FieldError message={errors.lines?.[index]?.quantity?.message} />
                      </div>

                      <div className="lg:col-span-2">
                        <Label htmlFor={`return-line-reason-${index}`}>Line reason</Label>
                        <Input
                          id={`return-line-reason-${index}`}
                          {...register(`lines.${index}.reason`)}
                          autoComplete="off"
                          placeholder="Falls back to the document reason"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.reason)}
                        />
                        <FieldError message={errors.lines?.[index]?.reason?.message} />
                      </div>

                      <div>
                        <Label htmlFor={`return-line-notes-${index}`}>Line notes</Label>
                        <Input
                          id={`return-line-notes-${index}`}
                          {...register(`lines.${index}.notes`)}
                          autoComplete="off"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.notes)}
                        />
                        <FieldError message={errors.lines?.[index]?.notes?.message} />
                      </div>
                    </div>

                    {/* Unsymbolised on purpose: neither `GoodsReceiptDetail`
                        nor its lines carry a currency, so printing a rupee sign
                        here would be this screen's invention. The return itself
                        states its currency once it exists. */}
                    <p className="mt-2 text-xs text-muted-foreground">
                      Received at {formatMoney(field.unit_cost, { showSymbol: false })} each. The
                      return is credited at that same cost.
                    </p>
                  </div>
                ))}

                {goodsValue.greaterThan(0) && (
                  <p className="text-right text-sm">
                    <span className="text-muted-foreground">
                      Goods value in the order&apos;s currency, before tax:{' '}
                    </span>
                    <span className="tabular font-medium">
                      {formatMoney(goodsValue.toFixed(), { showSymbol: false })}
                    </span>
                  </p>
                )}
              </div>
            )}

            <div>
              <Label htmlFor="return-notes">Notes</Label>
              <Textarea
                id="return-notes"
                {...register('notes')}
                className="mt-1.5"
                aria-invalid={Boolean(errors.notes)}
              />
              <FieldError message={errors.notes?.message} />
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
            <Button type="submit" loading={create.isPending} disabled={fields.length === 0}>
              Raise draft return
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------- read one -----

export function PurchaseReturnDetailDialog({
  returnId,
  onClose,
  onPost,
  onCancel,
  canAllocate = false,
}: {
  returnId: string | null
  onClose: () => void
  /** Opens the posting confirmation. Only passed when the caller may post. */
  onPost?: (row: PurchaseReturnDetail) => void
  onCancel?: (row: PurchaseReturnDetail) => void
  canAllocate?: boolean
}) {
  const { can } = useAuth()
  const job = useReportJob()
  // A stable callback, so closing one return cannot leave the next one watching
  // the previous debit-note job.
  const resetJob = job.reset
  const pdf = useMutation({
    mutationFn: () =>
      api.post<AcceptedJobData>(
        `/procurement/purchase-returns/${returnId}/debit-note-pdf`,
        undefined,
      ),
    onSuccess: (accepted) => job.start(accepted),
  })

  const vendors = useVendorOptions(Boolean(returnId) && can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const warehouses = useWarehouseOptions(Boolean(returnId) && can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])
  const uoms = useUomOptions(Boolean(returnId) && can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  const query = useQuery({
    queryKey: purchaseReturnKeys.detail(returnId ?? ''),
    queryFn: () => api.get<PurchaseReturnDetail>(`/procurement/purchase-returns/${returnId}`),
    enabled: Boolean(returnId),
    staleTime: staleTimes.transactional,
  })

  useEffect(() => {
    resetJob()
  }, [resetJob, returnId])

  const data = query.data
  const currency = data?.currency
  // `lines` is optional on the wire, exactly as it is on a goods receipt.
  const lines = data?.lines ?? []
  const vendor = data ? vendorById.get(data.vendor_id) : undefined
  const warehouse = data ? warehouseById.get(data.warehouse_id) : undefined

  return (
    <Dialog
      open={Boolean(returnId)}
      onOpenChange={(open) => {
        if (!open) {
          job.reset()
          onClose()
        }
      }}
    >
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{data ? `Purchase return ${data.code}` : 'Purchase return'}</DialogTitle>
          <DialogDescription>
            What is going back to the vendor, and whether it has actually left.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {query.isLoading && (
            <div className="space-y-3" aria-hidden>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-24 w-full" />
            </div>
          )}

          {!query.isLoading && query.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this purchase return</AlertTitle>
              <AlertDescription>{query.error.message}</AlertDescription>
            </Alert>
          )}

          {data && (
            <>
              <DescriptionList
                columns={4}
                items={[
                  { label: 'Status', value: <StatusBadge status={data.status} /> },
                  { label: 'Return date', value: formatDate(data.return_date) },
                  {
                    label: 'Vendor',
                    value: vendor ? `${vendor.code} - ${vendor.name}` : 'Not resolved',
                  },
                  {
                    label: 'Leaves',
                    value: warehouse ? `${warehouse.code} - ${warehouse.name}` : 'Not resolved',
                  },
                  {
                    label: 'Debit note',
                    value: data.debit_note_number ?? (
                      <span className="text-muted-foreground">Not raised</span>
                    ),
                  },
                  { label: 'Currency', value: data.currency },
                  {
                    // The two terminal states are reported differently on
                    // purpose: posted means the stock physically left, cancelled
                    // means the document was abandoned and nothing moved.
                    label: 'Stock',
                    value:
                      data.status === 'POSTED' ? (
                        <span className="text-success">
                          Left on {formatDateTime(data.posted_at)}
                        </span>
                      ) : data.status === 'CANCELLED' ? (
                        <span className="text-muted-foreground">Never left - return abandoned</span>
                      ) : (
                        <span className="text-warning">Still in the warehouse</span>
                      ),
                  },
                  {
                    label: 'Against',
                    // The receipt arrives as an id and there is no screen that
                    // addresses one receipt, so it is not offered as a link. The
                    // purchase order has a detail route and is nullable on the
                    // wire - a receipt can outlive the order reference.
                    value: data.purchase_order_id ? (
                      <Link
                        to={`/procurement/purchase-orders/${data.purchase_order_id}`}
                        className="inline-flex items-center gap-1 hover:underline"
                      >
                        Purchase order
                        <ExternalLink className="h-3 w-3" aria-hidden />
                      </Link>
                    ) : (
                      <span className="text-muted-foreground">No purchase order</span>
                    ),
                  },
                ]}
              />

              <Alert variant="info">
                <AlertTitle>Reason</AlertTitle>
                <AlertDescription>{data.reason}</AlertDescription>
              </Alert>

              {canAllocate && data.status === 'POSTED' && (
                <PurchaseReturnDebitAllocation purchaseReturn={data} />
              )}

              {data.cancellation_reason && (
                <Alert variant="warning">
                  <AlertTitle>Cancelled</AlertTitle>
                  <AlertDescription>{data.cancellation_reason}</AlertDescription>
                </Alert>
              )}

              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[40rem] text-sm">
                  <caption className="sr-only">Purchase return lines</caption>
                  <thead className="whitespace-nowrap bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th scope="col" className="px-3 py-2 text-left font-medium">
                        #
                      </th>
                      <th scope="col" className="px-3 py-2 text-left font-medium">
                        Item
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Quantity
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Unit price
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Tax rate
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Tax
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Line total
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {lines.length === 0 && (
                      <tr>
                        <td colSpan={7} className="px-3 py-8 text-center text-muted-foreground">
                          This return has no lines, so there is nothing to post. Cancel it and raise
                          a new one against the receipt.
                        </td>
                      </tr>
                    )}
                    {lines.map((line) => (
                      <tr key={line.id}>
                        <td className="px-3 py-2 tabular text-muted-foreground">
                          {line.line_number}
                        </td>
                        <td className="max-w-[18rem] px-3 py-2">
                          <span className="block truncate">{line.description ?? 'Item'}</span>
                          {line.reason && (
                            <span className="block truncate text-xs text-muted-foreground">
                              {line.reason}
                            </span>
                          )}
                          {/* Only that a lot is involved: the line carries the
                              batch id and no batch number to name it with. */}
                          {line.batch_id && (
                            <span className="block text-2xs text-muted-foreground">
                              Returned against the lot it arrived on
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {formatQuantity(line.quantity, uomById.get(line.uom_id)?.code)}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {formatMoney(line.unit_price, { currency })}
                        </td>
                        <td className="px-3 py-2 text-right tabular text-muted-foreground">
                          {formatPercent(line.tax_rate, 2)}
                        </td>
                        <td className="px-3 py-2 text-right tabular">
                          {formatMoney(line.tax_amount, { currency })}
                        </td>
                        <td className="px-3 py-2 text-right tabular font-medium">
                          {formatMoney(line.line_total, { currency })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <DescriptionList
                columns={3}
                items={[
                  { label: 'Subtotal', value: formatMoney(data.subtotal, { currency }) },
                  { label: 'Tax', value: formatMoney(data.tax_amount, { currency }) },
                  {
                    label: 'Total debited',
                    value: (
                      <span className="font-medium">
                        {formatMoney(data.total_amount, { currency })}
                      </span>
                    ),
                  },
                ]}
              />

              {data.status === 'POSTED' && toDecimal(data.total_amount).greaterThan(0) && (
                <section className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="outline"
                    onClick={() => pdf.mutate()}
                    loading={pdf.isPending || job.phase === 'queued' || job.phase === 'running'}
                  >
                    <FileDown />
                    {job.phase === 'failed' ? 'Retry debit note PDF' : 'Generate debit note PDF'}
                  </Button>
                  {job.phase === 'ready' && (
                    <Button variant="outline" onClick={job.download} loading={job.isDownloading}>
                      Download debit note PDF
                    </Button>
                  )}
                  {job.error && (
                    <p role="alert" className="text-sm text-destructive">
                      Could not check PDF status: {job.error.message}
                    </p>
                  )}
                  {job.phase === 'failed' && job.job?.error_message && (
                    <p role="alert" className="text-sm text-destructive">
                      {job.job.error_message}
                    </p>
                  )}
                  {job.error && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={job.retry}
                      loading={job.isRetrying}
                    >
                      Retry status
                    </Button>
                  )}
                </section>
              )}

              {data.notes && (
                <div>
                  <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Notes
                  </h3>
                  <p className="mt-1 whitespace-pre-wrap text-sm">{data.notes}</p>
                </div>
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          {data && isReturnPostable(data.status) && onCancel && (
            <Button variant="destructive" onClick={() => onCancel(data)}>
              Cancel return
            </Button>
          )}
          {data && isReturnPostable(data.status) && onPost && (
            <Button onClick={() => onPost(data)} disabled={lines.length === 0}>
              <Undo2 />
              Post return
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------- post one -----

/**
 * Confirm a posting, having first said exactly what it will do.
 *
 * The lines are fetched rather than summarised from the list row, because
 * `PurchaseReturnSummary` carries totals but no line count and no quantities -
 * and "three lines, 240 NOS out of WH-01" is the sentence that lets somebody
 * notice this is the wrong document before the stock is gone. The query shares
 * its key with the detail dialog, so reading a return and then posting it costs
 * one request rather than two.
 */
export function PurchaseReturnPostDialog({
  purchaseReturn,
  onOpenChange,
}: {
  purchaseReturn: PurchaseReturnRow | null
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const warehouses = useWarehouseOptions(Boolean(purchaseReturn) && can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])
  const uoms = useUomOptions(Boolean(purchaseReturn) && can(P.UOM_VIEW))
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  const detail = useQuery({
    queryKey: purchaseReturnKeys.detail(purchaseReturn?.id ?? ''),
    queryFn: () =>
      api.get<PurchaseReturnDetail>(`/procurement/purchase-returns/${purchaseReturn?.id}`),
    enabled: Boolean(purchaseReturn),
    staleTime: staleTimes.transactional,
  })

  const post = useMutation({
    mutationFn: (row: PurchaseReturnRow) =>
      // No body: `POST /purchase-returns/{id}/post` takes none. The reason on
      // the document is what every movement it writes is stamped with.
      api.post<PurchaseReturnPostResult>(`/procurement/purchase-returns/${row.id}/post`),
    onSuccess: (result) => {
      // Everything procurement holds is dropped, not just the return: posting
      // writes the returned quantity back onto the goods receipt line and the
      // purchase order line it reverses.
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      // And stock actually moved, so balances and availability are dropped
      // rather than left to expire (master rules 34).
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      const financialMessage = result.debit_note_journal_id
        ? 'The vendor debit note and input-tax reversal were posted to finance.'
        : 'No finance journal was needed because the return has zero value.'
      toast.success(
        success.posted('Purchase return'),
        `${result.purchase_return.code} wrote ${result.movements.length} stock movement(s). The receipt and the order now show the returned quantity. ${financialMessage}`,
      )
      onOpenChange(false)
    },
  })

  const lines = detail.data?.lines ?? []
  const warehouse = purchaseReturn ? warehouseById.get(purchaseReturn.warehouse_id) : undefined
  // The API refuses a return with no lines outright - "has no lines to post" -
  // so the button is disabled rather than offering a certain failure. A detail
  // that has not loaded yet is not the same thing, hence the explicit check.
  const nothingToPost = Boolean(detail.data) && lines.length === 0

  return (
    <Dialog open={Boolean(purchaseReturn)} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {purchaseReturn ? `Post ${purchaseReturn.code}?` : 'Post purchase return'}
          </DialogTitle>
          <DialogDescription>
            This moves the goods and posts the vendor debit note and input-tax reversal in the same
            transaction. Everything before it is paperwork.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <Alert variant="warning">
            <AlertTitle>Posting cannot be undone</AlertTitle>
            <AlertDescription>
              A posted return is terminal: it can never be cancelled, edited or reversed. If the
              goods come back from the vendor, that is a new receipt, not an undo.
            </AlertDescription>
          </Alert>

          <div>
            <h3 className="text-sm font-medium">What posting will do</h3>
            <ul className="mt-1.5 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {/* The quantities are listed per line below and deliberately not
                  added up: lines can be in different units of measure, and a
                  single figure summing NOS to KG would be a number that means
                  nothing. */}
              <li>
                Take {lines.length > 0 ? `all ${lines.length} line(s)` : 'every line'} of this
                return out of{' '}
                {warehouse ? `${warehouse.code} - ${warehouse.name}` : 'the receiving warehouse'},
                against the same lot each line arrived on.
              </li>
              <li>
                Credit the goods receipt line and the purchase order line with what went back, so
                neither can be returned twice.
              </li>
              <li>
                Debit the vendor{' '}
                {purchaseReturn
                  ? formatMoney(purchaseReturn.total_amount, {
                      currency: purchaseReturn.currency,
                    })
                  : ''}
                .
              </li>
            </ul>
          </div>

          {/* The movement is written against available stock. Anything still
              held for inspection is quality's to release first, and saying so
              here is cheaper than a failed posting at the loading bay. */}
          <p className="text-xs text-muted-foreground">
            The quantity leaves available stock. Material still sitting on QC hold has to be
            released by quality before it can go back to the vendor.
          </p>

          {detail.isLoading && (
            <div className="space-y-2" aria-hidden>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          )}

          {!detail.isLoading && detail.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load the lines</AlertTitle>
              <AlertDescription>
                {detail.error.message} Post without reading them only if you raised this return
                yourself.
              </AlertDescription>
            </Alert>
          )}

          {nothingToPost && (
            <Alert variant="destructive">
              <AlertTitle>This return has no lines</AlertTitle>
              <AlertDescription>
                There is nothing to move, and the API refuses a posting without lines. Cancel it and
                raise a new return against the receipt.
              </AlertDescription>
            </Alert>
          )}

          {lines.length > 0 && (
            <ul className="divide-y rounded-lg border text-sm">
              {lines.map((line) => (
                <li key={line.id} className="flex items-start justify-between gap-3 px-3 py-2">
                  <span className="min-w-0">
                    <span className="tabular mr-2 text-muted-foreground">{line.line_number}</span>
                    {line.description ?? 'Item'}
                  </span>
                  <span className="tabular shrink-0 font-medium">
                    {formatQuantity(line.quantity, uomById.get(line.uom_id)?.code)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={post.isPending}>
            Keep as draft
          </Button>
          <Button
            onClick={() => purchaseReturn && post.mutate(purchaseReturn)}
            loading={post.isPending}
            disabled={nothingToPost}
          >
            <Undo2 />
            Post and move the stock
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------- abandon one ----

/**
 * Cancel a draft return.
 *
 * The reason is not optional courtesy: `ReasonRequest` requires 3..255
 * characters and the API stores it on the document and in the audit trail, so
 * the dialog collects it rather than letting a 422 come back with nothing typed.
 */
export function PurchaseReturnCancelDialog({
  purchaseReturn,
  onOpenChange,
}: {
  purchaseReturn: PurchaseReturnRow | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    if (!purchaseReturn) return
    setReason('')
    setTouched(false)
  }, [purchaseReturn])

  const cancel = useMutation({
    mutationFn: ({ row, text }: { row: PurchaseReturnRow; text: string }) => {
      const payload: ReturnReasonPayload = { reason: text }
      return api.post<PurchaseReturnRow>(`/procurement/purchase-returns/${row.id}/cancel`, payload)
    },
    onSuccess: (row) => {
      void queryClient.invalidateQueries({ queryKey: purchaseReturnKeys.all })
      // No inventory invalidation: a cancelled draft never moved anything.
      toast.success(
        success.cancelled('Purchase return'),
        `${row.code} is closed and no stock moved.`,
      )
      onOpenChange(false)
    },
  })

  const tooShort = reason.trim().length < REASON_MIN_LENGTH

  return (
    <ConfirmDialog
      open={Boolean(purchaseReturn)}
      onOpenChange={onOpenChange}
      title={purchaseReturn ? `Cancel ${purchaseReturn.code}?` : 'Cancel purchase return'}
      description="The draft is closed and can never be posted. The material stays where it is - cancelling is not the same as sending the goods back."
      confirmLabel="Cancel return"
      cancelLabel="Keep it"
      destructive
      loading={cancel.isPending}
      onConfirm={() => {
        if (!purchaseReturn) return
        if (tooShort) {
          setTouched(true)
          return
        }
        cancel.mutate({ row: purchaseReturn, text: reason.trim() })
      }}
    >
      <div>
        <Label htmlFor="return-cancel-reason" required>
          Reason
        </Label>
        <Input
          id="return-cancel-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={REASON_MAX_LENGTH}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={touched && tooShort}
        />
        {touched && tooShort && (
          <FieldError message={`Record at least ${REASON_MIN_LENGTH} characters of reason`} />
        )}
      </div>
    </ConfirmDialog>
  )
}

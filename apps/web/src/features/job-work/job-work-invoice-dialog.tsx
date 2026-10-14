/**
 * Record a job-work invoice: pick the vendor, then an issued order of that
 * vendor, then the receipt lines to bill.
 *
 * The dialog walks the same path the API validates: billable lines arrive from
 * `GET /job-work/{id}/billable-lines` with the unbilled charges prefilled, and
 * the payload posts `invoice_type: JOB_WORK` with `job_work_order_id` and one
 * `job_work_receipt_line_id` per line. Saving creates the bill in DRAFT and
 * submits it in the same action, so the approver sees the match straight away.
 * Approval itself stays on the vendor-invoices screen - whoever keys a bill
 * must not be the person who clears it.
 *
 * Money never passes through a float: quantities and prices stay decimal
 * strings to the wire, and the live totals reuse `priceInvoiceLine` from the
 * finance feature, which mirrors the server's pricing step for step.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

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
  Checkbox,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import {
  financeKeys,
  invoiceTotals,
  priceInvoiceLine,
  type VendorInvoiceCreatePayload,
  type VendorInvoiceDetail,
} from '@/features/finance/finance-types'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, SELECT_NONE } from '@/lib/constants'
import { formatDate, formatMoney, humanise, toDateInput, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { DecimalString } from '@/types/api'

import {
  jobWorkKeys,
  useBillableLines,
  type BillableLine,
  type JobWorkOrderRow,
} from './job-work-model'

/** `Quantity` is NUMERIC(18,4), `UnitPrice` NUMERIC(18,6). */
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const RATE = /^\d{1,12}(\.\d{1,6})?$/
const PERCENT = /^(\d{1,3}(\.\d{1,4})?)?$/

/** One billable line with the clerk's edits on top of the server's figures. */
interface LineEdit {
  selected: boolean
  quantity: string
  unit_price: string
  tax_rate: string
}

function editFrom(line: BillableLine): LineEdit {
  return {
    selected: true,
    quantity: line.output_quantity,
    unit_price: line.rate_per_unit,
    tax_rate: line.tax_rate ?? '',
  }
}

/** The orders of one vendor, newest first, for the order step. */
function useVendorOrders(vendorId: string, enabled: boolean) {
  return useQuery({
    queryKey: [...jobWorkKeys.all, 'vendor-orders', vendorId] as const,
    queryFn: () =>
      api.getPage<JobWorkOrderRow>('/job-work', {
        params: {
          vendor_id: vendorId,
          limit: LOOKUP_LIST_LIMIT,
          sort_by: 'created_at',
          sort_dir: 'desc',
        },
      }),
    staleTime: staleTimes.transactional,
    enabled: enabled && Boolean(vendorId),
  })
}

export function JobWorkInvoiceDialog({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The saved-then-submitted invoice, so the caller can point at it. */
  onSaved?: (invoice: VendorInvoiceDetail) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const [vendorId, setVendorId] = useState<string | null>(null)
  const [orderId, setOrderId] = useState<string>('')
  const [edits, setEdits] = useState<Record<string, LineEdit>>({})
  const [invoiceNumber, setInvoiceNumber] = useState('')
  const [invoiceDate, setInvoiceDate] = useState(() => toDateInput(new Date()))
  const [isInterstate, setIsInterstate] = useState(false)
  const [touched, setTouched] = useState(false)

  const canPickVendor = can(P.VENDOR_VIEW)

  // A fresh dialog every time it opens: a vendor left over from the previous
  // bill would silently scope the order list.
  useEffect(() => {
    if (!open) return
    setVendorId(null)
    setOrderId('')
    setEdits({})
    setInvoiceNumber('')
    setInvoiceDate(toDateInput(new Date()))
    setIsInterstate(false)
    setTouched(false)
  }, [open])

  const ordersQuery = useVendorOrders(vendorId ?? '', open && Boolean(vendorId))
  const orders = useMemo(() => ordersQuery.data?.data ?? [], [ordersQuery.data])
  const order = orders.find((entry) => entry.id === orderId) ?? null

  const linesQuery = useBillableLines(orderId, open && Boolean(orderId))
  const billable: BillableLine[] = useMemo(() => linesQuery.data ?? [], [linesQuery.data])

  // The line set belongs to the order on screen: selecting another order
  // starts from its unbilled charges rather than carrying yesterday's edits.
  useEffect(() => {
    if (!open || !orderId) return
    if (!linesQuery.data) return
    const next: Record<string, LineEdit> = {}
    for (const line of linesQuery.data) next[line.job_work_receipt_line_id] = editFrom(line)
    setEdits(next)
  }, [open, orderId, linesQuery.data])

  const setEdit = (id: string, patch: Partial<LineEdit>) =>
    setEdits((current) => {
      const prev = current[id]
      if (!prev) return current
      return { ...current, [id]: { ...prev, ...patch } }
    })

  const priced = useMemo(
    () =>
      billable.map((line) => {
        const edit = edits[line.job_work_receipt_line_id]
        if (!edit?.selected) return null
        if (!QUANTITY.test(edit.quantity.trim()) || !RATE.test(edit.unit_price.trim())) return null
        if (!PERCENT.test(edit.tax_rate.trim())) return null
        return {
          line,
          edit,
          pricing: priceInvoiceLine({
            quantity: edit.quantity.trim() as DecimalString,
            unit_price: edit.unit_price.trim() as DecimalString,
            discount_amount: '0' as DecimalString,
            tax_rate: (edit.tax_rate.trim() === '' ? '0' : edit.tax_rate.trim()) as DecimalString,
            is_interstate: isInterstate,
          }),
        }
      }),
    [billable, edits, isInterstate],
  )

  const chosen = priced.filter((entry) => entry !== null)
  const totals = useMemo(
    () =>
      invoiceTotals(
        chosen.map((entry) => entry.pricing),
        '0',
      ),
    [chosen],
  )
  const pendingRecovery = useMemo(
    () =>
      billable
        .filter((line) => edits[line.job_work_receipt_line_id]?.selected)
        .reduce((sum, line) => sum.plus(toDecimal(line.pending_vendor_debit)), toDecimal(0)),
    [billable, edits],
  )

  // The failures the server would report, caught before the round trip. The
  // over-bill is re-checked under a row lock at approval, so this is guidance,
  // not the guarantee.
  const problems = useMemo(() => {
    const found: string[] = []
    if (!vendorId) found.push('Choose the vendor who sent the bill.')
    if (vendorId && !orderId) found.push('Choose the job-work order being billed.')
    if (orderId && chosen.length === 0) found.push('Tick at least one receipt line to bill.')
    for (const entry of chosen) {
      const quantity = toDecimal(entry.edit.quantity)
      if (quantity.greaterThan(toDecimal(entry.line.output_quantity))) {
        found.push(
          `Line ${entry.line.line_number} bills more than the ${entry.line.output_quantity} good output delivered.`,
        )
      }
      if (entry.pricing.taxable.greaterThan(toDecimal(entry.line.unbilled_amount))) {
        found.push(
          `Line ${entry.line.line_number} bills more than the ${entry.line.unbilled_amount} still unbilled.`,
        )
      }
    }
    if (invoiceNumber.trim() === '') found.push('Enter the vendor invoice number.')
    if (invoiceDate === '') found.push('Enter the vendor invoice date.')
    return found
  }, [vendorId, orderId, chosen, invoiceNumber, invoiceDate])

  const save = useMutation({
    mutationFn: async () => {
      const payload: VendorInvoiceCreatePayload = {
        vendor_id: vendorId ?? '',
        vendor_invoice_number: invoiceNumber.trim(),
        invoice_date: invoiceDate,
        invoice_type: 'JOB_WORK',
        job_work_order_id: orderId,
        round_off_amount: '0',
        lines: chosen.map((entry) => ({
          job_work_receipt_line_id: entry.line.job_work_receipt_line_id,
          description: `Processing charges - receipt ${entry.line.receipt_code} line ${entry.line.line_number}`,
          hsn_code: order?.sac_code ?? entry.line.sac_code ?? null,
          quantity: entry.edit.quantity.trim(),
          unit_price: entry.edit.unit_price.trim(),
          discount_amount: '0',
          tax_rate: (() => {
            const raw = entry.edit.tax_rate.trim()
            return raw === '' ? '0' : raw
          })(),
          is_interstate: isInterstate,
        })),
      }
      const created = await api.post<VendorInvoiceDetail>('/finance/vendor-invoices', payload)
      return api.post<VendorInvoiceDetail>(`/finance/vendor-invoices/${created.id}/submit`)
    },
    onSuccess: (submitted) => {
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
      toast.success(
        success.submitted('Job-work invoice'),
        `${submitted.code} is awaiting approval.`,
      )
      onOpenChange(false)
      onSaved?.(submitted)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Record job-work invoice</DialogTitle>
          <DialogDescription>
            Bill an issued job-work order against its receipt lines. Saving submits the bill, so the
            approver sees the match straight away.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {!canPickVendor && (
            <Alert variant="warning">
              <AlertTitle>You cannot pick the vendor from here</AlertTitle>
              <AlertDescription>
                Your account cannot read the vendor list. Ask somebody who can to record this bill.
              </AlertDescription>
            </Alert>
          )}

          <div className="grid gap-4 sm:grid-cols-2">
            <VendorPicker
              id="jw-invoice-vendor"
              label="Vendor"
              value={vendorId}
              onChange={(next) => {
                setVendorId(next)
                setOrderId('')
                setEdits({})
              }}
              placeholder="Search the vendor master"
            />

            <div>
              <Label htmlFor="jw-invoice-order" required>
                Job-work order
              </Label>
              <Select
                value={orderId || SELECT_NONE}
                onValueChange={(value) => {
                  setOrderId(value === SELECT_NONE ? '' : value)
                  setEdits({})
                }}
                disabled={!vendorId || ordersQuery.isLoading}
              >
                <SelectTrigger id="jw-invoice-order" className="mt-1.5">
                  <SelectValue
                    placeholder={
                      !vendorId
                        ? 'Choose a vendor first'
                        : ordersQuery.isLoading
                          ? 'Loading orders…'
                          : 'Choose an order'
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_NONE}>Choose an order</SelectItem>
                  {orders.map((entry) => (
                    <SelectItem key={entry.id} value={entry.id}>
                      {entry.code} - {humanise(entry.status)}
                      {entry.receipt_count === 0 ? ' (no receipts)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {ordersQuery.error && !ordersQuery.isLoading && (
                <FieldError message={ordersQuery.error.message} />
              )}
            </div>
          </div>

          {orderId !== '' && (
            <>
              {linesQuery.isLoading && <Skeleton className="h-32 w-full" />}
              {linesQuery.error && !linesQuery.isLoading && (
                <Alert variant="destructive">
                  <AlertTitle>The billable lines could not be read</AlertTitle>
                  <AlertDescription>{linesQuery.error.message}</AlertDescription>
                </Alert>
              )}
              {!linesQuery.isLoading && !linesQuery.error && billable.length === 0 && (
                <Alert variant="info">
                  <AlertTitle>Nothing left to bill</AlertTitle>
                  <AlertDescription>
                    {order
                      ? `Every receipt line of ${order.code} is fully billed.`
                      : 'Every receipt line of this order is fully billed.'}
                  </AlertDescription>
                </Alert>
              )}

              {billable.length > 0 && (
                <div className="space-y-3">
                  {billable.map((line) => {
                    const edit = edits[line.job_work_receipt_line_id]
                    if (!edit) return null
                    return (
                      <div key={line.job_work_receipt_line_id} className="rounded-lg border p-3">
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                          <Checkbox
                            id={`jw-bill-${line.job_work_receipt_line_id}`}
                            checked={edit.selected}
                            onCheckedChange={(checked) =>
                              setEdit(line.job_work_receipt_line_id, {
                                selected: checked === true,
                              })
                            }
                          />
                          <Label
                            htmlFor={`jw-bill-${line.job_work_receipt_line_id}`}
                            className="font-medium"
                          >
                            Receipt {line.receipt_code} line {line.line_number}
                          </Label>
                          <span className="text-xs text-muted-foreground">
                            {formatDate(line.receipt_date)} · good output {line.output_quantity} ·
                            billed {line.invoiced_amount} of {line.charges}
                          </span>
                          <span className="ml-auto text-sm font-medium tabular">
                            Unbilled {formatMoney(line.unbilled_amount)}
                          </span>
                        </div>

                        {edit.selected && (
                          <div className="mt-3 grid gap-3 sm:grid-cols-3">
                            <div>
                              <Label htmlFor={`jw-qty-${line.job_work_receipt_line_id}`} required>
                                Quantity
                              </Label>
                              <Input
                                id={`jw-qty-${line.job_work_receipt_line_id}`}
                                value={edit.quantity}
                                onChange={(event) =>
                                  setEdit(line.job_work_receipt_line_id, {
                                    quantity: event.target.value,
                                  })
                                }
                                inputMode="decimal"
                                autoComplete="off"
                                className="mt-1.5 text-right tabular"
                              />
                            </div>
                            <div>
                              <Label htmlFor={`jw-price-${line.job_work_receipt_line_id}`} required>
                                Unit price
                              </Label>
                              <Input
                                id={`jw-price-${line.job_work_receipt_line_id}`}
                                value={edit.unit_price}
                                onChange={(event) =>
                                  setEdit(line.job_work_receipt_line_id, {
                                    unit_price: event.target.value,
                                  })
                                }
                                inputMode="decimal"
                                autoComplete="off"
                                className="mt-1.5 text-right tabular"
                              />
                              <p className="mt-1 text-xs text-muted-foreground">
                                Agreed {line.rate_per_unit}{' '}
                                {humanise(line.rate_basis).toLowerCase()}
                              </p>
                            </div>
                            <div>
                              <Label htmlFor={`jw-tax-${line.job_work_receipt_line_id}`}>
                                GST rate %
                              </Label>
                              <Input
                                id={`jw-tax-${line.job_work_receipt_line_id}`}
                                value={edit.tax_rate}
                                onChange={(event) =>
                                  setEdit(line.job_work_receipt_line_id, {
                                    tax_rate: event.target.value,
                                  })
                                }
                                inputMode="decimal"
                                autoComplete="off"
                                placeholder={line.tax_rate ?? '0'}
                                className="mt-1.5 text-right tabular"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </>
          )}

          {chosen.length > 0 && (
            <>
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="jw-invoice-number" required>
                    Vendor invoice number
                  </Label>
                  <Input
                    id="jw-invoice-number"
                    value={invoiceNumber}
                    onChange={(event) => setInvoiceNumber(event.target.value)}
                    autoComplete="off"
                    maxLength={64}
                    className="mt-1.5"
                    placeholder="As printed on the vendor's bill"
                  />
                </div>
                <div>
                  <Label htmlFor="jw-invoice-date" required>
                    Vendor invoice date
                  </Label>
                  <Input
                    id="jw-invoice-date"
                    type="date"
                    value={invoiceDate}
                    onChange={(event) => setInvoiceDate(event.target.value)}
                    className="mt-1.5"
                  />
                </div>
              </div>

              <label className="flex items-center gap-2.5 text-sm">
                <Checkbox
                  checked={isInterstate}
                  onCheckedChange={(checked) => setIsInterstate(checked === true)}
                />
                Inter-state supply - the whole tax is IGST
              </label>

              <div className="rounded-lg border p-3 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Subtotal</span>
                  <span className="tabular">{formatMoney(totals.subtotal.toFixed())}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Tax</span>
                  <span className="tabular">{formatMoney(totals.tax.toFixed())}</span>
                </div>
                <div className="mt-1 flex justify-between border-t pt-1 font-medium">
                  <span>Total</span>
                  <span className="tabular">{formatMoney(totals.total.toFixed())}</span>
                </div>
                {!pendingRecovery.isZero() && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Approval will also recover {formatMoney(pendingRecovery.toFixed())} of excess
                    loss against this vendor.
                  </p>
                )}
              </div>
            </>
          )}

          {touched && problems.length > 0 && (
            <Alert variant="destructive">
              <AlertTitle>Check the bill before saving</AlertTitle>
              <AlertDescription>
                <ul className="list-disc space-y-0.5 pl-4">
                  {problems.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={save.isPending || !vendorId || orderId === ''}
            onClick={() => {
              if (problems.length > 0) {
                setTouched(true)
                return
              }
              save.mutate()
            }}
          >
            {save.isPending ? 'Saving…' : 'Save and submit'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

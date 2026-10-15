/**
 * The four dialogs of the payments screen: record/amend, read, settle, command.
 *
 * The settlement dialog is the one with a rule worth stating up front. The
 * allocate endpoint takes a whole allocation *set*, not a delta, and `replace`
 * decides whether that set is added to what the payment already carries or
 * becomes the whole of it. This screen always sends `replace: true`, which is
 * only safe because the draft it submits is seeded from
 * `PaymentDetail.allocations` - the complete set the payment carries, not a page
 * of it. What the user sees in the table is therefore exactly what the payment
 * will settle when they save, and a line taken out of that table is released.
 *
 * Sending an amendment additively would have been the smaller change and the
 * wrong one: there would be no way to say "and nothing else", so a mis-keyed
 * allocation could only ever be added to.
 *
 * Notes travel with the set for the same reason. `_apply_allocation_plan`
 * rewrites `notes` from the request on every amended row, so a resubmission that
 * omitted them would silently wipe what somebody wrote against a settlement.
 */

import { SalesReceiptAllocationDialog } from './sales-receipt-allocation-dialog'
import { zodResolver } from '@hookform/resolvers/zod'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import Decimal from 'decimal.js'
import { Plus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, DescriptionList, FieldError, StatusBadge } from '@/components/shared'
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
  Badge,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import type { CustomerRow } from '@/features/customers/customer-types'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import type { VendorRow } from '@/features/vendors/vendor-types'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, formatDateTime, formatMoney, toDateInput, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { financeKeys, type VendorInvoiceSummary } from './finance-types'
import {
  canSettleInvoices,
  isPayableInvoice,
  MAX_ALLOCATIONS,
  MAX_REASON_LENGTH,
  MIN_REASON_LENGTH,
  MONEY_MOVEMENT_METHODS,
  PAYMENT_METHODS,
  paymentKeys,
  PAYMENT_COMMANDS,
  PAYMENT_METHOD_LABELS,
  PAYMENT_PARTY_LABELS,
  PAYMENT_PARTY_SIDES,
  PAYMENT_TYPE_LABELS,
  PAYMENT_TYPE_SHORT,
  PAYMENT_TYPES,
  sumAmounts,
  unallocatedAfter,
  usesInstrument,
  type PaymentCommand,
  type PaymentCreateBody,
  type PaymentDetail,
  type PaymentPartySide,
  type PaymentPartyType,
  type PaymentRow,
  type PaymentType,
  type PaymentUpdateBody,
} from './payment-model'

/**
 * Money as it is actually stored: `to_money` quantises to two places.
 *
 * The contract's own pattern permits four, so a third decimal would be accepted
 * by validation and then silently rounded away. The form asks for what will be
 * kept.
 */
const MONEY = /^\d{1,14}(\.\d{1,2})?$/

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/** A `z.enum` built from a contract-derived list, so the two cannot drift. */
function enumOf<T extends string>(values: readonly T[]) {
  return z.enum(values as unknown as [T, ...T[]])
}

// ------------------------------------------------------------ party name ----

/**
 * The party a payment is with, resolved to a name for one open document.
 *
 * The list cannot do this - `PaymentSummary` carries `party_id` and no name, and
 * resolving one per row is the N+1 the codebase forbids - but a dialog shows one
 * payment, so a single request is honest. The vendor and customer masters are
 * behind their own rights, which a payments clerk need not hold, so the lookup
 * is gated on them and falls back to the id rather than firing a request that
 * would come back 403 and raise a toast the user can do nothing about.
 *
 * Typed as the list row the pickers already use for these endpoints:
 * `/vendors/{id}` and `/customers/{id}` return a superset of it.
 */
function PartyName({ partyType, partyId }: { partyType: PaymentPartyType; partyId: string }) {
  const { can } = useAuth()
  const isVendor = partyType === 'VENDOR'
  const allowed = isVendor ? can(P.VENDOR_VIEW) : partyType === 'CUSTOMER' && can(P.CUSTOMER_VIEW)

  const party = useQuery({
    queryKey: isVendor ? queryKeys.vendors.detail(partyId) : queryKeys.customers.detail(partyId),
    queryFn: () =>
      api.get<VendorRow | CustomerRow>(`${isVendor ? '/vendors' : '/customers'}/${partyId}`),
    enabled: allowed && Boolean(partyId),
    staleTime: staleTimes.master,
  })

  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <Badge variant="secondary">
        {partyType === 'NONE' ? 'No party' : PAYMENT_PARTY_LABELS[partyType]}
      </Badge>
      {party.data ? (
        <span className="truncate">
          {party.data.code} - {party.data.name}
        </span>
      ) : (
        <span className="font-mono text-xs text-muted-foreground">{partyId.slice(0, 8)}</span>
      )}
    </span>
  )
}

// ----------------------------------------------------------- record/amend ----

/**
 * The side a direction is recorded against.
 *
 * `PaymentCreate._party_matches_direction` on the server: a payment goes to a
 * vendor and a receipt comes from a customer. The inverse pairings describe a
 * refund, which is a different document with different accounting, and the
 * API refuses them.
 */
const partySideFor = (paymentType: PaymentType): PaymentPartySide =>
  paymentType === 'RECEIPT' ? 'CUSTOMER' : 'VENDOR'

const paymentFormSchema = z
  .object({
    payment_type: enumOf(PAYMENT_TYPES),
    party_type: enumOf(PAYMENT_PARTY_SIDES),
    party_id: z.string().min(1, 'Choose who this money is with'),
    payment_date: z.string().min(1, 'A payment needs a date'),
    amount: z
      .string()
      .trim()
      .regex(MONEY, 'An amount with up to two decimal places')
      .refine((value) => toDecimal(value).greaterThan(0), 'The amount must be more than zero'),
    currency: z
      .string()
      .trim()
      .regex(/^([A-Za-z]{3})?$/, 'A three-letter code, or blank for the company currency'),
    payment_method: enumOf(PAYMENT_METHODS).refine(
      (method): boolean => method !== 'ADJUSTMENT',
      'Choose an actual payment method; noncash adjustments cannot use this form.',
    ),
    reference_number: z.string().trim().max(64, 'At most 64 characters'),
    bank_name: z.string().trim().max(120, 'At most 120 characters'),
    instrument_date: z.string(),
    notes: z.string().trim().max(2000, 'At most 2000 characters'),
  })
  .superRefine((values, ctx) => {
    // The server refuses the inverse pairing as a model-level 422, which
    // arrives under `_root` - a key no field on this form reads - while the
    // global toast stays quiet for a 422 that carries field errors. Said here
    // instead, on the side that is wrong, before the round trip.
    const expected = partySideFor(values.payment_type)
    if (values.party_type !== expected) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['party_type'],
        message: `A ${PAYMENT_TYPE_SHORT[values.payment_type].toLowerCase()} is recorded against a ${PAYMENT_PARTY_LABELS[expected].toLowerCase()}, not a ${PAYMENT_PARTY_LABELS[values.party_type].toLowerCase()}.`,
      })
    }
  })

type PaymentFormValues = z.infer<typeof paymentFormSchema>

function formDefaults(payment?: PaymentDetail): PaymentFormValues {
  if (!payment) {
    return {
      payment_type: 'PAYMENT',
      party_type: 'VENDOR',
      party_id: '',
      payment_date: toDateInput(new Date()),
      amount: '',
      currency: '',
      payment_method: 'BANK_TRANSFER',
      reference_number: '',
      bank_name: '',
      instrument_date: '',
      notes: '',
    }
  }
  return {
    payment_type: payment.payment_type,
    // Fixed after creation - carried here only so the form type stays whole.
    party_type: payment.party_type === 'CUSTOMER' ? 'CUSTOMER' : 'VENDOR',
    party_id: payment.party_id,
    payment_date: toDateInput(payment.payment_date),
    amount: payment.amount,
    currency: payment.currency,
    payment_method: payment.payment_method,
    reference_number: payment.reference_number ?? '',
    bank_name: payment.bank_name ?? '',
    instrument_date: toDateInput(payment.instrument_date),
    notes: payment.notes ?? '',
  }
}

/**
 * Record a payment or receipt, or amend one that has not been approved.
 *
 * Amending is deliberately the smaller form. `PaymentUpdate` has no party, no
 * direction and no currency: those are fixed once the document exists, because
 * changing the party of a payment is a different payment. They are shown as
 * facts rather than as disabled inputs.
 *
 * Gated by the caller on `payment.record`, which is what both `POST /payments`
 * and `PATCH /payments/{id}` enforce.
 */
export function PaymentFormDialog({
  open,
  onOpenChange,
  paymentId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Null records a new payment; an id amends that draft. */
  paymentId: string | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(paymentId)

  const detail = useQuery({
    queryKey: paymentKeys.detail(paymentId ?? ''),
    queryFn: () => api.get<PaymentDetail>(`/finance/payments/${paymentId}`),
    enabled: open && isEdit,
    staleTime: staleTimes.transactional,
  })

  const {
    register,
    control,
    watch,
    setValue,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<PaymentFormValues>({
    resolver: zodResolver(paymentFormSchema),
    defaultValues: formDefaults(),
  })

  const payment = detail.data

  // Seeded when the document lands rather than when the dialog opens, since the
  // amendable values only exist on the detail response.
  useEffect(() => {
    if (!open) return
    if (isEdit) {
      if (payment) reset(formDefaults(payment))
    } else {
      reset(formDefaults())
    }
  }, [open, isEdit, payment, reset])

  const partyType = watch('party_type')
  const paymentMethod = watch('payment_method')
  const canPickParty = partyType === 'VENDOR' ? can(P.VENDOR_VIEW) : can(P.CUSTOMER_VIEW)

  const save = useMutation({
    mutationFn: (values: PaymentFormValues) => {
      if (paymentId) {
        const body: PaymentUpdateBody = {
          payment_date: values.payment_date,
          amount: values.amount.trim(),
          payment_method: values.payment_method,
          reference_number: orNull(values.reference_number),
          bank_name: orNull(values.bank_name),
          instrument_date: orNull(values.instrument_date),
          notes: orNull(values.notes),
        }
        return api.patch<PaymentDetail>(`/finance/payments/${paymentId}`, body)
      }
      const body: PaymentCreateBody = {
        payment_type: values.payment_type,
        party_type: values.party_type,
        party_id: values.party_id,
        payment_date: values.payment_date,
        // A decimal string all the way to the wire; never parsed into a double.
        amount: values.amount.trim(),
        payment_method: values.payment_method,
        reference_number: orNull(values.reference_number),
        bank_name: orNull(values.bank_name),
        instrument_date: orNull(values.instrument_date),
        notes: orNull(values.notes),
        // Absent means the company's own currency, defaulted server-side so it
        // matches the invoices this payment will later be allowed to settle.
        currency: orNull(values.currency)?.toUpperCase() ?? null,
      }
      return api.post<PaymentDetail>('/finance/payments', body)
    },
    onSuccess: (saved) => {
      // A draft has posted nothing, so only the payment lists move - the ledger,
      // the ageing report and the payables register are untouched.
      void queryClient.invalidateQueries({ queryKey: paymentKeys.all })
      toast.success(
        isEdit ? success.updated('Payment') : success.created('Payment'),
        `${saved.code} is in draft until it is approved.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Amend payment' : 'Record a payment or receipt'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Only a draft can be amended. Nothing has posted yet, so these figures are still free to change.'
              : 'The document is created in draft. The money moves at approval, and which invoices it settles is decided after that.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            {isEdit && detail.isLoading && <Skeleton className="h-56 w-full" />}

            {isEdit && detail.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load this payment</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            )}

            {(!isEdit || payment) && (
              <>
                {isEdit && payment && (
                  <DescriptionList
                    columns={3}
                    items={[
                      { label: 'Document', value: payment.code },
                      {
                        label: 'Direction',
                        value: PAYMENT_TYPE_LABELS[payment.payment_type],
                      },
                      {
                        label: 'Party',
                        value: (
                          <PartyName partyType={payment.party_type} partyId={payment.party_id} />
                        ),
                      },
                      { label: 'Currency', value: payment.currency },
                    ]}
                  />
                )}

                <div className="grid gap-4 sm:grid-cols-2">
                  {!isEdit && (
                    <>
                      <div>
                        <Label htmlFor="payment-direction" required>
                          Direction
                        </Label>
                        <Controller
                          control={control}
                          name="payment_type"
                          render={({ field }) => (
                            <Select
                              value={field.value}
                              onValueChange={(next) => {
                                field.onChange(next)
                                // The side follows the direction - a payment
                                // goes to a vendor, a receipt comes from a
                                // customer - and the party chosen under the
                                // previous side goes with it.
                                const side = partySideFor(next as PaymentType)
                                if (side !== partyType) {
                                  setValue('party_type', side)
                                  setValue('party_id', '')
                                }
                              }}
                            >
                              <SelectTrigger id="payment-direction" className="mt-1.5">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {PAYMENT_TYPES.map((entry) => (
                                  <SelectItem key={entry} value={entry}>
                                    {PAYMENT_TYPE_LABELS[entry]}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          )}
                        />
                      </div>

                      <div>
                        <Label htmlFor="payment-party-type" required>
                          Party
                        </Label>
                        <Controller
                          control={control}
                          name="party_type"
                          render={({ field }) => (
                            <Select
                              value={field.value}
                              onValueChange={(next) => {
                                field.onChange(next)
                                // A vendor id under a customer party type
                                // resolves to nothing, so the chosen party goes
                                // with the side it was chosen from.
                                setValue('party_id', '')
                              }}
                            >
                              <SelectTrigger
                                id="payment-party-type"
                                className="mt-1.5"
                                aria-invalid={Boolean(errors.party_type)}
                              >
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {PAYMENT_PARTY_SIDES.map((entry) => (
                                  <SelectItem key={entry} value={entry}>
                                    {PAYMENT_PARTY_LABELS[entry]}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          )}
                        />
                        <FieldError message={errors.party_type?.message} />
                      </div>

                      <div className="sm:col-span-2">
                        {canPickParty ? (
                          <Controller
                            control={control}
                            name="party_id"
                            render={({ field }) =>
                              partyType === 'VENDOR' ? (
                                <VendorPicker
                                  id="payment-vendor"
                                  label="Vendor"
                                  value={field.value || null}
                                  onChange={(next) => field.onChange(next ?? '')}
                                  invalid={Boolean(errors.party_id)}
                                  placeholder="Choose the vendor being paid"
                                />
                              ) : (
                                <CustomerPicker
                                  id="payment-customer"
                                  label="Customer"
                                  value={field.value || null}
                                  onChange={(next) => field.onChange(next ?? '')}
                                  invalid={Boolean(errors.party_id)}
                                  placeholder="Choose the customer who paid"
                                />
                              )
                            }
                          />
                        ) : (
                          <Alert variant="warning">
                            <AlertTitle>The party master is not open to you</AlertTitle>
                            <AlertDescription>
                              Recording a payment also needs the right to look up the{' '}
                              {partyType === 'VENDOR' ? 'vendor' : 'customer'} it is against.
                            </AlertDescription>
                          </Alert>
                        )}
                        <FieldError message={errors.party_id?.message} />
                      </div>
                    </>
                  )}

                  <div>
                    <Label htmlFor="payment-date" required>
                      Payment date
                    </Label>
                    <Input
                      id="payment-date"
                      type="date"
                      {...register('payment_date')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.payment_date)}
                    />
                    <FieldError message={errors.payment_date?.message} />
                  </div>

                  <div>
                    <Label htmlFor="payment-amount" required>
                      Amount
                    </Label>
                    <Input
                      id="payment-amount"
                      inputMode="decimal"
                      autoComplete="off"
                      placeholder="0.00"
                      {...register('amount')}
                      className="mt-1.5 tabular"
                      aria-invalid={Boolean(errors.amount)}
                    />
                    <FieldError message={errors.amount?.message} />
                  </div>

                  {!isEdit && (
                    <div>
                      <Label htmlFor="payment-currency">Currency</Label>
                      <Input
                        id="payment-currency"
                        autoComplete="off"
                        maxLength={3}
                        placeholder="Company currency"
                        {...register('currency')}
                        className="mt-1.5 uppercase"
                        aria-invalid={Boolean(errors.currency)}
                      />
                      <p className="mt-1 text-xs text-muted-foreground">
                        An allocation is refused unless the invoice is billed in the same
                        denomination, so leave this blank unless the money really moved in another.
                      </p>
                      <FieldError message={errors.currency?.message} />
                    </div>
                  )}

                  <div>
                    <Label htmlFor="payment-method" required>
                      Method
                    </Label>
                    <Controller
                      control={control}
                      name="payment_method"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="payment-method" className="mt-1.5">
                            <SelectValue placeholder="Choose an actual payment method" />
                          </SelectTrigger>
                          <SelectContent>
                            {MONEY_MOVEMENT_METHODS.map((entry) => (
                              <SelectItem key={entry} value={entry}>
                                {PAYMENT_METHOD_LABELS[entry]}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    {payment?.payment_method === 'ADJUSTMENT' && (
                      <p className="mt-1.5 text-xs text-warning">
                        This legacy draft uses a noncash adjustment. Choose an actual payment method
                        before saving, or cancel the draft.
                      </p>
                    )}
                    <FieldError message={errors.payment_method?.message} />
                  </div>

                  <div>
                    <Label htmlFor="payment-reference">Reference</Label>
                    <Input
                      id="payment-reference"
                      autoComplete="off"
                      placeholder="Cheque number, UTR or transaction reference"
                      {...register('reference_number')}
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.reference_number)}
                    />
                    <FieldError message={errors.reference_number?.message} />
                  </div>

                  {usesInstrument(paymentMethod) && (
                    <>
                      <div>
                        <Label htmlFor="payment-bank">Bank</Label>
                        <Input
                          id="payment-bank"
                          autoComplete="off"
                          {...register('bank_name')}
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.bank_name)}
                        />
                        <FieldError message={errors.bank_name?.message} />
                      </div>

                      <div>
                        <Label htmlFor="payment-instrument-date">Instrument date</Label>
                        <Input
                          id="payment-instrument-date"
                          type="date"
                          {...register('instrument_date')}
                          className="mt-1.5"
                        />
                        <p className="mt-1 text-xs text-muted-foreground">
                          The date on the cheque or advice, which is not always the day it is
                          recorded.
                        </p>
                      </div>
                    </>
                  )}

                  <div className="sm:col-span-2">
                    <Label htmlFor="payment-notes">Notes</Label>
                    <Textarea
                      id="payment-notes"
                      {...register('notes')}
                      className="mt-1.5"
                      maxLength={2000}
                      aria-invalid={Boolean(errors.notes)}
                    />
                    <FieldError message={errors.notes?.message} />
                  </div>
                </div>

                {isEdit && (
                  <p className="text-xs text-muted-foreground">
                    An amendment only applies what is filled in. The endpoint ignores an empty value
                    rather than treating it as a clearance, so emptying the reference, bank or note
                    here leaves what is stored exactly as it was.
                  </p>
                )}
              </>
            )}

            {/* A refusal of the whole body - the party on the wrong side of the
                direction, say - names no field and lands on `root`. */}
            <FieldError message={errors.root?.message} />
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending} disabled={isEdit && !payment}>
              {isEdit ? 'Save changes' : 'Record payment'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------- read a row ----

/**
 * One payment, everything it carries, and what it has settled.
 *
 * Read-only: every way of changing a payment is its own endpoint with its own
 * right, so this dialog offers the settlement editor as a door rather than
 * doing anything itself.
 */
export function PaymentDetailDialog({
  paymentId,
  onClose,
  onSettle,
}: {
  paymentId: string | null
  onClose: () => void
  /** Opens the settlement editor. Absent when the reader may not record. */
  onSettle?: (paymentId: string) => void
}) {
  const detail = useQuery({
    queryKey: paymentKeys.detail(paymentId ?? ''),
    queryFn: () => api.get<PaymentDetail>(`/finance/payments/${paymentId}`),
    enabled: Boolean(paymentId),
    staleTime: staleTimes.transactional,
  })

  const payment = detail.data
  const allocations = payment?.allocations ?? []
  const currency = payment?.currency

  return (
    <Dialog
      open={Boolean(paymentId)}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{payment ? `Payment ${payment.code}` : 'Payment'}</DialogTitle>
          <DialogDescription>
            {payment
              ? `${PAYMENT_TYPE_LABELS[payment.payment_type]}, recorded ${formatDate(payment.created_at)}.`
              : 'Loading the document and what it has settled.'}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {detail.isLoading && <Skeleton className="h-52 w-full" />}

          {detail.error && !detail.isLoading && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this payment</AlertTitle>
              <AlertDescription>{detail.error.message}</AlertDescription>
            </Alert>
          )}

          {payment && (
            <>
              <DescriptionList
                columns={3}
                items={[
                  { label: 'Status', value: <StatusBadge status={payment.status} /> },
                  {
                    label: 'Party',
                    value: <PartyName partyType={payment.party_type} partyId={payment.party_id} />,
                  },
                  { label: 'Payment date', value: formatDate(payment.payment_date) },
                  { label: 'Method', value: PAYMENT_METHOD_LABELS[payment.payment_method] },
                  { label: 'Reference', value: payment.reference_number ?? '-' },
                  { label: 'Bank', value: payment.bank_name ?? '-' },
                  { label: 'Instrument date', value: formatDate(payment.instrument_date) },
                  { label: 'Currency', value: payment.currency },
                  { label: 'Amount', value: formatMoney(payment.amount, { currency }) },
                  {
                    label: 'Allocated',
                    value: formatMoney(payment.allocated_amount, { currency }),
                  },
                  {
                    label: 'Unallocated',
                    // Meaningless on a receipt: there is no receivable document
                    // in this contract for it to be matched against, so the
                    // whole amount stays "unallocated" for ever.
                    value:
                      payment.payment_type === 'RECEIPT'
                        ? formatMoney(payment.unallocated_amount, { currency })
                        : formatMoney(payment.unallocated_amount, { currency }),
                  },
                  {
                    label: 'Approved',
                    value: payment.approved_at ? formatDateTime(payment.approved_at) : '-',
                  },
                ]}
              />

              {payment.notes && (
                <Alert variant="info">
                  <AlertTitle>Notes</AlertTitle>
                  <AlertDescription>{payment.notes}</AlertDescription>
                </Alert>
              )}

              {payment.cancellation_reason && (
                <Alert variant="warning">
                  <AlertTitle>Cancelled and reversed</AlertTitle>
                  <AlertDescription>{payment.cancellation_reason}</AlertDescription>
                </Alert>
              )}

              <section className="space-y-2">
                <h3 className="text-sm font-semibold">Settled invoices</h3>
                {allocations.length === 0 ? (
                  <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                    {payment.payment_type === 'RECEIPT'
                      ? 'Allocate this receipt to customer invoices to show which balances it settled.'
                      : 'Nothing is settled from this payment yet. Approve it, then settle it against the vendor invoices it pays.'}
                  </p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <caption className="sr-only">Invoices settled by {payment.code}</caption>
                      <thead>
                        <tr className="border-b">
                          <th
                            scope="col"
                            className="py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Invoice
                          </th>
                          <th
                            scope="col"
                            className="py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Note
                          </th>
                          <th
                            scope="col"
                            className="py-1.5 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Settled
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {allocations.map((allocation) => (
                          <tr key={allocation.id} className="border-b last:border-0">
                            <td className="py-1.5">
                              <span className="block break-all">
                                {allocation.vendor_invoice_number ?? allocation.invoice_code ?? '-'}
                              </span>
                              <span className="block font-mono text-2xs text-muted-foreground">
                                {allocation.invoice_code ??
                                  (
                                    allocation.vendor_invoice_id ??
                                    allocation.sales_invoice_id ??
                                    '-'
                                  ).slice(0, 8)}
                              </span>
                            </td>
                            <td className="py-1.5 text-xs text-muted-foreground">
                              {allocation.notes ?? '-'}
                            </td>
                            <td className="whitespace-nowrap py-1.5 text-right tabular">
                              {formatMoney(allocation.amount, { currency })}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          {payment && onSettle && canSettleInvoices(payment) && (
            <Button onClick={() => onSettle(payment.id)}>Settle invoices</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------- settlement ---

/** One line of the settlement the payment will carry after saving. */
interface DraftLine {
  amount: string
  notes: string
  /** From the payment's own allocations; absent for a line just picked. */
  code: string | null
  number: string | null
  /**
   * The invoice's outstanding at the moment it was picked, and what this
   * payment already puts against it. Both are null for a line seeded from an
   * existing allocation: `PaymentAllocationRead` carries neither, so that line's
   * ceiling is known only to the server.
   */
  outstanding: string | null
  settled: string | null
  currency: string | null
}

type Draft = Record<string, DraftLine>

/**
 * Open invoices offered per page inside the settlement dialog. Smaller than a
 * list screen's default: the candidate table shares a modal with the draft
 * above it, and a vendor rarely has more open bills than this at once.
 */
const INVOICE_PICK_PAGE_SIZE = 25

function seedDraft(payment: PaymentDetail): Draft {
  const seeded: Draft = {}
  for (const allocation of payment.allocations ?? []) {
    if (!allocation.vendor_invoice_id) continue
    seeded[allocation.vendor_invoice_id] = {
      amount: allocation.amount,
      // Carried back on save: an amended row's notes are rewritten from the
      // request, so dropping them here would erase them there.
      notes: allocation.notes ?? '',
      code: allocation.invoice_code ?? null,
      number: allocation.vendor_invoice_number ?? null,
      outstanding: null,
      settled: allocation.amount,
      currency: null,
    }
  }
  return seeded
}

/** What this line may hold, or null when only the server knows. */
function ceilingOf(line: DraftLine): Decimal | null {
  if (line.outstanding === null) return null
  // The invoice's outstanding already has this payment's own allocation taken
  // off it, and that amount is about to stop counting - so it is added back,
  // exactly as `allocate_payment` does under the row lock.
  return toDecimal(line.outstanding).plus(toDecimal(line.settled ?? '0'))
}

/**
 * Apply an approved payment against the vendor invoices it settles.
 *
 * Gated by the caller on `payment.record`, which is what `POST /allocate`
 * enforces - not the approval right, and not the page's own view right.
 * Browsing the vendor's open invoices additionally reads
 * `GET /finance/vendor-invoices`, which is behind `vendor_invoice.view`: a user
 * without it can still correct the amounts already on the payment, and is told
 * why the candidate list is missing rather than being shown an empty table.
 */
export function PaymentAllocationDialog({
  paymentId,
  onClose,
}: {
  paymentId: string | null
  onClose: () => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const canRecord = can(P.PAYMENT_RECORD)
  const canBrowseInvoices = can(P.VENDOR_INVOICE_VIEW)

  const [draft, setDraft] = useState<Draft>({})
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(INVOICE_PICK_PAGE_SIZE)
  const seededFor = useRef<string | null>(null)

  const detail = useQuery({
    queryKey: paymentKeys.detail(paymentId ?? ''),
    queryFn: () => api.get<PaymentDetail>(`/finance/payments/${paymentId}`),
    enabled: Boolean(paymentId),
    // Settlement is decided against figures that move, so this is never served
    // from a cache that could predate this morning's allocation.
    staleTime: 0,
  })

  const payment = detail.data

  // Seeded once per payment: re-seeding on every refetch would throw away what
  // the user is in the middle of typing.
  useEffect(() => {
    if (!paymentId) {
      seededFor.current = null
      return
    }
    if (!payment || seededFor.current === payment.id) return
    seededFor.current = payment.id
    setDraft(seedDraft(payment))
    setSearch('')
    setPage(1)
  }, [paymentId, payment])

  const settles = payment ? canSettleInvoices(payment) : false
  const paymentAmount = payment?.amount ?? '0'
  const currency = payment?.currency

  const invoiceParams = useMemo(
    () => ({
      page,
      limit,
      with_total: true,
      vendor_id: payment?.party_id,
      // Only invoices that still owe money; the API decides that, never the
      // browser.
      open_only: true,
      sort_by: 'due_date',
      sort_dir: 'asc' as const,
      ...(search ? { search } : {}),
    }),
    [page, limit, payment?.party_id, search],
  )

  const invoices = useQuery({
    queryKey: paymentKeys.settleable(paymentId ?? '', invoiceParams),
    queryFn: () =>
      api.getPage<VendorInvoiceSummary>('/finance/vendor-invoices', { params: invoiceParams }),
    enabled:
      Boolean(payment) && payment?.payment_type === 'PAYMENT' && settles && canBrowseInvoices,
    staleTime: staleTimes.transactional,
    // Hold the current page on screen while the next one loads. Without it
    // the table collapses to a skeleton on every page click - `useListQuery`
    // sets this for every list that goes through it, and these four are the
    // hand-rolled ones that did not.
    placeholderData: keepPreviousData,
  })

  /**
   * Put an invoice into the draft, filled to whatever the payment has left.
   *
   * Written through the functional updater so the callback never depends on the
   * draft it is reading - otherwise every keystroke in an amount box would
   * rebuild the candidate table's columns.
   */
  const addInvoice = useCallback(
    (invoice: VendorInvoiceSummary) => {
      setDraft((current) => {
        if (current[invoice.id]) return current
        if (Object.keys(current).length >= MAX_ALLOCATIONS) return current
        const used = sumAmounts(Object.values(current).map((line) => line.amount))
        const left = toDecimal(paymentAmount).minus(used)
        const proposed = Decimal.max(
          0,
          Decimal.min(toDecimal(invoice.outstanding_amount), left),
        ).toFixed(2)
        return {
          ...current,
          [invoice.id]: {
            amount: proposed,
            notes: '',
            code: invoice.code,
            number: invoice.vendor_invoice_number,
            outstanding: invoice.outstanding_amount,
            settled: null,
            currency: invoice.currency,
          },
        }
      })
    },
    [paymentAmount],
  )

  const removeLine = useCallback((invoiceId: string) => {
    setDraft((current) => {
      const next = { ...current }
      delete next[invoiceId]
      return next
    })
  }, [])

  const draftIds = useMemo(() => new Set(Object.keys(draft)), [draft])
  const draftEntries = Object.entries(draft)
  const payableEntries = draftEntries.filter(([, line]) => toDecimal(line.amount).greaterThan(0))
  const draftTotal = sumAmounts(draftEntries.map(([, line]) => line.amount))
  const remaining = payment ? unallocatedAfter(payment, draftTotal) : new Decimal(0)

  /**
   * Everything the API would refuse, said before the round trip.
   *
   * The server is still the authority - it re-reads both bounds under a row
   * lock - so this is a courtesy, not a substitute. Nothing here loosens a rule;
   * a line whose ceiling is unknown is simply left to the server.
   */
  const problems: string[] = []
  if (draftEntries.some(([, line]) => !MONEY.test(line.amount.trim()))) {
    problems.push('Every amount takes up to two decimal places.')
  }
  if (payableEntries.length > MAX_ALLOCATIONS) {
    problems.push(`A payment may settle at most ${MAX_ALLOCATIONS} invoices.`)
  }
  if (remaining.isNegative()) {
    problems.push(
      `This settles ${formatMoney(draftTotal.toFixed(2), { currency })}, which is more than ${formatMoney(paymentAmount, { currency })} left the bank.`,
    )
  }
  for (const [, line] of payableEntries) {
    const ceiling = ceilingOf(line)
    if (ceiling && toDecimal(line.amount).greaterThan(ceiling)) {
      problems.push(
        `${line.number ?? line.code ?? 'That invoice'} owes ${formatMoney(ceiling.toFixed(2), { currency })}; it cannot absorb more.`,
      )
    }
  }

  const allocate = useMutation({
    mutationFn: () =>
      api.post<PaymentDetail>(`/finance/payments/${paymentId}/allocate`, {
        allocations: payableEntries.map(([invoiceId, line]) => ({
          vendor_invoice_id: invoiceId,
          amount: line.amount.trim(),
          notes: orNull(line.notes),
        })),
        // The draft is the complete set - see the note at the top of this file.
        replace: true,
      }),
    onSuccess: (updated) => {
      // An allocation moves the invoices' paid totals and statuses as well as
      // the payment, so the whole finance namespace goes - the payables list,
      // the outstanding report and the ageing all read those figures.
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      toast.success(
        success.saved('Settlement'),
        `${updated.code} now settles ${formatMoney(updated.allocated_amount, { currency: updated.currency })}.`,
      )
      onClose()
    },
  })

  const columns = useMemo<ColumnDef<VendorInvoiceSummary, unknown>[]>(
    () => [
      {
        id: 'invoice',
        header: 'Invoice',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[14rem]">
            <span className="block truncate font-medium">{row.original.vendor_invoice_number}</span>
            <span className="block truncate font-mono text-2xs text-muted-foreground">
              {row.original.code}
            </span>
          </div>
        ),
      },
      {
        id: 'due_date',
        header: 'Due',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.due_date),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'outstanding_amount',
        header: 'Outstanding',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">
            {formatMoney(row.original.outstanding_amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'add',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const invoice = row.original
          const alreadyDrafted = draftIds.has(invoice.id)
          // Each of these is a rule `allocate_payment` enforces; saying it here
          // costs a sentence and saves a rejected request.
          const refusal = alreadyDrafted
            ? 'Already in the settlement'
            : invoice.currency !== currency
              ? `Billed in ${invoice.currency}`
              : !isPayableInvoice(invoice.status)
                ? 'Not approved for payment'
                : draftIds.size >= MAX_ALLOCATIONS
                  ? `At the limit of ${MAX_ALLOCATIONS}`
                  : null

          return (
            <div className="flex items-center justify-end gap-2">
              {refusal && <span className="text-2xs text-muted-foreground">{refusal}</span>}
              <Button
                variant="outline"
                size="sm"
                disabled={Boolean(refusal)}
                aria-label={`Settle ${invoice.vendor_invoice_number} from this payment`}
                onClick={() => addInvoice(invoice)}
              >
                <Plus />
                Add
              </Button>
            </div>
          )
        },
      },
    ],
    [addInvoice, currency, draftIds],
  )

  if (payment?.payment_type === 'RECEIPT' && settles) {
    return <SalesReceiptAllocationDialog key={payment.id} payment={payment} onClose={onClose} />
  }

  return (
    <Dialog
      open={Boolean(paymentId)}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>
            {payment ? `Settle invoices from ${payment.code}` : 'Settle invoices'}
          </DialogTitle>
          <DialogDescription>
            What this table holds is what the payment will settle. A line taken out of it is
            released back to the invoice when you save, and the amount that left the bank never
            changes.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {detail.isLoading && <Skeleton className="h-40 w-full" />}

          {detail.error && !detail.isLoading && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this payment</AlertTitle>
              <AlertDescription>{detail.error.message}</AlertDescription>
            </Alert>
          )}

          {payment && !settles && (
            <Alert variant="warning">
              <AlertTitle>This payment cannot settle invoices</AlertTitle>
              <AlertDescription>
                {payment.payment_type === 'RECEIPT'
                  ? 'Approve the receipt before allocating it to sales invoices.'
                  : 'Only an approved payment settles invoices. Money that has not moved cannot pay a bill.'}
              </AlertDescription>
            </Alert>
          )}

          {payment && settles && (
            <>
              <DescriptionList
                columns={4}
                items={[
                  {
                    label: 'Party',
                    value: <PartyName partyType={payment.party_type} partyId={payment.party_id} />,
                  },
                  { label: 'Payment', value: formatMoney(payment.amount, { currency }) },
                  {
                    label: 'This settlement',
                    value: (
                      <span className="tabular">
                        {formatMoney(draftTotal.toFixed(2), { currency })}
                      </span>
                    ),
                  },
                  {
                    label: 'Left unallocated',
                    value: (
                      <span
                        className={remaining.isNegative() ? 'tabular text-destructive' : 'tabular'}
                      >
                        {formatMoney(remaining.toFixed(2), { currency })}
                      </span>
                    ),
                  },
                ]}
              />

              <section className="space-y-2">
                <h3 className="text-sm font-semibold">What this payment settles</h3>
                {draftEntries.length === 0 ? (
                  <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                    Nothing yet. Add an invoice from the vendor's open bills below, and the amount
                    fills itself in to whatever this payment has left.
                  </p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[32rem] text-sm">
                      <caption className="sr-only">
                        Invoices this payment will settle when saved
                      </caption>
                      <thead>
                        <tr className="border-b">
                          <th
                            scope="col"
                            className="py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Invoice
                          </th>
                          <th
                            scope="col"
                            className="py-1.5 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Outstanding
                          </th>
                          <th
                            scope="col"
                            className="py-1.5 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Settle
                          </th>
                          <th
                            scope="col"
                            className="py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          >
                            Note
                          </th>
                          <th scope="col" className="w-10">
                            <span className="sr-only">Remove</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {draftEntries.map(([invoiceId, line]) => {
                          const label = line.number ?? line.code ?? invoiceId.slice(0, 8)
                          return (
                            <tr key={invoiceId} className="border-b last:border-0">
                              <td className="py-1.5 pr-2">
                                <span className="block">{label}</span>
                                <span className="block font-mono text-2xs text-muted-foreground">
                                  {line.code ?? invoiceId.slice(0, 8)}
                                </span>
                              </td>
                              <td className="py-1.5 pr-2 text-right tabular">
                                {line.outstanding === null ? (
                                  <span
                                    className="text-2xs text-muted-foreground"
                                    title="An allocation row does not carry the invoice's outstanding; the server checks it when you save."
                                  >
                                    Not reported
                                  </span>
                                ) : (
                                  formatMoney(line.outstanding, {
                                    currency: line.currency ?? currency,
                                  })
                                )}
                              </td>
                              <td className="py-1.5 pr-2 text-right">
                                <Input
                                  inputMode="decimal"
                                  autoComplete="off"
                                  value={line.amount}
                                  aria-label={`Amount settled against ${label}`}
                                  aria-invalid={!MONEY.test(line.amount.trim())}
                                  className="h-8 w-32 text-right tabular"
                                  onChange={(event) =>
                                    setDraft((current) => ({
                                      ...current,
                                      [invoiceId]: { ...line, amount: event.target.value },
                                    }))
                                  }
                                />
                              </td>
                              <td className="py-1.5 pr-2">
                                <Input
                                  value={line.notes}
                                  maxLength={2000}
                                  aria-label={`Note against ${label}`}
                                  className="h-8"
                                  onChange={(event) =>
                                    setDraft((current) => ({
                                      ...current,
                                      [invoiceId]: { ...line, notes: event.target.value },
                                    }))
                                  }
                                />
                              </td>
                              <td className="py-1.5 text-right">
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`Release ${label} from this payment`}
                                  onClick={() => removeLine(invoiceId)}
                                >
                                  <Trash2 />
                                </Button>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              {problems.length > 0 && (
                <Alert variant="warning">
                  <AlertTitle>Not ready to save</AlertTitle>
                  <AlertDescription>
                    <ul className="list-disc space-y-0.5 pl-4">
                      {problems.map((problem) => (
                        <li key={problem}>{problem}</li>
                      ))}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}

              <section className="space-y-2">
                <h3 className="text-sm font-semibold">This vendor's open invoices</h3>
                {canBrowseInvoices ? (
                  <DataTable
                    columns={columns}
                    data={invoices.data?.data ?? []}
                    pagination={invoices.data?.pagination}
                    isLoading={invoices.isLoading}
                    isFetching={invoices.isFetching}
                    error={invoices.error}
                    onPageChange={setPage}
                    onLimitChange={(next) => {
                      setLimit(next)
                      setPage(1)
                    }}
                    onSearchChange={(term) => {
                      setSearch(term)
                      setPage(1)
                    }}
                    searchValue={search}
                    searchPlaceholder="Search invoice number"
                    getRowId={(row) => row.id}
                    stickyHeader={false}
                    emptyTitle="No open invoices"
                    emptyDescription="This vendor has nothing left owing that matches. Clear the search, or leave the payment on account until a bill arrives."
                  />
                ) : (
                  <Alert variant="info">
                    <AlertTitle>The invoice register is not open to you</AlertTitle>
                    <AlertDescription>
                      Settling from this payment needs the right to read vendor invoices as well.
                      The amounts already on the payment can still be corrected above.
                    </AlertDescription>
                  </Alert>
                )}
              </section>
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          {payment && settles && canRecord && (
            <Button
              loading={allocate.isPending}
              disabled={problems.length > 0}
              onClick={() => allocate.mutate()}
            >
              Save settlement
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------- commands ---

/**
 * Approve, cancel or delete, with the reason each endpoint asks for.
 *
 * The three differ in more than wording. Approve takes an optional body, so an
 * empty box means *no body at all* rather than an empty reason - a
 * `ReasonRequest` with a blank reason is a 422. Cancel wants its reason in the
 * body. Delete wants it in the query string, which is this router's one
 * departure from the pattern.
 *
 * Each command's permission comes from `PAYMENT_COMMANDS`, which states what the
 * endpoint itself enforces: approve and cancel share `payment.approve`, delete
 * belongs to `payment.record`.
 */
export function PaymentCommandDialog({
  command,
  payment,
  onClose,
}: {
  command: PaymentCommand | null
  payment: PaymentRow | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  const copy = command ? PAYMENT_COMMANDS[command] : null
  const open = Boolean(command && payment)

  const run = useMutation({
    mutationFn: () => {
      const trimmed = reason.trim()
      if (command === 'delete') {
        // The only reason on this router that travels as a query parameter.
        return api.delete(`/finance/payments/${payment?.id}`, { params: { reason: trimmed } })
      }
      if (command === 'cancel') {
        return api.post(`/finance/payments/${payment?.id}/cancel`, { reason: trimmed })
      }
      return api.post(
        `/finance/payments/${payment?.id}/approve`,
        trimmed === '' ? undefined : { reason: trimmed },
      )
    },
    onSuccess: () => {
      if (command === 'delete') {
        // A draft posted nothing, so nothing outside the payment lists moved.
        void queryClient.invalidateQueries({ queryKey: paymentKeys.all })
      } else {
        // Approving and cancelling both write the journal, so every finance
        // report reading it is stale.
        void queryClient.invalidateQueries({ queryKey: financeKeys.all })
        // A receipt moves the customer's cached outstanding balance in the same
        // transaction as the posting.
        void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      }
      toast.success(
        {
          approve: success.approved('Payment'),
          cancel: success.cancelled('Payment'),
          delete: success.deleted('Draft payment'),
        }[command ?? 'approve'],
        command === 'cancel' && payment
          ? `${payment.code} - the posting was reversed in the journal.`
          : payment?.code,
      )
      onClose()
    },
  })

  const tooShort = reason.trim().length < MIN_REASON_LENGTH
  const reasonRequired = copy?.reason === 'required'
  /**
   * Whether the box holds something the endpoint would refuse.
   *
   * A required reason must clear three characters. An optional one may be left
   * blank - that sends no body at all - but once written it travels inside a
   * `ReasonRequest`, and the same bound applies. A two-character note would
   * otherwise go out, come back a 422 with a field error, and vanish: the
   * mutation cache stays quiet for field errors on the understanding that a
   * form shows them, and this dialog has no form to do so.
   */
  const reasonUnusable = tooShort && (reasonRequired || reason.trim() !== '')

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setReason('')
          setTouched(false)
          onClose()
        }
      }}
      title={copy?.title ?? ''}
      description={
        payment && copy ? `${copy.description} (${payment.code})` : (copy?.description ?? '')
      }
      confirmLabel={copy?.confirmLabel ?? 'Confirm'}
      destructive={copy?.destructive}
      loading={run.isPending}
      onConfirm={() => {
        if (reasonUnusable) {
          setTouched(true)
          return
        }
        run.mutate()
      }}
    >
      <div>
        <Label htmlFor="payment-command-reason" required={reasonRequired}>
          {reasonRequired ? 'Reason' : 'Reason (optional)'}
        </Label>
        <Input
          id="payment-command-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          autoComplete="off"
          maxLength={MAX_REASON_LENGTH}
          className="mt-1.5"
          aria-invalid={touched && reasonUnusable}
        />
        {touched && reasonUnusable ? (
          <FieldError message={`Record at least ${MIN_REASON_LENGTH} characters of reason`} />
        ) : (
          <p className="mt-1 text-xs text-muted-foreground">
            {reasonRequired
              ? 'Kept in the audit log against this document.'
              : `Left blank, the approval is recorded without one; a reason that is written must run to at least ${MIN_REASON_LENGTH} characters.`}
          </p>
        )}
      </div>
    </ConfirmDialog>
  )
}

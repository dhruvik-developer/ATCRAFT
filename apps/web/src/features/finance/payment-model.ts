/**
 * Wire shapes, vocabulary and settlement arithmetic for payments and receipts.
 *
 * A payment document is a piece of *money*, not a settlement. The contract keeps
 * the two apart on purpose, and the screens above it have to keep them apart
 * too:
 *
 * 1. `POST /finance/payments` records the document in DRAFT. Nothing has moved,
 *    which is why a draft is the only thing that can be amended or deleted.
 * 2. `POST /finance/payments/{id}/approve` posts it - the journal is written,
 *    the bank moves and a receipt reduces the customer's outstanding balance,
 *    all in one transaction. After that the document can only be cancelled and
 *    reversed; there is no field on `PaymentUpdate` that could unpick it.
 * 3. `POST /finance/payments/{id}/allocate` decides *which invoices* the money
 *    settled. It can never change the amount that left the bank.
 *
 * Two consequences that shape every screen here:
 *
 * * **Allocations name exactly one invoice.** Vendor payments settle vendor
 *   invoices; customer receipts settle issued sales invoices. Approval already
 *   posts the money; allocating identifies the invoices that it paid.
 * * **Money is decimal strings.** Every figure here is computed with decimal.js
 *   and formatted at the very end. The server sends `unallocated_amount`
 *   already derived, so no screen subtracts two columns and disagrees with it.
 */

import Decimal from 'decimal.js'

import { REASON_MIN_LENGTH } from '@/lib/constants'
import { toDecimal } from '@/lib/format'
import { P, type PermissionKey } from '@/lib/permissions'
import type { SchemaOf } from '@/types/api'

import type { VendorInvoiceStatus } from './finance-types'

// ------------------------------------------------------------ query keys ----

/**
 * Query keys for payments.
 *
 * Prefixed with `finance` like `financeKeys` in `finance-types.ts`, which keeps
 * one hierarchy over the module: a mutation that moves the ledger invalidates
 * `financeKeys.all` and sweeps these lists with it, because an allocation
 * changes the payables register and the ageing report as well as the payment.
 */
export const paymentKeys = {
  all: ['finance', 'payments'] as const,
  list: (params: unknown) => ['finance', 'payments', 'list', params] as const,
  detail: (id: string) => ['finance', 'payments', 'detail', id] as const,
  /** The vendor's open invoices offered inside one payment's allocation dialog. */
  settleable: (paymentId: string, params: unknown) =>
    ['finance', 'payments', 'detail', paymentId, 'settleable', params] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /finance/payments`. Carries no party name - see below. */
export type PaymentRow = SchemaOf<'PaymentSummary'>

/**
 * `GET /finance/payments/{id}`.
 *
 * `allocations` is the *complete* set the payment carries, not a page of it -
 * the server reads every row for the payment and the write path caps it at
 * {@link MAX_ALLOCATIONS}. That completeness is what makes a `replace` request
 * safe to build from what the dialog is showing.
 */
export type PaymentDetail = SchemaOf<'PaymentDetail'>

/**
 * One settled invoice on a payment.
 *
 * `invoice_code` and `vendor_invoice_number` are resolved for us in one batched
 * query - the only place in this feature where a document id comes back with a
 * human name attached. The invoice's own currency and outstanding are *not*
 * here, so a row that was already settled shows what it settled and nothing
 * about the invoice behind it.
 */
export type PaymentAllocation = SchemaOf<'PaymentAllocationRead'>

export type PaymentStatus = SchemaOf<'PaymentStatus'>
export type PaymentType = SchemaOf<'PaymentType'>
export type PaymentMethod = SchemaOf<'PaymentMethod'>

/** `NONE` exists on the wire for a ledger entry against no party. */
export type PaymentPartyType = SchemaOf<'PartyType'>

/**
 * The two sides a payment can actually be against.
 *
 * `_party_facts` in the finance service resolves a CUSTOMER or a VENDOR and
 * raises on anything else, so `NONE` - legal on a ledger entry - is never a
 * value this screen may send or offer.
 */
export type PaymentPartySide = Extract<PaymentPartyType, 'CUSTOMER' | 'VENDOR'>

export type PaymentCreateBody = SchemaOf<'PaymentCreate'>
export type PaymentUpdateBody = SchemaOf<'PaymentUpdate'>
export type AllocationRequestBody = SchemaOf<'AllocationRequest'>
export type PaymentAllocationIn = SchemaOf<'PaymentAllocationIn'>

// ------------------------------------------------------------ vocabulary ----

/**
 * Every status with the label the filter shows.
 *
 * A `Record` keyed by the contract union rather than a hand-kept array: a
 * status added to the API stops the build here instead of quietly disappearing
 * from the filter, which is exactly how three of nine assembly statuses came to
 * be unreachable.
 */
export const PAYMENT_STATUS_LABELS: Record<PaymentStatus, string> = {
  DRAFT: 'Draft',
  APPROVED: 'Approved',
  PARTIALLY_ALLOCATED: 'Partially allocated',
  ALLOCATED: 'Fully allocated',
  CANCELLED: 'Cancelled',
}

export const PAYMENT_STATUSES = Object.keys(PAYMENT_STATUS_LABELS) as PaymentStatus[]

/** Direction, named the way the accounts department says it out loud. */
export const PAYMENT_TYPE_LABELS: Record<PaymentType, string> = {
  PAYMENT: 'Payment - money out',
  RECEIPT: 'Receipt - money in',
}

export const PAYMENT_TYPES = Object.keys(PAYMENT_TYPE_LABELS) as PaymentType[]

/** Short form for a grid column, where the direction is one word. */
export const PAYMENT_TYPE_SHORT: Record<PaymentType, string> = {
  PAYMENT: 'Payment',
  RECEIPT: 'Receipt',
}

export const PAYMENT_METHOD_LABELS: Record<PaymentMethod, string> = {
  CASH: 'Cash',
  BANK_TRANSFER: 'Bank transfer',
  CHEQUE: 'Cheque',
  UPI: 'UPI',
  CARD: 'Card',
  // A book entry with no cash movement: contra, write-off or set-off.
  ADJUSTMENT: 'Adjustment',
}

export const PAYMENT_METHODS = Object.keys(PAYMENT_METHOD_LABELS) as PaymentMethod[]

export const PAYMENT_PARTY_LABELS: Record<PaymentPartySide, string> = {
  VENDOR: 'Vendor',
  CUSTOMER: 'Customer',
}

export const PAYMENT_PARTY_SIDES = Object.keys(PAYMENT_PARTY_LABELS) as PaymentPartySide[]

/**
 * Methods that name a bank and an instrument.
 *
 * Cash and an adjustment have neither, so the two fields are asked for only
 * when they mean something. The API accepts them on any method - this is a form
 * decision, not a rule, and nothing is refused if they arrive anyway.
 */
const INSTRUMENT_METHODS = new Set<PaymentMethod>(['BANK_TRANSFER', 'CHEQUE', 'UPI', 'CARD'])

export const usesInstrument = (method: PaymentMethod): boolean => INSTRUMENT_METHODS.has(method)

// ------------------------------------------------------------- lifecycle ----

/**
 * Statuses `POST /allocate` accepts.
 *
 * `ALLOCATED` is in the list on purpose: a fully settled payment can still be
 * *amended*, which is the only way back from a mis-keyed figure. The service
 * accepts an invoice that is already PAID when this payment is the one that
 * paid it.
 */
const ALLOCATABLE_STATUSES = new Set<PaymentStatus>([
  'APPROVED',
  'PARTIALLY_ALLOCATED',
  'ALLOCATED',
])

/**
 * Invoice statuses a payment may settle.
 *
 * Mirrors `PAYABLE_INVOICE_STATUSES` on the server. An invoice nobody has
 * approved cannot be paid, and settling a DISPUTED one would concede the
 * dispute silently - so those are shown in the candidate list and refused
 * there, rather than offered and rejected after a round trip.
 */
const PAYABLE_INVOICE_STATUSES = new Set<VendorInvoiceStatus>(['APPROVED', 'PARTIALLY_PAID'])

export const isPayableInvoice = (status: VendorInvoiceStatus): boolean =>
  PAYABLE_INVOICE_STATUSES.has(status)

/** The one state in which the document is still only a piece of paper. */
export const isDraft = (payment: { status: PaymentStatus }): boolean => payment.status === 'DRAFT'

/**
 * Whether this payment can settle invoices at all.
 *
 * Money must be approved before it can settle either kind of invoice.
 */
export const canSettleInvoices = (payment: PaymentRow | PaymentDetail): boolean =>
  ALLOCATABLE_STATUSES.has(payment.status)

/**
 * Whether a cancellation would be accepted.
 *
 * Cancelling reverses the posting, so it is refused while allocations stand:
 * un-settling the invoices is a deliberate act, not a side effect. Cancelling
 * an already-cancelled payment is refused outright rather than treated as a
 * no-op, because the reversal would run a second time.
 */
export const canCancel = (payment: PaymentRow | PaymentDetail): boolean =>
  payment.status !== 'CANCELLED' &&
  (payment.payment_type === 'RECEIPT' || toDecimal(payment.allocated_amount).isZero())

// -------------------------------------------------------------- commands ----

/** The three lifecycle commands the list offers on a row. */
export type PaymentCommand = 'approve' | 'cancel' | 'delete'

export interface PaymentCommandCopy {
  /** The menu entry. */
  label: string
  title: string
  description: string
  confirmLabel: string
  destructive: boolean
  /**
   * The permission the *endpoint* enforces - not the page's own right.
   *
   * Recording and approving are deliberately different rights: whoever keys a
   * payment in should not be the one who releases the money. Approve and cancel
   * share the approval right because a cancellation reverses a posting, which is
   * the same authority in the other direction. Deleting a draft has posted
   * nothing, so it stays with the person who recorded it.
   */
  permission: PermissionKey
  /**
   * Whether the API insists on a reason.
   *
   * `approve` takes an optional `ReasonRequest` body - the whole body may be
   * omitted, but if one is sent the reason must be at least three characters, so
   * an empty box means "send no body at all" rather than "send an empty reason".
   * `cancel` requires one in the body. `delete` requires one as a **query
   * parameter**, which is the odd one out on this router.
   */
  reason: 'required' | 'optional'
}

export const PAYMENT_COMMANDS: Record<PaymentCommand, PaymentCommandCopy> = {
  approve: {
    label: 'Approve - post the money',
    title: 'Approve this payment?',
    description:
      'The money moves. The journal is posted, and a receipt also reduces the customer balance in the same transaction. After this the document can only be cancelled and reversed - it can never be edited.',
    confirmLabel: 'Approve payment',
    destructive: false,
    permission: P.PAYMENT_APPROVE,
    reason: 'optional',
  },
  cancel: {
    label: 'Cancel and reverse',
    title: 'Cancel and reverse this payment?',
    description:
      'An approved payment is reversed rather than deleted: the original journal stays and its mirror image is posted beside it. Refused while any allocation still stands, so release the settlement first.',
    confirmLabel: 'Cancel payment',
    destructive: true,
    permission: P.PAYMENT_APPROVE,
    reason: 'required',
  },
  delete: {
    label: 'Delete draft',
    title: 'Delete this draft payment?',
    description:
      'Nothing has posted yet, so there is nothing to reverse - the document simply goes, and the reason is kept in the audit log.',
    confirmLabel: 'Delete draft',
    destructive: true,
    permission: P.PAYMENT_RECORD,
    reason: 'required',
  },
}

/**
 * The bounds `ReasonRequest.reason` and the delete query parameter accept: the
 * shared floor, but 500 characters at the top rather than the usual 255.
 */
export const MIN_REASON_LENGTH = REASON_MIN_LENGTH
export const MAX_REASON_LENGTH = 500

// --------------------------------------------------------------- derived ----

/**
 * The most invoices one payment may settle (`AllocationRequest.allocations`
 * maxItems, and the bound the service puts on the *resulting* set).
 */
export const MAX_ALLOCATIONS = 100

/** Sum of a set of decimal strings. Never a float, however small the figures. */
export function sumAmounts(amounts: string[]): Decimal {
  return amounts.reduce((total, amount) => total.plus(toDecimal(amount)), new Decimal(0))
}

/**
 * What is left of a payment once a draft allocation is applied.
 *
 * Negative means the draft settles more than the payment is worth, which the
 * API refuses under a row lock - the dialog says so before spending the round
 * trip.
 */
export function unallocatedAfter(payment: PaymentDetail, draftTotal: Decimal): Decimal {
  return toDecimal(payment.amount).minus(draftTotal)
}

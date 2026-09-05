/**
 * Wire shapes and vocabulary for the customer master.
 *
 * Declared in the feature folder rather than in `types/api.ts` because these are
 * the payloads of three screens, not part of the transport contract every module
 * shares (the same arrangement the inventory feature uses).
 *
 * Credit limit and outstanding balance are decimal strings and stay strings all
 * the way to the wire: a customer who is one rupee over their limit is a real
 * business decision, and an IEEE-754 double is not allowed to make it.
 *
 * `credit_limit` is nullable and the null is load-bearing - the API's own words:
 * "Omit or send null for an unlimited limit; send 0 to forbid credit entirely."
 * Treating a missing limit as zero would refuse orders for the accounts that are
 * trusted most, so nothing here collapses the two.
 */

import { toDecimal } from '@/lib/format'
import type { DecimalString } from '@/types/api'

import { DEALER_CHANNELS } from './dealer-model'

// ------------------------------------------------------------ vocabulary ----

/** `CustomerType` in the API schema. */
export const CUSTOMER_TYPES = ['CUSTOMER', 'DEALER', 'DISTRIBUTOR', 'OEM'] as const
export type CustomerType = (typeof CUSTOMER_TYPES)[number]

/** `PriceType` - which `item_prices.price_type` applies to this account. */
export const PRICE_TYPES = ['SALE', 'DEALER', 'DISTRIBUTOR'] as const
export type PriceType = (typeof PRICE_TYPES)[number]

/** `AddressType`. An address is billing or shipping; there is no "both". */
export const ADDRESS_TYPES = ['BILLING', 'SHIPPING'] as const
export type AddressType = (typeof ADDRESS_TYPES)[number]

/** An unlimited limit is `null`, not a large number and not zero. */
export const isUnlimitedCredit = (limit: DecimalString | null): boolean => limit === null

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /customers` (`CustomerSummary`). */
export interface CustomerRow {
  id: string
  code: string
  name: string
  legal_name: string | null
  customer_type: CustomerType
  price_list_type: PriceType
  gstin: string | null
  /** Null means unlimited credit. Zero means no credit at all. */
  credit_limit: DecimalString | null
  credit_days: number
  /** Unpaid invoiced value. Compared against the limit by the API, not here. */
  outstanding_amount: DecimalString
  is_active: boolean
  is_blocked: boolean
  assigned_sales_user_id: string | null
  created_at: string
}

/**
 * `GET /customers/{id}` (`CustomerDetail`).
 *
 * `addresses` and `contacts` are optional on the wire: the detail read may or
 * may not carry them, and the relations panels page them from their own
 * endpoints regardless.
 */
export interface CustomerDetail extends CustomerRow {
  pan: string | null
  block_reason: string | null
  blocked_at: string | null
  blocked_by_id: string | null
  default_transporter_id: string | null
  /** Limit less exposure. Null when the limit is unlimited. */
  credit_headroom: DecimalString | null
  notes: string | null
  updated_at: string
  addresses?: CustomerAddress[]
  contacts?: CustomerContact[]
}

/** `GET /customers/{id}/addresses` (`AddressRead`). */
export interface CustomerAddress {
  id: string
  customer_id: string
  address_type: AddressType
  label: string | null
  address_line1: string
  address_line2: string | null
  landmark: string | null
  city: string
  state: string | null
  state_code: string | null
  postal_code: string | null
  country: string
  gstin: string | null
  contact_person: string | null
  phone: string | null
  email: string | null
  is_default: boolean
  is_active: boolean
  notes: string | null
  created_at: string
  updated_at: string
}

/** `GET /customers/{id}/contacts` (`ContactRead`). */
export interface CustomerContact {
  id: string
  customer_id: string
  name: string
  designation: string | null
  email: string | null
  phone: string | null
  is_primary: boolean
  is_active: boolean
  notes: string | null
  created_at: string
  updated_at: string
}

/** `GET /customers/{id}/credit-status` (`CreditStatus`). */
export interface CreditStatus {
  customer_id: string
  customer_code: string
  is_active: boolean
  is_blocked: boolean
  block_reason: string | null
  credit_limit: DecimalString | null
  credit_days: number
  outstanding_amount: DecimalString
  order_value: DecimalString
  exposure: DecimalString
  headroom: DecimalString | null
  is_unlimited: boolean
  within_limit: boolean
}

// ------------------------------------------------------------ retirement ----

/**
 * `MasterLifecycleState`, as far as a customer payload reveals it.
 *
 * The service derives the state from `deleted_at` first and `is_active` second.
 * Neither the summary nor the detail carries `deleted_at`, and no list in this
 * feature sends `include_deleted`, so a retired account is simply not among the
 * rows a screen holds. ARCHIVED is named anyway - it is what the retirement
 * rule excludes, and a rule that cannot name what it excludes is unreadable.
 */
export const CUSTOMER_LIFECYCLE_STATES = ['ACTIVE', 'INACTIVE', 'ARCHIVED'] as const
export type CustomerLifecycleState = (typeof CUSTOMER_LIFECYCLE_STATES)[number]

/** What `lifecycle_state()` returns for a row this application can see. */
export const lifecycleStateOf = (
  customer: Pick<CustomerRow, 'is_active'>,
): CustomerLifecycleState => (customer.is_active ? 'ACTIVE' : 'INACTIVE')

/**
 * The two edges of `CUSTOMER_LIFECYCLE` labelled "Retire": ACTIVE -> ARCHIVED
 * and INACTIVE -> ARCHIVED. Retiring an account that is already archived is not
 * an edge the machine has, so the API refuses it rather than treating it as a
 * no-op - hence the predicate rather than an `is_active` check at the call site.
 *
 * `is_blocked` is deliberately not consulted. Credit standing is a second,
 * independent machine, and a blocked account that owes nothing is the one most
 * likely to be retired.
 */
const RETIREABLE_STATES: readonly CustomerLifecycleState[] = ['ACTIVE', 'INACTIVE']

export const acceptsRetirement = (customer: Pick<CustomerRow, 'is_active'>): boolean =>
  RETIREABLE_STATES.includes(lifecycleStateOf(customer))

/**
 * Does an unsettled balance stand in the way of retiring this account?
 *
 * The service refuses to retire an account that still owes money
 * (`BLOCK_RETIREMENT_WITH_OUTSTANDING`): soft-deleting the master row would
 * hide a live receivable behind it. Compared against zero as a decimal rather
 * than with `> 0`, because the service compares the same way - a credit balance
 * left by an over-payment is also non-zero and is also refused.
 */
export const hasOutstandingBalance = (customer: Pick<CustomerRow, 'outstanding_amount'>): boolean =>
  !toDecimal(customer.outstanding_amount).isZero()

/**
 * Is this account part of the dealer channel?
 *
 * Every *write* to a DEALER or DISTRIBUTOR account - retirement included - goes
 * through `assert_channel_write_allowed`, which demands `dealer.manage` on top
 * of whatever right the route itself declares. The channel list is read from
 * `dealer-model` rather than restated, so there is one statement of which
 * channels are the channel team's and it cannot drift.
 */
const DEALER_CHANNEL_TYPES: readonly string[] = DEALER_CHANNELS

export const isDealerChannel = (customer: Pick<CustomerRow, 'customer_type'>): boolean =>
  DEALER_CHANNEL_TYPES.includes(customer.customer_type)

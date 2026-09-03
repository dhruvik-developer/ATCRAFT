/**
 * Wire shapes, vocabulary and credit arithmetic for the dealer channel.
 *
 * A dealer is not a separate table. `GET /customers/dealers` is the customer
 * master restricted **in SQL** to the two channels the service calls
 * `DEALER_CHANNELS` - DEALER and DISTRIBUTOR - which is why this module derives
 * its row type from `CustomerSummary` rather than declaring a dealer shape the
 * API does not have.
 *
 * Three properties of that contract shape the screen above it.
 *
 * 1. **The channel filter is narrower than the enum.** `customer_type` accepts
 *    the whole `CustomerType` union, but `list_dealers` refuses CUSTOMER and
 *    OEM with a 422 rather than answering an empty page - an empty page would
 *    read as "there are no such dealers", which is a lie. So the filter offers
 *    the two dealer channels and nothing else; `DEALER_CHANNELS` below is that
 *    restriction, keyed off the contract union so a third channel added to the
 *    API breaks the build instead of quietly disappearing from the picker.
 *
 * 2. **There is no dealer write endpoint.** The whole module is one GET. Dealer
 *    accounts are created and edited through `/customers`, where the *service*
 *    additionally demands `dealer.manage` before it will write a row in either
 *    channel (`assert_channel_write_allowed`). That right therefore gates no
 *    route of its own, and nothing here offers a create, edit or delete.
 *
 * 3. **The summary carries no `deleted_at`.** Retired accounts are excluded by
 *    default and the list projection has no field that would distinguish a
 *    retired row from a merely inactive one, so the `include_deleted` parameter
 *    is deliberately not exposed: it would add rows the grid cannot label.
 *    (Retiring also clears `is_active`, so nothing retired can reach an
 *    active-only view in the first place.)
 *
 * Credit limit and outstanding balance are decimal strings and stay strings all
 * the way to the wire. `credit_limit` is nullable and the null is load-bearing:
 * null means *unlimited*, zero means *no credit at all*, and collapsing the two
 * would refuse orders for the accounts trusted most.
 */

import type Decimal from 'decimal.js'

import { toDecimal } from '@/lib/format'
import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no dealer branch, so the keys
 * live here in the same hierarchical shape as `assemblyKeys`.
 *
 * Deliberately *not* filed under the customers keys even though the rows come
 * from the customer table: the two lists answer different questions and a
 * dealer page must not read a cache entry populated by the unrestricted
 * customer list, which contains rows this screen may not show.
 */
export const dealerKeys = {
  all: ['dealers'] as const,
  list: (params: unknown) => ['dealers', 'list', params] as const,
}

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /customers/dealers` (`PaginatedResponse[CustomerSummary]`).
 *
 * Narrower than the detail model on purpose - no address, no contact, no block
 * reason. Anything this screen cannot show is genuinely absent from the
 * projection rather than merely unrendered.
 */
export type Dealer = SchemaOf<'CustomerSummary'>

/** The full `CustomerType` union. Only two of its members reach this list. */
export type CustomerChannel = SchemaOf<'CustomerType'>

/** `PriceType` - which `item_prices.price_type` order entry quotes from. */
export type PriceType = SchemaOf<'PriceType'>

// ------------------------------------------------------------ vocabulary ----

/** The channels `list_dealers` will accept; anything else is a 422. */
export type DealerChannel = Extract<CustomerChannel, 'DEALER' | 'DISTRIBUTOR'>

/**
 * A `Record` keyed on the narrowed union rather than a hand-kept array: if the
 * API widens or renames the dealer channels, this stops compiling instead of
 * leaving the filter silently short of an option.
 */
export const DEALER_CHANNEL_LABELS: Record<DealerChannel, string> = {
  DEALER: 'Dealer',
  DISTRIBUTOR: 'Distributor',
}

export const DEALER_CHANNELS = Object.keys(DEALER_CHANNEL_LABELS) as DealerChannel[]

export const PRICE_TYPE_LABELS: Record<PriceType, string> = {
  SALE: 'Sale',
  DEALER: 'Dealer',
  DISTRIBUTOR: 'Distributor',
}

export const PRICE_TYPES = Object.keys(PRICE_TYPE_LABELS) as PriceType[]

// ------------------------------------------------------------ state code ----

/**
 * The GST state code filter is two digits and nothing else.
 *
 * `_validate_state_code` rejects anything but `^[0-9]{2}$`, so a half-typed
 * "2" must not be sent - it would replace a working list with a 422 while the
 * user is still typing. The input strips non-digits so the only invalid state
 * reachable is a single digit.
 */
const STATE_CODE_LENGTH = 2

export const isCompleteStateCode = (value: string): boolean =>
  new RegExp(`^[0-9]{${STATE_CODE_LENGTH}}$`).test(value)

export const toStateCodeInput = (value: string): string =>
  value.replace(/[^0-9]/g, '').slice(0, STATE_CODE_LENGTH)

// ----------------------------------------------------------------- credit ---

/** Null is unlimited. Zero is a real ceiling of nothing. */
export const isUnlimitedCredit = (dealer: Dealer): boolean => dealer.credit_limit === null

/**
 * Limit less outstanding, or null when the account has no ceiling.
 *
 * This is `evaluate_credit` with an order value of zero - the same subtraction
 * the API performs for `credit_headroom` on the detail read, which the list
 * projection does not carry. It is a *position*, not a decision: whether a
 * particular order may be taken is `GET /customers/{id}/credit-status`, which
 * also weighs the block flag and the order in hand.
 */
export function creditHeadroom(dealer: Dealer): Decimal | null {
  if (dealer.credit_limit === null) return null
  return toDecimal(dealer.credit_limit).minus(toDecimal(dealer.outstanding_amount))
}

/**
 * Whether this account has already used up its limit.
 *
 * Mirrors the `over_credit_limit` clause exactly: a NULL limit can never be
 * exceeded, and the comparison is strictly greater-than, so an account sitting
 * precisely on its limit is still within it. A zero limit is included, because
 * zero means credit is forbidden and any outstanding balance against it is a
 * real breach rather than a rounding artefact.
 *
 * Decimal comparison throughout - an account one paisa over its limit is a
 * credit decision and must not ride on a float.
 */
export function isOverCreditLimit(dealer: Dealer): boolean {
  if (dealer.credit_limit === null) return false
  return toDecimal(dealer.outstanding_amount).greaterThan(toDecimal(dealer.credit_limit))
}

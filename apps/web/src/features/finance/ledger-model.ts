/**
 * Wire shapes and vocabulary for the general ledger: the chart of accounts and
 * the per-party statement.
 *
 * Every shape here is DERIVED from the generated contract through `SchemaOf`,
 * never retyped. A hand-written mirror of a server schema is a second opinion,
 * and `tsc` can only see a disagreement between two pieces of TypeScript - not
 * between an interface and a live API.
 *
 * Four properties of the contract shape the screens above it, and each one is
 * the reason for a helper below:
 *
 * 1. **A statement is not a paginated envelope.** `GET /finance/ledger/...`
 *    answers `ApiResponse<LedgerStatement>` - the rows sit inside a statement
 *    that also carries the opening and closing balances. It accepts `page` and
 *    `limit` and pages the rows server-side, but the router drops the
 *    pagination block, so nothing tells the client the total or whether another
 *    page exists. `useListQuery`, which reads envelope-level `pagination`,
 *    cannot drive it; `inferStatementPagination` says what is actually known.
 * 2. **The running balance belongs to the server.** PostgreSQL computes it with
 *    a window function over the whole filtered set plus the opening balance
 *    carried in. Accumulating it in the browser would restart the total at every
 *    page boundary, which is worse than showing no balance at all.
 * 3. **A statement is scoped by party, not by account.** `statement_stmt`
 *    filters on `party_type`/`party_id` only, so a party's statement spans every
 *    account their postings touch - not just the receivable or payable control
 *    account a reader may have arrived from.
 * 4. **`rows` is optional on the wire.** It defaults to an empty list on the
 *    server, so the generated type marks it optional; `statementRows` guards it
 *    once instead of at every call site.
 *
 * Money is a decimal string throughout and is only ever computed on with
 * `toDecimal`.
 */

import { toDecimal } from '@/lib/format'
import type { DecimalString, OffsetPagination, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no ledger branch, so the keys
 * live here in the same hierarchical shape - and they sit under
 * `financeKeys.all` (`finance-types.ts`) on purpose: approving a payment or an
 * invoice invalidates that root, and a statement keyed anywhere else would keep
 * showing pre-posting balances until it went stale on its own. Invalidating
 * `ledgerKeys.all` still clears every account list, account detail and
 * statement at once.
 *
 * `account(id)` sits under the list root deliberately - the chart-of-accounts
 * screen resolves a parent account by id for a chip and for the detail dialog,
 * and sharing the key means those two reads of the same account are one
 * request, not two.
 *
 * `kind` is the lower-case URL segment while `financeKeys.ledger` takes the
 * upper-case party type, so the two statement keys cannot collide.
 */
export const ledgerKeys = {
  all: ['finance', 'ledger'] as const,
  accounts: (params: unknown) => ['finance', 'ledger', 'accounts', params] as const,
  account: (id: string) => ['finance', 'ledger', 'accounts', 'detail', id] as const,
  statement: (kind: PartyKind, partyId: string, params: unknown) =>
    ['finance', 'ledger', kind, partyId, params] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /finance/accounts` and all of `GET /finance/accounts/{id}`. */
export type LedgerAccount = SchemaOf<'LedgerAccountRead'>

/** The five account classes, which fix the sign of a normal balance. */
export type LedgerAccountType = SchemaOf<'LedgerAccountType'>

/** `GET /finance/ledger/{customer|vendor}/{id}` - a whole party statement. */
export type LedgerStatement = SchemaOf<'LedgerStatement'>

/** One posting on a statement, with the balance after it. */
export type LedgerStatementRow = SchemaOf<'LedgerStatementRow'>

// ------------------------------------------------------------ vocabulary ----

/**
 * The two sides a statement can be asked for, spelled as the URL segment.
 *
 * Client-side by nature. The contract's `PartyType` also carries `NONE` - the
 * value a ledger entry reports when it is against no party at all (a bank, tax
 * or expense leg) - and there is no endpoint that would accept it here.
 */
export type PartyKind = 'customer' | 'vendor'

export const PARTY_KIND_LABEL: Record<PartyKind, string> = {
  customer: 'customer',
  vendor: 'vendor',
}

/** Narrow a URL parameter to a side, so a hand-edited link cannot build a path. */
export function toPartyKind(value: string | null | undefined): PartyKind | null {
  return value === 'customer' || value === 'vendor' ? value : null
}

/**
 * Every account class, for the filter.
 *
 * Typed as the contract's union rather than as free strings: drop a class from
 * the API and this list stops compiling instead of sending a 422 that only
 * shows up when somebody picks the dead option.
 */
export const ACCOUNT_TYPES: readonly LedgerAccountType[] = [
  'ASSET',
  'LIABILITY',
  'INCOME',
  'EXPENSE',
  'EQUITY',
]

/**
 * Which side increases each class - the definition of the class, not a rule
 * this screen invented (`LedgerAccountType`: "the five account classes, which
 * fix the sign of a normal balance").
 *
 * A `Record` over the union rather than a lookup with a fallback, so a class
 * ADDED to the contract fails the build here rather than silently rendering as
 * whatever the default happened to be.
 */
export const NORMAL_BALANCE: Record<LedgerAccountType, 'Debit' | 'Credit'> = {
  ASSET: 'Debit',
  EXPENSE: 'Debit',
  LIABILITY: 'Credit',
  INCOME: 'Credit',
  EQUITY: 'Credit',
}

/**
 * The two control accounts that carry party postings.
 *
 * These are `SystemAccount` values from the finance module, which resolves them
 * by `system_code` rather than by a chart code somebody may rename. The wire
 * types `system_code` as a plain string, so the coupling is named here once
 * instead of being spelled out wherever a screen wants to recognise one.
 */
const RECEIVABLE_SYSTEM_CODE = 'ACCOUNTS_RECEIVABLE'
const PAYABLE_SYSTEM_CODE = 'ACCOUNTS_PAYABLE'

/**
 * Which side of the ledger an account collects, if it is a control account.
 *
 * Receivable is customers, payable is vendors, everything else is `null` - a
 * bank, tax or expense account has no party behind its balance, so there is no
 * statement to offer from it.
 */
export function partyKindForControlAccount(systemCode: string | null): PartyKind | null {
  if (systemCode === RECEIVABLE_SYSTEM_CODE) return 'customer'
  if (systemCode === PAYABLE_SYSTEM_CODE) return 'vendor'
  return null
}

// -------------------------------------------------------------- derived -----

/** `/finance/ledger/customer/{id}` or `/finance/ledger/vendor/{id}`. */
export function statementPath(kind: PartyKind, partyId: string): string {
  return `/finance/ledger/${kind}/${partyId}`
}

/** `rows` defaults to an empty list on the server, so it is optional on the wire. */
export function statementRows(statement: LedgerStatement | undefined): LedgerStatementRow[] {
  return statement?.rows ?? []
}

/**
 * What is actually known about paging a statement.
 *
 * The endpoint takes `page` and `limit` and pages the rows, but answers an
 * `ApiResponse` with no pagination block - so the only honest signals are the
 * page that was asked for and whether it came back full. `total` stays `null`,
 * which the table renders as an unknown count rather than as a number nobody
 * counted.
 *
 * A page that comes back exactly full when the ledger ends there costs one
 * empty page, which is the safe direction: the alternative hides a real page.
 */
export function inferStatementPagination(
  rowCount: number,
  page: number,
  limit: number,
): OffsetPagination {
  return {
    page,
    limit,
    total: null,
    total_pages: null,
    has_next: rowCount >= limit,
    has_previous: page > 1,
  }
}

/**
 * What a closing balance means for this side of the ledger.
 *
 * A positive balance is always money owed (the endpoint guarantees the sign),
 * so the only thing the reader has to be told is *which way* - and that a
 * negative balance is unusual enough to be worth flagging. A party in credit is
 * far more often an unallocated receipt or a payment against the wrong document
 * than a genuine advance, and it is the one number on this screen a controller
 * would want to chase.
 */
export function describeClosingBalance(
  kind: PartyKind,
  balance: DecimalString | null | undefined,
): { label: string; tone: 'default' | 'warning' } {
  const amount = toDecimal(balance)
  if (amount.isZero()) return { label: 'Settled - nothing outstanding', tone: 'default' }
  if (amount.isPositive()) {
    return {
      label: kind === 'customer' ? 'Owed by this customer' : 'Owed to this vendor',
      tone: 'default',
    }
  }
  return {
    label:
      kind === 'customer'
        ? 'In credit - the customer has paid more than they were charged'
        : 'In credit - this vendor has been paid more than they billed',
    tone: 'warning',
  }
}

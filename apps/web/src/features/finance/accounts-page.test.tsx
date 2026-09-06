/**
 * The chart of accounts, and the party ledger that hangs off its two control
 * accounts.
 *
 * This screen writes nothing, which makes its interesting properties the ones
 * about *reading*.
 *
 * **Three rights meet here and none of them implies another.** `ledger.view`
 * answers for the chart and for the statement; `customer.view` and
 * `vendor.view` answer for the party picker that chooses whose statement to
 * read. Offering the drill-down to somebody who cannot search the party master
 * opens a section with nothing usable in it, and firing the query anyway puts a
 * 403 in front of a user who did nothing wrong - so the gate has to be on the
 * query, not only on the markup.
 *
 * **The status filter is three-valued and the third value is an absence.**
 * "Active" and "Retired" send `is_active=true/false`; "Active and retired" must
 * send *no* `is_active` at all. Getting that wrong is the same shape as the
 * `if xs: stmt = stmt.where(...)` inversion this codebase has already shipped
 * once - a filter that silently stops filtering, or one that never widens.
 *
 * **The running balance belongs to the server.** PostgreSQL computes it with a
 * window function over the whole filtered set plus the opening balance carried
 * in; adding the rows up in the browser restarts the total at every page
 * boundary. The statement fixture below deliberately carries a running balance
 * that a browser-side accumulation would not produce, so a regression to local
 * arithmetic fails here rather than on somebody's page two.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import AccountsPage from './accounts-page'
import type { LedgerAccount, LedgerStatement, LedgerStatementRow } from './ledger-model'

const RECEIVABLE_ID = 'ac000000-0000-7000-8000-000000000001'
const PAYABLE_ID = 'ac000000-0000-7000-8000-000000000002'
const BANK_ID = 'ac000000-0000-7000-8000-000000000003'
const CUSTOMER_ID = 'cu000000-0000-7000-8000-000000000001'

function account(overrides: Partial<LedgerAccount> = {}): LedgerAccount {
  return {
    id: RECEIVABLE_ID,
    code: '1200',
    name: 'Accounts Receivable',
    account_type: 'ASSET',
    parent_id: null,
    is_active: true,
    is_system: true,
    system_code: 'ACCOUNTS_RECEIVABLE',
    description: 'Amounts owed by customers',
    created_at: '2026-04-01T00:00:00Z',
    updated_at: '2026-04-01T00:00:00Z',
    ...overrides,
  }
}

const PAYABLE = account({
  id: PAYABLE_ID,
  code: '2100',
  name: 'Accounts Payable',
  account_type: 'LIABILITY',
  system_code: 'ACCOUNTS_PAYABLE',
  description: 'Amounts owed to vendors',
})

const BANK = account({
  id: BANK_ID,
  code: '1010',
  name: 'HDFC current account',
  account_type: 'ASSET',
  is_system: false,
  system_code: null,
  description: null,
})

function statementRow(overrides: Partial<LedgerStatementRow> = {}): LedgerStatementRow {
  return {
    entry_id: 'le000000-0000-7000-8000-000000000001',
    journal_id: 'jn000000-0000-7000-8000-000000000001',
    entry_date: '2026-08-04',
    account_code: '1200',
    account_name: 'Accounts Receivable',
    reference_type: 'SALES_INVOICE',
    reference_code: 'SINV/2026-27/00014',
    narration: 'Brass hardware, August consignment',
    debit_amount: '48910.00',
    credit_amount: '0.00',
    running_balance: '61410.00',
    is_reversal: false,
    ...overrides,
  }
}

function statement(overrides: Partial<LedgerStatement> = {}): LedgerStatement {
  return {
    party_type: 'CUSTOMER',
    party_id: CUSTOMER_ID,
    party_code: 'CUS-0031',
    party_name: 'Meridian Interiors',
    date_from: '2026-08-01',
    date_to: '2026-08-31',
    opening_balance: '12500.00',
    total_debit: '48910.00',
    total_credit: '9500.75',
    closing_balance: '51909.25',
    rows: [
      statementRow(),
      statementRow({
        entry_id: 'le000000-0000-7000-8000-000000000002',
        entry_date: '2026-08-19',
        reference_type: 'PAYMENT',
        reference_code: 'RCT/2026-27/00009',
        narration: 'Receipt against SINV/2026-27/00014',
        debit_amount: '0.00',
        credit_amount: '9500.75',
        running_balance: '51909.25',
      }),
    ],
    ...overrides,
  }
}

/** The row a control lives in, so a multi-row assertion cannot cross rows. */
function rowFor(code: string): HTMLElement {
  const cell = screen.getByText(code)
  const row = cell.closest('tr')
  expect(row, `no row for ${code}`).not.toBeNull()
  return row as HTMLElement
}

/** The chart list requests, captured so the query parameters can be asserted. */
function accountsRoute(rows: LedgerAccount[], seen: URL[]) {
  return ({ url }: { url: URL }) => {
    seen.push(url)
    return page(rows)
  }
}

/** The parameters of the most recent chart request. */
function latest(seen: URL[]): URLSearchParams {
  const last = seen[seen.length - 1]
  expect(last, 'the chart was never requested').toBeDefined()
  return (last as URL).searchParams
}

// -------------------------------------------------------- permission gating --

describe('reading the chart at all', () => {
  it('asks for nothing and says why when ledger.view is missing', async () => {
    // Gating the query rather than only the markup is the point. A list request
    // fired without the right comes back 403 and raises a toast in front of a
    // user who did nothing wrong, on a screen that already knows the answer.
    const { fetch } = renderScreen(<AccountsPage />, { permissions: [] })

    expect(await screen.findByText('The ledger is not visible to this account')).toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path === '/finance/accounts')).toBe(false)
  })

  it('lists the chart for a reader who holds it', async () => {
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: { 'GET /finance/accounts': page([account(), PAYABLE, BANK]) },
    })

    expect(await screen.findByText('Accounts Receivable')).toBeInTheDocument()
    // The system code is shown rather than hidden: it is the name the automatic
    // postings resolve, so it is what somebody tracing a posting looks for.
    expect(within(rowFor('1200')).getByText('ACCOUNTS_RECEIVABLE')).toBeInTheDocument()
    expect(within(rowFor('1010')).getByText('User-defined')).toBeInTheDocument()
  })
})

describe('the party drill-down', () => {
  it('offers a customer ledger from receivable only to someone who may read customers', async () => {
    // The statement itself needs only `ledger.view`, but the picker it opens
    // reads `/customers`. Offering the drill-down without `customer.view` opens
    // a section with nothing in it that can be used.
    const withoutMaster = renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: {
        'GET /finance/accounts': page([account()]),
        [`GET /finance/accounts/${RECEIVABLE_ID}`]: ok(account()),
      },
    })
    await withoutMaster.user.click(await screen.findByText('Accounts Receivable'))
    const gatedDialog = await screen.findByRole('dialog')
    expect(await within(gatedDialog).findByText('Control account')).toBeInTheDocument()
    expect(
      within(gatedDialog).queryByRole('button', { name: /open a customer ledger/i }),
    ).not.toBeInTheDocument()
    withoutMaster.unmount()

    const withMaster = renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW, P.CUSTOMER_VIEW],
      routes: {
        'GET /finance/accounts': page([account()]),
        [`GET /finance/accounts/${RECEIVABLE_ID}`]: ok(account()),
      },
    })
    await withMaster.user.click(await screen.findByText('Accounts Receivable'))
    const dialog = await screen.findByRole('dialog')
    expect(
      await within(dialog).findByRole('button', { name: /open a customer ledger/i }),
    ).toBeInTheDocument()
  })

  it('resolves payable to vendors and offers nothing from a bank account', async () => {
    // The two control accounts are recognised by `system_code`, never by a
    // chart code somebody may rename. A bank account has no party behind its
    // balance, so there is no statement to offer from it at all.
    const payable = renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW, P.VENDOR_VIEW, P.CUSTOMER_VIEW],
      routes: {
        'GET /finance/accounts': page([PAYABLE, BANK]),
        [`GET /finance/accounts/${PAYABLE_ID}`]: ok(PAYABLE),
      },
    })
    await payable.user.click(await screen.findByText('Accounts Payable'))
    const payableDialog = await screen.findByRole('dialog')
    expect(
      await within(payableDialog).findByRole('button', { name: /open a vendor ledger/i }),
    ).toBeInTheDocument()
    payable.unmount()

    const bank = renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW, P.VENDOR_VIEW, P.CUSTOMER_VIEW],
      routes: {
        'GET /finance/accounts': page([PAYABLE, BANK]),
        [`GET /finance/accounts/${BANK_ID}`]: ok(BANK),
      },
    })
    await bank.user.click(await screen.findByText('HDFC current account'))
    const bankDialog = await screen.findByRole('dialog')
    expect(await within(bankDialog).findByText('1010')).toBeInTheDocument()
    expect(
      within(bankDialog).queryByRole('button', { name: /open a .* ledger/i }),
    ).not.toBeInTheDocument()
    // Nor is a bank account marked as one the automatic postings resolve.
    expect(within(bankDialog).queryByText('Control account')).not.toBeInTheDocument()
  })
})

// ------------------------------------------------------- server-side filters -

describe('the filters are the server’s, not the browser’s', () => {
  it('narrows to active accounts by default', async () => {
    // Retired accounts are noise on a chart usually read to find where
    // something posts, so the default narrows - and the narrowing is a query
    // parameter, never a filter applied to a page already in hand.
    const seen: URL[] = []
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: { 'GET /finance/accounts': accountsRoute([account()], seen) },
    })

    await screen.findByText('Accounts Receivable')
    expect(latest(seen).get('is_active')).toBe('true')
    expect(latest(seen).get('sort_by')).toBe('code')
  })

  it('drops the restriction entirely for "Active and retired"', async () => {
    // The third value is an ABSENCE, not a word the API understands. Sending
    // `is_active=any` would be a 422; sending `is_active=false` would show only
    // the retired ones. This is the same shape as the empty-collection filter
    // inversion that once showed a tenant every ledger entry in the company.
    const seen: URL[] = []
    const { user } = renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: { 'GET /finance/accounts': accountsRoute([account()], seen) },
    })

    await screen.findByText('Accounts Receivable')
    await user.click(screen.getByRole('combobox', { name: 'Account status' }))
    await user.click(await screen.findByRole('option', { name: 'Active and retired' }))

    await expect.poll(() => latest(seen).has('is_active')).toBe(false)
    // The narrowing is gone rather than replaced by something the API would
    // reject, and the account class filter beside it is untouched.
    expect(latest(seen).get('account_type')).toBeNull()
  })

  it('drills into an account by asking the server for its children', async () => {
    // The chart is flat and paginated on purpose: a tree needs every level in
    // the browser at once, which is the fetch-everything-and-filter-locally
    // this codebase forbids. `parent_id` is a server-side filter instead.
    const seen: URL[] = []
    const { user } = renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: {
        'GET /finance/accounts': accountsRoute([account()], seen),
        [`GET /finance/accounts/${RECEIVABLE_ID}`]: ok(account()),
      },
    })

    await user.click(await screen.findByText('Accounts Receivable'))
    const dialog = await screen.findByRole('dialog')
    await user.click(
      await within(dialog).findByRole('button', { name: /accounts beneath this one/i }),
    )

    await expect.poll(() => latest(seen).get('parent_id')).toBe(RECEIVABLE_ID)
    // A chip names the account being drilled into, and it shares the detail
    // dialog's query key - so naming it is the same request, not a second one.
    expect(await screen.findByRole('button', { name: /show the whole chart again/i })).toBeVisible()
  })
})

// -------------------------------------------------------------- the ledger ---

describe('the party statement mounted beneath the chart', () => {
  const LEDGER_ROUTE = `/finance/ledger/customer/${CUSTOMER_ID}`
  const openStatement = `/?party_kind=customer&party_id=${CUSTOMER_ID}`

  it('shows the statement for a party named in the URL even with the master closed', async () => {
    // Deliberate: a statement shared as a link has to open for whoever holds
    // `ledger.view`, even if they cannot search the customer master to pick a
    // different party. The picker is what needs `customer.view`, not the
    // statement.
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      route: openStatement,
      routes: {
        'GET /finance/accounts': page([account()]),
        [`GET ${LEDGER_ROUTE}`]: ok(statement()),
      },
    })

    expect(await screen.findByText('Meridian Interiors')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Customer' })).not.toBeInTheDocument()
  })

  it('prints the statement figures the server sent, in rupees and lakhs', async () => {
    // Every one of these is `formatMoney` of a decimal string the API derived
    // over the whole filtered period - not over the page on screen. The Indian
    // grouping is the visible half: 48,910.00, not 48,910.00 with a thousands
    // comma in the wrong place.
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      route: openStatement,
      routes: {
        'GET /finance/accounts': page([account()]),
        [`GET ${LEDGER_ROUTE}`]: ok(statement()),
      },
    })

    expect(await screen.findByText('₹12,500.00')).toBeInTheDocument()
    // Each side appears twice on purpose - the period total, and the single
    // posting that makes it up - which is the arithmetic a reader checks by
    // eye before believing the closing balance.
    expect(screen.getAllByText('₹48,910.00')).toHaveLength(2)
    expect(screen.getAllByText('₹9,500.75')).toHaveLength(2)
    expect(screen.getAllByText('₹51,909.25').length).toBeGreaterThan(0)
  })

  it('prints the running balance the window function computed, not a local sum', async () => {
    // The balance is a window function over the whole filtered set plus the
    // opening balance carried in. Accumulating it here restarts the total at
    // every page boundary, and a balance that is only right on page one is
    // worse than none. The fixture's first row carries 61,410.00 - a figure no
    // browser-side accumulation of debit minus credit would produce, since the
    // opening balance is not on the row.
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      route: openStatement,
      routes: {
        'GET /finance/accounts': page([account()]),
        [`GET ${LEDGER_ROUTE}`]: ok(statement()),
      },
    })

    const first = await screen.findByText('SINV/2026-27/00014')
    expect(within(first.closest('tr') as HTMLElement).getByText('₹61,410.00')).toBeInTheDocument()
    // Exactly one of debit and credit is posted on a ledger row, and the empty
    // side is a dash: a column of "0.00" against every charge reads as an
    // amount somebody posted that happened to be nothing.
    expect(within(first.closest('tr') as HTMLElement).getAllByText('-')).not.toHaveLength(0)
  })

  it('flags a party in credit rather than reporting it as settled', async () => {
    // A negative balance on a customer is far more often an unallocated receipt
    // or a payment against the wrong document than a genuine advance, and it is
    // the one figure on this screen a controller would want to chase.
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      route: openStatement,
      routes: {
        'GET /finance/accounts': page([account()]),
        [`GET ${LEDGER_ROUTE}`]: ok(statement({ closing_balance: '-4200.00' })),
      },
    })

    expect(
      await screen.findByText('In credit - the customer has paid more than they were charged'),
    ).toBeInTheDocument()
    expect(screen.getByText('-₹4,200.00')).toBeInTheDocument()
  })

  it('asks a reader without customer.view to be given a party rather than showing an empty picker', async () => {
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      route: '/?party_kind=customer',
      routes: { 'GET /finance/accounts': page([account()]) },
    })

    expect(await screen.findByText('Customers are not visible to this account')).toBeInTheDocument()
    expect(screen.getByText(/customer view right \(customer\.view\)/i)).toBeInTheDocument()
  })
})

// ------------------------------------------------------ empty and broken -----

describe('the states with no rows in them', () => {
  it('suggests widening the status when nothing matches', async () => {
    // The default hides retired accounts, so "nothing here" most often means
    // the filter rather than an empty chart - and saying so is the difference
    // between a dead end and one click.
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: { 'GET /finance/accounts': page([]) },
    })

    expect(await screen.findByText('No accounts match')).toBeInTheDocument()
    expect(screen.getByText(/widen the status to include retired accounts/i)).toBeInTheDocument()
  })

  it('shows the server message when the chart cannot be read', async () => {
    renderScreen(<AccountsPage />, {
      permissions: [P.LEDGER_VIEW],
      routes: {
        'GET /finance/accounts': fail(500, 'INTERNAL_ERROR', 'The ledger is being rebuilt'),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeInTheDocument()
    expect(screen.getByText('The ledger is being rebuilt')).toBeInTheDocument()
  })
})

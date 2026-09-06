/**
 * The customer master list, the dialog that opens an account, and the one that
 * retires it.
 *
 * Three properties earn most of this file.
 *
 * **A null credit limit means unlimited, and zero means no credit at all.**
 * They are opposite instructions and the API's own words say so. A grid that
 * collapsed them would print "₹0.00" against the accounts trusted most, and
 * order entry would look, to whoever reads this screen, as though it were about
 * to refuse them.
 *
 * **Retirement is offered only where it can actually be taken.** `DELETE
 * /customers/{id}` declares `customer.delete`, the service then demands
 * `dealer.manage` on top of it for a DEALER or DISTRIBUTOR account, and it
 * refuses outright while the account still owes anything. All three are known
 * on the row, so the control is present, disabled-with-a-reason, or absent -
 * never a form whose only possible ending is a refusal.
 *
 * **The reason travels in the query string, not in a body.** A DELETE body is
 * legal HTTP that several proxies drop, so the API declares `reason` as a
 * required query parameter. Sending it as a body is a 422 at the moment of use,
 * and no type checker can see the difference - only the request can.
 *
 * The `<Select>` filters are driven through the URL rather than clicked: that is
 * where these controls keep their state, and every assertion closes the loop by
 * reading the request the URL produced.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/overlays'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import type { CustomerRow } from './customer-types'
import CustomersPage from './customers-page'

// ------------------------------------------------------------- fixtures ----

const ACME_ID = '01a00000-0000-7000-8000-0000000cust1'
const DEALER_ID = '01a00000-0000-7000-8000-0000000cust2'

function customer(overrides: Partial<CustomerRow> = {}): CustomerRow {
  return {
    id: ACME_ID,
    code: 'C-0001',
    name: 'Acme Hardware',
    legal_name: 'Acme Hardware Private Limited',
    customer_type: 'CUSTOMER',
    price_list_type: 'SALE',
    gstin: '27AAAAA0000A1Z2',
    credit_limit: '5000.0000',
    credit_days: 30,
    outstanding_amount: '0.0000',
    is_active: true,
    is_blocked: false,
    assigned_sales_user_id: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const DEALER = customer({
  id: DEALER_ID,
  code: 'D-0007',
  name: 'Northline Distributors',
  legal_name: null,
  customer_type: 'DEALER',
  price_list_type: 'DEALER',
})

/** `GET /customers/{id}`: the summary row plus the fields only the detail carries. */
function detailOf(row: CustomerRow) {
  return {
    ...row,
    pan: null,
    block_reason: null,
    blocked_at: null,
    blocked_by_id: null,
    default_transporter_id: null,
    credit_headroom: row.credit_limit,
    notes: null,
    updated_at: '2026-02-01T00:00:00Z',
  }
}

// -------------------------------------------------------------- helpers ----

/**
 * The screen as `app-layout` mounts it.
 *
 * The row's disabled Retire control explains itself through a `Tooltip`, and
 * Radix requires a provider above it. The app has one at the layout; without it
 * here the screen would throw for a reason the user never sees.
 */
function renderCustomers(options: { permissions: string[]; routes: RouteTable; route?: string }) {
  return renderScreen(
    <TooltipProvider>
      <CustomersPage />
    </TooltipProvider>,
    options,
  )
}

/** Wait until the screen has stopped fetching, so a press lands on live DOM. */
async function settled(fetch: FetchRecorder): Promise<void> {
  let seen = -1
  await waitFor(() => {
    const now = fetch.calls.length
    const stopped = now > 0 && now === seen
    seen = now
    expect(stopped, 'the screen is still fetching').toBe(true)
  })
}

/** Wait for a control, then read it again - the node may have been replaced. */
async function stable<T extends HTMLElement>(read: () => T): Promise<T> {
  await waitFor(read)
  return read()
}

/** Records the query string of every list request so a test can read it back. */
function listSpy(rows: CustomerRow[] = [customer()]) {
  const queries: URLSearchParams[] = []
  return {
    handler: ({ url }: { url: URL }) => {
      queries.push(url.searchParams)
      return page(rows)
    },
    last() {
      const params = queries[queries.length - 1]
      expect(params, 'expected the screen to have queried /customers').toBeDefined()
      return params!
    },
  }
}

// --------------------------------------------------------------- credit ----

describe('what the row says about credit', () => {
  it('calls a null limit unlimited rather than printing zero', async () => {
    // The API's own words: "Omit or send null for an unlimited limit; send 0 to
    // forbid credit entirely." Printing ₹0.00 for a null says the opposite of
    // what the record means, about the accounts that are trusted most.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: { 'GET /customers': page([customer({ credit_limit: null })]) },
    })

    expect(await screen.findByText('Unlimited')).toBeVisible()
  })

  it('prints a real ceiling as money', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: { 'GET /customers': page([customer({ credit_limit: '5000.0000' })]) },
    })

    expect(await screen.findByText(/5,000\.00/)).toBeVisible()
    expect(screen.queryByText('Unlimited')).not.toBeInTheDocument()
  })

  it('prints a zero ceiling as zero, because that is a real instruction', async () => {
    // Zero means credit is forbidden - orders are expected against payment. It
    // is not the same as no ceiling and must not read as one.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: {
        'GET /customers': page([
          customer({ credit_limit: '0.0000', outstanding_amount: '750.0000' }),
        ]),
      },
    })

    await screen.findByText('C-0001')
    expect(screen.queryByText('Unlimited')).not.toBeInTheDocument()
    // Exactly zero, in the credit limit cell - the outstanding balance beside it
    // is 750, so this cannot be matching the wrong column.
    expect(screen.getByText('₹0.00')).toBeVisible()
    expect(screen.getByText('₹750.00')).toBeVisible()
  })

  it('shows the outstanding balance the API holds, not one it re-derives', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: {
        'GET /customers': page([
          customer({ credit_limit: '5000.0000', outstanding_amount: '6200.0000' }),
        ]),
      },
    })

    expect(await screen.findByText(/6,200\.00/)).toBeVisible()
  })
})

// -------------------------------------------------------------- the list ----

describe('the list', () => {
  it('asks the API for active accounts unless told otherwise', async () => {
    // The default is a narrowing, not "everything": a retired or deactivated
    // account arriving unannounced in a picker-shaped list is how a dead account
    // ends up on a new order.
    const spy = listSpy()
    const { fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: { 'GET /customers': spy.handler },
    })

    await settled(fetch)
    expect(spy.last().get('is_active')).toBe('true')
  })

  it('sends every filter to the API rather than narrowing the page in the browser', async () => {
    // Master rules 31/36: a dealer network runs to thousands of accounts and the
    // browser never receives more than the page it asked for. Only the request
    // can tell a server-side filter from a `useMemo`.
    const spy = listSpy()
    const { fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      route: '/customers?customer_type=DEALER&standing=over_limit&is_active=false',
      routes: { 'GET /customers': spy.handler },
    })

    await settled(fetch)
    const params = spy.last()
    expect(params.get('customer_type')).toBe('DEALER')
    // The API decides who is over their limit: it holds the exposure the browser
    // cannot see, and a null limit is unlimited rather than zero.
    expect(params.get('over_credit_limit')).toBe('true')
    expect(params.get('is_active')).toBe('false')
  })

  it('turns the blocked filter into the flag the API understands', async () => {
    const spy = listSpy()
    const { fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      route: '/customers?standing=blocked',
      routes: { 'GET /customers': spy.handler },
    })

    await settled(fetch)
    expect(spy.last().get('is_blocked')).toBe('true')
    expect(spy.last().get('over_credit_limit')).toBeNull()
  })

  it('names a blocked account as blocked', async () => {
    // Credit standing is stated in words, not only in a colour: it is the reason
    // an order will be refused and the operator needs to read it.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: { 'GET /customers': page([customer({ is_blocked: true })]) },
    })

    expect(await screen.findByText('Blocked')).toBeVisible()
  })

  it('offers a way out of an empty list rather than a dead end', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: { 'GET /customers': page([]) },
    })

    expect(await screen.findByText('No customers found')).toBeVisible()
    expect(screen.getAllByRole('button', { name: /new customer/i })).toHaveLength(2)
  })

  it('surfaces the API sentence when the list fails, with a way to retry', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: {
        'GET /customers': fail(503, 'SERVICE_UNAVAILABLE', 'The customer service is restarting'),
      },
    })

    expect(await screen.findByText('The customer service is restarting')).toBeVisible()
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })
})

// -------------------------------------------------------- permission gating --

describe('permission gating', () => {
  it('offers New customer to a holder of customer.create', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: { 'GET /customers': page([customer()]) },
    })

    await screen.findByText('C-0001')
    expect(screen.getByRole('button', { name: /new customer/i })).toBeVisible()
  })

  it('withholds it from a reader', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: { 'GET /customers': page([customer()]) },
    })

    await screen.findByText('C-0001')
    expect(screen.queryByRole('button', { name: /new customer/i })).not.toBeInTheDocument()
  })

  it('offers the row edit to a holder of customer.update', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_UPDATE],
      routes: { 'GET /customers': page([customer()]) },
    })

    expect(await screen.findByRole('button', { name: 'Edit C-0001' })).toBeVisible()
  })

  it('withholds the row edit from a reader', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW],
      routes: { 'GET /customers': page([customer()]) },
    })

    await screen.findByText('C-0001')
    expect(screen.queryByRole('button', { name: 'Edit C-0001' })).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------- retiring an acct --

describe('retiring an account', () => {
  it('offers retirement where the account owes nothing', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: { 'GET /customers': page([customer({ outstanding_amount: '0.0000' })]) },
    })

    expect(await screen.findByRole('button', { name: 'Retire C-0001' })).toBeEnabled()
  })

  it('withholds it from a principal without customer.delete', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_UPDATE],
      routes: { 'GET /customers': page([customer()]) },
    })

    await screen.findByText('C-0001')
    expect(screen.queryByRole('button', { name: 'Retire C-0001' })).not.toBeInTheDocument()
  })

  it('still offers it for an account that is merely inactive', async () => {
    // The lifecycle has a Retire edge from ACTIVE *and* from INACTIVE. Gating on
    // `is_active` instead of the edge would hide the action on exactly the
    // accounts most likely to be retired.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      route: '/customers?is_active=false',
      routes: { 'GET /customers': page([customer({ is_active: false })]) },
    })

    expect(await screen.findByRole('button', { name: 'Retire C-0001' })).toBeEnabled()
  })

  it('withholds it on a dealer account from a principal without dealer.manage', async () => {
    // Every write to a DEALER or DISTRIBUTOR account goes through
    // `assert_channel_write_allowed`, which demands `dealer.manage` on top of
    // the route's own right. That right gates no route of its own, so the check
    // has to happen per row - and without it, the channel team's accounts are
    // the only ones this user may not retire.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: { 'GET /customers': page([DEALER]) },
    })

    await screen.findByText('D-0007')
    expect(screen.queryByRole('button', { name: 'Retire D-0007' })).not.toBeInTheDocument()
  })

  it('offers it on a dealer account once dealer.manage is held', async () => {
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE, P.DEALER_MANAGE],
      routes: { 'GET /customers': page([DEALER]) },
    })

    expect(await screen.findByRole('button', { name: 'Retire D-0007' })).toBeEnabled()
  })

  it('disables it, naming the balance, while the account still owes money', async () => {
    // The service refuses outright: soft-deleting the master row would hide a
    // live receivable behind it. The amount is in the control's accessible name
    // rather than only in a tooltip, because a disabled button explains nothing.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: { 'GET /customers': page([customer({ outstanding_amount: '1200.0000' })]) },
    })

    const blocked = await screen.findByRole('button', {
      name: /C-0001 cannot be retired while .*1,200\.00 is outstanding/,
    })
    expect(blocked).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Retire C-0001' })).not.toBeInTheDocument()
  })

  it('refuses just as firmly when the balance is a credit', async () => {
    // An over-payment leaves a non-zero balance too, and the service compares
    // against zero rather than with `> 0`. Retiring the account would hide money
    // owed *to* the customer.
    renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: { 'GET /customers': page([customer({ outstanding_amount: '-500.0000' })]) },
    })

    const blocked = await screen.findByRole('button', {
      name: /cannot be retired while .*500\.00 is outstanding/,
    })
    expect(blocked).toBeDisabled()
  })

  it('sends the reason as a query parameter, with no body at all', async () => {
    // A DELETE body is legal HTTP that proxies drop, so the API declares
    // `reason` in the query string. Sending it as a body is a 422 at the moment
    // of use - and the request is the only place the difference is visible.
    const seen: URLSearchParams[] = []
    const { user, fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: {
        'GET /customers': page([customer()]),
        [`DELETE /customers/${ACME_ID}`]: ({ url }) => {
          seen.push(url.searchParams)
          return ok(null)
        },
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Retire C-0001' }))
    await user.type(
      await stable(() => screen.getByLabelText(/reason/i)),
      'Account merged into C-0002',
    )
    await user.click(screen.getByRole('button', { name: 'Retire customer' }))

    await waitFor(() => fetch.requestFor('DELETE', `/customers/${ACME_ID}`))
    expect(seen[0]?.get('reason')).toBe('Account merged into C-0002')
    expect(fetch.requestFor('DELETE', `/customers/${ACME_ID}`)).toBeUndefined()
  })

  it('holds back a reason too short for the API to accept', async () => {
    // The API's own `min_length` is 3. Sending a shorter one earns a 422 against
    // a field the dialog would then have to explain after the fact.
    const { user, fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: { 'GET /customers': page([customer()]) },
    })

    await user.click(await screen.findByRole('button', { name: 'Retire C-0001' }))
    await user.type(await stable(() => screen.getByLabelText(/reason/i)), 'x')
    await user.click(screen.getByRole('button', { name: 'Retire customer' }))

    expect(await screen.findByText('Record at least three characters of reason')).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'DELETE')).toHaveLength(0)
  })

  it('shows the API refusal in the dialog, next to what was typed', async () => {
    // The race the pre-emption cannot cover: an invoice posted between the page
    // load and the click. The API's sentence names the balance that stood in the
    // way, which is more use than "request failed" - and it has to appear where
    // the reason still is, so it can be corrected rather than retyped.
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_DELETE],
      routes: {
        'GET /customers': page([customer()]),
        [`DELETE /customers/${ACME_ID}`]: fail(
          422,
          'BLOCK_RETIREMENT_WITH_OUTSTANDING',
          'C-0001 still has 1,200.00 outstanding',
        ),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Retire C-0001' }))
    await user.type(await stable(() => screen.getByLabelText(/reason/i)), 'Duplicate account')
    await user.click(screen.getByRole('button', { name: 'Retire customer' }))

    expect(await screen.findByText('The account was not retired')).toBeVisible()
    expect(screen.getByText('C-0001 still has 1,200.00 outstanding')).toBeVisible()
    expect(screen.getByLabelText(/reason/i)).toHaveValue('Duplicate account')
  })
})

// --------------------------------------------------------- the account form --

describe('what the account form sends', () => {
  /**
   * Open the create dialog and return it.
   *
   * Every field query below is scoped to the dialog: the table's own search box
   * is labelled "Search code, name or GSTIN", which an unscoped `/code/i` finds
   * as readily as the Code field.
   */
  async function openCreate(user: ReturnType<typeof renderCustomers>['user']) {
    await user.click(await screen.findByRole('button', { name: /new customer/i }))
    return stable(() => screen.getByRole('dialog'))
  }

  it('keeps the credit limit a string and the credit days a number', async () => {
    // Money is a decimal string end to end - an account one rupee over its limit
    // is a real credit decision and an IEEE-754 double is not allowed to make
    // it. Credit days is a whole number the contract declares as an integer.
    const { user, fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: {
        'GET /customers': page([customer()]),
        'POST /customers': ok(customer({ code: 'C-0002' })),
      },
    })

    const dialog = await openCreate(user)
    await user.type(within(dialog).getByLabelText(/code/i), 'C-0002')
    await user.type(within(dialog).getByLabelText(/trading name/i), 'Bharat Fittings')
    const limit = within(dialog).getByLabelText(/credit limit/i)
    await user.clear(limit)
    await user.type(limit, '250000.5000')
    const days = within(dialog).getByLabelText(/credit days/i)
    await user.clear(days)
    await user.type(days, '45')
    await user.click(within(dialog).getByRole('button', { name: 'Create customer' }))

    await waitFor(() => fetch.requestFor('POST', '/customers'))
    const body = fetch.requestFor('POST', '/customers') as Record<string, unknown>
    expect(body.code).toBe('C-0002')
    expect(body.name).toBe('Bharat Fittings')
    expect(body.credit_limit).toBe('250000.5000')
    expect(typeof body.credit_limit).toBe('string')
    expect(body.credit_days).toBe(45)
    // Nothing was typed into these; they are sent as null, not "" - an empty
    // string would be stored as a legal name of no characters.
    expect(body.legal_name).toBeNull()
    expect(body.gstin).toBeNull()
    expect(body.pan).toBeNull()
    expect(body.notes).toBeNull()
  })

  it('sends a null limit when the account is marked unlimited', async () => {
    // Not "" and not 0: null is what the API reads as no ceiling, and 0 would
    // forbid credit entirely on an account meant to have none.
    const { user, fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: {
        'GET /customers': page([customer()]),
        'POST /customers': ok(customer({ code: 'C-0003' })),
      },
    })

    const dialog = await openCreate(user)
    await user.type(within(dialog).getByLabelText(/code/i), 'C-0003')
    await user.type(within(dialog).getByLabelText(/trading name/i), 'Kaveri Hardware')
    await user.click(within(dialog).getByRole('checkbox', { name: /unlimited/i }))
    await user.click(within(dialog).getByRole('button', { name: 'Create customer' }))

    await waitFor(() => fetch.requestFor('POST', '/customers'))
    const body = fetch.requestFor('POST', '/customers') as Record<string, unknown>
    expect(body.credit_limit).toBeNull()
  })

  it('refuses a limited account with no figure rather than guessing', async () => {
    // An empty box usually means "nothing"; the API reads a missing limit as
    // unlimited. The two readings are opposite, so the form will not send it.
    const { user, fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: { 'GET /customers': page([customer()]) },
    })

    const dialog = await openCreate(user)
    await user.type(within(dialog).getByLabelText(/code/i), 'C-0004')
    await user.type(within(dialog).getByLabelText(/trading name/i), 'Sundar Traders')
    await user.clear(within(dialog).getByLabelText(/credit limit/i))
    await user.click(within(dialog).getByRole('button', { name: 'Create customer' }))

    expect(
      await screen.findByText('Enter a limit, or tick unlimited. Zero means no credit at all.'),
    ).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('renders a rejected field against the field the API named', async () => {
    // The query client stays quiet about a 422 carrying field errors, on the
    // understanding that the form renders them inline. If it does not, the save
    // appears to do nothing at all.
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: {
        'GET /customers': page([customer()]),
        'POST /customers': fail(422, 'VALIDATION_ERROR', 'Invalid', {
          fields: { code: ['A customer with this code already exists'] },
        }),
      },
    })

    const dialog = await openCreate(user)
    await user.type(within(dialog).getByLabelText(/code/i), 'C-0001')
    await user.type(within(dialog).getByLabelText(/trading name/i), 'Acme Again')
    await user.click(within(dialog).getByRole('button', { name: 'Create customer' }))

    const message = await screen.findByText('A customer with this code already exists')
    expect(message).toBeVisible()
    expect(within(dialog).getByLabelText(/code/i)).toHaveAttribute('aria-invalid', 'true')
  })

  it('locks the code on an edit, because documents already quote it', async () => {
    const { user, fetch } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_UPDATE],
      routes: {
        'GET /customers': page([customer()]),
        [`GET /customers/${ACME_ID}`]: ok(detailOf(customer())),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Edit C-0001' }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    expect(await within(dialog).findByLabelText(/code/i)).toBeDisabled()
    expect(fetch.calls.some((call) => call.path === `/customers/${ACME_ID}`)).toBe(true)
  })
})

// ------------------------------------------------------- the dealer channel --

describe('the dealer channel in the account form', () => {
  /**
   * Every write to a DEALER or DISTRIBUTOR account - `update_customer`,
   * `set_active`, `block`, `unblock` - goes through `assert_channel_write_allowed`,
   * which demands `dealer.manage` on top of `customer.update`. The row still
   * opens, because the address and contact books are writable on `customer.update`
   * alone; what the dialog withholds is the details Save and the standing
   * actions, which would otherwise be offered and then refused with a 403.
   */
  async function openEdit(user: ReturnType<typeof renderCustomers>['user'], code: string) {
    await user.click(await screen.findByRole('button', { name: `Edit ${code}` }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    // The rule needs the loaded record; the locked code field is the sign it is in.
    expect(await within(dialog).findByLabelText(/code/i)).toBeDisabled()
    return dialog
  }

  it('withholds Save and the standing actions on a dealer from a principal without dealer.manage', async () => {
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_UPDATE],
      routes: {
        'GET /customers': page([DEALER]),
        [`GET /customers/${DEALER_ID}`]: ok(detailOf(DEALER)),
      },
    })

    const dialog = await openEdit(user, 'D-0007')
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(within(dialog).getByRole('combobox', { name: /customer type/i })).toBeDisabled()
    expect(within(dialog).queryByRole('button', { name: 'Block' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/amended by the channel team/i)).toBeVisible()
  })

  it('offers them on a dealer once dealer.manage is held', async () => {
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_UPDATE, P.DEALER_MANAGE],
      routes: {
        'GET /customers': page([DEALER]),
        [`GET /customers/${DEALER_ID}`]: ok(detailOf(DEALER)),
      },
    })

    const dialog = await openEdit(user, 'D-0007')
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeEnabled()
    expect(within(dialog).getByRole('combobox', { name: /customer type/i })).toBeEnabled()
    expect(within(dialog).getByRole('button', { name: 'Block' })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Deactivate' })).toBeVisible()
    expect(within(dialog).queryByText(/amended by the channel team/i)).not.toBeInTheDocument()
  })

  it('leaves a direct account fully editable without dealer.manage', async () => {
    // The channel rule is about the channel, not about the right: an ordinary
    // account is amended on `customer.update` alone.
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_UPDATE],
      routes: {
        'GET /customers': page([customer()]),
        [`GET /customers/${ACME_ID}`]: ok(detailOf(customer())),
      },
    })

    const dialog = await openEdit(user, 'C-0001')
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeEnabled()
    expect(within(dialog).getByRole('button', { name: 'Block' })).toBeVisible()
  })

  it('offers no dealer channel on a new account to a principal without dealer.manage', async () => {
    // The service refuses to open an account in either channel without the
    // right, so the type select does not list them - a create that could only
    // end in a refusal is not offered.
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE],
      routes: { 'GET /customers': page([customer()]) },
    })

    await user.click(await screen.findByRole('button', { name: /new customer/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    await user.click(within(dialog).getByRole('combobox', { name: /customer type/i }))

    expect(await screen.findByRole('option', { name: 'Customer' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Oem' })).toBeVisible()
    expect(screen.queryByRole('option', { name: 'Dealer' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Distributor' })).not.toBeInTheDocument()
  })

  it('offers both dealer channels on a new account to a holder of dealer.manage', async () => {
    const { user } = renderCustomers({
      permissions: [P.CUSTOMER_VIEW, P.CUSTOMER_CREATE, P.DEALER_MANAGE],
      routes: { 'GET /customers': page([customer()]) },
    })

    await user.click(await screen.findByRole('button', { name: /new customer/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    await user.click(within(dialog).getByRole('combobox', { name: /customer type/i }))

    expect(await screen.findByRole('option', { name: 'Dealer' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Distributor' })).toBeVisible()
  })
})

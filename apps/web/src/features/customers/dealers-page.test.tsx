/**
 * The dealer channel list.
 *
 * **This screen is read-only by design, and that is the property most worth a
 * test.** `GET /customers/dealers` is the entire dealer surface of the API -
 * there is no dealer create, update, block or delete route to put behind a
 * button. Dealer and distributor accounts are opened and amended on the
 * Customers screen, where the service demands `dealer.manage` on top of the
 * customer right. So the absence of write controls here is a fact about the
 * contract, not a permission that happens to be missing today, and the test
 * below holds every write right at once to prove nothing appears.
 *
 * Second: **either `dealer.view` or `customer.view` opens this list**, because
 * the route enforces `require_any_permission` of the two. Demanding
 * `dealer.view` alone would hide it from a sales manager the API would have
 * answered - and the query is not fired at all without one of them, so a user
 * who has neither sees an explanation rather than a 403 toast.
 *
 * Third: **the credit position is arithmetic on decimal strings**, and null
 * means unlimited rather than zero. An account one paisa over its limit is a
 * credit decision, so the headroom is stated in words next to the balance
 * rather than left to a colour.
 *
 * The filters are driven through the URL rather than clicked, which is where
 * these controls keep their state; each assertion closes the loop by reading the
 * request the URL produced. The one control opened for real is the channel
 * select, because *which options exist* is the assertion.
 */

import { screen, waitFor } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, page, renderScreen, type FetchRecorder } from '@/test/harness'

import type { Dealer } from './dealer-model'
import DealersPage from './dealers-page'

/** Pseudo-classes jsdom answers by re-entering its own selector engine. */
const TOP_LAYER_SELECTORS = new Set([':modal', ':fullscreen', ':popover-open'])

beforeAll(() => {
  // Floating UI asks every popper whether its element is in the top layer, and
  // nwsapi answers `:modal` by asking jsdom to match `:fullscreen`, which
  // re-enters nwsapi: one dropdown costs tens of millions of `matches` calls.
  const nativeMatches = Element.prototype.matches
  Element.prototype.matches = function (this: Element, selector: string) {
    return TOP_LAYER_SELECTORS.has(selector) ? false : nativeMatches.call(this, selector)
  }
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
})

// ------------------------------------------------------------- fixtures ----

const DEALERS_PATH = '/customers/dealers'
/** The id `meResponse` signs the test user in with. */
const SIGNED_IN_USER = '01a00000-0000-7000-8000-000000000001'

function dealer(overrides: Partial<Dealer> = {}): Dealer {
  return {
    id: '01a00000-0000-7000-8000-000000000d01',
    code: 'D-0007',
    name: 'Northline Distributors',
    legal_name: 'Northline Distributors LLP',
    customer_type: 'DEALER',
    price_list_type: 'DEALER',
    gstin: '27AAAAA0000A1Z2',
    credit_limit: '5000.0000',
    credit_days: 45,
    outstanding_amount: '1000.0000',
    is_active: true,
    is_blocked: false,
    assigned_sales_user_id: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

// -------------------------------------------------------------- helpers ----

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

/** Records the query string of every list request so a test can read it back. */
function listSpy(rows: Dealer[] = [dealer()]) {
  const queries: URLSearchParams[] = []
  return {
    handler: ({ url }: { url: URL }) => {
      queries.push(url.searchParams)
      return page(rows)
    },
    last() {
      const params = queries[queries.length - 1]
      expect(params, `expected the screen to have queried ${DEALERS_PATH}`).toBeDefined()
      return params!
    },
  }
}

/** What a screen reader would call each button currently on screen. */
function buttonNames(): string[] {
  return screen
    .getAllByRole('button')
    .map((button) => button.getAttribute('aria-label') ?? button.textContent ?? '')
    .map((name) => name.trim())
}

/** Verbs that would mean this screen had grown a write it has no endpoint for. */
const WRITE_VERBS = ['new', 'add', 'create', 'edit', 'retire', 'delete', 'remove', 'block', 'save']

/**
 * The write verbs a control's name contains, as whole words.
 *
 * Compared word by word rather than as a substring because the sortable "Credit
 * limit" header is itself a button, and "Cr-edit" is not an edit control.
 */
function writeVerbsIn(name: string): string[] {
  const words = name.toLowerCase().split(/[^a-z]+/)
  return WRITE_VERBS.filter((verb) => words.includes(verb))
}

// ----------------------------------------------------------- read-only -----

describe('the dealer list is read-only', () => {
  it('offers no create, edit or retire even to a principal holding every write right', async () => {
    // Nobody should go looking for the missing dialogs: they are not hidden by a
    // permission, they do not exist. Granting `customer.create`, `.update`,
    // `.delete` and `dealer.manage` together is the strongest form of that
    // statement - if a write control were gated on any of them it would appear.
    const { fetch } = renderScreen(<DealersPage />, {
      permissions: [
        P.DEALER_VIEW,
        P.DEALER_MANAGE,
        P.CUSTOMER_VIEW,
        P.CUSTOMER_CREATE,
        P.CUSTOMER_UPDATE,
        P.CUSTOMER_DELETE,
      ],
      routes: { [`GET ${DEALERS_PATH}`]: page([dealer()]) },
    })

    await settled(fetch)
    await screen.findByText('D-0007')

    const controls = buttonNames()
    // Guard the guard: an empty button list would satisfy the loop below while
    // proving nothing at all.
    expect(controls.length).toBeGreaterThan(0)
    for (const name of controls) {
      expect(writeVerbsIn(name), `a write-shaped control appeared: ${name}`).toEqual([])
    }
    // And no row is a link into an editor either.
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('says where an account is opened instead of offering to open one here', async () => {
    // The empty state of a read-only list has to point somewhere, or it reads as
    // a screen that is simply broken.
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW, P.CUSTOMER_CREATE],
      routes: { [`GET ${DEALERS_PATH}`]: page([]) },
    })

    expect(await screen.findByText('No dealer accounts found')).toBeVisible()
    expect(
      screen.getByText(/opened on the Customers screen with the Dealer or Distributor channel/i),
    ).toBeVisible()
    expect(screen.queryByRole('button', { name: /new/i })).not.toBeInTheDocument()
  })

  it('offers only the two channels the endpoint accepts', async () => {
    // `list_dealers` refuses CUSTOMER and OEM with a 422 rather than answering an
    // empty page - an empty page would read as "there are no such dealers",
    // which is a lie. Listing them would build that 422 into a menu.
    const { user, fetch } = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: { [`GET ${DEALERS_PATH}`]: page([dealer()]) },
    })

    await settled(fetch)
    await user.click(screen.getByRole('combobox', { name: 'Filter by channel' }))

    const options = await screen.findAllByRole('option')
    const labels = options.map((option) => option.textContent?.trim())
    expect(labels).toEqual(['Both channels', 'Dealer', 'Distributor'])
  })
})

// --------------------------------------------------------------- access ----

describe('who may see the channel', () => {
  it('opens for a holder of dealer.view', async () => {
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: { [`GET ${DEALERS_PATH}`]: page([dealer()]) },
    })

    expect(await screen.findByText('D-0007')).toBeVisible()
  })

  it('opens for a sales manager holding only customer.view', async () => {
    // The route enforces `require_any_permission(DEALER_VIEW, CUSTOMER_VIEW)`.
    // Demanding `dealer.view` alone here would hide a list the API would have
    // answered - the mirror has to match the route, not tighten it.
    renderScreen(<DealersPage />, {
      permissions: [P.CUSTOMER_VIEW],
      routes: { [`GET ${DEALERS_PATH}`]: page([dealer()]) },
    })

    expect(await screen.findByText('D-0007')).toBeVisible()
  })

  it('explains itself, and asks for nothing, when neither right is held', async () => {
    // The query is disabled rather than fired into a 403: an error toast about a
    // permission is not something the reader can act on, and the empty state
    // names the two rights an administrator would grant.
    const { fetch } = renderScreen(<DealersPage />, { permissions: [P.SALES_ORDER_VIEW] })

    expect(await screen.findByText('You cannot see the dealer list')).toBeVisible()
    expect(screen.getByText(/dealer.view or the customer.view permission/i)).toBeVisible()
    // The absence of a route for the list is itself the assertion: an unrouted
    // request throws rather than quietly answering with nothing.
    expect(fetch.calls.map((call) => call.path)).toEqual(['/auth/me'])
  })
})

// --------------------------------------------------------------- credit ----

describe('the credit position of an account', () => {
  it('calls a null limit unlimited rather than printing zero', async () => {
    // Null is unlimited; zero is a real ceiling of nothing. Collapsing the two
    // would refuse orders for the accounts trusted most.
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: { [`GET ${DEALERS_PATH}`]: page([dealer({ credit_limit: null })]) },
    })

    expect(await screen.findByText('Unlimited')).toBeVisible()
  })

  it('states the headroom left against a real ceiling', async () => {
    // Limit less outstanding - the same subtraction the API publishes as
    // `credit_headroom` on the detail read, which this projection does not
    // carry. Computed on Decimal strings, never a float.
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: {
        [`GET ${DEALERS_PATH}`]: page([
          dealer({ credit_limit: '5000.0000', outstanding_amount: '1000.0000' }),
        ]),
      },
    })

    expect(await screen.findByText(/4,000\.00 left/)).toBeVisible()
  })

  it('says by how much an account is over, in words rather than in a colour', async () => {
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: {
        [`GET ${DEALERS_PATH}`]: page([
          dealer({ credit_limit: '5000.0000', outstanding_amount: '6200.0000' }),
        ]),
      },
    })

    expect(await screen.findByText(/1,200\.00 over/)).toBeVisible()
  })

  it('treats an account exactly on its limit as still within it', async () => {
    // The API's `over_credit_limit` clause is strictly greater-than, and the row
    // must agree with the filter that selected it - otherwise a dealer sitting
    // on the limit is flagged here and absent from the "over limit" list.
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: {
        [`GET ${DEALERS_PATH}`]: page([
          dealer({ credit_limit: '5000.0000', outstanding_amount: '5000.0000' }),
        ]),
      },
    })

    expect(await screen.findByText(/0\.00 left/)).toBeVisible()
    expect(screen.queryByText(/over$/)).not.toBeInTheDocument()
  })

  it('offers no headroom figure at all for an unlimited account', async () => {
    // There is no arithmetic to do: "unlimited less what is owed" is not a
    // number, and printing one would be an invention.
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: {
        [`GET ${DEALERS_PATH}`]: page([
          dealer({ credit_limit: null, outstanding_amount: '9000.0000' }),
        ]),
      },
    })

    await screen.findByText('Unlimited')
    expect(screen.queryByText(/left$/)).not.toBeInTheDocument()
    expect(screen.queryByText(/over$/)).not.toBeInTheDocument()
  })
})

// -------------------------------------------------------------- filters ----

describe('every narrowing is a query parameter', () => {
  it('sends the channel, price list and standing the URL holds', async () => {
    // Master rules 31/36: thousands of accounts, one page in the browser. Only
    // the request distinguishes a server-side filter from a `useMemo`.
    const spy = listSpy()
    const { fetch } = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?customer_type=DISTRIBUTOR&price_list_type=DEALER&standing=over_limit',
      routes: { [`GET ${DEALERS_PATH}`]: spy.handler },
    })

    await settled(fetch)
    const params = spy.last()
    expect(params.get('customer_type')).toBe('DISTRIBUTOR')
    expect(params.get('price_list_type')).toBe('DEALER')
    // The API compares the stored outstanding against the stored ceiling in SQL,
    // which is the only way to get the right rows on page two and after.
    expect(params.get('over_credit_limit')).toBe('true')
  })

  it('asks for active accounts by default and drops the flag for "any status"', async () => {
    const activeOnly = listSpy()
    const first = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: { [`GET ${DEALERS_PATH}`]: activeOnly.handler },
    })
    await settled(first.fetch)
    expect(activeOnly.last().get('is_active')).toBe('true')
    first.unmount()

    const anyStatus = listSpy()
    const second = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?is_active=all',
      routes: { [`GET ${DEALERS_PATH}`]: anyStatus.handler },
    })
    await settled(second.fetch)
    // "Any status" is active plus inactive: retired accounts are excluded by the
    // API whichever is chosen, so this never widens to rows the grid could not
    // label.
    expect(anyStatus.last().get('is_active')).toBeNull()
  })

  it('sends a complete GST state code and withholds a half-typed one', async () => {
    // `_validate_state_code` rejects anything but two digits, so sending "2"
    // would replace a working list with a 422 while the user is still typing.
    const complete = listSpy()
    const first = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?state_code=27',
      routes: { [`GET ${DEALERS_PATH}`]: complete.handler },
    })
    await settled(first.fetch)
    expect(complete.last().get('state_code')).toBe('27')
    first.unmount()

    const partial = listSpy()
    const second = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?state_code=2',
      routes: { [`GET ${DEALERS_PATH}`]: partial.handler },
    })
    await settled(second.fetch)
    expect(partial.last().get('state_code')).toBeNull()
    // And the box says why it is being ignored rather than silently doing nothing.
    expect(screen.getByLabelText('GST state code')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Two digits')).toBeVisible()
  })

  it('resolves "assigned to me" against the signed-in user', async () => {
    // There is no user picker on this screen, so "mine" is the only sales-owner
    // narrowing available and it needs the session's own id to send.
    const spy = listSpy()
    const { fetch } = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?mine=true',
      routes: { [`GET ${DEALERS_PATH}`]: spy.handler },
    })

    await settled(fetch)
    expect(spy.last().get('assigned_sales_user_id')).toBe(SIGNED_IN_USER)
    expect(screen.getByRole('checkbox', { name: /assigned to me/i })).toBeChecked()
  })

  it('hides "assigned to me" when the session carries no user id', async () => {
    // Hidden rather than disabled: a checkbox that cannot name anyone would send
    // an empty filter and quietly return the whole channel.
    const spy = listSpy()
    const { fetch } = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?mine=true',
      user: { user_id: undefined },
      routes: { [`GET ${DEALERS_PATH}`]: spy.handler },
    })

    await settled(fetch)
    expect(screen.queryByRole('checkbox', { name: /assigned to me/i })).not.toBeInTheDocument()
    expect(spy.last().get('assigned_sales_user_id')).toBeNull()
  })

  it('clears every filter in one navigation, back to the active-only default', async () => {
    // `setSearchParams` builds the next URL from the params captured when the
    // callback was created, so six `setParam` calls in a row would all start
    // from the same stale base and only the last would survive.
    const spy = listSpy()
    const { user, fetch } = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route:
        '/dealers?customer_type=DEALER&price_list_type=SALE&standing=blocked&is_active=all' +
        '&state_code=27&mine=true',
      routes: { [`GET ${DEALERS_PATH}`]: spy.handler },
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /^clear/i }))

    await waitFor(() => expect(spy.last().get('customer_type')).toBeNull())
    const params = spy.last()
    for (const key of [
      'customer_type',
      'price_list_type',
      'is_blocked',
      'over_credit_limit',
      'state_code',
      'assigned_sales_user_id',
    ]) {
      expect(params.get(key), `${key} survived the clear`).toBeNull()
    }
    // Active-only is the screen's default, not a narrowing the user applied, so
    // Clear returns to it rather than to "any status".
    expect(params.get('is_active')).toBe('true')
  })

  it('counts the narrowings it is offering to clear', async () => {
    // The count reads from the same list Clear deletes, so a filter added to the
    // toolbar cannot end up counted but not cleared.
    const { fetch } = renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?customer_type=DEALER&standing=blocked&state_code=27',
      routes: { [`GET ${DEALERS_PATH}`]: listSpy().handler },
    })

    await settled(fetch)
    expect(screen.getByRole('button', { name: 'Clear 3' })).toBeVisible()
  })
})

// ------------------------------------------------------ loading and error ---

describe('when the list does not arrive', () => {
  it('surfaces the API sentence, with a way to retry', async () => {
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      routes: {
        [`GET ${DEALERS_PATH}`]: fail(503, 'SERVICE_UNAVAILABLE', 'The customer service is down'),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeVisible()
    expect(screen.getByText('The customer service is down')).toBeVisible()
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })

  it('says nothing matched, and points at the filters, when a narrowing empties it', async () => {
    renderScreen(<DealersPage />, {
      permissions: [P.DEALER_VIEW],
      route: '/dealers?customer_type=DISTRIBUTOR',
      routes: { [`GET ${DEALERS_PATH}`]: page([]) },
    })

    expect(await screen.findByText('No dealer accounts found')).toBeVisible()
    expect(screen.getByText(/Nothing matches these filters/i)).toBeVisible()
  })
})

import { screen, waitFor, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { SEARCH_DEBOUNCE_MS } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { cursorPage, fail, ok, page, renderScreen } from '@/test/harness'
import type { SchemaOf } from '@/types/api'

import CollectionsPage from './collections-page'

type WorklistRow = SchemaOf<'WorklistRow'>
type FollowUpDetail = SchemaOf<'FollowUpDetail'>

const CUSTOMER_ID = '01a00000-0000-7000-8000-0000000000c1'
const FOLLOW_UP_ID = '01a00000-0000-7000-8000-0000000000f1'
const WORKLIST_PATH = '/collections/worklist'

function worklistRow(overrides: Partial<WorklistRow> = {}): WorklistRow {
  return {
    customer_id: CUSTOMER_ID,
    customer_code: 'D-0007',
    customer_name: 'Northline Distributors',
    customer_type: 'DEALER',
    currency: 'INR',
    overdue_amount: '1250.0000',
    overdue_invoice_count: 2,
    oldest_overdue_days: 14,
    bucket_0_30_amount: '1250.0000',
    bucket_31_60_amount: '0',
    bucket_61_90_amount: '0',
    bucket_90_plus_amount: '0',
    ...overrides,
  }
}

function followUpDetail(): FollowUpDetail {
  return {
    id: FOLLOW_UP_ID,
    code: 'FU-0001',
    customer_id: CUSTOMER_ID,
    customer_code: 'D-0007',
    customer_name: 'Northline Distributors',
    currency: 'INR',
    status: 'OPEN',
    assigned_to_id: null,
    assigned_to_name: null,
    opened_overdue_amount: '1250.0000',
    current_overdue_amount: '1250.0000',
    oldest_due_date: '2026-11-08',
    next_action_date: null,
    promised_amount: null,
    promised_date: null,
    last_reminded_at: null,
    escalated_at: null,
    resolution_note: null,
    resolved_at: null,
    resolved_by_id: null,
    cancellation_reason: null,
    created_at: '2026-11-22T09:00:00Z',
    updated_at: '2026-11-22T09:00:00Z',
    version: 1,
    invoices: [],
  }
}

/**
 * The follow-up register's own filters.
 *
 * The register is a second list on the screen, so its page lives under its own
 * URL key (`followups_page`) beside the worklist's (`page`). Its status select
 * and its two checkboxes used to reset the worklist's page instead: narrowing
 * the register on page 2 kept asking for page 2 of a result that might have
 * only one, which the table drew as "No records" - and it moved the worklist
 * the planner was not looking at back to page 1.
 */
describe('the follow-up register', () => {
  const FOLLOW_UPS_PATH = '/collections/follow-ups'

  it.each<[string, (user: UserEvent) => Promise<void>, string, string]>([
    [
      'ticking Assigned to me',
      (user) => user.click(screen.getByRole('checkbox', { name: 'Assigned to me' })),
      'assigned_to_me',
      'true',
    ],
    [
      'clearing Live only',
      (user) => user.click(screen.getByRole('checkbox', { name: 'Live only' })),
      'live_only',
      'false',
    ],
    [
      'choosing a status',
      async (user) => {
        await user.click(screen.getByRole('combobox', { name: 'Register status' }))
        await user.click(await screen.findByRole('option', { name: 'Promised' }))
      },
      'status',
      'PROMISED',
    ],
  ])(
    'sends the register back to its first page on %s, and leaves the worklist where it was',
    async (_label, narrow, param, value) => {
      const worklistReads: URLSearchParams[] = []
      const registerReads: URLSearchParams[] = []
      const { user } = renderScreen(<CollectionsPage />, {
        permissions: [P.COLLECTION_VIEW],
        route: '/finance/collections?view=follow-ups&page=3&followups_page=2',
        routes: {
          [`GET ${WORKLIST_PATH}`]: ({ url }) => {
            worklistReads.push(url.searchParams)
            return page([worklistRow()])
          },
          [`GET ${FOLLOW_UPS_PATH}`]: ({ url }) => {
            registerReads.push(url.searchParams)
            return page([])
          },
        },
      })
      await waitFor(() => expect(registerReads.at(-1)?.get('page')).toBe('2'))
      await waitFor(() => expect(worklistReads.at(-1)?.get('page')).toBe('3'))
      const worklistReadCount = worklistReads.length

      await narrow(user)

      await waitFor(() => expect(registerReads.at(-1)?.get(param)).toBe(value))
      expect(registerReads.at(-1)?.get('page')).toBe('1')
      // Nothing asked the worklist to move, so it was not read again.
      expect(worklistReads).toHaveLength(worklistReadCount)
    },
  )
})

describe('collections worklist', () => {
  it('passes bookmarked filters to the server and renders the overdue position', async () => {
    let requestQuery: URLSearchParams | undefined
    const { fetch } = renderScreen(<CollectionsPage />, {
      permissions: [P.COLLECTION_VIEW],
      route:
        '/finance/collections?assigned_to_me=true&dealer_only=true&min_overdue_days=30&customer_id=' +
        CUSTOMER_ID,
      routes: {
        [`GET ${WORKLIST_PATH}`]: ({ url }) => {
          requestQuery = url.searchParams
          return page([worklistRow()])
        },
      },
    })

    expect(await screen.findAllByText(/Northline Distributors/)).not.toHaveLength(0)
    await waitFor(() => {
      expect(fetch.calls.some((call) => call.path === WORKLIST_PATH)).toBe(true)
    })
    const request = fetch.calls.find((call) => call.path === WORKLIST_PATH)
    expect(request).toBeDefined()

    expect(requestQuery?.get('customer_id')).toBe(CUSTOMER_ID)
    expect(requestQuery?.get('assigned_to_me')).toBe('true')
    expect(requestQuery?.get('dealer_only')).toBe('true')
    expect(requestQuery?.get('min_overdue_days')).toBe('30')
  })

  it('filters to my follow-ups from the shared checkbox, through the URL the worklist reads', async () => {
    const queries: URLSearchParams[] = []
    const { user } = renderScreen(<CollectionsPage />, {
      permissions: [P.COLLECTION_VIEW],
      route: '/finance/collections',
      routes: {
        [`GET ${WORKLIST_PATH}`]: ({ url }) => {
          queries.push(url.searchParams)
          return page([worklistRow()])
        },
      },
    })

    expect(await screen.findAllByText(/Northline Distributors/)).not.toHaveLength(0)
    // The phone filter panel and the desktop toolbar each draw one.
    const [mine] = screen.getAllByRole('checkbox', { name: 'My follow-ups' })
    expect(mine).not.toBeChecked()
    await user.click(mine!)

    await waitFor(() => expect(queries.at(-1)?.get('assigned_to_me')).toBe('true'))
    for (const box of screen.getAllByRole('checkbox', { name: 'My follow-ups' })) {
      expect(box).toBeChecked()
    }
    await user.click(screen.getAllByRole('checkbox', { name: 'My follow-ups' })[0]!)
    await waitFor(() => expect(queries.at(-1)?.get('assigned_to_me')).toBe('false'))
  })

  it('says why the worklist did not load and loads it on a retry', async () => {
    let reads = 0
    const { user } = renderScreen(<CollectionsPage />, {
      permissions: [P.COLLECTION_VIEW],
      route: '/finance/collections',
      routes: {
        [`GET ${WORKLIST_PATH}`]: () => {
          reads += 1
          return reads === 1
            ? fail(503, 'SERVICE_UNAVAILABLE', 'Receivables are being recalculated')
            : page([worklistRow()])
        },
      },
    })

    // The phone card list and the desktop table both render here; the card
    // list's failure is the LoadError, the table keeps its own empty-row retry.
    const alerts = await screen.findAllByRole('alert')
    const failure = alerts.find((alert) =>
      alert.textContent?.includes('Receivables are being recalculated'),
    )
    expect(failure).toBeDefined()
    await user.click(within(failure!).getByRole('button', { name: 'Try again' }))
    expect(await screen.findAllByText(/Northline Distributors/)).not.toHaveLength(0)
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('opens a follow-up from an overdue customer and loads its detail and keyset notes', async () => {
    const { user, fetch } = renderScreen(<CollectionsPage />, {
      permissions: [P.COLLECTION_VIEW, P.COLLECTION_MANAGE],
      routes: {
        [`GET ${WORKLIST_PATH}`]: page([worklistRow()]),
        'POST /collections/follow-ups': ok(followUpDetail()),
        [`GET /collections/follow-ups/${FOLLOW_UP_ID}`]: ok(followUpDetail()),
        [`GET /collections/follow-ups/${FOLLOW_UP_ID}/notes`]: cursorPage([]),
      },
    })

    const customerButtons = await screen.findAllByRole('button', {
      name: /D-0007.*Northline Distributors/,
    })
    await user.click(customerButtons[0]!)

    expect(
      await screen.findByRole('heading', { name: /FU-0001.*Northline Distributors/ }),
    ).toBeVisible()
    expect(fetch.requestFor('POST', '/collections/follow-ups')).toEqual({
      customer_id: CUSTOMER_ID,
      currency: 'INR',
    })
    expect(
      fetch.calls.some((call) => call.path === `/collections/follow-ups/${FOLLOW_UP_ID}/notes`),
    ).toBe(true)
  })

  it('announces a refused follow-up once, with a headline and the reason', async () => {
    const { user } = renderScreen(
      <>
        <CollectionsPage />
        <Toaster />
      </>,
      {
        permissions: [P.COLLECTION_VIEW, P.COLLECTION_MANAGE],
        errorNets: true,
        routes: {
          [`GET ${WORKLIST_PATH}`]: page([worklistRow()]),
          'POST /collections/follow-ups': fail(
            409,
            'CONFLICT',
            'An open follow-up already covers this customer.',
          ),
        },
      },
    )

    const region = screen.getByRole('region', { name: 'Notifications' })
    const before = within(region).queryAllByRole('alert').length
    const customerButtons = await screen.findAllByRole('button', {
      name: /D-0007.*Northline Distributors/,
    })
    await user.click(customerButtons[0]!)

    const title = await within(region).findByText('Could not open a payment follow-up')
    expect(title.closest('[role="alert"]')).toHaveTextContent(
      'An open follow-up already covers this customer.',
    )
    // The control is usable again only once both of TanStack's onError hooks ran.
    await waitFor(() => expect(customerButtons[0]).toBeEnabled())
    expect(within(region).getAllByRole('alert')).toHaveLength(before + 1)
  })

  it('sends the contact note and payment promise payloads from the drawer', async () => {
    const { user, fetch } = renderScreen(<CollectionsPage />, {
      permissions: [P.COLLECTION_VIEW, P.COLLECTION_MANAGE],
      routes: {
        [`GET ${WORKLIST_PATH}`]: page([worklistRow({ follow_up_id: FOLLOW_UP_ID })]),
        [`GET /collections/follow-ups/${FOLLOW_UP_ID}`]: ok(followUpDetail()),
        [`GET /collections/follow-ups/${FOLLOW_UP_ID}/notes`]: cursorPage([]),
        [`POST /collections/follow-ups/${FOLLOW_UP_ID}/notes`]: ok({}),
        [`POST /collections/follow-ups/${FOLLOW_UP_ID}/promise`]: ok(followUpDetail()),
      },
    })

    await user.click(await screen.findByRole('button', { name: /D-0007.*Northline Distributors/ }))
    await screen.findByRole('heading', { name: /FU-0001.*Northline Distributors/ })

    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('Record a payment promise')
    await user.type(within(dialog).getByLabelText(/Promised amount/), '500')
    const promisedDate = (within(dialog).getByLabelText(/Promised date/) as HTMLInputElement).value
    await user.click(within(dialog).getByRole('button', { name: 'Record promise' }))
    await waitFor(() => {
      expect(fetch.requestFor('POST', `/collections/follow-ups/${FOLLOW_UP_ID}/promise`)).toEqual({
        promised_amount: '500',
        promised_date: promisedDate,
      })
    })

    await user.type(
      within(dialog).getByLabelText(/Contact notes/),
      'Called about the overdue invoices.',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Record attempt' }))
    await waitFor(() => {
      expect(fetch.requestFor('POST', `/collections/follow-ups/${FOLLOW_UP_ID}/notes`)).toEqual({
        channel: 'CALL',
        outcome: null,
        note: 'Called about the overdue invoices.',
        next_action_date: null,
      })
    })
  })
})

/**
 * The worklist's search, which two boxes write.
 *
 * The phone card list draws a search box of its own beside the table's, and
 * both stay mounted at every width - CSS hides one. The phone box used to write
 * its settled value to the URL whenever the URL disagreed with it, and the URL
 * disagrees with it every time the table writes a search the phone box has not
 * caught up with: the table's "north" was replaced by the phone box's "" in the
 * same commit, so a desktop search emptied itself - and a bookmarked search came
 * back as soon as the table cleared it.
 */
describe('searching the worklist', () => {
  const SEARCH = 'Search customer code or name'

  /** Long enough for every debounce on the screen to settle, so a revert has happened. */
  const debouncesSettle = () =>
    new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS * 3))

  function renderWorklist(route = '/finance/collections') {
    const reads: URLSearchParams[] = []
    const screenResult = renderScreen(<CollectionsPage />, {
      permissions: [P.COLLECTION_VIEW],
      route,
      routes: {
        [`GET ${WORKLIST_PATH}`]: ({ url }) => {
          reads.push(url.searchParams)
          return page([worklistRow()])
        },
      },
    })
    return { ...screenResult, reads }
  }

  it('keeps a search typed in the table, and the phone box follows it', async () => {
    const { user, reads } = renderWorklist()

    // The table's box is a plain textbox; the phone's is a searchbox.
    await user.type(await screen.findByRole('textbox', { name: SEARCH }), 'north')
    await waitFor(() => expect(reads.some((read) => read.get('search') === 'north')).toBe(true))
    await debouncesSettle()

    expect(reads.at(-1)?.get('search')).toBe('north')
    expect(screen.getByRole('textbox', { name: SEARCH })).toHaveValue('north')
    expect(screen.getByRole('searchbox', { name: SEARCH })).toHaveValue('north')
  })

  it('clears a bookmarked search from the table, and it stays cleared', async () => {
    const { user, reads } = renderWorklist('/finance/collections?q=north')

    const table = await screen.findByRole('textbox', { name: SEARCH })
    await waitFor(() => expect(reads.at(-1)?.get('search')).toBe('north'))
    expect(table).toHaveValue('north')
    await user.clear(table)
    await waitFor(() => expect(reads.at(-1)?.has('search')).toBe(false))
    await debouncesSettle()

    expect(reads.at(-1)?.has('search')).toBe(false)
    expect(screen.getByRole('textbox', { name: SEARCH })).toHaveValue('')
    expect(screen.getByRole('searchbox', { name: SEARCH })).toHaveValue('')
  })

  it('searches from the phone box through the URL the table reads', async () => {
    const { user, reads } = renderWorklist()

    await user.type(await screen.findByRole('searchbox', { name: SEARCH }), 'north')
    await waitFor(() => expect(reads.at(-1)?.get('search')).toBe('north'))
    await debouncesSettle()

    expect(reads.at(-1)?.get('search')).toBe('north')
    expect(screen.getByRole('textbox', { name: SEARCH })).toHaveValue('north')
  })
})

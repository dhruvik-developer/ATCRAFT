import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
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

/**
 * The customer picker's search.
 *
 * Every settled term is a trigram scan on the API. A term the next keystroke
 * replaced is a question nobody is waiting for, so the picker hands TanStack's
 * abort signal to the request and the superseded one is cancelled rather than
 * run to completion beside its successor.
 */

import { screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { page, renderScreen, type RouteBody } from '@/test/harness'

import { CustomerPicker } from './customer-picker'

afterEach(() => {
  vi.unstubAllGlobals()
})

/** The signal each search term's request carried, keyed by the term. */
function recordSignals(): Map<string, AbortSignal | null> {
  const signals = new Map<string, AbortSignal | null>()
  const answer = globalThis.fetch
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    signals.set(url.searchParams.get('search') ?? '', init?.signal ?? null)
    return answer(input, init)
  })
  return signals
}

describe('the customer picker search', () => {
  it('cancels the search a later term superseded', async () => {
    const { user } = renderScreen(
      <CustomerPicker id="customer" label="Customer" value={null} onChange={() => {}} />,
      {
        routes: {
          // The first refined term never answers, so it is still in flight when
          // the next one settles - the case cancellation exists for.
          'GET /customers': ({ url }) =>
            url.searchParams.get('search') === 'BHA' ? new Promise<RouteBody>(() => {}) : page([]),
        },
      },
    )
    const signals = recordSignals()

    await user.click(screen.getByRole('combobox', { name: 'Customer' }))
    const search = await screen.findByRole('textbox', { name: 'Search customers' })
    await user.type(search, 'BHA')
    await waitFor(() => expect(signals.has('BHA')).toBe(true))
    await user.type(search, 'RAT')
    await waitFor(() => expect(signals.has('BHARAT')).toBe(true))

    expect(signals.get('BHA')?.aborted, 'the superseded search was left running').toBe(true)
    expect(signals.get('BHARAT')?.aborted).toBe(false)
  })

  it('cancels a search still in flight when the form holding it goes away', async () => {
    // A dialog's body unmounts when it closes, and the picker with it: nothing
    // is left to read the answer, so the request is abandoned with it.
    const { user, unmount } = renderScreen(
      <CustomerPicker id="customer" label="Customer" value={null} onChange={() => {}} />,
      { routes: { 'GET /customers': () => new Promise<RouteBody>(() => {}) } },
    )
    const signals = recordSignals()

    await user.click(screen.getByRole('combobox', { name: 'Customer' }))
    await waitFor(() => expect(signals.has('')).toBe(true))
    expect(signals.get('')?.aborted).toBe(false)
    unmount()

    expect(signals.get('')?.aborted).toBe(true)
  })
})

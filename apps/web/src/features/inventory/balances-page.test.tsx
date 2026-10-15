/**
 * Stock balances - the screen the business opens most.
 *
 * Two properties are worth a test here and the rest of the grid is decoration.
 *
 * **1. Availability is never served from a cache.** Master rules 34, and the
 * only rule on this screen with a number attached to it: a planner who promises
 * 100 pieces against a five-minute-old balance creates a shortage nobody
 * discovers until picking. The app's own query client defaults to a five-minute
 * `staleTime`, so this query has to say `staleTimes.live` explicitly - deleting
 * that one line is a silent regression that renders identically.
 *
 * **2. Valuation is a different right from quantity.** A store keeper counts
 * stock; a controller values it. `P.INVENTORY_VALUATION_VIEW` is the gate, and
 * a column gated on the wrong right is the defect class that has shipped six
 * times in this codebase.
 *
 * The sort assertion looks pedantic until you know the history: the API accepts
 * exactly `BALANCE_SORT_FIELDS`, and naming `item_code` as the default sort made
 * the screen's very first request a 422, so the grid never painted at all.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { page, fail, renderScreen } from '@/test/harness'

import InventoryBalancesPage from './balances-page'
import type { InventoryBalanceRow } from './types'

function balance(overrides: Partial<InventoryBalanceRow> = {}): InventoryBalanceRow {
  return {
    id: 'ba000000-0000-7000-8000-000000000001',
    item_id: 'it000000-0000-7000-8000-000000000001',
    item_code: 'BRASS-ROD-12',
    item_name: 'Brass rod 12mm',
    warehouse_id: 'wh000000-0000-7000-8000-000000000001',
    warehouse_code: 'MAIN',
    location_id: null,
    location_code: null,
    batch_id: null,
    batch_number: null,
    on_hand_quantity: '120.0000',
    reserved_quantity: '20.0000',
    allocated_quantity: '0.0000',
    in_transit_quantity: '0.0000',
    qc_hold_quantity: '0.0000',
    damaged_quantity: '0.0000',
    job_work_quantity: '0.0000',
    scrap_quantity: '0.0000',
    available_quantity: '100.0000',
    average_cost: '45.5000',
    stock_value: '5460.00',
    reorder_level: '50.0000',
    last_movement_at: '2026-08-30T10:00:00Z',
    updated_at: '2026-08-30T10:00:00Z',
    ...overrides,
  }
}

/** The warehouse filter loads on mount whatever else the test is about. */
const WAREHOUSES = page([
  { id: 'wh000000-0000-7000-8000-000000000001', code: 'MAIN', name: 'Main store' },
])

describe('the balance grid', () => {
  it('shows on-hand, reserved and available side by side', async () => {
    // The three quantities are the whole point of the screen: stock that is
    // here, stock that is spoken for, and what is left to promise. A row that
    // renders on-hand alone reads as 120 free pieces when 20 are already sold.
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': page([balance()]),
        'GET /warehouses': WAREHOUSES,
      },
    })

    const row = (await screen.findByRole('link', { name: 'BRASS-ROD-12' })).closest('tr')
    expect(row).not.toBeNull()
    const cells = within(row!).getAllByRole('cell')
    expect(cells.map((cell) => cell.textContent)).toEqual(
      expect.arrayContaining(['120', '20', '100']),
    )
  })

  it('marks a position with nothing free in a way a screen reader reaches', async () => {
    // The colour bar down the first cell is the only visual signal that a
    // position is exhausted, and a coloured border has no accessible name at
    // all - so the row carries the sentence as well.
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': page([
          balance({ available_quantity: '0.0000', on_hand_quantity: '20.0000' }),
        ]),
        'GET /warehouses': WAREHOUSES,
      },
    })

    expect(
      await screen.findByText(/nothing available to promise/i, { selector: 'span' }),
    ).toBeInTheDocument()
  })

  it('does not ask the API to sort by a column it rejects', async () => {
    // `BalanceRepository.sortable_fields` is five quantitative columns. Naming
    // any other one - `item_code` was the one that shipped - is rejected
    // outright, and the screen opens on a 422 with an empty grid.
    const seen: URL[] = []
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': ({ url }) => {
          seen.push(url)
          return page([balance()])
        },
        'GET /warehouses': WAREHOUSES,
      },
    })

    await screen.findByRole('link', { name: 'BRASS-ROD-12' })
    expect(seen).toHaveLength(1)
    expect(seen[0]!.searchParams.get('sort_by')).toBeNull()
  })
})

describe('valuation is a separate right from quantity', () => {
  it('hides the stock value column from someone who may only see quantities', async () => {
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': page([balance()]),
        'GET /warehouses': WAREHOUSES,
      },
    })

    await screen.findByRole('link', { name: 'BRASS-ROD-12' })
    expect(screen.queryByRole('columnheader', { name: /stock value/i })).not.toBeInTheDocument()
    // Not just the header: the figure itself must not appear anywhere.
    expect(screen.queryByText(/5,460/)).not.toBeInTheDocument()
  })

  it('shows it to someone holding INVENTORY_VALUATION_VIEW', async () => {
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_VALUATION_VIEW],
      routes: {
        'GET /inventory/balances': page([balance()]),
        'GET /warehouses': WAREHOUSES,
      },
    })

    expect(await screen.findByRole('columnheader', { name: /stock value/i })).toBeInTheDocument()
    expect(screen.getByText(/5,460\.00/)).toBeInTheDocument()
  })
})

/**
 * The live options the screen's query is actually running with, read after the
 * client's defaults have been replaced.
 *
 * `sentinelGcTime` is the honesty check. The whole technique rests on a
 * re-render re-reading the client defaults, and if that ever stopped happening
 * every assertion built on it would pass while measuring the *original*
 * defaults - which are the ones the harness chose, not the ones the screen
 * asked for. Asserting the sentinel arrived proves the swap took effect before
 * anything is concluded from what did not change.
 */
function observedOptions(
  queryClient: ReturnType<typeof renderScreen>['queryClient'],
  queryKey: readonly unknown[],
): { staleTime?: number; gcTime?: number; refetchOnWindowFocus?: unknown } {
  const queries = queryClient.getQueryCache().findAll({ queryKey })
  expect(queries, `expected exactly one query under ${JSON.stringify(queryKey)}`).toHaveLength(1)
  const { observers } = queries[0] as unknown as {
    observers: {
      options: { staleTime?: number; gcTime?: number; refetchOnWindowFocus?: unknown }
    }[]
  }
  expect(observers.length, 'expected the screen to be observing its own query').toBeGreaterThan(0)
  return observers[0]!.options
}

const SENTINEL_GC_TIME = 987_654

describe('availability is never cached', () => {
  it('asks for live data rather than inheriting the client default', async () => {
    // Read this one carefully, because a naive version of it passes vacuously.
    // The harness's query client defaults to `staleTime: 0`, so a screen that
    // asked for nothing at all would look identical here - while in the real app
    // it would inherit `staleTimes.master`, five minutes, and hand a planner a
    // balance from before lunch. Making the client default long *first* is what
    // turns this into an assertion about the screen: only an explicit
    // `staleTimes.live` survives it.
    const { queryClient, user } = renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': page([balance()]),
        'GET /warehouses': WAREHOUSES,
      },
    })
    await screen.findByRole('link', { name: 'BRASS-ROD-12' })

    queryClient.setDefaultOptions({
      queries: { staleTime: 5 * 60 * 1000, retry: false, gcTime: SENTINEL_GC_TIME },
    })
    // Any re-render re-runs the hook, which is what re-reads the defaults.
    await user.click(screen.getByRole('button', { name: /refresh/i }))

    const options = observedOptions(queryClient, ['inventory', 'balances'])
    expect(options.gcTime, 'the new defaults never reached the hook').toBe(SENTINEL_GC_TIME)
    // The screen overrode the five-minute default with zero. Delete the
    // `staleTime: staleTimes.live` line and this reads 300000.
    expect(options.staleTime).toBe(0)
  })

  it('refetches when a tab left open all morning is focused again', async () => {
    // The app turns `refetchOnWindowFocus` off globally because refetching every
    // list on focus is wasteful; this screen opts back in, and that opt-in is
    // the difference between a returning user seeing this morning's stock and
    // today's.
    const { queryClient, user } = renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': page([balance()]),
        'GET /warehouses': WAREHOUSES,
      },
    })
    await screen.findByRole('link', { name: 'BRASS-ROD-12' })

    queryClient.setDefaultOptions({
      queries: {
        refetchOnWindowFocus: false,
        staleTime: 0,
        retry: false,
        gcTime: SENTINEL_GC_TIME,
      },
    })
    await user.click(screen.getByRole('button', { name: /refresh/i }))

    const options = observedOptions(queryClient, ['inventory', 'balances'])
    expect(options.gcTime, 'the new defaults never reached the hook').toBe(SENTINEL_GC_TIME)
    expect(options.refetchOnWindowFocus).toBe(true)
  })
})

describe('filters go to the server', () => {
  it('sends below_reorder and returns to page 1', async () => {
    // Filtering in the browser would be filtering one page of a table with
    // millions of rows, and staying on page 3 while the result set shrinks to
    // two pages is how a filtered grid comes back empty.
    const seen: URL[] = []
    const { user } = renderScreen(<InventoryBalancesPage />, {
      route: '/inventory/balances?page=3',
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': ({ url }) => {
          seen.push(url)
          return page([balance()])
        },
        'GET /warehouses': WAREHOUSES,
      },
    })
    await screen.findByRole('link', { name: 'BRASS-ROD-12' })

    await user.click(screen.getByRole('checkbox', { name: /below reorder level/i }))

    await expect.poll(() => seen[seen.length - 1]!.searchParams.get('below_reorder')).toBe('true')
    expect(seen[seen.length - 1]!.searchParams.get('page')).toBe('1')
  })
})

describe('states a person can act on', () => {
  it('says what to do when no position matches', async () => {
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': page([]),
        'GET /warehouses': WAREHOUSES,
      },
    })

    expect(await screen.findByText('No stock matches these filters')).toBeInTheDocument()
    expect(screen.getByText(/change the warehouse or clear the toggles/i)).toBeInTheDocument()
  })

  it('tells the user when the API refuses the list rather than showing an empty grid', async () => {
    // An empty grid and a refused request look identical to an operator, and one
    // of them means "there is no stock" - which on this screen is a decision
    // somebody acts on.
    renderScreen(<InventoryBalancesPage />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/balances': fail(
          403,
          'PERMISSION_DENIED',
          'You may not view stock balances.',
        ),
        'GET /warehouses': WAREHOUSES,
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeInTheDocument()
    expect(screen.getByText('You may not view stock balances.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})

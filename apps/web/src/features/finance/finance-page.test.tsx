/**
 * The finance hub, and which of its tabs an account is offered.
 *
 * `/finance` opens on `finance.view`, but the party statement behind the
 * Ledger tab is served by `GET /finance/ledger/{customer|vendor}/{id}`, which
 * the API declares on `ledger.view`. A tab offered on the page's own right and
 * refused by its endpoint fails in the worst shape: the reader picks a party,
 * the request fires, and a "not permitted" toast answers every choice. So the
 * tab follows the endpoint's right, exactly as the valuation and GST tabs
 * already do - and a bookmarked `?tab=ledger` falls back rather than rendering
 * an empty shell.
 */

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { page, renderScreen } from '@/test/harness'

import FinancePage from './finance-page'

const routes = { 'GET /finance/outstanding': page([]) }

describe('the Ledger tab follows the right its endpoint enforces', () => {
  it('withholds the tab from an account that holds finance.view alone', async () => {
    renderScreen(<FinancePage />, { permissions: [P.FINANCE_VIEW], routes })

    expect(await screen.findByRole('tab', { name: 'Outstanding' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Ageing' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Ledger' })).not.toBeInTheDocument()
  })

  it('offers the tab to an account that may read the ledger', async () => {
    renderScreen(<FinancePage />, {
      permissions: [P.FINANCE_VIEW, P.LEDGER_VIEW],
      route: '/finance?tab=ledger',
      routes,
    })

    expect(await screen.findByRole('tab', { name: 'Ledger' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    // The tab's own body - a statement is read one party at a time.
    expect(await screen.findByText(/A ledger is read one party at a time/i)).toBeInTheDocument()
  })

  it('falls back from a bookmarked ledger tab this account cannot open', async () => {
    const { fetch } = renderScreen(<FinancePage />, {
      permissions: [P.FINANCE_VIEW],
      route: '/finance?tab=ledger',
      routes,
    })

    expect(await screen.findByRole('tab', { name: 'Outstanding' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.queryByRole('tab', { name: 'Ledger' })).not.toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path.startsWith('/finance/ledger'))).toBe(false)
  })
})

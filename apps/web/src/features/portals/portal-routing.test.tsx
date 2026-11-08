import { screen, waitFor, within } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'
import { useLocation } from 'react-router-dom'

import { AppRouter } from '@/app/router'
import { P } from '@/lib/permissions'
import { ok, renderScreen } from '@/test/harness'

import PortalLayout from './portal-layout'

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>
}

/** Every subset of `values`, so a test covers whatever rights a role may hold. */
function subsets<T>(values: readonly T[]): T[][] {
  return values.reduce<T[][]>((all, value) => [...all, ...all.map((set) => [...set, value])], [[]])
}

const VENDOR_RIGHTS = [
  P.VENDOR_PORTAL_RESPOND,
  P.VENDOR_PORTAL_JOB_WORK,
  P.VENDOR_PORTAL_SHIP,
  P.VENDOR_PORTAL_INVOICE,
  P.VENDOR_PORTAL_QUALITY,
]
const DEALER_RIGHTS = [
  P.DEALER_PORTAL_BROWSE,
  P.DEALER_PORTAL_ORDER,
  P.DEALER_PORTAL_ACCOUNT,
  P.DEALER_PORTAL_RETURN,
]

/** The fixed phone bar is one row of this many cells; one more wraps it. */
const PHONE_BAR_CELLS = 5

const COMPANY = 'Brasswell Hardware'

async function renderPortal(portal: 'vendor' | 'dealer', permissions: string[]) {
  const binding = portal === 'vendor' ? { vendor_id: 'vendor' } : { customer_id: 'dealer' }
  const rendered = renderScreen(<PortalLayout />, {
    permissions,
    user: { ...binding, company_name: COMPANY },
    routes: {
      'GET /vendor-portal/me': ok({ name: 'Acme Metals' }),
      'GET /dealer-portal/me': ok({ customer_name: 'Meridian Interiors' }),
    },
  })
  // Until the session arrives the layout draws an empty dealer bar, which would
  // pass any count - so wait for the company name only the session carries.
  await screen.findByText(COMPANY)
  expect(screen.getByText(portal === 'vendor' ? /Vendor portal/ : /Dealer portal/)).toBeVisible()
  return rendered
}

function phoneBar(): HTMLElement {
  return screen.getByRole('navigation', { name: 'Mobile portal navigation' })
}

beforeAll(async () => {
  await import('./portal-layout')
})

describe('portal session boundaries', () => {
  it.each([
    ['vendor_id', '/vendor', '/sales-orders'],
    ['customer_id', '/dealer', '/procurement/purchase-orders'],
    ['vendor_id', '/vendor', '/dealer/catalog'],
    ['customer_id', '/dealer', '/vendor/orders'],
  ])('confines %s sessions to %s', async (binding, destination, route) => {
    const result = renderScreen(
      <>
        <AppRouter />
        <LocationProbe />
      </>,
      {
        route,
        user: { [binding]: '01a00000-0000-7000-8000-000000000099' },
      },
    )
    await waitFor(() =>
      expect(screen.getByLabelText('Current route')).toHaveTextContent(destination),
    )
    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this page' }),
    ).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(result.fetch.calls.filter((call) => !call.path.startsWith('/auth/'))).toEqual([])
  })

  it.each(['/vendor/orders', '/dealer/cart'])('keeps staff out of %s', async (route) => {
    renderScreen(
      <>
        <AppRouter />
        <LocationProbe />
      </>,
      { route },
    )
    await waitFor(() => expect(screen.getByLabelText('Current route')).toHaveTextContent(/^\/$/))
    expect(screen.queryByRole('navigation', { name: 'Portal navigation' })).not.toBeInTheDocument()
  })

  it('keeps forced password changes outside every portal shell', async () => {
    renderScreen(
      <>
        <AppRouter />
        <LocationProbe />
      </>,
      {
        route: '/vendor/orders',
        user: { vendor_id: 'vendor', must_change_password: true },
      },
    )
    await waitFor(() =>
      expect(screen.getByLabelText('Current route')).toHaveTextContent('/change-password'),
    )
    expect(screen.queryByRole('navigation', { name: 'Portal navigation' })).not.toBeInTheDocument()
  })

  it('keeps shared notifications in the portal shell and gates its data', async () => {
    renderScreen(
      <>
        <AppRouter />
        <LocationProbe />
      </>,
      {
        route: '/notifications',
        user: { vendor_id: 'vendor' },
      },
    )
    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this page' }),
    ).toBeVisible()
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/notifications')
    expect(screen.getByRole('navigation', { name: 'Portal navigation' })).toBeVisible()
  })

  it('hides invoice navigation for a vendor without invoice rights and exposes More', async () => {
    const result = renderScreen(<AppRouter />, {
      route: '/vendor/invoices',
      user: { vendor_id: 'vendor', roles: ['VENDOR_DISPATCHER'] },
    })
    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this page' }),
    ).toBeVisible()
    const nav = screen.getByRole('navigation', { name: 'Mobile portal navigation' })
    expect(within(nav).queryByRole('link', { name: 'Invoices' })).not.toBeInTheDocument()
    await result.user.click(within(nav).getByRole('button', { name: 'More' }))
    expect(screen.getByRole('menuitem', { name: 'Profile' })).toBeVisible()
    expect(screen.queryByRole('menuitem', { name: 'Payments' })).not.toBeInTheDocument()
    expect(result.fetch.calls.some((call) => call.path.startsWith('/vendor-portal/invoices'))).toBe(
      false,
    )
  })

  it('keeps job work under More for a vendor owner, rather than in a sixth bar cell', async () => {
    // VENDOR_OWNER holds both job work and invoices. Job work is one of More's
    // pages, so the bar does not spend a cell on it as well.
    const { user } = await renderPortal('vendor', [
      P.VENDOR_PORTAL_ACCESS,
      P.VENDOR_PORTAL_JOB_WORK,
      P.VENDOR_PORTAL_INVOICE,
    ])

    const bar = phoneBar()
    expect(
      within(bar)
        .getAllByRole('link')
        .map((link) => link.textContent),
    ).toEqual(['Home', 'Orders', 'Shipments', 'Invoices'])
    // The desktop list keeps every page, each once.
    const aside = screen.getByRole('navigation', { name: 'Portal navigation' })
    expect(within(aside).getAllByRole('link', { name: 'Job work' })).toHaveLength(1)

    await user.click(within(bar).getByRole('button', { name: 'More' }))
    expect(screen.getAllByRole('menuitem', { name: 'Job work' })).toHaveLength(1)
  })

  it.each(subsets(VENDOR_RIGHTS).map((rights) => [rights]))(
    'fits the vendor phone bar on one row for a vendor holding %j',
    async (rights) => {
      await renderPortal('vendor', [P.VENDOR_PORTAL_ACCESS, ...rights])
      const bar = phoneBar()
      const cells = within(bar).queryAllByRole('link').length
      expect(cells + within(bar).getAllByRole('button').length).toBeLessThanOrEqual(PHONE_BAR_CELLS)
    },
  )

  it.each(subsets(DEALER_RIGHTS).map((rights) => [rights]))(
    'fits the dealer phone bar on one row for a dealer holding %j',
    async (rights) => {
      await renderPortal('dealer', rights)
      const bar = phoneBar()
      const cells = within(bar).queryAllByRole('link').length
      expect(cells + within(bar).queryAllByRole('button').length).toBeLessThanOrEqual(
        PHONE_BAR_CELLS,
      )
    },
  )

  it('uses the exact endpoint permission on dealer invoices', async () => {
    renderScreen(<AppRouter />, {
      route: '/dealer/invoices',
      permissions: [P.DEALER_PORTAL_ORDER],
      user: { customer_id: 'dealer' },
    })
    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this page' }),
    ).toBeVisible()
  })
})

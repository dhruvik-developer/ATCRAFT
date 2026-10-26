import { screen, waitFor, within } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'
import { useLocation } from 'react-router-dom'

import { AppRouter } from '@/app/router'
import { P } from '@/lib/permissions'
import { renderScreen } from '@/test/harness'

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>
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

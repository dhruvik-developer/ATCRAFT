import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import CourierIntegrationPage from './courier-integration-page'

const ACCOUNT = {
  id: 'courier-1',
  code: 'SHIP-01',
  provider: 'SHIPROCKET' as const,
  display_name: 'Shiprocket',
  is_active: true,
  auto_confirm_delivery: true,
  has_credentials: true,
  has_webhook: false,
  last_poll_at: null,
  last_error: null,
}

describe('courier integration', () => {
  it('lets settings viewers inspect accounts without write controls', async () => {
    renderScreen(
      <>
        <CourierIntegrationPage />
        <Toaster />
      </>,
      {
        permissions: [P.SETTINGS_VIEW],
        route: '/admin/courier-integration',
        routes: { 'GET /integrations/courier/accounts': page([ACCOUNT]) },
      },
    )

    expect(await screen.findByText('Shiprocket')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Create account' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Test connection' })).toBeNull()
    expect(screen.getByText(/You have read-only access/)).toBeVisible()
  })

  it('sends credentials only on create and never echoes them in the account response', async () => {
    const { user, fetch } = renderScreen(
      <>
        <CourierIntegrationPage />
        <Toaster />
      </>,
      {
        permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
        route: '/admin/courier-integration',
        routes: {
          'GET /integrations/courier/accounts': page([]),
          'POST /integrations/courier/accounts': ok(ACCOUNT),
        },
      },
    )

    await user.type(await screen.findByLabelText('Account code'), 'SHIP-01')
    await user.selectOptions(screen.getByLabelText('Provider'), 'SHIPROCKET')
    await user.type(screen.getByLabelText('Display name'), 'Shiprocket')
    await user.type(screen.getByLabelText('Shiprocket email'), 'admin@example.com')
    await user.type(screen.getByLabelText('Shiprocket password'), 'correct-horse-battery')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    await waitFor(() => {
      expect(
        fetch.calls.filter(
          (call) => call.method === 'POST' && call.path === '/integrations/courier/accounts',
        ),
      ).toHaveLength(1)
    })
    const createCall = fetch.calls.find(
      (call) => call.method === 'POST' && call.path === '/integrations/courier/accounts',
    )
    expect(createCall?.body).toMatchObject({
      code: 'SHIP-01',
      credentials: { email: 'admin@example.com', password: 'correct-horse-battery' },
    })
    expect(screen.getByLabelText('Shiprocket password')).toHaveValue('')
    expect(screen.queryByText('correct-horse-battery')).toBeNull()
  })

  it('paginates courier accounts through the server list contract', async () => {
    const requestedPages: string[] = []
    const { user } = renderScreen(
      <>
        <CourierIntegrationPage />
        <Toaster />
      </>,
      {
        permissions: [P.SETTINGS_VIEW],
        route: '/admin/courier-integration',
        routes: {
          'GET /integrations/courier/accounts': ({ url }) => {
            const requestedPage = url.searchParams.get('page') ?? '1'
            requestedPages.push(requestedPage)
            return page(requestedPage === '2' ? [] : [ACCOUNT], {
              page: Number(requestedPage),
              total: 51,
              total_pages: 2,
              has_next: requestedPage !== '2',
              has_previous: requestedPage === '2',
            })
          },
        },
      },
    )

    expect(await screen.findByText('Page 1 of 2')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => {
      expect(requestedPages).toContain('2')
    })
    expect(await screen.findByText('Page 2 of 2')).toBeVisible()
  })
})

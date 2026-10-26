import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ok, renderScreen } from '@/test/harness'

import NotificationsPage from './notifications-page'

describe('notification preferences', () => {
  it('saves only changed rows and includes the push channel', async () => {
    const { user, fetch } = renderScreen(<NotificationsPage />, {
      route: '/notifications?tab=preferences',
      routes: {
        'GET /notifications/push/config': ok({ enabled: false, vapid_public_key: null }),
        'GET /notifications/preferences': ok([
          {
            notification_type: 'STOCK_BELOW_REORDER',
            in_app_enabled: true,
            email_enabled: false,
            push_enabled: false,
          },
          {
            notification_type: 'STOCK_SHORTAGE',
            in_app_enabled: true,
            email_enabled: false,
            push_enabled: false,
          },
        ]),
        'PUT /notifications/preferences': ok([]),
      },
    })
    await user.click(await screen.findByRole('checkbox', { name: 'Stock Below Reorder by push' }))
    await user.click(screen.getByRole('button', { name: /Save/ }))
    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/notifications/preferences')).toEqual({
        preferences: [
          {
            notification_type: 'STOCK_BELOW_REORDER',
            in_app_enabled: true,
            email_enabled: false,
            push_enabled: true,
          },
        ],
      }),
    )
  })

  it('restoring a checkbox removes the change instead of sending an untouched row', async () => {
    const { user } = renderScreen(<NotificationsPage />, {
      route: '/notifications?tab=preferences',
      routes: {
        'GET /notifications/push/config': ok({ enabled: false, vapid_public_key: null }),
        'GET /notifications/preferences': ok([
          {
            notification_type: 'STOCK_BELOW_REORDER',
            in_app_enabled: true,
            email_enabled: false,
            push_enabled: false,
          },
        ]),
      },
    })
    const checkbox = await screen.findByRole('checkbox', { name: 'Stock Below Reorder by push' })
    await user.click(checkbox)
    await user.click(checkbox)
    expect(screen.getByRole('button', { name: /Save/ })).toBeDisabled()
  })
})

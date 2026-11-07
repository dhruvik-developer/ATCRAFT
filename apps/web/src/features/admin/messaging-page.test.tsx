import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { cursorPage, ok, renderScreen } from '@/test/harness'
import { P } from '@/lib/permissions'

import MessagingPage from './messaging-page'

const templates = [
  {
    channel: 'WHATSAPP',
    notification_type: 'SYSTEM_ANNOUNCEMENT',
    template_name: 'announcement',
    template_id: null,
    language: 'en',
    parameters: ['title'],
    sms_text: null,
    is_active: true,
  },
]

function routes() {
  return {
    'GET /notifications/channel-status': ok({
      whatsapp_enabled: true,
      sms_enabled: false,
    }),
    'GET /notifications/channel-templates': ok(templates),
    'GET /notifications/deliveries': cursorPage([]),
  }
}

describe('messaging administration', () => {
  it('keeps alert titles from skipping levels in the page heading hierarchy', async () => {
    renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW],
      routes: routes(),
    })

    expect(await screen.findByLabelText('Approved template name')).toBeVisible()
    expect(await screen.findByText('Provider status')).toBeVisible()
    expect(screen.getAllByRole('heading').map((heading) => heading.tagName)).toEqual([
      'H1',
      'H2',
      'H2',
    ])
  })

  it('keeps template changes unavailable to settings viewers without manage permission', async () => {
    renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW],
      routes: routes(),
    })

    expect(
      await screen.findByText('You have read-only access to messaging settings.'),
    ).toBeVisible()
    expect(await screen.findByLabelText('Approved template name')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Add template' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save templates' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send test' })).toBeDisabled()
  })

  it('saves a tenant template and sends a test only through the explicit action', async () => {
    const { user, fetch } = renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
      routes: {
        ...routes(),
        'PUT /notifications/channel-templates': ok(templates),
        'POST /notifications/channel-templates/test': ok({ sent: true }),
      },
    })

    const templateName = await screen.findByLabelText('Approved template name')
    await user.clear(templateName)
    await user.type(templateName, 'approved_alert')
    await user.click(screen.getByRole('button', { name: 'Save templates' }))

    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/notifications/channel-templates')).toEqual({
        templates: [{ ...templates[0], template_name: 'approved_alert' }],
      }),
    )
    await user.click(screen.getByRole('button', { name: 'Send test' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/notifications/channel-templates/test')).toEqual({
        channel: 'WHATSAPP',
        notification_type: 'SYSTEM_ANNOUNCEMENT',
      }),
    )
  })
})

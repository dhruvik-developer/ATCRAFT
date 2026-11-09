import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { cursorPage, fail, ok, renderScreen } from '@/test/harness'
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

  it('refuses a new mapping with no template name and sends nothing', async () => {
    // Add seeds a blank name; the API refuses it as `templates.1`, a field
    // error the global handler leaves to the page.
    const { user, fetch } = renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
      routes: { ...routes(), 'PUT /notifications/channel-templates': ok(templates) },
    })

    await screen.findByLabelText('Approved template name')
    await user.click(screen.getByRole('button', { name: 'Add template' }))
    await user.click(screen.getByRole('button', { name: 'Save templates' }))

    const headline = await screen.findByText('The templates were not saved')
    const summary = headline.closest<HTMLElement>('[role="alert"]')!
    expect(
      within(summary).getByText('WHATSAPP · Approval Pending: Approved template name is required.'),
    ).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'PUT')).toHaveLength(0)

    // Naming the template clears the complaint, and the save goes through.
    const names = screen.getAllByLabelText('Approved template name')
    await user.type(names[1]!, 'approval_pending')
    expect(screen.queryByText('The templates were not saved')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save templates' }))
    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/notifications/channel-templates')).toMatchObject({
        templates: [templates[0], { channel: 'WHATSAPP', template_name: 'approval_pending' }],
      }),
    )
  })

  it('refuses SMS text whose placeholders do not match the parameters', async () => {
    const sms = {
      channel: 'SMS',
      notification_type: 'APPROVAL_PENDING',
      template_name: null,
      template_id: '1207160000000000001',
      language: 'en',
      parameters: ['title'],
      sms_text: 'Approval {#var#} is waiting for {#var#}',
      is_active: true,
    }
    const { user, fetch } = renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
      routes: { ...routes(), 'GET /notifications/channel-templates': ok([sms]) },
    })

    await screen.findByLabelText('DLT template ID')
    await user.click(screen.getByRole('button', { name: 'Save templates' }))

    expect(
      await screen.findAllByText(
        /The registered text has 2 \{#var#\} placeholders but 1 parameter is listed\./,
      ),
    ).toHaveLength(2)
    expect(fetch.calls.filter((call) => call.method === 'PUT')).toHaveLength(0)
  })

  it('shows a refusal the API names by position on the mapping it belongs to', async () => {
    const refusal = 'WhatsApp mappings require a template name only.'
    const { user } = renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
      routes: {
        ...routes(),
        'PUT /notifications/channel-templates': fail(
          422,
          'VALIDATION_ERROR',
          'Some fields need attention.',
          { fields: { 'templates.0': [refusal] } },
        ),
      },
    })

    await screen.findByLabelText('Approved template name')
    await user.click(screen.getByRole('button', { name: 'Save templates' }))

    expect(await screen.findByText(`WHATSAPP · System Announcement: ${refusal}`)).toBeVisible()
    // Twice: in the summary by the Save button, and on the mapping itself.
    expect(screen.getAllByText(refusal, { exact: false })).toHaveLength(2)
  })
})

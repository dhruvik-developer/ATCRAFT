import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { formatDateTime } from '@/lib/format'
import { P } from '@/lib/permissions'
import { cursorPage, fail, ok, renderScreen } from '@/test/harness'

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

  it('says each failed read as a headline that stays out of the page outline', async () => {
    renderScreen(
      <>
        <MessagingPage />
        <Toaster />
      </>,
      {
        permissions: [P.SETTINGS_VIEW],
        errorNets: true,
        routes: {
          ...routes(),
          'GET /notifications/channel-status': fail(503, 'UNAVAILABLE', 'Provider status is down'),
          'GET /notifications/deliveries': fail(503, 'UNAVAILABLE', 'Delivery log is down'),
        },
      },
    )

    const settings = await screen.findByText('Could not load the provider status')
    // AlertTitle's own styling, on a paragraph rather than an h5.
    expect(settings.tagName).toBe('P')
    expect(settings).toHaveClass('font-medium')
    expect(settings.closest('[role="alert"]')).toHaveTextContent('Provider status is down')
    const history = await screen.findByText('Could not load the delivery history')
    expect(history.closest('[role="alert"]')).toHaveTextContent('Delivery log is down')
    expect(await screen.findByLabelText('Approved template name')).toBeVisible()
    expect(screen.getAllByRole('heading').map((heading) => heading.tagName)).toEqual([
      'H1',
      'H2',
      'H2',
    ])
    // Each said where it applies, and not toasted as well.
    const toasts = within(screen.getByRole('region', { name: 'Notifications' }))
    expect(toasts.queryAllByRole('alert')).toHaveLength(0)
  })

  it('will not add or save until the saved templates are read, and says the failed read once', async () => {
    // The save upserts every mapping in the draft, and Add only refuses a pair
    // the draft already holds. With the read failed the draft was empty, so Add
    // seeded a mapping that already existed and Save wrote it over the saved one.
    let reads = 0
    const { user, fetch } = renderScreen(
      <>
        <MessagingPage />
        <Toaster />
      </>,
      {
        permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
        errorNets: true,
        routes: {
          ...routes(),
          'GET /notifications/channel-templates': () =>
            ++reads === 1 ? fail(503, 'UNAVAILABLE', 'Templates are down') : ok(templates),
        },
      },
    )

    const headline = await screen.findByText('Could not load the templates')
    expect(headline.closest('[role="alert"]')).toHaveTextContent('Templates are down')
    // Not an empty list: nothing says there are no templates when none were read.
    expect(screen.queryByText(/no templates configured/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add template' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save templates' })).toBeDisabled()
    // Said once, in place, and the headline keeps out of the page outline.
    const toasts = within(screen.getByRole('region', { name: 'Notifications' }))
    expect(toasts.queryAllByRole('alert')).toHaveLength(0)
    expect(screen.getAllByRole('heading').map((heading) => heading.tagName)).toEqual(['H1', 'H2'])

    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByLabelText('Approved template name')).toHaveValue('announcement')
    expect(screen.queryByText('Could not load the templates')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add template' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Save templates' })).toBeEnabled()
    expect(fetch.calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('says an empty list once the templates are read and none exist', async () => {
    renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
      routes: { ...routes(), 'GET /notifications/channel-templates': ok([]) },
    })

    expect(await screen.findByText(/no templates configured/i)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Add template' })).toBeEnabled()
    // Nothing to save until a mapping is added.
    expect(screen.getByRole('button', { name: 'Save templates' })).toBeDisabled()
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

    const headline = await screen.findByText('Could not save the templates')
    const summary = headline.closest<HTMLElement>('[role="alert"]')!
    expect(
      within(summary).getByText('WHATSAPP · Approval Pending: Approved template name is required.'),
    ).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'PUT')).toHaveLength(0)

    // Naming the template clears the complaint, and the save goes through.
    const names = screen.getAllByLabelText('Approved template name')
    await user.type(names[1]!, 'approval_pending')
    expect(screen.queryByText('Could not save the templates')).not.toBeInTheDocument()
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

  it('dates each delivery attempt in the application format', async () => {
    // Not the browser's toLocaleString, which read differently on every machine.
    const attemptedAt = '2026-03-14T10:30:00Z'
    renderScreen(<MessagingPage />, {
      permissions: [P.SETTINGS_VIEW],
      routes: {
        ...routes(),
        'GET /notifications/deliveries': cursorPage([
          {
            id: 'delivery-1',
            notification_id: 'notification-1',
            channel: 'WHATSAPP',
            outcome: 'DELIVERED',
            provider_message_id: 'wamid.1',
            attempted_at: attemptedAt,
            error_code: null,
          },
        ]),
      },
    })

    const stamp = await screen.findByText(formatDateTime(attemptedAt))
    expect(stamp).toHaveAttribute('dateTime', attemptedAt)
  })
})

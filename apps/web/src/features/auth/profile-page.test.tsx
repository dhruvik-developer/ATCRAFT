import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { fail, ok, page, renderScreen } from '@/test/harness'

import ProfilePage from './profile-page'

const sessions = page([])
const messagingChannels = ok({ whatsapp_enabled: false, sms_enabled: false })

describe('changing the password', () => {
  it('renders a policy refusal once, under the field it describes', async () => {
    // The one shared 422 mapping in lib/form-errors puts a refusal keyed on
    // `new_password` under that field, and the mutation cache stays quiet for
    // it. This form used to carry a second copy of that mapping which also
    // read `details.requirements` and wrote the same sentence onto the field a
    // second time, so the user saw it twice.
    const { user } = renderScreen(<ProfilePage />, {
      user: { mfa_enabled: false, must_change_password: false },
      routes: {
        'GET /auth/sessions': sessions,
        'GET /notifications/channels': messagingChannels,
        'POST /auth/change-password': fail(422, 'PASSWORD_POLICY', 'Password is too common.', {
          requirements: ['is too common'],
          fields: { new_password: ['Password is too common.'] },
        }),
      },
    })

    // Anchored and excluding the two-factor card's "Current password for ..."
    // fields, which share the prefix.
    await user.type(await screen.findByLabelText(/^Current password(?! for)/), 'Old-Password-123!')
    await user.type(screen.getByLabelText(/^New password/), 'Correct-Horse-Battery-9')
    await user.type(screen.getByLabelText(/^Repeat new password/), 'Correct-Horse-Battery-9')
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    const shown = await screen.findAllByText('Password is too common.')
    expect(shown).toHaveLength(1)
    expect(shown[0]).toBeVisible()
    expect(screen.getByLabelText(/^New password/)).toHaveAttribute('aria-invalid', 'true')
  })

  it('shows a wrong current password under its field and keeps the session', async () => {
    // A wrong current password on an authenticated route is a 422 keyed on
    // the field, never a 401: a 401 would send the api client through a
    // refresh and, failing that, out to the login page - discarding a session
    // that was perfectly valid.
    const { user, fetch } = renderScreen(
      <Routes>
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/login" element={<p>login page</p>} />
      </Routes>,
      {
        route: '/profile',
        user: { mfa_enabled: false, must_change_password: false },
        routes: {
          'GET /auth/sessions': sessions,
          'GET /notifications/channels': messagingChannels,
          'POST /auth/change-password': fail(
            422,
            'INVALID_CREDENTIALS',
            'Current password is incorrect.',
            { fields: { current_password: ['Current password is incorrect.'] } },
          ),
        },
      },
    )

    await user.type(await screen.findByLabelText(/^Current password(?! for)/), 'Not-The-Password-1')
    await user.type(screen.getByLabelText(/^New password/), 'Correct-Horse-Battery-9')
    await user.type(screen.getByLabelText(/^Repeat new password/), 'Correct-Horse-Battery-9')
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByText('Current password is incorrect.')).toBeVisible()
    expect(screen.getByLabelText(/^Current password(?! for)/)).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    // Neither a refresh attempt nor a sign-out: `clearTokens` would have set
    // the suppression marker, and a redirect would have left the form.
    expect(fetch.calls.some((call) => call.path === '/auth/refresh')).toBe(false)
    expect(localStorage.getItem('atcraft.session_suppressed')).toBeNull()
    expect(screen.queryByText('login page')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Change password' })).toBeVisible()
  })
})

describe('profile two-factor authentication', () => {
  it('sets up and confirms an authenticator code', async () => {
    const { fetch, user } = renderScreen(<ProfilePage />, {
      user: { mfa_enabled: false, must_change_password: false },
      routes: {
        'GET /auth/sessions': sessions,
        'GET /notifications/channels': messagingChannels,
        'POST /auth/mfa/setup': ok({
          secret: 'JBSWY3DPEHPK3PXP',
          provisioning_uri: 'otpauth://totp/ATCRAFT:test?secret=JBSWY3DPEHPK3PXP',
        }),
        'POST /auth/mfa/confirm': ok({ mfa_enabled: true, revoked_sessions: 2 }),
      },
    })

    await user.type(
      await screen.findByLabelText(/Current password for two-factor setup/),
      'correct horse battery staple',
    )
    await user.click(screen.getByRole('button', { name: 'Set up two-factor' }))

    expect(await screen.findByText('JBSWY3DPEHPK3PXP')).toBeVisible()
    expect(fetch.requestFor('POST', '/auth/mfa/setup')).toEqual({
      current_password: 'correct horse battery staple',
    })

    await user.type(screen.getByLabelText(/Authenticator code/), '123456')
    await user.click(screen.getByRole('button', { name: 'Enable two-factor' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', '/auth/mfa/confirm')).toEqual({ code: '123456' }),
    )
  })

  it('requires both the password and current code to disable two-factor authentication', async () => {
    const { fetch, user } = renderScreen(<ProfilePage />, {
      user: { mfa_enabled: true, must_change_password: false },
      routes: {
        'GET /auth/sessions': sessions,
        'GET /notifications/channels': messagingChannels,
        'POST /auth/mfa/disable': ok({ mfa_enabled: false, revoked_sessions: 1 }),
      },
    })

    await user.type(
      await screen.findByLabelText(/Current password for two-factor removal/),
      'correct horse battery staple',
    )
    await user.type(screen.getByLabelText(/Current authenticator code/), '654321')
    await user.click(screen.getByRole('button', { name: 'Disable two-factor' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', '/auth/mfa/disable')).toEqual({
        current_password: 'correct horse battery staple',
        code: '654321',
      }),
    )
  })
})

describe('messaging profile', () => {
  it('requires an enabled channel and verified number before recording consent', async () => {
    const { fetch, user } = renderScreen(<ProfilePage />, {
      user: {
        mfa_enabled: false,
        must_change_password: false,
        phone_e164: '+919876543210',
        phone_verified_at: '2026-01-01T00:00:00Z',
        whatsapp_opt_in_at: null,
        sms_opt_in_at: null,
      },
      routes: {
        'GET /auth/sessions': sessions,
        'GET /notifications/channels': ok({
          whatsapp_enabled: true,
          sms_enabled: false,
        }),
        'POST /auth/me/messaging-consent': ok({
          channel: 'WHATSAPP',
          opted_in: true,
        }),
      },
    })

    await user.click(await screen.findByRole('checkbox', { name: 'WhatsApp messages' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', '/auth/me/messaging-consent')).toEqual({
        channel: 'WHATSAPP',
        opt_in: true,
      }),
    )
    expect(screen.queryByRole('checkbox', { name: 'SMS messages' })).not.toBeInTheDocument()
  })
})

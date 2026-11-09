import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { errors } from '@/lib/messages'
import { ok, renderScreen, type RouteResponder } from '@/test/harness'

import { ForgotPasswordPage, ResetPasswordPage } from './password-reset-pages'

describe('password recovery', () => {
  it('requests a reset without exposing whether the account exists', async () => {
    const { user, fetch } = renderScreen(
      <Routes>
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      </Routes>,
      {
        route: '/forgot-password',
        restoreSession: false,
        routes: {
          'POST /auth/password-reset/request': ok({ acknowledged: true }),
        },
      },
    )

    await user.type(screen.getByLabelText(/Email/), 'person@example.com')
    await user.type(screen.getByLabelText(/Company code/), 'ACME')
    await user.click(screen.getByRole('button', { name: 'Send reset link' }))

    expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeVisible()
    expect(fetch.requestFor('POST', '/auth/password-reset/request')).toEqual({
      email: 'person@example.com',
      company_code: 'ACME',
    })
  })

  it('announces a missing email and reads it as the description of the email input', async () => {
    const { user, fetch } = renderScreen(
      <Routes>
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      </Routes>,
      { route: '/forgot-password', restoreSession: false, routes: {} },
    )

    await user.click(screen.getByRole('button', { name: 'Send reset link' }))

    expect(await screen.findByText('Email is required.')).toHaveAttribute('role', 'alert')
    expect(screen.getByLabelText(/Email/)).toHaveAccessibleDescription('Email is required.')
    expect(fetch.calls.some((call) => call.path === '/auth/password-reset/request')).toBe(false)
  })

  it('redeems the token captured from the email link', async () => {
    const { user, fetch } = renderScreen(
      <Routes>
        <Route path="/reset-password" element={<ResetPasswordPage />} />
      </Routes>,
      {
        route: '/reset-password#token=one-time-token-value',
        restoreSession: false,
        routes: {
          'POST /auth/password-reset/confirm': ok({ acknowledged: true }),
        },
      },
    )

    await user.type(screen.getByLabelText(/New password/), 'A-Secure-Password-123')
    await user.type(screen.getByLabelText(/Confirm new password/), 'A-Secure-Password-123')
    await user.click(screen.getByRole('button', { name: 'Update password' }))

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Password updated' })).toBeVisible(),
    )
    expect(fetch.requestFor('POST', '/auth/password-reset/confirm')).toEqual({
      token: 'one-time-token-value',
      new_password: 'A-Secure-Password-123',
    })
  })

  it('still redeems a token from the query string of an older link', async () => {
    // The worker moved the token into the fragment so it never reaches a log
    // or a referrer; a link mailed before that switch must not be a dead end.
    const { user, fetch } = renderScreen(
      <Routes>
        <Route path="/reset-password" element={<ResetPasswordPage />} />
      </Routes>,
      {
        route: '/reset-password?token=query-token-value',
        restoreSession: false,
        routes: {
          'POST /auth/password-reset/confirm': ok({ acknowledged: true }),
        },
      },
    )

    await user.type(screen.getByLabelText(/New password/), 'A-Secure-Password-123')
    await user.type(screen.getByLabelText(/Confirm new password/), 'A-Secure-Password-123')
    await user.click(screen.getByRole('button', { name: 'Update password' }))

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Password updated' })).toBeVisible(),
    )
    expect(fetch.requestFor('POST', '/auth/password-reset/confirm')).toMatchObject({
      token: 'query-token-value',
    })
  })
})

describe('a recovery request that fails', () => {
  const FORMS = [
    {
      form: 'the reset request',
      route: '/forgot-password',
      element: <Route path="/forgot-password" element={<ForgotPasswordPage />} />,
      endpoint: 'POST /auth/password-reset/request',
      async submit(user: ReturnType<typeof renderScreen>['user']) {
        await user.type(screen.getByLabelText(/Email/), 'person@example.com')
        await user.click(screen.getByRole('button', { name: 'Send reset link' }))
      },
    },
    {
      form: 'the new password',
      route: '/reset-password#token=one-time-token-value',
      element: <Route path="/reset-password" element={<ResetPasswordPage />} />,
      endpoint: 'POST /auth/password-reset/confirm',
      async submit(user: ReturnType<typeof renderScreen>['user']) {
        await user.type(screen.getByLabelText(/New password/), 'A-Secure-Password-123')
        await user.type(screen.getByLabelText(/Confirm new password/), 'A-Secure-Password-123')
        await user.click(screen.getByRole('button', { name: 'Update password' }))
      },
    },
  ]

  function renderForm(form: (typeof FORMS)[number], respond: RouteResponder) {
    return renderScreen(<Routes>{form.element}</Routes>, {
      route: form.route,
      restoreSession: false,
      routes: { [form.endpoint]: respond },
    })
  }

  it.each(FORMS)(
    'states a dropped connection on $form as the API client words it',
    async (form) => {
      const { user } = renderForm(form, () => {
        throw new TypeError('Failed to fetch')
      })

      await form.submit(user)

      expect(await screen.findByText(errors.networkFailed)).toBeVisible()
    },
  )

  it.each(FORMS)('does not call any other failure on $form a connection problem', async (form) => {
    // Only the API client sees the network, and it reports a dropped connection
    // as an ApiError. Anything else that reached these forms - here a cancelled
    // request - was announced as "Could not reach the server" all the same.
    const { user } = renderForm(form, () => {
      throw new DOMException('The operation was aborted.', 'AbortError')
    })

    await form.submit(user)

    expect(await screen.findByText(errors.unexpectedDetail)).toBeVisible()
    expect(screen.queryByText(/could not reach/i)).not.toBeInTheDocument()
  })
})

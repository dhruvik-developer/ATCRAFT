import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { ok, renderScreen } from '@/test/harness'

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

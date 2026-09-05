/**
 * The session's side effects.
 *
 * `AuthProvider` is exercised through the harness in every screen test; what
 * is pinned here is the one thing no screen can see - that the crash reporter
 * is told who the session belongs to, and told again when it ends.
 */

import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { setErrorReportingUser } from '@/lib/error-reporting'
import { ok, renderScreen } from '@/test/harness'

import { useAuth } from './auth-context'

vi.mock('@/lib/error-reporting', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/error-reporting')>()),
  setErrorReportingUser: vi.fn(),
}))

/** The id the harness signs the test user in as. */
const SIGNED_IN_ID = '01a00000-0000-7000-8000-000000000001'

function Probe() {
  const { user, logout } = useAuth()
  return (
    <div>
      <p>{user ? `signed in as ${user.id}` : 'signed out'}</p>
      <button type="button" onClick={() => void logout()}>
        Sign out
      </button>
    </div>
  )
}

describe('the crash reporter follows the session', () => {
  it('is told the user id on restore and null again on logout', async () => {
    // `setErrorReportingUser` was documented as "called from the auth provider"
    // and called from nowhere, so every report a configured collector received
    // carried `user_id: null` - the one field support needs to tie a report to
    // a call.
    const reporter = vi.mocked(setErrorReportingUser)
    const { user } = renderScreen(<Probe />, { routes: { 'POST /auth/logout': ok({}) } })

    await screen.findByText(`signed in as ${SIGNED_IN_ID}`)
    expect(reporter).toHaveBeenLastCalledWith(SIGNED_IN_ID)

    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    await screen.findByText('signed out')
    expect(reporter).toHaveBeenLastCalledWith(null)
  })
})

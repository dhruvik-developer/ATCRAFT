/**
 * The session's side effects.
 *
 * `AuthProvider` is exercised through the harness in every screen test; what
 * is pinned here is the one thing no screen can see - that the crash reporter
 * is told who the session belongs to, and told again when it ends.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { setErrorReportingUser } from '@/lib/error-reporting'
import { api } from '@/lib/api-client'
import { fail, meResponse, ok, renderScreen, type RouteBody } from '@/test/harness'

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

function AudienceProbe({ mfa }: { mfa: boolean }) {
  const { user, isVendorUser, isDealerUser, login, verifyMfa } = useAuth()
  return (
    <div>
      <p>
        {user
          ? isVendorUser
            ? 'vendor account'
            : isDealerUser
              ? 'dealer account'
              : 'staff account'
          : 'signed out'}
      </p>
      <p>{user?.vendor_id ?? user?.customer_id}</p>
      <p>{user?.permissions.length ?? 0} permissions</p>
      <button
        onClick={() => {
          void (mfa
            ? verifyMfa('challenge-token', '123456')
            : login('portal@example.test', 'correct password'))
        }}
      >
        Sign in
      </button>
    </div>
  )
}

describe('transient profile failures after authentication', () => {
  it.each([false, true])(
    'aborts an earlier cookie refresh before sending a new authentication request (MFA %s)',
    async (mfa) => {
      let finishRefresh!: (body: RouteBody) => void
      let refreshSignal: AbortSignal | null | undefined
      let profileCalls = 0
      const response = ok({
        mfa_required: false,
        access_token: 'new-user-access',
        token_type: 'bearer',
        expires_in: 900,
        refresh_expires_in: 86400,
        session_id: 'new-session',
        user: {
          id: 'portal-user',
          company_id: 'company',
          email: 'portal@example.test',
          full_name: 'Portal user',
          is_superuser: false,
          must_change_password: false,
          vendor_id: 'vendor-1',
          customer_id: null,
        },
      })
      const { user } = renderScreen(<AudienceProbe mfa={mfa} />, {
        routes: {
          'GET /auth/me': () =>
            ++profileCalls === 1
              ? meResponse([])
              : fail(503, 'SERVICE_UNAVAILABLE', 'Profile temporarily unavailable'),
          'GET /protected': fail(401, 'TOKEN_EXPIRED', 'Expired'),
          'POST /auth/refresh': () =>
            new Promise<RouteBody>((resolve) => {
              finishRefresh = resolve
            }),
          [mfa ? 'POST /auth/mfa/verify' : 'POST /auth/login']: () => {
            expect(refreshSignal?.aborted).toBe(true)
            return response
          },
        },
      })
      await screen.findByText('staff account')
      const originalFetch = vi.mocked(globalThis.fetch).getMockImplementation()!
      vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
        if (String(input).endsWith('/auth/refresh')) refreshSignal = init?.signal
        return originalFetch(input, init)
      })
      const oldRequest = api.get('/protected').catch((error: unknown) => error)
      await waitFor(() => expect(refreshSignal).toBeDefined())
      await user.click(screen.getByRole('button', { name: 'Sign in' }))
      expect(await screen.findByText('vendor account')).toBeVisible()
      finishRefresh(ok({ access_token: 'old-user-late-access' }))
      await oldRequest
      expect(screen.getByText('vendor account')).toBeVisible()
    },
  )
  it.each([
    ['vendor', false],
    ['dealer', false],
    ['vendor', true],
    ['dealer', true],
  ] as const)(
    'preserves the %s binding after %s MFA login without inventing permissions',
    async (audience, mfa) => {
      const binding =
        audience === 'vendor'
          ? { vendor_id: 'vendor-1', customer_id: null }
          : { vendor_id: null, customer_id: 'dealer-1' }
      const response = ok({
        mfa_required: false,
        access_token: 'access',
        token_type: 'bearer',
        expires_in: 900,
        refresh_expires_in: 86400,
        session_id: 'session-1',
        user: {
          id: 'portal-user',
          company_id: 'company',
          email: 'portal@example.test',
          full_name: 'Portal user',
          is_superuser: false,
          must_change_password: false,
          ...binding,
        },
      })
      const { user } = renderScreen(<AudienceProbe mfa={mfa} />, {
        restoreSession: false,
        routes: {
          'GET /auth/me': fail(503, 'SERVICE_UNAVAILABLE', 'Profile temporarily unavailable'),
          [mfa ? 'POST /auth/mfa/verify' : 'POST /auth/login']: response,
        },
      })
      await user.click(screen.getByRole('button', { name: 'Sign in' }))
      expect(await screen.findByText(`${audience} account`)).toBeVisible()
      expect(screen.getByText(audience === 'vendor' ? 'vendor-1' : 'dealer-1')).toBeVisible()
      expect(screen.getByText('0 permissions')).toBeVisible()
      expect(screen.queryByText('staff account')).toBeNull()
    },
  )
})

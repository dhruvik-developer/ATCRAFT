import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { beforeAll, describe, expect, it } from 'vitest'

import { AppRouter } from '@/app/router'
import { hasRefreshToken } from '@/lib/api-client'
import { errors } from '@/lib/messages'
import { P } from '@/lib/permissions'
import {
  fail,
  meResponse,
  ok,
  page,
  renderScreen,
  type RouteBody,
  type RouteTable,
} from '@/test/harness'

import { useAuth } from './auth-context'

beforeAll(async () => {
  await import('@/features/audit/audit-page')
})

function LocationProbe() {
  const location = useLocation()
  return <p data-testid="current-route">{location.pathname + location.search}</p>
}

function renderAt(route: string, routes: RouteTable) {
  return renderScreen(
    <>
      <AppRouter />
      <LocationProbe />
    </>,
    { route, routes },
  )
}

const profile = () => meResponse([P.AUDIT_LOG_VIEW])
const denied = () => fail(401, 'UNAUTHENTICATED', 'Access token expired')
const unavailable = (kind: number | string): RouteBody => {
  if (kind === 'network') throw new TypeError('Internal credentials could not be accessed')
  if (kind === 'malformed') return { data: null }
  return fail(
    Number(kind),
    kind === 429 ? 'RATE_LIMITED' : 'SERVICE_UNAVAILABLE',
    'Please retry.',
    {
      retry_after_seconds: 1,
    },
  )
}

function expectBlocked() {
  expect(screen.getByRole('heading', { name: errors.sessionRestoreFailed })).toBeVisible()
  expect(screen.queryByRole('heading', { name: 'Sign in' })).toBeNull()
  expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
  expect(screen.getByTestId('current-route')).toHaveTextContent('/audit?source=bookmark')
  expect(hasRefreshToken()).toBe(true)
  expect(localStorage.getItem('atcraft.session_suppressed')).toBeNull()
}

describe('recovering an unverified cookie-backed session', () => {
  it.each([503, 'network', 'malformed'])(
    'blocks protected content and restores the bookmarked route after profile failure %s',
    async (kind) => {
      let healthy = false
      const { fetch, user } = renderAt('/audit?source=bookmark', {
        'GET /auth/me': () => (healthy ? profile() : unavailable(kind)),
        'GET /audit/logs': page([]),
      })
      await screen.findByRole('heading', { name: errors.sessionRestoreFailed })
      expectBlocked()
      expect(screen.queryByText(/Internal credentials/)).toBeNull()
      expect(fetch.calls.filter((call) => call.path === '/auth/me')).toHaveLength(1)
      expect(fetch.calls.some((call) => call.path === '/audit/logs')).toBe(false)

      healthy = true
      await user.click(screen.getByRole('button', { name: 'Try again' }))
      expect(await screen.findByRole('heading', { name: 'Audit trail' })).toBeVisible()
      expect(screen.getByTestId('current-route')).toHaveTextContent('/audit?source=bookmark')
      expect(fetch.calls.some((call) => call.path === '/auth/login')).toBe(false)
    },
  )

  it.each([503, 'network', 'malformed'])(
    'preserves the cookie after refresh failure %s and retries only at the user request',
    async (kind) => {
      let healthy = false
      let refreshed = false
      const { fetch, user } = renderAt('/audit?source=bookmark', {
        'GET /auth/me': () => (refreshed ? profile() : denied()),
        'POST /auth/refresh': () => {
          if (!healthy) return unavailable(kind)
          refreshed = true
          return ok({ access_token: 'renewed-access' })
        },
        'GET /audit/logs': page([]),
      })
      await screen.findByRole('heading', { name: errors.sessionRestoreFailed })
      expectBlocked()
      expect(fetch.calls.filter((call) => call.path === '/auth/refresh')).toHaveLength(1)
      healthy = true
      await user.click(screen.getByRole('button', { name: 'Try again' }))
      expect(await screen.findByRole('heading', { name: 'Audit trail' })).toBeVisible()
      expect(fetch.calls.filter((call) => call.path === '/auth/refresh')).toHaveLength(2)
      expect(fetch.calls.some((call) => call.path === '/auth/login')).toBe(false)
    },
  )

  it.each(['profile', 'refresh'])(
    'honors the %s429 cooldown without automatic requests',
    async (surface) => {
      let healthy = false
      let refreshed = surface === 'profile'
      const { fetch } = renderAt('/audit?source=bookmark', {
        'GET /auth/me': () => {
          if (surface === 'profile' && !healthy) return unavailable(429)
          return refreshed ? profile() : denied()
        },
        'POST /auth/refresh': () => {
          if (!healthy) return unavailable(429)
          refreshed = true
          return ok({ access_token: 'renewed-access' })
        },
        'GET /audit/logs': page([]),
      })
      const wait = await screen.findByRole('button', { name: 'Try again in 1s' })
      expectBlocked()
      expect(wait).toBeDisabled()
      const attempts = fetch.calls.length
      healthy = true
      await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled(), {
        timeout: 2000,
      })
      expect(fetch.calls).toHaveLength(attempts)
      fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
      expect(await screen.findByRole('heading', { name: 'Audit trail' })).toBeVisible()
    },
  )

  it.each([401, 403])('ends a profile-rejected session on HTTP%s', async (status) => {
    renderAt('/audit?source=bookmark', {
      'GET /auth/me': fail(status, 'UNAUTHENTICATED', 'Session no longer valid'),
      'POST /auth/refresh': fail(status, 'INVALID_TOKEN', 'Refresh token rejected'),
    })
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(hasRefreshToken()).toBe(false)
    expect(screen.queryByRole('heading', { name: errors.sessionRestoreFailed })).toBeNull()
  })

  it('also offers recovery on the public sign-in route instead of hiding a valid cookie', async () => {
    renderAt('/login', { 'GET /auth/me': fail(503, 'SERVICE_UNAVAILABLE', 'Please retry.') })
    expect(await screen.findByRole('heading', { name: errors.sessionRestoreFailed })).toBeVisible()
    expect(screen.getByTestId('current-route')).toHaveTextContent('/login')
    expect(screen.queryByRole('textbox', { name: 'Email' })).toBeNull()
    expect(hasRefreshToken()).toBe(true)
  })

  it('lets the user explicitly end restoration and sign in instead', async () => {
    const { fetch, user } = renderAt('/audit?source=bookmark', {
      'GET /auth/me': fail(503, 'SERVICE_UNAVAILABLE', 'Please retry.'),
      'POST /auth/logout': ok({}),
    })
    await screen.findByRole('heading', { name: errors.sessionRestoreFailed })
    await user.click(screen.getByRole('button', { name: 'Sign in instead' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeVisible()
    expect(hasRefreshToken()).toBe(false)
    expect(fetch.calls.filter((call) => call.path === '/auth/logout')).toHaveLength(1)
  })

  it('does not overlap repeated retries while the next profile is unresolved', async () => {
    let retry = false
    let finish!: (body: RouteBody) => void
    const { fetch } = renderAt('/audit?source=bookmark', {
      'GET /auth/me': () =>
        retry
          ? new Promise<RouteBody>((resolve) => {
              finish = resolve
            })
          : unavailable(503),
      'GET /audit/logs': page([]),
    })
    const button = await screen.findByRole('button', { name: 'Try again' })
    retry = true
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() =>
      expect(fetch.calls.filter((call) => call.path === '/auth/me')).toHaveLength(2),
    )
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    await act(async () => finish(profile()))
    expect(await screen.findByRole('heading', { name: 'Audit trail' })).toBeVisible()
  })
})

function RaceProbe() {
  const { user, restorationError, retryRestoration, logout, login } = useAuth()
  return (
    <>
      <p>{user ? `account ${user.id}` : restorationError ? 'blocked' : 'pending'}</p>
      <button onClick={retryRestoration}>Retry</button>
      <button onClick={() => void logout()}>Leave</button>
      <button onClick={() => void login('new@example.test', 'correct password')}>
        Replace account
      </button>
    </>
  )
}

describe('restoration belongs to one account attempt', () => {
  it('cannot reinstall a profile that finishes after explicit logout', async () => {
    let retry = false
    let finish!: (body: RouteBody) => void
    const { user } = renderScreen(<RaceProbe />, {
      routes: {
        'GET /auth/me': () =>
          retry
            ? new Promise<RouteBody>((resolve) => {
                finish = resolve
              })
            : unavailable(503),
        'POST /auth/logout': ok({}),
      },
    })
    await screen.findByText('blocked')
    retry = true
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(finish).toBeTypeOf('function'))
    await user.click(screen.getByRole('button', { name: 'Leave' }))
    await waitFor(() => expect(hasRefreshToken()).toBe(false))
    await act(async () => finish(profile()))
    expect(screen.getByText('pending')).toBeVisible()
    expect(screen.queryByText(/account /)).toBeNull()
  })

  it('ignores an old pending profile after replacement authentication succeeds', async () => {
    let finish!: (body: RouteBody) => void
    let replacement = false
    const { user } = renderScreen(<RaceProbe />, {
      routes: {
        'GET /auth/me': () =>
          replacement
            ? meResponse([], { user_id: 'replacement' })
            : new Promise<RouteBody>((resolve) => {
                finish = resolve
              }),
        'POST /auth/login': () => {
          replacement = true
          return ok({
            mfa_required: false,
            access_token: 'new-access',
            token_type: 'bearer',
            expires_in: 900,
            refresh_expires_in: 86400,
            session_id: 'new-session',
            user: {
              id: 'replacement',
              email: 'new@example.test',
              full_name: 'New user',
              company_id: 'company',
              is_superuser: false,
              must_change_password: false,
            },
          })
        },
      },
    })
    await waitFor(() => expect(finish).toBeTypeOf('function'))
    await user.click(screen.getByRole('button', { name: 'Replace account' }))
    await screen.findByText('account replacement')
    await act(async () => finish(meResponse([], { user_id: 'old' })))
    expect(screen.getByText('account replacement')).toBeVisible()
    expect(screen.queryByText('account old')).toBeNull()
  })
})

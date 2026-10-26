import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, hasRefreshToken, setTokens } from '@/lib/api-client'
import { fail, meResponse, ok, renderScreen, type RouteBody } from '@/test/harness'

import { useAuth } from './auth-context'

const push = vi.hoisted(() => {
  let loaded!: () => void
  const ready = new Promise<void>((resolve) => {
    loaded = resolve
  })
  return {
    ready,
    loaded: () => loaded(),
    unregisterOnLogout: vi.fn(async () => ({ ok: true })),
    cancelPushSetup: vi.fn(),
  }
})

vi.mock('@/features/notifications/push', async () => {
  await push.ready
  return { unregisterOnLogout: push.unregisterOnLogout, cancelPushSetup: push.cancelPushSetup }
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function Probe() {
  const { user, logout, login, verifyMfa } = useAuth()
  return (
    <div>
      <p>{user ? `signed in as ${user.id}` : 'signed out'}</p>
      <button
        onClick={() => {
          void logout()
        }}
      >
        Sign out
      </button>
      <button
        onClick={() => {
          void login('new@example.test', 'correct password').catch(() => undefined)
        }}
      >
        Sign in again
      </button>
      <button
        onClick={() => {
          void verifyMfa('challenge', '123456').catch(() => undefined)
        }}
      >
        Verify sign in
      </button>
    </div>
  )
}

const loginData = {
  mfa_required: false,
  access_token: 'new-user-access',
  token_type: 'bearer',
  expires_in: 900,
  refresh_expires_in: 86400,
  session_id: 'new-session',
  user: {
    id: 'new-user',
    company_id: 'company',
    email: 'new@example.test',
    full_name: 'New user',
    is_superuser: false,
    must_change_password: false,
  },
}
const loginResponse = ok(loginData)
const oldLoginResponse = ok({
  ...loginData,
  access_token: 'old-user-access',
  user: { ...loginData.user, id: 'old-user' },
})

describe('logout belongs to one bounded session', () => {
  it('bounds a stalled push import and never runs it after another account signs in', async () => {
    let loggedInAgain = false
    const { fetch, queryClient } = renderScreen(<Probe />, {
      routes: {
        'GET /auth/me': () => meResponse([], { user_id: loggedInAgain ? 'new-user' : 'old-user' }),
        'POST /auth/login': () => {
          loggedInAgain = true
          return loginResponse
        },
        'POST /auth/logout': ok({}),
      },
    })
    await screen.findByText('signed in as old-user')
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })
      expect(screen.getByText('signed out')).toBeVisible()
      expect(hasRefreshToken()).toBe(false)
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Sign in again' }))
      })
      expect(screen.getByText('signed in as new-user')).toBeVisible()
      queryClient.setQueryDefaults(['new-account-private-data'], { gcTime: Infinity })
      queryClient.setQueryData(['new-account-private-data'], 'retain')
      await act(async () => {
        push.loaded()
        await vi.dynamicImportSettled()
      })
      expect(push.unregisterOnLogout).not.toHaveBeenCalled()
      expect(fetch.calls.filter((call) => call.path === '/auth/logout')).toHaveLength(1)
      expect(screen.getByText('signed in as new-user')).toBeVisible()
      expect(queryClient.getQueryData(['new-account-private-data'])).toBe('retain')
      expect(hasRefreshToken()).toBe(true)
    } finally {
      push.loaded()
    }
  })

  it('clears local credentials and private cache within seven seconds even when cleanup and revocation never settle', async () => {
    let finishCleanup!: () => void
    push.unregisterOnLogout.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishCleanup = () => resolve({ ok: true })
        }),
    )
    const revoke = vi.spyOn(api, 'post')
    const { fetch, queryClient } = renderScreen(<Probe />, {
      routes: { 'POST /auth/logout': () => new Promise<RouteBody>(() => {}) },
    })
    await screen.findByText(/signed in as/)
    queryClient.setQueryDefaults(['private-data'], { gcTime: Infinity })
    queryClient.setQueryData(['private-data'], 'secret')
    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
      await vi.dynamicImportSettled()
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(fetch.calls.filter((call) => call.path === '/auth/logout')).toHaveLength(1)
    expect(push.cancelPushSetup).toHaveBeenCalledOnce()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_999)
    })
    expect(screen.getByText(/signed in as/)).toBeVisible()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(screen.getByText('signed out')).toBeVisible()
    expect(hasRefreshToken()).toBe(false)
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined()
    expect(revoke.mock.calls.find(([path]) => path === '/auth/logout')?.[2]?.signal?.aborted).toBe(
      true,
    )
    await act(async () => {
      finishCleanup()
    })
  })

  it('ignores a previous logout response after a newer login and never refreshes its rejected revocation', async () => {
    let loggedInAgain = false
    let finishRevocation!: (response: RouteBody) => void
    const { fetch, queryClient } = renderScreen(<Probe />, {
      routes: {
        'GET /auth/me': () => meResponse([], { user_id: loggedInAgain ? 'new-user' : 'old-user' }),
        'POST /auth/login': () => {
          loggedInAgain = true
          return loginResponse
        },
        'POST /auth/logout': () =>
          new Promise<RouteBody>((resolve) => {
            finishRevocation = resolve
          }),
      },
    })
    await screen.findByText('signed in as old-user')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
      await vi.dynamicImportSettled()
    })
    expect(fetch.calls.some((call) => call.path === '/auth/logout')).toBe(true)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in again' }))
    })
    expect(screen.getByText('signed in as new-user')).toBeVisible()
    queryClient.setQueryDefaults(['new-account-private-data'], { gcTime: Infinity })
    queryClient.setQueryData(['new-account-private-data'], 'retain')
    await act(async () => {
      finishRevocation(fail(401, 'UNAUTHENTICATED', 'Old session ended'))
    })
    expect(screen.getByText('signed in as new-user')).toBeVisible()
    expect(queryClient.getQueryData(['new-account-private-data'])).toBe('retain')
    expect(fetch.calls.some((call) => call.path === '/auth/refresh')).toBe(false)
    expect(hasRefreshToken()).toBe(true)
  })

  it.each([
    { label: 'password login', path: '/auth/login', button: 'Sign in again' },
    { label: 'MFA verification', path: '/auth/mfa/verify', button: 'Verify sign in' },
  ])('aborts the old logout fetch before starting $label', async ({ path, button }) => {
    let loggedInAgain = false
    let finishRevocation!: (response: RouteBody) => void
    const { queryClient } = renderScreen(<Probe />, {
      routes: {
        'GET /auth/me': () => meResponse([], { user_id: loggedInAgain ? 'new-user' : 'old-user' }),
        [`POST ${path}`]: () => {
          loggedInAgain = true
          return loginResponse
        },
        'POST /auth/logout': () =>
          new Promise<RouteBody>((resolve) => {
            finishRevocation = resolve
          }),
      },
    })
    await screen.findByText('signed in as old-user')
    const originalFetch = globalThis.fetch
    let logoutSignal: AbortSignal | null | undefined
    const order: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), 'http://localhost')
        if (url.pathname.endsWith('/auth/logout')) {
          logoutSignal = init?.signal
          expect(logoutSignal).toBeDefined()
          logoutSignal!.addEventListener('abort', () => order.push('logout aborted'), {
            once: true,
          })
          order.push('logout sent')
        }
        if (url.pathname.endsWith(path)) {
          // A stale JSON guard is too late: a browser applies Set-Cookie headers
          // before fetch resolves. Cancellation must precede the next request.
          expect(logoutSignal?.aborted).toBe(true)
          order.push('new authentication sent')
        }
        const response = await originalFetch(input, init)
        if (url.pathname.endsWith('/auth/logout') && !init?.signal?.aborted) {
          // Model the old response expiring the readable companion cookie.
          document.cookie = 'atcraft_csrf=; Path=/; Max-Age=0'
        }
        return response
      }),
    )
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
      await vi.dynamicImportSettled()
    })
    expect(logoutSignal?.aborted).toBe(false)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: button }))
    })
    expect(order).toEqual(['logout sent', 'logout aborted', 'new authentication sent'])
    expect(screen.getByText('signed in as new-user')).toBeVisible()
    queryClient.setQueryDefaults(['new-account-private-data'], { gcTime: Infinity })
    queryClient.setQueryData(['new-account-private-data'], 'retain')
    await act(async () => {
      finishRevocation(ok({}))
    })
    expect(screen.getByText('signed in as new-user')).toBeVisible()
    expect(queryClient.getQueryData(['new-account-private-data'])).toBe('retain')
    expect(hasRefreshToken()).toBe(true)
  })

  it('cancels old device cleanup before a new login without later cancelling the new account setup', async () => {
    let finishCleanup!: () => void
    push.unregisterOnLogout.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishCleanup = () => resolve({ ok: true })
        }),
    )
    let loggedInAgain = false
    const { fetch } = renderScreen(<Probe />, {
      routes: {
        'GET /auth/me': () => meResponse([], { user_id: loggedInAgain ? 'new-user' : 'old-user' }),
        'POST /auth/login': () => {
          expect(push.cancelPushSetup).toHaveBeenCalledOnce()
          loggedInAgain = true
          return loginResponse
        },
        'POST /auth/logout': ok({}),
      },
    })
    await screen.findByText('signed in as old-user')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
      await vi.dynamicImportSettled()
    })
    expect(push.cancelPushSetup).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in again' }))
    })
    expect(screen.getByText('signed in as new-user')).toBeVisible()
    expect(fetch.calls.some((call) => call.path === '/auth/logout')).toBe(false)
    expect(push.cancelPushSetup).toHaveBeenCalledOnce()
    await act(async () => {
      finishCleanup()
    })
    expect(screen.getByText('signed in as new-user')).toBeVisible()
    expect(push.cancelPushSetup).toHaveBeenCalledOnce()
    expect(fetch.calls.some((call) => call.path === '/auth/logout')).toBe(false)
  })

  it.each([
    { label: 'password login', path: '/auth/login', button: 'Sign in again' },
    { label: 'MFA verification', path: '/auth/mfa/verify', button: 'Verify sign in' },
  ])('keeps the old account signed out when replacement $label fails', async ({ path, button }) => {
    let finishRevocation!: (response: RouteBody) => void
    const { queryClient } = renderScreen(<Probe />, {
      routes: {
        [`POST ${path}`]: fail(401, 'UNAUTHENTICATED', 'Credentials were rejected'),
        'POST /auth/logout': () =>
          new Promise<RouteBody>((resolve) => {
            finishRevocation = resolve
          }),
        'GET /session-probe': ok({}),
      },
    })
    await screen.findByText(/signed in as/)
    setTokens('old-account-access')
    queryClient.setQueryDefaults(['old-account-private-data'], { gcTime: Infinity })
    queryClient.setQueryData(['old-account-private-data'], 'discard')
    const originalFetch = globalThis.fetch
    let logoutSignal: AbortSignal | null | undefined
    const boundary = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.pathname.endsWith('/auth/logout')) logoutSignal = init?.signal
      if (url.pathname.endsWith(path)) {
        expect(logoutSignal?.aborted).toBe(true)
        expect(hasRefreshToken()).toBe(false)
        expect(queryClient.getQueryData(['old-account-private-data'])).toBeUndefined()
      }
      if (url.pathname.endsWith('/session-probe')) {
        expect(new Headers(init?.headers).get('Authorization')).toBeNull()
      }
      return originalFetch(input, init)
    })
    vi.stubGlobal('fetch', boundary)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
      await vi.dynamicImportSettled()
    })
    expect(logoutSignal?.aborted).toBe(false)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: button }))
    })
    expect(screen.getByText('signed out')).toBeVisible()
    expect(hasRefreshToken()).toBe(false)
    expect(queryClient.getQueryData(['old-account-private-data'])).toBeUndefined()
    await api.get('/session-probe')
    await act(async () => {
      finishRevocation(ok({}))
    })
    expect(screen.getByText('signed out')).toBeVisible()
    expect(hasRefreshToken()).toBe(false)
  })

  it.each([
    {
      first: '/auth/mfa/verify',
      firstButton: 'Verify sign in',
      next: '/auth/login',
      nextButton: 'Sign in again',
    },
    {
      first: '/auth/login',
      firstButton: 'Sign in again',
      next: '/auth/mfa/verify',
      nextButton: 'Verify sign in',
    },
  ])(
    'aborts pending $first before replacement $next can set a cookie',
    async ({ first, firstButton, next, nextButton }) => {
      let finishFirst!: (response: RouteBody) => void
      const { queryClient } = renderScreen(<Probe />, {
        restoreSession: false,
        routes: {
          'GET /auth/me': meResponse([], { user_id: 'new-user' }),
          [`POST ${first}`]: () =>
            new Promise<RouteBody>((resolve) => {
              finishFirst = resolve
            }),
          [`POST ${next}`]: loginResponse,
          'GET /session-probe': ok({}),
        },
      })
      const originalFetch = globalThis.fetch
      let firstSignal: AbortSignal | null | undefined
      const order: string[] = []
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = new URL(String(input), 'http://localhost')
          if (url.pathname.endsWith(first)) {
            firstSignal = init?.signal
            firstSignal?.addEventListener('abort', () => order.push('first aborted'), {
              once: true,
            })
            order.push('first sent')
          }
          if (url.pathname.endsWith(next)) {
            expect(firstSignal?.aborted).toBe(true)
            order.push('replacement sent')
          }
          if (url.pathname.endsWith('/session-probe')) {
            expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer new-user-access')
          }
          const response = await originalFetch(input, init)
          if (
            !init?.signal?.aborted &&
            (url.pathname.endsWith(first) || url.pathname.endsWith(next))
          ) {
            document.cookie = `atcraft_csrf=${url.pathname.endsWith(first) ? 'old-session' : 'new-session'}; Path=/`
          }
          return response
        }),
      )
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: firstButton }))
      })
      expect(firstSignal?.aborted).toBe(false)
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: nextButton }))
      })
      expect(order).toEqual(['first sent', 'first aborted', 'replacement sent'])
      expect(screen.getByText('signed in as new-user')).toBeVisible()
      queryClient.setQueryDefaults(['new-account-private-data'], { gcTime: Infinity })
      queryClient.setQueryData(['new-account-private-data'], 'retain')
      await act(async () => {
        finishFirst(oldLoginResponse)
      })
      expect(screen.getByText('signed in as new-user')).toBeVisible()
      expect(document.cookie).toContain('atcraft_csrf=new-session')
      expect(queryClient.getQueryData(['new-account-private-data'])).toBe('retain')
      await api.get('/session-probe')
    },
  )

  it.each([
    { path: '/auth/login', button: 'Sign in again' },
    { path: '/auth/mfa/verify', button: 'Verify sign in' },
  ])(
    'aborts pending $path when signing out and ignores its later credential',
    async ({ path, button }) => {
      let finishAuthentication!: (response: RouteBody) => void
      renderScreen(<Probe />, {
        restoreSession: false,
        routes: {
          [`POST ${path}`]: () =>
            new Promise<RouteBody>((resolve) => {
              finishAuthentication = resolve
            }),
          'POST /auth/logout': ok({}),
          'GET /session-probe': ok({}),
        },
      })
      const originalFetch = globalThis.fetch
      let authenticationSignal: AbortSignal | null | undefined
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = new URL(String(input), 'http://localhost')
          if (url.pathname.endsWith(path)) authenticationSignal = init?.signal
          if (url.pathname.endsWith('/auth/logout'))
            expect(authenticationSignal?.aborted).toBe(true)
          if (url.pathname.endsWith('/session-probe'))
            expect(new Headers(init?.headers).get('Authorization')).toBeNull()
          return originalFetch(input, init)
        }),
      )
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: button }))
      })
      expect(authenticationSignal?.aborted).toBe(false)
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
        await vi.dynamicImportSettled()
      })
      expect(authenticationSignal?.aborted).toBe(true)
      expect(screen.getByText('signed out')).toBeVisible()
      await act(async () => {
        finishAuthentication(oldLoginResponse)
      })
      expect(screen.getByText('signed out')).toBeVisible()
      expect(hasRefreshToken()).toBe(false)
      await api.get('/session-probe')
    },
  )
})

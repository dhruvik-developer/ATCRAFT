/**
 * Authentication state for the SPA.
 *
 * The permission set held here drives *affordances only* - which menu items and
 * buttons are shown. It is never the enforcement point: master rules 38 is
 * explicit that frontend permissions are for UX and the backend authorises every
 * request regardless. Anyone can edit this state in a debugger; the API does not
 * care.
 */

import { useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import {
  ApiError,
  api,
  beginAuthentication,
  clearTokens,
  getAuthGeneration,
  hasRefreshToken,
  onAuthChange,
  setTokens,
} from '@/lib/api-client'
import { setErrorReportingUser } from '@/lib/error-reporting'
import { queryKeys } from '@/lib/query-client'

/**
 * `GET /auth/me` (`CurrentUserRead`), verbatim.
 *
 * Note the primary key is `user_id` here and `id` on the login payload - the
 * two endpoints genuinely disagree, so both are normalised into `CurrentUser`
 * below rather than letting the difference leak into every screen.
 */
interface MeResponse {
  user_id: string
  company_id: string
  company_name: string
  email: string
  full_name: string
  is_superuser: boolean
  must_change_password: boolean
  mfa_enabled: boolean
  session_id?: string | null
  last_login_at?: string | null
  branch_id?: string | null
  plant_id?: string | null
  department_id?: string | null
  vendor_id?: string | null
  customer_id?: string | null
  /** Both are omitted, not nulled, when empty - never dereference directly. */
  roles?: string[]
  permissions?: string[]
  allowed_warehouse_ids?: string[]
  allowed_category_ids?: string[]
}

/** The normalised user every screen reads. */
export interface CurrentUser {
  id: string
  email: string
  full_name: string
  company_id: string
  /**
   * The tenant's name, for the sidebar. It was declared here and rendered on
   * every screen for months while no endpoint sent it, so the line under
   * "Atcraft ERP" was simply blank; `/auth/me` carries it now.
   *
   * Still optional, because the sign-in response does not carry it - the
   * session is established from `/auth/me` a moment later, and a name that
   * appears one paint after the shell is better than a second request on the
   * critical path.
   */
  company_name?: string
  is_superuser: boolean
  mfa_enabled: boolean
  /** Always an array here, even though the API omits an empty one. */
  permissions: string[]
  roles: string[]
  vendor_id: string | null
  customer_id: string | null
  must_change_password: boolean
  branch_id: string | null
  plant_id: string | null
  department_id: string | null
  session_id: string | null
  last_login_at: string | null
  allowed_warehouse_ids: string[]
  allowed_category_ids: string[]
}

function toCurrentUser(me: MeResponse): CurrentUser {
  return {
    id: me.user_id,
    email: me.email,
    full_name: me.full_name,
    company_id: me.company_id,
    company_name: me.company_name,
    is_superuser: me.is_superuser,
    mfa_enabled: me.mfa_enabled,
    permissions: me.permissions ?? [],
    roles: me.roles ?? [],
    vendor_id: me.vendor_id ?? null,
    customer_id: me.customer_id ?? null,
    must_change_password: me.must_change_password,
    branch_id: me.branch_id ?? null,
    plant_id: me.plant_id ?? null,
    department_id: me.department_id ?? null,
    session_id: me.session_id ?? null,
    last_login_at: me.last_login_at ?? null,
    allowed_warehouse_ids: me.allowed_warehouse_ids ?? [],
    allowed_category_ids: me.allowed_category_ids ?? [],
  }
}

/**
 * `POST /auth/login` (`TokenPair`).
 *
 * The embedded user is deliberately thin: it carries no permission set, no
 * vendor id and no placement. Signing in on that payload alone would leave
 * `can()` answering false for everything until the next full page load, which
 * is why `login` follows it with `/auth/me`.
 */
interface BrowserLoginResponse {
  mfa_required: false
  access_token: string
  token_type: string
  expires_in: number
  refresh_expires_in: number
  session_id: string
  user: {
    id: string
    email: string
    full_name: string
    company_id: string
    is_superuser: boolean
    must_change_password: boolean
    roles?: string[]
    vendor_id?: string | null
    customer_id?: string | null
  }
}

export interface MfaChallenge {
  mfa_required: true
  challenge_token: string
  expires_in: number
}

export type LoginOutcome = CurrentUser | MfaChallenge

interface AuthContextValue {
  user: CurrentUser | null
  isAuthenticated: boolean
  /** True only while the initial session restore is in flight. */
  isRestoring: boolean
  /** A failed profile lookup blocks protected content without discarding a valid cookie. */
  restorationError: Error | null
  retryRestoration: () => void
  login: (email: string, password: string) => Promise<LoginOutcome>
  verifyMfa: (challengeToken: string, code: string) => Promise<CurrentUser>
  cancelAuthentication: () => void
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
  can: (permission: string) => boolean
  canAny: (...permissions: string[]) => boolean
  canAll: (...permissions: string[]) => boolean
  isVendorUser: boolean
  isDealerUser: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

const LOGOUT_DEVICE_BUDGET_MS = 2_000
const LOGOUT_API_BUDGET_MS = 5_000

/** Bound even imports and providers that do not settle when their signal aborts. */
async function withinLogoutBudget(
  work: (signal: AbortSignal) => Promise<unknown>,
  milliseconds: number,
  parent: AbortSignal,
): Promise<void> {
  const controller = new AbortController()
  let expire!: () => void
  const expiry = new Promise<void>((resolve) => {
    expire = () => {
      controller.abort()
      resolve()
    }
  })
  parent.addEventListener('abort', expire, { once: true })
  const timer = setTimeout(expire, milliseconds)
  try {
    if (parent.aborted) return
    await Promise.race([work(controller.signal).catch(() => undefined), expiry])
  } finally {
    clearTimeout(timer)
    parent.removeEventListener('abort', expire)
    controller.abort()
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [isRestoring, setIsRestoring] = useState(hasRefreshToken())
  const [restorationError, setRestorationError] = useState<Error | null>(null)
  const [restorationAttempt, setRestorationAttempt] = useState(0)
  const pendingRestoration = useRef<AbortController | null>(null)
  const queryClient = useQueryClient()
  const pendingLogout = useRef<{
    generation: number
    controller: AbortController
    promise: Promise<void>
  } | null>(null)
  const pendingAuthentication = useRef<AbortController | null>(null)

  const beginLogin = useCallback(() => {
    pendingRestoration.current?.abort()
    setRestorationError(null)
    setIsRestoring(false)
    pendingAuthentication.current?.abort()
    // Cookie changes happen before JavaScript receives a response. Stop the old
    // logout request before a new login can install its refresh credential.
    const leaving = pendingLogout.current
    if (leaving?.generation === getAuthGeneration()) {
      leaving.controller.abort()
      // Cancelling revocation must not preserve the account the user left if
      // replacement credentials are rejected. Cancel push before changing its generation.
      clearTokens()
      setUser(null)
      queryClient.clear()
    }
    const controller = new AbortController()
    pendingAuthentication.current = controller
    return { generation: beginAuthentication(), controller }
  }, [queryClient])

  const cancelAuthentication = useCallback(() => {
    if (!pendingAuthentication.current) return
    pendingAuthentication.current.abort()
    pendingAuthentication.current = null
    // Backing out of MFA must also discard a credential whose profile request
    // was still pending. Suppress restoration of any abandoned browser cookie.
    clearTokens()
  }, [])

  // Keep the crash reporter's user id in step with the session, whichever path
  // installed or cleared it (restore, login, the fallback below, refresh,
  // logout, or the API client revoking tokens via `onAuthChange`). Tracking the
  // state rather than each `setUser` call site means a new installer cannot
  // miss it - and until this existed, no installer called it at all, so every
  // report arrived with `user_id: null`.
  useEffect(() => {
    setErrorReportingUser(user?.id ?? null)
  }, [user])

  // Restore the session on a hard reload. The access token is memory-only, so
  // after F5 there is a refresh token and nothing else; /auth/me triggers the
  // transparent refresh inside the API client.
  useEffect(() => {
    let cancelled = false
    const generation = getAuthGeneration()

    if (!hasRefreshToken()) {
      setRestorationError(null)
      setIsRestoring(false)
      return
    }

    const controller = new AbortController()
    pendingRestoration.current = controller

    void (async () => {
      try {
        const me = await api.get<MeResponse>('/auth/me', { signal: controller.signal })
        if (!cancelled && !controller.signal.aborted && generation === getAuthGeneration()) {
          setRestorationError(null)
          setUser(toCurrentUser(me))
        }
      } catch (error) {
        // Only an authentication failure ends the session. A 5xx or a dropped
        // connection on the restore call says nothing about whether the refresh
        // token is still good, and discarding it there signs the user out for
        // an API hiccup - they come back to a login form with no explanation
        // and a refresh token that was perfectly valid. The client clears the
        // tokens itself when a refresh genuinely fails, and `onAuthChange`
        // below mirrors that into this state.
        if (!cancelled && !controller.signal.aborted && generation === getAuthGeneration()) {
          if (error instanceof ApiError && (error.isAuthError || error.isPermissionError)) {
            // A profile/refresh rejection concerns the session itself. Ordinary
            // resource403s remain permission failures in their own screens.
            clearTokens()
            setUser(null)
            setRestorationError(null)
          } else {
            setRestorationError(error instanceof Error ? error : new Error('Session unavailable'))
          }
        }
      } finally {
        if (!cancelled) setIsRestoring(false)
        if (pendingRestoration.current === controller) pendingRestoration.current = null
      }
    })()

    return () => {
      cancelled = true
      controller.abort()
      if (pendingRestoration.current === controller) pendingRestoration.current = null
    }
  }, [restorationAttempt])

  const retryRestoration = useCallback(() => {
    if (isRestoring) return
    setRestorationError(null)
    setIsRestoring(true)
    setRestorationAttempt((attempt) => attempt + 1)
  }, [isRestoring])

  // The API client clears tokens when a refresh fails. Mirror that into React
  // state so the router redirects instead of rendering a shell with no data.
  useEffect(
    () =>
      onAuthChange((authenticated) => {
        if (!authenticated) {
          pendingRestoration.current?.abort()
          setUser(null)
          setRestorationError(null)
          setIsRestoring(false)
          queryClient.clear()
        }
      }),
    [queryClient],
  )

  const completeLogin = useCallback(
    async (result: BrowserLoginResponse, signal: AbortSignal): Promise<CurrentUser> => {
      signal.throwIfAborted()
      setTokens(result.access_token)
      const generation = getAuthGeneration()
      queryClient.clear()
      // The login payload has no permission set, so the signed-in user is read
      // back from /auth/me. Without this every permission-gated control - the
      // whole sidebar included - stays hidden until the next hard reload.
      try {
        const me = await api.get<MeResponse>('/auth/me', { signal })
        signal.throwIfAborted()
        if (generation !== getAuthGeneration())
          throw new DOMException('The signed-in session changed.', 'AbortError')
        const current = toCurrentUser(me)
        setUser(current)
        return current
      } catch (error) {
        if (signal.aborted || generation !== getAuthGeneration()) throw error
        // A 401 here is not a profile lookup that went wrong - it is a session
        // that is already dead. `/auth/me` got a 401, the API client ran its one
        // transparent refresh, that refresh failed, and it called `clearTokens`.
        // Falling back to the login payload on top of that installs a user the
        // context reports as authenticated while the client holds no token at
        // all: the router lets them into the shell, every query 401s, and the
        // only way out is to find the logout button. The failure has to be
        // re-raised so the login form shows it and the user can try again.
        //
        if (error instanceof ApiError && error.isAuthError) {
          clearTokens()
          setUser(null)
          throw error
        }

        // Authenticated, tokens intact, but the profile could not be resolved -
        // a 5xx or a dropped connection. Fall back to what the token response
        // carried so the redirect below still knows whether the password must be
        // changed. Affordances stay hidden until the next load, which is the safe
        // direction to fail in.
        const fallback: CurrentUser = {
          id: result.user.id,
          email: result.user.email,
          full_name: result.user.full_name,
          company_id: result.user.company_id,
          is_superuser: result.user.is_superuser,
          mfa_enabled: false,
          permissions: [],
          roles: result.user.roles ?? [],
          vendor_id: result.user.vendor_id ?? null,
          customer_id: result.user.customer_id ?? null,
          must_change_password: result.user.must_change_password,
          branch_id: null,
          plant_id: null,
          department_id: null,
          session_id: result.session_id,
          last_login_at: null,
          allowed_warehouse_ids: [],
          allowed_category_ids: [],
        }
        setUser(fallback)
        return fallback
      }
    },
    [queryClient],
  )

  const login = useCallback(
    async (email: string, password: string): Promise<LoginOutcome> => {
      const operation = beginLogin()
      try {
        const result = await api.post<BrowserLoginResponse | MfaChallenge>(
          '/auth/login',
          { email, password },
          { anonymous: true, signal: operation.controller.signal },
        )
        operation.controller.signal.throwIfAborted()
        if (operation.generation !== getAuthGeneration())
          throw new DOMException('The signed-in session changed.', 'AbortError')
        return result.mfa_required
          ? result
          : await completeLogin(result, operation.controller.signal)
      } finally {
        if (pendingAuthentication.current === operation.controller)
          pendingAuthentication.current = null
      }
    },
    [beginLogin, completeLogin],
  )

  const verifyMfa = useCallback(
    async (challengeToken: string, code: string): Promise<CurrentUser> => {
      const operation = beginLogin()
      try {
        const result = await api.post<BrowserLoginResponse>(
          '/auth/mfa/verify',
          { challenge_token: challengeToken, code },
          { anonymous: true, signal: operation.controller.signal },
        )
        operation.controller.signal.throwIfAborted()
        if (operation.generation !== getAuthGeneration())
          throw new DOMException('The signed-in session changed.', 'AbortError')
        return await completeLogin(result, operation.controller.signal)
      } finally {
        if (pendingAuthentication.current === operation.controller)
          pendingAuthentication.current = null
      }
    },
    [beginLogin, completeLogin],
  )

  const logout = useCallback((): Promise<void> => {
    pendingRestoration.current?.abort()
    setRestorationError(null)
    setIsRestoring(false)
    pendingAuthentication.current?.abort()
    pendingAuthentication.current = null
    const generation = getAuthGeneration()
    if (pendingLogout.current?.generation === generation) return pendingLogout.current.promise
    const operation = { generation, controller: new AbortController(), promise: Promise.resolve() }
    operation.promise = (async () => {
      try {
        await withinLogoutBudget(
          async (signal) => {
            const push = await import('@/features/notifications/push')
            // A chunk may finish loading after its caller timed out or another user signed in.
            if (signal.aborted || generation !== getAuthGeneration()) return
            const cancel = () => {
              if (generation === getAuthGeneration()) push.cancelPushSetup()
            }
            signal.addEventListener('abort', cancel, { once: true })
            try {
              await push.unregisterOnLogout()
            } finally {
              signal.removeEventListener('abort', cancel)
            }
          },
          LOGOUT_DEVICE_BUDGET_MS,
          operation.controller.signal,
        )
        if (!operation.controller.signal.aborted && generation === getAuthGeneration()) {
          await withinLogoutBudget(
            (signal) => api.post('/auth/logout', undefined, { signal }),
            LOGOUT_API_BUDGET_MS,
            operation.controller.signal,
          )
        }
      } finally {
        // An old logout cannot clear a newer account that signed in while it was waiting.
        if (generation === getAuthGeneration()) {
          clearTokens()
          setUser(null)
          queryClient.clear()
        }
        if (pendingLogout.current === operation) pendingLogout.current = null
      }
    })()
    pendingLogout.current = operation
    return operation.promise
  }, [queryClient])

  const refreshUser = useCallback(async () => {
    const generation = getAuthGeneration()
    const me = await api.get<MeResponse>('/auth/me')
    if (generation !== getAuthGeneration()) return
    setUser(toCurrentUser(me))
    await queryClient.invalidateQueries({ queryKey: queryKeys.auth.me })
  }, [queryClient])

  const permissionSet = useMemo(() => new Set(user?.permissions ?? []), [user])

  const can = useCallback(
    (permission: string) => Boolean(user?.is_superuser) || permissionSet.has(permission),
    [user, permissionSet],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      isRestoring,
      restorationError,
      retryRestoration,
      login,
      verifyMfa,
      cancelAuthentication,
      logout,
      refreshUser,
      can,
      canAny: (...permissions) => permissions.some(can),
      canAll: (...permissions) => permissions.every(can),
      isVendorUser: Boolean(user?.vendor_id),
      isDealerUser: Boolean(user?.customer_id),
    }),
    [
      user,
      isRestoring,
      restorationError,
      retryRestoration,
      login,
      verifyMfa,
      cancelAuthentication,
      logout,
      refreshUser,
      can,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}

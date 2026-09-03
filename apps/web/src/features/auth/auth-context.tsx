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
  useState,
  type ReactNode,
} from 'react'

import { ApiError, api, clearTokens, hasRefreshToken, onAuthChange, setTokens } from '@/lib/api-client'
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
   * Not returned by any auth endpoint - `/auth/me` carries `company_id` only.
   * Kept optional so the callers that show it degrade to nothing rather than
   * rendering `undefined`; see the report's shared-changes note.
   */
  company_name?: string
  is_superuser: boolean
  mfa_enabled: boolean
  /** Always an array here, even though the API omits an empty one. */
  permissions: string[]
  roles: string[]
  vendor_id: string | null
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
    is_superuser: me.is_superuser,
    mfa_enabled: me.mfa_enabled,
    permissions: me.permissions ?? [],
    roles: me.roles ?? [],
    vendor_id: me.vendor_id ?? null,
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
interface LoginResponse {
  access_token: string
  refresh_token: string
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
  }
}

interface AuthContextValue {
  user: CurrentUser | null
  isAuthenticated: boolean
  /** True only while the initial session restore is in flight. */
  isRestoring: boolean
  login: (email: string, password: string) => Promise<CurrentUser>
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
  can: (permission: string) => boolean
  canAny: (...permissions: string[]) => boolean
  canAll: (...permissions: string[]) => boolean
  isVendorUser: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [isRestoring, setIsRestoring] = useState(hasRefreshToken())
  const queryClient = useQueryClient()

  // Restore the session on a hard reload. The access token is memory-only, so
  // after F5 there is a refresh token and nothing else; /auth/me triggers the
  // transparent refresh inside the API client.
  useEffect(() => {
    let cancelled = false

    if (!hasRefreshToken()) {
      setIsRestoring(false)
      return
    }

    void (async () => {
      try {
        const me = await api.get<MeResponse>('/auth/me')
        if (!cancelled) setUser(toCurrentUser(me))
      } catch (error) {
        // Only an authentication failure ends the session. A 5xx or a dropped
        // connection on the restore call says nothing about whether the refresh
        // token is still good, and discarding it there signs the user out for
        // an API hiccup - they come back to a login form with no explanation
        // and a refresh token that was perfectly valid. The client clears the
        // tokens itself when a refresh genuinely fails, and `onAuthChange`
        // below mirrors that into this state.
        if (error instanceof ApiError && error.isAuthError) {
          clearTokens()
          if (!cancelled) setUser(null)
        }
      } finally {
        if (!cancelled) setIsRestoring(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [])

  // The API client clears tokens when a refresh fails. Mirror that into React
  // state so the router redirects instead of rendering a shell with no data.
  useEffect(
    () =>
      onAuthChange((authenticated) => {
        if (!authenticated) {
          setUser(null)
          queryClient.clear()
        }
      }),
    [queryClient],
  )

  const login = useCallback(async (email: string, password: string): Promise<CurrentUser> => {
    const result = await api.post<LoginResponse>(
      '/auth/login',
      { email, password },
      { anonymous: true },
    )
    setTokens(result.access_token, result.refresh_token)

    // The login payload has no permission set, so the signed-in user is read
    // back from /auth/me. Without this every permission-gated control - the
    // whole sidebar included - stays hidden until the next hard reload.
    try {
      const me = await api.get<MeResponse>('/auth/me')
      const current = toCurrentUser(me)
      setUser(current)
      return current
    } catch (error) {
      // A 401 here is not a profile lookup that went wrong - it is a session
      // that is already dead. `/auth/me` got a 401, the API client ran its one
      // transparent refresh, that refresh failed, and it called `clearTokens`.
      // Falling back to the login payload on top of that installs a user the
      // context reports as authenticated while the client holds no token at
      // all: the router lets them into the shell, every query 401s, and the
      // only way out is to find the logout button. The failure has to be
      // re-raised so the login form shows it and the user can try again.
      //
      // `hasRefreshToken()` is checked as well as the status, because the
      // tokens are what actually decide whether a session exists; a refresh
      // that cleared them behind a differently-shaped error must not slip past.
      if (!hasRefreshToken() || (error instanceof ApiError && error.isAuthError)) {
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
        vendor_id: null,
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
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout')
    } catch {
      // A failed logout call must still sign the user out locally - the session
      // may already be revoked, which is exactly when logging out matters.
    } finally {
      clearTokens()
      setUser(null)
      queryClient.clear()
    }
  }, [queryClient])

  const refreshUser = useCallback(async () => {
    const me = await api.get<MeResponse>('/auth/me')
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
      login,
      logout,
      refreshUser,
      can,
      canAny: (...permissions) => permissions.some(can),
      canAll: (...permissions) => permissions.every(can),
      isVendorUser: Boolean(user?.vendor_id),
    }),
    [user, isRestoring, login, logout, refreshUser, can],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}

/** Convenience for conditional rendering: `const canApprove = usePermission(P.PO_APPROVE)`. */
export function usePermission(permission: string): boolean {
  return useAuth().can(permission)
}

/**
 * The single HTTP boundary between the SPA and the ERP API.
 *
 * Nothing else in the application calls `fetch`. Everything goes through here so
 * that token refresh, the response envelope, error shaping and the correlation
 * id are handled in exactly one place.
 *
 * Token storage
 * -------------
 * The access token lives in memory only. The refresh credential is an HttpOnly,
 * SameSite=Strict cookie set by the API, so JavaScript never receives or stores
 * it. A readable, non-secret CSRF cookie is both the double-submit value for
 * state-changing auth calls and the hint used to restore a session after F5.
 */

import { API_BASE_URL, CSRF_COOKIE_NAME, STORAGE_KEYS } from '@/lib/constants'
import type { ApiEnvelope, PaginatedEnvelope } from '@/types/api'

const BASE_URL = API_BASE_URL
const SESSION_SUPPRESS_KEY = STORAGE_KEYS.sessionSuppressed

/** Messages composed here rather than in lib/messages: that module imports this one. */
const SERVICE_UNAVAILABLE_MESSAGE = 'The service is temporarily unavailable. Please try again.'
const REQUEST_FAILED_MESSAGE = 'Request failed'

// ---------------------------------------------------------------- errors ----

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: Record<string, unknown>,
    readonly requestId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** Field-level messages from a 422, keyed by form field path. */
  get fieldErrors(): Record<string, string[]> {
    const fields = this.details?.fields
    return typeof fields === 'object' && fields !== null ? (fields as Record<string, string[]>) : {}
  }

  get isAuthError(): boolean {
    return this.status === 401
  }

  get isPermissionError(): boolean {
    return this.status === 403
  }

  /** Conflicts and rate limits are worth retrying; a 422 never is. */
  get isRetryable(): boolean {
    return this.status === 429 || this.status >= 500
  }
}

/**
 * For a lookup that is decoration on its screen - labels on a grid, a
 * placement select - fetched under a right that does not imply the lookup's
 * own: a 403 becomes an empty list, and every other failure still propagates,
 * because an empty picker after a 500 reads as "none exist".
 *
 *     catch (error) { return emptyOnPermissionError<Row>(error) }
 */
export function emptyOnPermissionError<T>(error: unknown): T[] {
  if (error instanceof ApiError && error.isPermissionError) return []
  throw error
}

// ----------------------------------------------------------------- tokens ---

let accessToken: string | null = null

function cookieValue(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const found = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
  return found ? decodeURIComponent(found.slice(prefix.length)) : null
}

type AuthListener = (authenticated: boolean) => void
const authListeners = new Set<AuthListener>()

export function onAuthChange(listener: AuthListener): () => void {
  authListeners.add(listener)
  return () => authListeners.delete(listener)
}

export function setTokens(access: string | null): void {
  accessToken = access
  if (access) localStorage.removeItem(SESSION_SUPPRESS_KEY)
  authListeners.forEach((l) => l(Boolean(access)))
}

export function clearTokens(): void {
  accessToken = null
  // JavaScript cannot and must not erase the HttpOnly cookie. This marker stops
  // a failed logout request from silently restoring that cookie on the next
  // reload; a later successful login/refresh clears it.
  localStorage.setItem(SESSION_SUPPRESS_KEY, '1')
  authListeners.forEach((listener) => listener(false))
}

export function hasRefreshToken(): boolean {
  return (
    localStorage.getItem(SESSION_SUPPRESS_KEY) !== '1' && Boolean(cookieValue(CSRF_COOKIE_NAME))
  )
}

// ------------------------------------------------------- refresh handling ---

/**
 * A single in-flight refresh, shared by every request that got a 401.
 *
 * Without this, a dashboard that fires eight parallel queries on load would send
 * eight refresh requests. Because refresh tokens rotate server-side, seven of
 * them would present an already-replaced token - which the API correctly treats
 * as replay and responds to by revoking the entire session chain. The user gets
 * logged out simply for opening a busy page.
 */
let refreshInFlight: Promise<boolean> | null = null

async function refreshAccessToken(): Promise<boolean> {
  if (!hasRefreshToken()) return false

  refreshInFlight ??= (async () => {
    try {
      const response = await fetch(`${BASE_URL}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'X-CSRF-Token': cookieValue(CSRF_COOKIE_NAME) ?? '' },
      })
      if (!response.ok) {
        clearTokens()
        return false
      }
      const body = (await response.json()) as ApiEnvelope<{
        access_token: string
      }>
      if (!body.data?.access_token) {
        clearTokens()
        return false
      }
      setTokens(body.data.access_token)
      return true
    } catch {
      clearTokens()
      return false
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

// ----------------------------------------------------------------- core -----

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  /** Query parameters. Undefined and null values are dropped, arrays repeat. */
  params?: Record<string, unknown>
  /** Skip the bearer header - used only by the login endpoint. */
  anonymous?: boolean
  /** Return the raw Response (file downloads) instead of unwrapping JSON. */
  raw?: boolean
}

function buildUrl(path: string, params?: Record<string, unknown>): string {
  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
  if (!params) return url

  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)))
    else search.append(key, String(value))
  }
  const qs = search.toString()
  return qs ? `${url}?${qs}` : url
}

async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get('X-Request-ID') ?? undefined
  try {
    const body = await response.json()
    const error = body?.error ?? {}
    return new ApiError(
      response.status,
      error.code ?? 'UNKNOWN',
      error.message ?? response.statusText,
      error.details,
      body?.request_id ?? requestId,
    )
  } catch {
    // A non-JSON body means the failure happened before the app (nginx 502,
    // gateway timeout). There is nothing structured to report.
    return new ApiError(
      response.status,
      response.status >= 500 ? 'SERVICE_UNAVAILABLE' : 'UNKNOWN',
      response.status >= 500
        ? SERVICE_UNAVAILABLE_MESSAGE
        : response.statusText || REQUEST_FAILED_MESSAGE,
      undefined,
      requestId,
    )
  }
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const { body, params, anonymous, raw, headers, ...rest } = options

  const send = async (): Promise<Response> => {
    const finalHeaders = new Headers(headers)
    if (!anonymous && accessToken) finalHeaders.set('Authorization', `Bearer ${accessToken}`)
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
      const csrf = cookieValue(CSRF_COOKIE_NAME)
      if (csrf) finalHeaders.set('X-CSRF-Token', csrf)
    }

    let payload: BodyInit | undefined
    if (body instanceof FormData) {
      // Never set Content-Type for FormData: the browser must add the multipart
      // boundary itself.
      payload = body
    } else if (body !== undefined) {
      finalHeaders.set('Content-Type', 'application/json')
      payload = JSON.stringify(body)
    }

    return fetch(buildUrl(path, params), {
      ...rest,
      method,
      credentials: rest.credentials ?? 'include',
      headers: finalHeaders,
      body: payload,
    })
  }

  let response = await send()

  // One transparent refresh-and-retry. The login and refresh endpoints are
  // excluded, since a 401 from those means the credentials are simply wrong.
  if (response.status === 401 && !anonymous && !path.startsWith('/auth/refresh')) {
    if (await refreshAccessToken()) {
      response = await send()
    } else {
      clearTokens()
    }
  }

  if (!response.ok) throw await toApiError(response)
  if (raw) return response as unknown as T
  if (response.status === 204) return undefined as T

  return (await response.json()) as T
}

// --------------------------------------------------------------- surface ----

export const api = {
  /** Unwraps the envelope and returns `data`. */
  async get<T>(path: string, options?: RequestOptions): Promise<T> {
    const envelope = await request<ApiEnvelope<T>>('GET', path, options)
    return envelope.data as T
  },

  /** Keeps the envelope so `pagination` is available to the table. */
  async getPage<T>(path: string, options?: RequestOptions): Promise<PaginatedEnvelope<T>> {
    return request<PaginatedEnvelope<T>>('GET', path, options)
  },

  async post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    const envelope = await request<ApiEnvelope<T>>('POST', path, { ...options, body })
    return envelope.data as T
  },

  async put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    const envelope = await request<ApiEnvelope<T>>('PUT', path, { ...options, body })
    return envelope.data as T
  },

  async patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    const envelope = await request<ApiEnvelope<T>>('PATCH', path, { ...options, body })
    return envelope.data as T
  },

  async delete<T = void>(path: string, options?: RequestOptions): Promise<T> {
    const envelope = await request<ApiEnvelope<T>>('DELETE', path, options)
    return envelope?.data as T
  },

  /**
   * Authenticated file download.
   *
   * The API never exposes filesystem paths (master rules 43), so a download is a
   * normal authenticated request whose body is turned into a temporary object
   * URL rather than an <a href> the browser fetches without a bearer token.
   */
  async download(path: string, filename?: string, options?: RequestOptions): Promise<void> {
    const response = await request<Response>('GET', path, { ...options, raw: true })
    const blob = await response.blob()

    const disposition = response.headers.get('Content-Disposition') ?? ''
    const suggested = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)?.[1]
    const name = filename ?? (suggested ? decodeURIComponent(suggested) : 'download')

    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  },
}

export { BASE_URL }

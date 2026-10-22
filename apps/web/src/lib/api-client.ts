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

import { API_BASE_URL, CSRF_COOKIE_NAME, MAX_PAGE_SIZE, STORAGE_KEYS } from '@/lib/constants'
import { errors } from '@/lib/messages'
import type {
  AcceptedJob,
  ApiEnvelope,
  CursorEnvelope,
  ErrorResponse,
  PaginatedEnvelope,
} from '@/types/api'

const BASE_URL = API_BASE_URL
const SESSION_SUPPRESS_KEY = STORAGE_KEYS.sessionSuppressed

// ---------------------------------------------------------------- errors ----

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: ErrorResponse['error']['code'],
    message: string,
    readonly details?: NonNullable<ErrorResponse['error']['details']>,
    readonly requestId?: NonNullable<ErrorResponse['request_id']>,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** Field-level messages from a 422, keyed by form field path. */
  get fieldErrors(): Record<string, string[]> {
    const fields = this.details?.fields
    if (!isRecord(fields)) return {}
    return Object.fromEntries(
      Object.entries(fields).filter(
        (entry): entry is [string, string[]] =>
          Array.isArray(entry[1]) && entry[1].every((message) => typeof message === 'string'),
      ),
    )
  }

  get isAuthError(): boolean {
    return this.status === 401
  }

  get isPermissionError(): boolean {
    return this.status === 403
  }

  /** Conflicts and rate limits are worth retrying; a 422 never is. */
  get isRetryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500
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
      const body = await readEnvelope(response)
      if (
        !isRecord(body?.data) ||
        typeof body.data.access_token !== 'string' ||
        !body.data.access_token
      ) {
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
    const body: unknown = await response.json()
    if (!isErrorEnvelope(body)) throw invalidResponse(response)
    const error = body.error
    return new ApiError(
      response.status,
      error.code,
      error.message,
      error.details ?? undefined,
      body.request_id ?? requestId,
    )
  } catch (error) {
    if (isAbortError(error)) throw error
    // A proxy or gateway can fail before the app supplies a valid envelope.
    return new ApiError(
      response.status,
      response.status >= 500 ? 'SERVICE_UNAVAILABLE' : 'UNKNOWN',
      response.status >= 500
        ? errors.serviceUnavailable
        : response.statusText || errors.requestFailed,
      undefined,
      requestId,
    )
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isAbortError(error: unknown): boolean {
  return isRecord(error) && error.name === 'AbortError'
}

function isErrorEnvelope(value: unknown): value is ErrorResponse {
  return (
    isRecord(value) &&
    value.success === false &&
    isRecord(value.error) &&
    typeof value.error.code === 'string' &&
    typeof value.error.message === 'string' &&
    (value.error.details == null || isRecord(value.error.details)) &&
    (value.request_id == null || typeof value.request_id === 'string')
  )
}

function isAcceptedJobData(value: unknown): value is AcceptedJob['data'] {
  return (
    isRecord(value) &&
    typeof value.job_id === 'string' &&
    Boolean(value.job_id) &&
    typeof value.status === 'string' &&
    Boolean(value.status) &&
    typeof value.status_url === 'string' &&
    Boolean(value.status_url)
  )
}

function invalidResponse(response: Response): ApiError {
  return new ApiError(
    response.status,
    'INVALID_RESPONSE',
    errors.invalidResponse,
    undefined,
    response.headers.get('X-Request-ID') ?? undefined,
  )
}

/** Validate shared metadata once; resource data remains the caller's generic type. */
async function readEnvelope(response: Response): Promise<ApiEnvelope<unknown> | undefined> {
  if (response.status === 204 || response.status === 205) return undefined
  let value: unknown
  try {
    value = await response.json()
  } catch (error) {
    if (isAbortError(error)) throw error
    throw invalidResponse(response)
  }
  if (
    !isRecord(value) ||
    value.success !== true ||
    !Object.hasOwn(value, 'data') ||
    value.data === undefined ||
    typeof value.message !== 'string' ||
    (response.status === 202 && !isAcceptedJobData(value.data))
  ) {
    throw invalidResponse(response)
  }
  return value as ApiEnvelope<unknown>
}

function isNonnegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

function isPageSize(value: unknown): boolean {
  return isNonnegativeInteger(value) && value >= 1 && value <= MAX_PAGE_SIZE
}

async function readPage<T, Cursor extends boolean>(
  response: Response,
  cursor: Cursor,
): Promise<Cursor extends true ? CursorEnvelope<T> : PaginatedEnvelope<T>> {
  if (response.status === 202) throw invalidResponse(response)
  const envelope = await readEnvelope(response)
  const pagination = envelope && 'pagination' in envelope ? envelope.pagination : undefined
  if (
    !envelope ||
    !Array.isArray(envelope.data) ||
    !isRecord(pagination) ||
    !isPageSize(pagination.limit) ||
    typeof pagination.has_next !== 'boolean' ||
    (cursor
      ? pagination.page !== undefined ||
        (pagination.next_cursor != null && typeof pagination.next_cursor !== 'string') ||
        (pagination.has_next && !pagination.next_cursor)
      : !isNonnegativeInteger(pagination.page) ||
        pagination.page < 1 ||
        typeof pagination.has_previous !== 'boolean' ||
        (pagination.total != null && !isNonnegativeInteger(pagination.total)) ||
        (pagination.total_pages != null && !isNonnegativeInteger(pagination.total_pages)))
  ) {
    throw invalidResponse(response)
  }
  return envelope as Cursor extends true ? CursorEnvelope<T> : PaginatedEnvelope<T>
}

async function requestData<T>(method: string, path: string, options?: RequestOptions): Promise<T> {
  const envelope = await readEnvelope(await request(method, path, options))
  return envelope?.data as T
}

async function request(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<Response> {
  const { body, params, anonymous, headers, ...rest } = options

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

    try {
      return await fetch(buildUrl(path, params), {
        ...rest,
        method,
        credentials: rest.credentials ?? 'include',
        headers: finalHeaders,
        body: payload,
      })
    } catch (error) {
      // Cancellation belongs to the caller/query lifecycle, rather than an outage notice.
      if (isAbortError(error)) throw error
      const failure = new ApiError(
        0,
        'NETWORK_ERROR',
        navigator.onLine === false ? errors.networkOffline : errors.networkFailed,
      )
      failure.cause = error
      throw failure
    }
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
  return response
}

// --------------------------------------------------------------- surface ----

export const api = {
  /** Unwraps the envelope and returns `data`. */
  async get<T>(path: string, options?: RequestOptions): Promise<T> {
    return requestData<T>('GET', path, options)
  },

  /** Keeps the envelope so `pagination` is available to the table. */
  async getPage<T>(path: string, options?: RequestOptions): Promise<PaginatedEnvelope<T>> {
    return readPage<T, false>(await request('GET', path, options), false)
  },

  /** A report can return inline rows or escalate to a background job with HTTP 202. */
  async getPageOrAccepted<T>(
    path: string,
    options?: RequestOptions,
  ): Promise<PaginatedEnvelope<T> | AcceptedJob> {
    const response = await request('GET', path, options)
    if (response.status !== 202) return readPage<T, false>(response, false)
    const envelope = await readEnvelope(response)
    if (!envelope || 'pagination' in envelope) throw invalidResponse(response)
    return envelope as AcceptedJob
  },

  /** Keeps cursor pagination for append-heavy lists. */
  async getCursorPage<T>(path: string, options?: RequestOptions): Promise<CursorEnvelope<T>> {
    return readPage<T, true>(await request('GET', path, options), true)
  },

  async post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return requestData<T>('POST', path, { ...options, body })
  },

  async put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return requestData<T>('PUT', path, { ...options, body })
  },

  async patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return requestData<T>('PATCH', path, { ...options, body })
  },

  async delete<T = void>(path: string, options?: RequestOptions): Promise<T> {
    return requestData<T>('DELETE', path, options)
  },

  /**
   * Authenticated file download.
   *
   * The API never exposes filesystem paths (master rules 43), so a download is a
   * normal authenticated request whose body is turned into a temporary object
   * URL rather than an <a href> the browser fetches without a bearer token.
   */
  async download(path: string, filename?: string, options?: RequestOptions): Promise<void> {
    const response = await request('GET', path, options)
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

  /**
   * Authenticated object URL for inline display.
   *
   * The thumbnail twin of {@link download}: inspection photos are fetched
   * through `GET /documents/{id}/download` as blobs (never as public URLs)
   * and shown in `<img>` tags. The caller revokes the URL when it unmounts.
   */
  async blobUrl(path: string, options?: RequestOptions): Promise<string> {
    const response = await request('GET', path, options)
    return URL.createObjectURL(await response.blob())
  },
}

export { BASE_URL }

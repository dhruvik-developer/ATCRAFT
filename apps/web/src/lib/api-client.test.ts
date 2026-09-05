/**
 * `lib/api-client` is the only place in the SPA that calls `fetch`, which makes
 * it the only place these properties can be tested — and the only place a
 * regression in them is invisible to TypeScript.
 *
 * The property that earns most of this file: **`api.get` / `api.post` /
 * `api.patch` / `api.delete` return `envelope.data`, already unwrapped, while
 * `api.getPage` keeps the envelope so the table can read `pagination`.**
 *
 * That asymmetry is easy to get wrong in a way the compiler cannot see. Reading
 * `result.data.x` off an already-unwrapped `api.post` compiles perfectly against
 * the generated envelope schema — the generic parameter says the shape is there —
 * and is `undefined` in every browser. That exact defect shipped: a report job's
 * `status_url` was read as `accepted.data?.status_url`, type-checked clean, and
 * silently never polled. Optional chaining is what made it silent.
 *
 * Also pinned here: the shape of `ApiError`, because form error handling and
 * every toast in the app branch on it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, clearTokens, hasRefreshToken, setTokens } from './api-client'

/** Build a `Response`-alike carrying the API's standard success envelope. */
function envelope(data: unknown, extra: Record<string, unknown> = {}) {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'X-Request-ID': 'req-1' }),
    json: async () => ({ success: true, data, request_id: 'req-1', ...extra }),
  } as unknown as Response
}

function failure(status: number, body: unknown) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    headers: new Headers({ 'X-Request-ID': 'req-err' }),
    json: async () => body,
  } as unknown as Response
}

const fetchMock = vi.fn()

/** The nth `fetch` call, asserted to exist.

 `noUncheckedIndexedAccess` makes `mock.calls[n]` possibly-undefined, which is
 correct: reading it is an assumption that the call happened. Asserting it here
 turns "the request was never made" into that sentence, rather than into a
 `Cannot read properties of undefined` several lines later. */
function callAt(index: number): [string, RequestInit] {
  const call = fetchMock.mock.calls[index]
  expect(call, `expected at least ${index + 1} fetch call(s)`).toBeDefined()
  return call as [string, RequestInit]
}

/** The error a rejected request threw, typed. */
async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => null,
    (e: unknown) => e,
  )
  expect(error, 'expected the request to reject').toBeInstanceOf(ApiError)
  return error as ApiError
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  localStorage.clear()
  document.cookie = 'atcraft_csrf=; Max-Age=0; Path=/'
  clearTokens()
})

afterEach(() => {
  vi.unstubAllGlobals()
  clearTokens()
  document.cookie = 'atcraft_csrf=; Max-Age=0; Path=/'
  localStorage.clear()
})

describe('envelope unwrapping', () => {
  it('api.get returns data, not the envelope', async () => {
    fetchMock.mockResolvedValueOnce(envelope({ id: 'i1', code: 'ITEM-1' }))
    const item = await api.get<{ id: string; code: string }>('/products/items/i1')
    expect(item).toEqual({ id: 'i1', code: 'ITEM-1' })
    // The whole point: there is no second `.data` to reach through.
    expect((item as unknown as { data?: unknown }).data).toBeUndefined()
  })

  it('api.post returns data, so a caller must NOT reach through .data', async () => {
    // This is the shipped defect, pinned. `accepted.data?.status_url` here is
    // `undefined` and optional chaining swallows it, so the job is never polled
    // and nothing anywhere reports a failure.
    fetchMock.mockResolvedValueOnce(envelope({ job_id: 'j1', status_url: '/jobs/j1' }))
    const accepted = await api.post<{ job_id: string; status_url: string }>('/reports/export')
    expect(accepted.status_url).toBe('/jobs/j1')
    expect((accepted as unknown as { data?: unknown }).data).toBeUndefined()
  })

  it('api.patch and api.delete unwrap the same way', async () => {
    fetchMock.mockResolvedValueOnce(envelope({ id: 'x', version: 2 }))
    await expect(api.patch<{ version: number }>('/x/1', { a: 1 })).resolves.toEqual({
      id: 'x',
      version: 2,
    })
    fetchMock.mockResolvedValueOnce(envelope({ deleted: true }))
    await expect(api.delete<{ deleted: boolean }>('/x/1')).resolves.toEqual({ deleted: true })
  })

  it('api.getPage KEEPS the envelope, because the table needs pagination', async () => {
    fetchMock.mockResolvedValueOnce(
      envelope([{ id: 'a' }], { pagination: { page: 1, limit: 50, total: 1 } }),
    )
    const page = await api.getPage<{ id: string }>('/products/items')
    expect(page.data).toEqual([{ id: 'a' }])
    expect(page.pagination).toEqual({ page: 1, limit: 50, total: 1 })
  })

  it('a 204 resolves to undefined rather than throwing on an empty body', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 204,
      headers: new Headers(),
      json: async () => {
        throw new Error('no body')
      },
    } as unknown as Response)
    await expect(api.delete('/x/1')).resolves.toBeUndefined()
  })
})

describe('request construction', () => {
  it('prefixes every path with the API base', async () => {
    fetchMock.mockResolvedValueOnce(envelope(null))
    await api.get('/products/items')
    expect(String(callAt(0)[0])).toContain('/api/v1/products/items')
  })

  it('serialises params onto the query string', async () => {
    fetchMock.mockResolvedValueOnce(envelope([]))
    await api.getPage('/products/items', { params: { page: 2, search: 'brass hinge' } })
    const url = String(callAt(0)[0])
    expect(url).toContain('page=2')
    expect(url).toContain('search=brass+hinge')
  })

  it('sends the bearer token once one is set, and stops after clearTokens', async () => {
    setTokens('access-1')
    fetchMock.mockResolvedValueOnce(envelope(null))
    await api.get('/x')
    expect(new Headers(callAt(0)[1].headers).get('authorization')).toBe('Bearer access-1')

    clearTokens()
    fetchMock.mockResolvedValueOnce(envelope(null))
    await api.get('/x')
    expect(new Headers(callAt(1)[1].headers).get('authorization')).toBeNull()
  })

  it('never sets Content-Type for FormData, so the browser can add the boundary', async () => {
    // Setting it by hand produces a multipart body with no boundary, which the
    // server rejects as malformed - and only for uploads, so it survives review.
    fetchMock.mockResolvedValueOnce(envelope({ id: 'd1' }))
    const form = new FormData()
    form.append('file', new Blob(['x']), 'x.pdf')
    await api.post('/documents', form)
    expect(new Headers(callAt(0)[1].headers).get('content-type')).toBeNull()
  })

  it('JSON-encodes a plain object body and declares it', async () => {
    fetchMock.mockResolvedValueOnce(envelope(null))
    await api.post('/x', { code: 'A' })
    const init = callAt(0)[1]
    expect(new Headers(init.headers).get('content-type')).toBe('application/json')
    expect(init.body).toBe('{"code":"A"}')
  })

  it('sends cookies and the double-submit CSRF value on mutations', async () => {
    document.cookie = 'atcraft_csrf=csrf-value; Path=/'
    fetchMock.mockResolvedValueOnce(envelope(null))

    await api.post('/auth/logout')

    expect(callAt(0)[1].credentials).toBe('include')
    expect(new Headers(callAt(0)[1].headers).get('x-csrf-token')).toBe('csrf-value')
  })
})

describe('cookie-backed refresh', () => {
  it('rotates through the HttpOnly cookie without sending a refresh token in JSON', async () => {
    document.cookie = 'atcraft_csrf=csrf-value; Path=/'
    localStorage.removeItem('atcraft.session_suppressed')
    fetchMock
      .mockResolvedValueOnce(failure(401, { error: { code: 'TOKEN_EXPIRED', message: 'Expired' } }))
      .mockResolvedValueOnce(envelope({ access_token: 'new-access' }))
      .mockResolvedValueOnce(envelope({ id: 'ok' }))

    await expect(api.get<{ id: string }>('/protected')).resolves.toEqual({ id: 'ok' })

    const refreshCall = callAt(1)
    expect(String(refreshCall[0])).toContain('/auth/refresh')
    expect(refreshCall[1].credentials).toBe('include')
    expect(refreshCall[1].body).toBeUndefined()
    expect(new Headers(refreshCall[1].headers).get('x-csrf-token')).toBe('csrf-value')
    expect(JSON.stringify(localStorage)).not.toContain('new-access')
    expect(new Headers(callAt(2)[1].headers).get('authorization')).toBe('Bearer new-access')
  })
})

describe('ApiError', () => {
  it('carries the API error code, message and request id', async () => {
    fetchMock.mockResolvedValueOnce(
      failure(409, {
        error: { code: 'CONFLICT', message: 'Version mismatch' },
        request_id: 'req-9',
      }),
    )
    const error = await rejection(api.get('/x'))
    expect(error.status).toBe(409)
    expect(error.code).toBe('CONFLICT')
    expect(error.message).toBe('Version mismatch')
    expect(error.requestId).toBe('req-9')
  })

  it('exposes 422 field errors for the form layer', async () => {
    fetchMock.mockResolvedValueOnce(
      failure(422, {
        error: {
          code: 'VALIDATION_ERROR',
          message: 'Invalid',
          details: { fields: { code: ['already used'] } },
        },
      }),
    )
    const error = await rejection(api.post('/x', {}))
    expect(error.fieldErrors).toEqual({ code: ['already used'] })
  })

  it('reports an empty field map rather than undefined when there are no fields', async () => {
    // Form code spreads this; `undefined` would throw at the point of use.
    fetchMock.mockResolvedValueOnce(failure(400, { error: { code: 'BAD', message: 'no' } }))
    const error = await rejection(api.get('/x'))
    expect(error.fieldErrors).toEqual({})
  })

  it('classifies auth, permission and retryable failures', () => {
    expect(new ApiError(401, 'X', 'm').isAuthError).toBe(true)
    expect(new ApiError(403, 'X', 'm').isPermissionError).toBe(true)
    expect(new ApiError(429, 'X', 'm').isRetryable).toBe(true)
    expect(new ApiError(503, 'X', 'm').isRetryable).toBe(true)
    // A 422 is the user's input being wrong. Retrying it unchanged is pointless
    // and, on a create endpoint, risks a duplicate once the input is corrected.
    expect(new ApiError(422, 'X', 'm').isRetryable).toBe(false)
  })

  it('degrades to a service message when the body is not JSON at all', async () => {
    // A 502 from nginx never reaches the app, so there is no envelope to read.
    // Surfacing the parse failure instead of a service message would show the
    // user a JSON error for what is really an outage.
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      headers: new Headers(),
      json: async () => {
        throw new SyntaxError('Unexpected token <')
      },
    } as unknown as Response)
    const error = await rejection(api.get('/x'))
    expect(error.code).toBe('SERVICE_UNAVAILABLE')
    expect(error.message).toMatch(/temporarily unavailable/i)
  })
})

describe('token storage', () => {
  it('keeps both credentials out of browser-readable storage', () => {
    document.cookie = 'atcraft_csrf=non-secret-session-hint; Path=/'
    localStorage.removeItem('atcraft.session_suppressed')
    setTokens('access-1')
    const stored = JSON.stringify(window.localStorage)
    expect(stored).not.toContain('access-1')
    expect(stored).not.toContain('refresh')
    expect(hasRefreshToken()).toBe(true)

    clearTokens()
    expect(hasRefreshToken()).toBe(false)
  })
})

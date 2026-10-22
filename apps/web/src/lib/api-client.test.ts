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
import { errors } from './messages'

/** Build a `Response`-alike carrying the API's standard success envelope. */
function envelope(data: unknown, extra: Record<string, unknown> = {}) {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'X-Request-ID': 'req-1' }),
    json: async () => ({
      success: true,
      data,
      message: 'Operation completed successfully',
      ...extra,
    }),
  } as unknown as Response
}

function failure(status: number, body: unknown) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    headers: new Headers({ 'X-Request-ID': 'req-err' }),
    json: async () => ({ success: false, ...(body as Record<string, unknown>) }),
  } as unknown as Response
}

function jsonSuccess(body: unknown) {
  return { ...envelope(null), json: async () => body } as Response
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
  vi.restoreAllMocks()
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

  it('api.put, api.patch and api.delete unwrap the same way', async () => {
    fetchMock.mockResolvedValueOnce(envelope({ id: 'x', version: 1 }))
    await expect(api.put<{ version: number }>('/x/1', { a: 1 })).resolves.toEqual({
      id: 'x',
      version: 1,
    })
    fetchMock.mockResolvedValueOnce(envelope({ id: 'x', version: 2 }))
    await expect(api.patch<{ version: number }>('/x/1', { a: 1 })).resolves.toEqual({
      id: 'x',
      version: 2,
    })
    fetchMock.mockResolvedValueOnce(envelope({ deleted: true }))
    await expect(api.delete<{ deleted: boolean }>('/x/1')).resolves.toEqual({ deleted: true })
  })

  it('api.getPage KEEPS the envelope, because the table needs pagination', async () => {
    const pagination = { page: 1, limit: 50, total: 1, has_next: false, has_previous: false }
    fetchMock.mockResolvedValueOnce(envelope([{ id: 'a' }], { pagination }))
    const page = await api.getPage<{ id: string }>('/products/items')
    expect(page.data).toEqual([{ id: 'a' }])
    expect(page.pagination).toEqual(pagination)
  })

  it.each([204, 205])(
    'a %i consistently resolves to undefined for every data method',
    async (status) => {
      const json = vi.fn(async () => {
        throw new Error('no body')
      })
      fetchMock.mockResolvedValue({
        ok: true,
        status,
        headers: new Headers(),
        json,
      } as unknown as Response)
      await expect(api.get<void>('/x/1')).resolves.toBeUndefined()
      await expect(api.post<void>('/x/1')).resolves.toBeUndefined()
      await expect(api.put<void>('/x/1')).resolves.toBeUndefined()
      await expect(api.patch<void>('/x/1')).resolves.toBeUndefined()
      await expect(api.delete('/x/1')).resolves.toBeUndefined()
      expect(json).not.toHaveBeenCalled()
    },
  )

  it('preserves explicit null data for nullable resource responses', async () => {
    fetchMock.mockResolvedValueOnce(envelope(null))
    await expect(api.get<null>('/x/optional')).resolves.toBeNull()
  })
})

describe('response contract validation', () => {
  it.each([
    null,
    [],
    { id: 'unwrapped' },
    { success: true, message: 'Missing data' },
    { success: true, data: undefined, message: 'Undefined data' },
    { success: true, data: null },
    { success: true, data: null, message: null },
    { success: 'true', data: null, message: 'Invalid flag' },
    { success: false, error: { code: 'FAILURE', message: 'Invalid success status' } },
  ])('rejects malformed successful JSON: %j', async (body) => {
    fetchMock.mockResolvedValueOnce(jsonSuccess(body))
    const error = await rejection(api.get('/x'))
    expect(error.code).toBe('INVALID_RESPONSE')
    expect(error.requestId).toBe('req-1')
    expect(error.message).toMatch(/invalid response/i)
  })

  it('reports invalid JSON on a successful status as an API error', async () => {
    fetchMock.mockResolvedValueOnce({
      ...envelope(null),
      json: async () => {
        throw new SyntaxError('Unexpected end of JSON input')
      },
    })
    expect((await rejection(api.post('/x'))).code).toBe('INVALID_RESPONSE')
  })

  it.each([
    { data: {} },
    { pagination: undefined },
    { pagination: { page: 1, limit: 50 } },
    { pagination: { page: 0, limit: 50, has_next: false, has_previous: false } },
    { pagination: { page: 1, limit: 201, has_next: false, has_previous: false } },
    { pagination: { page: 1.5, limit: 50, has_next: false, has_previous: false } },
    { pagination: { page: 1, limit: 50, has_next: false, has_previous: false, total: -1 } },
    { pagination: { limit: 50, next_cursor: null, has_next: false } },
  ])('rejects malformed offset list metadata: %j', async (extra) => {
    fetchMock.mockResolvedValueOnce(
      envelope([], {
        pagination: { page: 1, limit: 50, has_next: false, has_previous: false },
        ...extra,
      }),
    )
    expect((await rejection(api.getPage('/x'))).code).toBe('INVALID_RESPONSE')
  })

  it('keeps validated cursor pagination without pretending it is offset pagination', async () => {
    const pagination = { limit: 50, next_cursor: 'cursor-2', has_next: true }
    fetchMock.mockResolvedValueOnce(envelope([{ id: 'a' }], { pagination }))
    const result = await api.getCursorPage<{ id: string }>('/audit/logs')
    expect(result.data).toEqual([{ id: 'a' }])
    expect(result.pagination).toEqual(pagination)
    expect(result.message).toBe('Operation completed successfully')
  })

  it('keeps ordinary report rows paginated on HTTP 200', async () => {
    const pagination = { page: 1, limit: 50, has_next: false, has_previous: false }
    fetchMock.mockResolvedValueOnce(envelope([{ id: 'a' }], { pagination }))
    await expect(api.getPageOrAccepted('/reports/stock-availability')).resolves.toMatchObject({
      data: [{ id: 'a' }],
      pagination,
    })
  })

  it('accepts an explicitly permitted HTTP 202 report job without pagination', async () => {
    const data = { job_id: 'job-1', status: 'queued', status_url: '/api/v1/reports/jobs/job-1' }
    fetchMock.mockResolvedValueOnce({ ...envelope(data), status: 202 })
    await expect(api.getPageOrAccepted('/reports/stock-availability')).resolves.toMatchObject({
      success: true,
      data,
    })
  })

  it.each([
    {},
    [],
    { job_id: 'job-1', status: 'queued', status_url: 123 },
    { job_id: 'job-1', status: 'queued', status_url: '' },
    { job_id: '', status: 'queued', status_url: '/api/v1/reports/jobs/job-1' },
    { job_id: 'job-1', status_url: '/api/v1/reports/jobs/job-1' },
    { job_id: 'job-1', status: 123, status_url: '/api/v1/reports/jobs/job-1' },
  ])('rejects malformed HTTP 202 report job data: %j', async (data) => {
    fetchMock.mockResolvedValueOnce({ ...envelope(data), status: 202 })
    expect((await rejection(api.getPageOrAccepted('/reports/stock-availability'))).code).toBe(
      'INVALID_RESPONSE',
    )
  })

  it('requires an accepted report job to use HTTP 202', async () => {
    fetchMock.mockResolvedValueOnce(
      envelope({ job_id: 'job-1', status_url: '/api/v1/reports/jobs/job-1' }),
    )
    expect((await rejection(api.getPageOrAccepted('/reports/stock-availability'))).code).toBe(
      'INVALID_RESPONSE',
    )
  })

  it('keeps ordinary list requests strict when a server returns a job', async () => {
    fetchMock.mockResolvedValueOnce({
      ...envelope({ job_id: 'job-1', status: 'queued', status_url: '/api/v1/reports/jobs/job-1' }),
      status: 202,
    })
    expect((await rejection(api.getPage('/x'))).code).toBe('INVALID_RESPONSE')
  })

  it('validates HTTP 202 job data for unwrapped mutation responses too', async () => {
    fetchMock.mockResolvedValueOnce({ ...envelope({ job_id: 'job-1' }), status: 202 })
    expect((await rejection(api.post('/reports/export'))).code).toBe('INVALID_RESPONSE')
  })

  it.each([
    { limit: 50, next_cursor: null, has_next: true },
    { limit: 50, next_cursor: 123, has_next: true },
    { limit: 0, next_cursor: null, has_next: false },
    { page: 1, limit: 50, has_next: false, has_previous: false },
  ])('rejects malformed cursor list metadata: %j', async (pagination) => {
    fetchMock.mockResolvedValueOnce(envelope([], { pagination }))
    expect((await rejection(api.getCursorPage('/audit/logs'))).code).toBe('INVALID_RESPONSE')
  })

  it('rejects no-content responses for list requests that require pagination', async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 204, headers: new Headers() })
    expect((await rejection(api.getPage('/x'))).code).toBe('INVALID_RESPONSE')
    expect((await rejection(api.getCursorPage('/x'))).code).toBe('INVALID_RESPONSE')
  })

  it('lets binary responses bypass JSON parsing through the blob helper', async () => {
    const blob = new Blob(['image'], { type: 'image/png' })
    const json = vi.fn()
    const createObjectURL = vi.fn(() => 'blob:test')
    vi.stubGlobal('URL', { createObjectURL })
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      blob: async () => blob,
      json,
    })
    await expect(api.blobUrl('/documents/1/download')).resolves.toBe('blob:test')
    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(json).not.toHaveBeenCalled()
  })
})

describe('request construction', () => {
  it('prefixes every path with the API base', async () => {
    fetchMock.mockResolvedValueOnce(envelope(null))
    await api.get('/products/items')
    expect(String(callAt(0)[0])).toContain('/api/v1/products/items')
  })

  it('serialises params onto the query string', async () => {
    fetchMock.mockResolvedValueOnce(
      envelope([], {
        pagination: { page: 2, limit: 50, has_next: false, has_previous: true },
      }),
    )
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

describe('transport errors', () => {
  it('uses a consistent, retryable API error for connection failures', async () => {
    const cause = new TypeError('Failed to fetch')
    fetchMock.mockRejectedValueOnce(cause)
    const error = await rejection(api.get('/x'))
    expect(error.status).toBe(0)
    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.message).toBe(errors.networkFailed)
    expect(error.isRetryable).toBe(true)
    expect(error.cause).toBe(cause)
  })

  it('gives an offline recovery message when the browser reports no connection', async () => {
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect((await rejection(api.post('/x'))).message).toBe(errors.networkOffline)
  })

  it('preserves cancellation rather than reporting a network outage', async () => {
    const error = new DOMException('The operation was aborted.', 'AbortError')
    fetchMock.mockRejectedValueOnce(error)
    await expect(api.get('/x')).rejects.toBe(error)
  })

  it.each([200, 409])(
    'preserves cancellation while reading an HTTP %i response body',
    async (status) => {
      const error = new DOMException('The operation was aborted.', 'AbortError')
      fetchMock.mockResolvedValueOnce({
        ...envelope(null),
        ok: status < 400,
        status,
        json: async () => {
          throw error
        },
      })
      await expect(api.get('/x')).rejects.toBe(error)
    },
  )
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

  it.each([
    { success: false, data: { access_token: 'bad-access' }, message: 'Failed' },
    { success: true, data: { access_token: 123 }, message: 'Invalid token type' },
    { success: true, data: { access_token: '' }, message: 'Empty token' },
    { success: true, data: null, message: 'Missing token' },
  ])('rejects malformed refresh data and clears the session: %j', async (body) => {
    document.cookie = 'atcraft_csrf=csrf-value; Path=/'
    setTokens('old-access')
    fetchMock
      .mockResolvedValueOnce(failure(401, { error: { code: 'TOKEN_EXPIRED', message: 'Expired' } }))
      .mockResolvedValueOnce(jsonSuccess(body))

    expect((await rejection(api.get('/protected'))).code).toBe('TOKEN_EXPIRED')
    expect(hasRefreshToken()).toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(2)
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

  it('exposes only string-array field messages to the form layer', () => {
    const error = new ApiError(422, 'VALIDATION_ERROR', 'Invalid', {
      fields: { code: ['already used'], quantity: 'incorrect shape', warehouse: [123] },
    })
    expect(error.fieldErrors).toEqual({ code: ['already used'] })
  })

  it.each([
    { error: { code: 123, message: 'Unexpected detail' } },
    { error: { code: 'BROKEN', message: { sql: 'private' } } },
    { error: { code: 'BROKEN', message: 'Unexpected detail', details: [] } },
    { error: { code: 'BROKEN', message: 'Unexpected detail' }, request_id: {} },
    { success: true, error: { code: 'BROKEN', message: 'Unexpected detail' } },
    { detail: 'A proxy error' },
  ])('uses a safe fallback for malformed JSON errors: %j', async (body) => {
    fetchMock.mockResolvedValueOnce(failure(502, body))
    const error = await rejection(api.get('/x'))
    expect(error.code).toBe('SERVICE_UNAVAILABLE')
    expect(error.message).toMatch(/temporarily unavailable/i)
    expect(error.requestId).toBe('req-err')
    expect(error.details).toBeUndefined()
  })

  it('uses the correlation header when an error body has a null request id', async () => {
    fetchMock.mockResolvedValueOnce(
      failure(409, {
        error: { code: 'CONFLICT', message: 'Version mismatch', details: null },
        request_id: null,
      }),
    )
    const error = await rejection(api.get('/x'))
    expect(error.requestId).toBe('req-err')
    expect(error.details).toBeUndefined()
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

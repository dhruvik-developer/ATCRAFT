/**
 * Shared harness for screen tests.
 *
 * Two decisions shape everything here, and both are deliberate.
 *
 * **1. The real `AuthProvider` runs, driven by a stubbed session restore.**
 * `AuthContext` is not exported, and it should not be: exporting a context
 * purely so tests can inject a fake value means the tests stop exercising the
 * thing that actually decides what a user may see. Instead `renderScreen` seeds
 * a refresh token and answers `GET /auth/me` with whatever permissions the test
 * asks for, so the provider populates itself exactly as it does after a reload.
 * Permission gating is then *real* in every test - which matters, because six
 * authorisation defects have shipped in this codebase and every one of them
 * would have passed a test that stubbed `can()` to return true.
 *
 * **2. `fetch` is stubbed, not the `api` module.** `lib/api-client` is the only
 * place the app calls `fetch`, and it is where the response envelope is
 * unwrapped, errors are shaped and the token is attached. Mocking `api.get`
 * would skip all of it and let a test pass against a response shape the server
 * never sends. Routing at the `fetch` boundary means a screen test fails if the
 * envelope is wrong, which is the failure worth catching.
 *
 * Usage::
 *
 *     renderScreen(<ItemsPage />, {
 *       permissions: [P.ITEM_VIEW],
 *       routes: { 'GET /products/items': page([{ id: '1', code: 'A' }]) },
 *     })
 *     expect(await screen.findByText('A')).toBeVisible()
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { expect, vi } from 'vitest'

import { AuthProvider } from '@/features/auth/auth-context'
import { setTokens } from '@/lib/api-client'

// ----------------------------------------------------------------- routing --

/** What a handler is told about the request it is answering. */
export interface RouteRequest {
  url: URL
  body: unknown
  method: string
}

/** A response body, as `page()`, `ok()` and `fail()` build it. */
export type RouteBody = Record<string, unknown>

/**
 * A handler: a canned body, or a function of the request.
 *
 * Written as `RouteBody | RouteResponder` rather than `unknown | ...` because
 * TypeScript collapses a union with `unknown` to `unknown`, which silently
 * removes the contextual type from a function handler's parameter - every
 * `({ url }) => ...` then errors with an implicit `any` under `strict`.
 */
export type RouteResponder = (info: RouteRequest) => RouteBody | Promise<RouteBody>
export type RouteHandler = RouteBody | RouteResponder

/** `"GET /products/items"` -> handler. Paths are matched after `/api/v1`. */
export type RouteTable = Record<string, RouteHandler>

/** Wrap rows in the paginated envelope `api.getPage` expects. */
export function page<T>(rows: T[], pagination: Record<string, unknown> = {}): RouteBody {
  return {
    success: true,
    data: rows,
    pagination: { page: 1, limit: 50, total: rows.length, pages: 1, ...pagination },
    request_id: 'test',
  }
}

/** Wrap a single object in the plain envelope `api.get`/`api.post` unwrap. */
export function ok<T>(data: T): RouteBody {
  return { success: true, data, request_id: 'test' }
}

/** An error body in the shape `toApiError` parses. */
export function fail(
  status: number,
  code: string,
  message: string,
  details?: unknown,
): RouteBody {
  return { __status: status, success: false, error: { code, message, details }, request_id: 'test' }
}

/** The signed-in user `/auth/me` returns. */
export function meResponse(
  permissions: string[],
  overrides: Record<string, unknown> = {},
): RouteBody {
  return ok({
    user_id: '01a00000-0000-7000-8000-000000000001',
    company_id: '01a00000-0000-7000-8000-0000000000c0',
    email: 'tester@atcraft.example',
    full_name: 'Test User',
    is_superuser: false,
    permissions,
    role_codes: [],
    ...overrides,
  })
}

// ------------------------------------------------------------------- fetch --

const PREFIX = '/api/v1'

function normalise(pathname: string): string {
  const rest = pathname.startsWith(PREFIX) ? pathname.slice(PREFIX.length) : pathname
  return rest.replace(/\/$/, '') || '/'
}

/** Longest-prefix match, so `GET /products/items` also serves `/products/items/1`. */
function lookup(table: RouteTable, method: string, path: string): RouteHandler | undefined {
  const exact = table[`${method} ${path}`]
  if (exact !== undefined) return exact
  const candidates = Object.keys(table)
    .filter((key) => key.startsWith(`${method} `))
    .map((key) => ({ key, path: key.slice(method.length + 1) }))
    .filter((entry) => path.startsWith(entry.path))
    .sort((a, b) => b.path.length - a.path.length)
  return candidates.length ? table[candidates[0]!.key] : undefined
}

export interface FetchRecorder {
  /** Every request the screen made, in order. */
  calls: { method: string; path: string; body: unknown }[]
  /** Assert a request was made, and return its body. */
  requestFor: (method: string, path: string) => unknown
}

function installFetch(table: RouteTable, unmatched: 'throw' | 'empty'): FetchRecorder {
  const calls: FetchRecorder['calls'] = []

  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init?.method ?? 'GET').toUpperCase()
    const path = normalise(url.pathname)
    let body: unknown
    if (typeof init?.body === 'string') {
      try {
        body = JSON.parse(init.body)
      } catch {
        body = init.body
      }
    }
    calls.push({ method, path, body })

    const handler = lookup(table, method, path)
    if (handler === undefined) {
      if (unmatched === 'throw') {
        // Louder than a 404: an unrouted call means the screen asked for
        // something the test did not anticipate, and a silent empty list would
        // let it pass while rendering nothing.
        throw new Error(
          `screen made an unrouted request: ${method} ${path}\n` +
            `routed: ${Object.keys(table).join(', ') || '(none)'}`,
        )
      }
      return jsonResponse(200, page([]))
    }

    const payload = typeof handler === 'function' ? await handler({ url, body, method }) : handler
    const status =
      payload && typeof payload === 'object' && '__status' in payload
        ? Number((payload as { __status: number }).__status)
        : 200
    return jsonResponse(status, payload)
  })

  vi.stubGlobal('fetch', impl)

  return {
    calls,
    requestFor(method, path) {
      const call = calls.find((c) => c.method === method.toUpperCase() && c.path === path)
      expect(
        call,
        `expected a ${method.toUpperCase()} ${path}; saw ${
          calls.map((c) => `${c.method} ${c.path}`).join(', ') || 'no requests'
        }`,
      ).toBeDefined()
      return call!.body
    },
  }
}

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status < 400,
    status,
    statusText: status < 400 ? 'OK' : 'Error',
    headers: new Headers({ 'X-Request-ID': 'test' }),
    json: async () => payload,
  } as unknown as Response
}

// ------------------------------------------------------------------ render --

export interface RenderScreenOptions {
  /** Permissions the signed-in user holds. Empty means a user with none. */
  permissions?: string[]
  /** Extra API routes. `GET /auth/me` is provided; override it to change the user. */
  routes?: RouteTable
  /** Initial URL. Use with `path` for a screen that reads route params. */
  route?: string
  /** Route pattern, when the screen uses `useParams`. */
  path?: string
  /** Fields to merge into the `/auth/me` payload (is_superuser, company_id...). */
  user?: Record<string, unknown>
  /** Let unrouted requests return an empty page instead of throwing. */
  allowUnrouted?: boolean
}

export interface ScreenResult extends RenderResult {
  fetch: FetchRecorder
  user: ReturnType<typeof userEvent.setup>
  queryClient: QueryClient
}

export function renderScreen(ui: ReactElement, options: RenderScreenOptions = {}): ScreenResult {
  const {
    permissions = [],
    routes = {},
    route = '/',
    path,
    user: userOverrides = {},
    allowUnrouted = false,
  } = options

  const table: RouteTable = {
    'GET /auth/me': meResponse(permissions, userOverrides),
    ...routes,
  }
  const recorder = installFetch(table, allowUnrouted ? 'empty' : 'throw')

  // A refresh token is what makes `AuthProvider` attempt the restore at all;
  // without one it decides immediately that nobody is signed in and every
  // permission-gated control stays hidden for the wrong reason.
  setTokens(null, 'test-refresh-token')

  // Retries turn a deliberate 4xx into a multi-second test, and `gcTime: 0`
  // stops one test's cache from answering the next one's query.
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  })

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <AuthProvider>
          {path ? <Routes>{<Route path={path} element={children} />}</Routes> : children}
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  )

  const result = render(ui, { wrapper })
  return { ...result, fetch: recorder, user: userEvent.setup(), queryClient }
}

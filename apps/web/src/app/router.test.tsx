/**
 * The route table.
 *
 * `routeGroups` pairs a path with the permission its own endpoints declare, and
 * the pairing is the security-adjacent part of this file: not because the
 * frontend enforces anything - the API re-resolves the caller's rights on every
 * request - but because a route that opens a screen the API will only ever 403
 * sends the user through a form for nothing, and a route left ungated
 * altogether does it silently on every page load.
 *
 * These tests deliberately do not route the API paths a permitted page would
 * call. Feature pages are lazy, and the guard runs *before* the `Suspense`
 * boundary, so a denied route never imports its chunk and never fetches. If the
 * guard regressed, the page would load and its first request would fail the
 * test on the unrouted call - which is a louder failure than a missing heading.
 */

import { screen, waitFor } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'

import { ok, page, renderScreen, type RenderScreenOptions } from '@/test/harness'
import { P } from '@/lib/permissions'

import { AppRouter } from './router'

beforeAll(async () => {
  // Feature routes are `React.lazy`. The first dynamic import of a page into a
  // cold vitest module graph can take several seconds - longer than the default
  // `findBy*` timeout - so an assertion lands on the Suspense spinner and
  // reports a routing failure that is really a transform cost. Importing them
  // up front makes the lazy import resolve on a microtask, which is what it
  // does in a warm browser anyway.
  await Promise.all([
    import('@/features/audit/audit-page'),
    import('@/features/auth/change-password-page'),
    import('@/features/auth/profile-page'),
    import('@/features/admin/users-page'),
  ])

  // Floating UI's `isTopLayer` asks `element.matches(':modal')` on every
  // position computation, and `nwsapi` - jsdom's selector engine - has no
  // implementation for that pseudo-class and takes about 1.2 s per call to say
  // so. One dropdown open then costs over twenty seconds. Nothing in jsdom is
  // ever in the top layer, so false is both correct and instant. Belongs in
  // `src/test/setup.ts` with the other jsdom stubs.
  const nativeMatches = Element.prototype.matches
  Element.prototype.matches = function patched(this: Element, selectors: string): boolean {
    if (selectors === ':modal' || selectors === ':popover-open') return false
    return nativeMatches.call(this, selectors)
  }
})

const DENIAL = 'You do not have access to this page'

function renderAt(route: string, options: RenderScreenOptions = {}) {
  return renderScreen(<AppRouter />, { route, ...options })
}

/** The shell has rendered, which means `RequireAuth` let the visitor in. */
async function shell(): Promise<HTMLElement> {
  return screen.findByRole('navigation', { name: 'Main' })
}

describe('a route the user may not open', () => {
  it('renders the denial instead of the page', async () => {
    // A user with one unrelated right, following a colleague's link.
    renderAt('/finance', { permissions: [P.ITEM_VIEW] })

    expect(await screen.findByRole('heading', { name: DENIAL })).toBeVisible()
    // Explained rather than redirected: bouncing to the dashboard leaves the
    // user believing the link is broken and re-sending it to someone else.
    expect(screen.getByText(/ask an administrator/i)).toBeVisible()
    expect(screen.getByRole('link', { name: /back to dashboard/i })).toBeVisible()
  })

  it('gates the notifications route, whose every endpoint requires the right', async () => {
    // Ungated, this route let a user without `notification.view` reach a page
    // where the list, the unread count, the mark-read call and the preferences
    // all 403 - a screen that can do nothing but fail.
    renderAt('/notifications', { permissions: [P.ITEM_VIEW] })

    expect(await screen.findByRole('heading', { name: DENIAL })).toBeVisible()
  })

  it('gates the users screen behind the identity rights, not merely sign-in', async () => {
    renderAt('/admin/users', { permissions: [P.ITEM_VIEW] })

    expect(await screen.findByRole('heading', { name: DENIAL })).toBeVisible()
  })
})

describe('a route the user may open', () => {
  it('renders the page once the right is held', async () => {
    // The other half of the gate. Without this, a `RequirePermission` that
    // denied everything unconditionally would pass every test above.
    renderAt('/audit', {
      permissions: [P.AUDIT_LOG_VIEW],
      routes: { 'GET /audit/logs': page([]) },
    })

    expect(await screen.findByRole('heading', { name: 'Audit trail' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: DENIAL })).toBeNull()
  })

  it('opens a route on ANY of its listed rights', async () => {
    // `/admin/users` lists two. An administrator holding only `role.view` is
    // meant to reach the roles and matrix tabs; requiring both would lock them
    // out of a screen the API would serve.
    renderAt('/admin/users', {
      permissions: [P.ROLE_VIEW],
      routes: {
        'GET /roles': page([]),
        'GET /users': page([]),
        'GET /organization/branches': page([]),
        'GET /organization/plants': page([]),
      },
    })

    expect(await screen.findByRole('heading', { name: 'Users & roles' })).toBeVisible()
  })

  it('lets anyone reach /profile, because its endpoints gate on nothing', async () => {
    // Self-service. `/auth/change-password`, `/auth/sessions` and
    // `/auth/logout-all` are all about the caller's own account, so a
    // permission on this route would lock a user out of changing the password
    // the system is forcing them to change.
    renderAt('/profile', {
      permissions: [],
      routes: { 'GET /auth/sessions': page([]) },
    })

    expect(await screen.findByRole('heading', { name: 'Your account' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: DENIAL })).toBeNull()
  })
})

describe('signing in first', () => {
  it('sends an unauthenticated visitor to the sign-in form', async () => {
    renderAt('/items', {
      routes: {
        'GET /auth/me': {
          __status: 401,
          success: false,
          error: { code: 'UNAUTHENTICATED', message: 'Not authenticated' },
        },
        'POST /auth/refresh': {
          __status: 401,
          success: false,
          error: { code: 'INVALID_TOKEN', message: 'Refresh token rejected' },
        },
      },
    })

    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    // And no shell: a signed-out visitor must not see the navigation at all.
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
  })

  it('keeps a reloading user on the page they asked for rather than signing them out', async () => {
    // The F5 case. After a hard reload there is a refresh token and no user
    // yet, so redirecting while `isRestoring` would bounce every reload to the
    // sign-in form and lose the route the user was on.
    renderAt('/audit', {
      permissions: [P.AUDIT_LOG_VIEW],
      routes: { 'GET /audit/logs': page([]) },
    })

    expect(await screen.findByRole('heading', { name: 'Audit trail' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Sign in' })).toBeNull()
  })

  it('confines a temporary-password session to the password change screen', async () => {
    const { fetch } = renderAt('/audit', {
      permissions: [P.AUDIT_LOG_VIEW],
      user: { must_change_password: true },
    })

    expect(await screen.findByRole('heading', { name: 'Choose a new password' })).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(fetch.calls.some((call) => call.path === '/audit/logs')).toBe(false)
    expect(fetch.calls.some((call) => call.path === '/auth/sessions')).toBe(false)
  })
})

describe('a path that does not exist', () => {
  it('renders the 404 inside the shell, not the denial', async () => {
    renderAt('/procurement/not-a-page', { permissions: [P.ITEM_VIEW] })

    // Distinct from the denial on purpose: "you may not open this" and "this
    // does not exist" send the user to different people for help.
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: DENIAL })).toBeNull()
    // Still inside the shell, so the user can navigate away without the back
    // button.
    expect(await shell()).toBeVisible()
  })
})

describe('the shell the routes render into', () => {
  it('signs the visitor in and shows only their own sections', async () => {
    const { fetch } = renderAt('/audit', {
      permissions: [P.AUDIT_LOG_VIEW],
      routes: { 'GET /audit/logs': page([]) },
    })
    await shell()

    expect(await screen.findByRole('link', { name: 'Audit Trail' })).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Users & Roles' })).toBeNull()
    // The restore itself: one `/auth/me`, from a refresh token, with no login
    // round trip.
    expect(fetch.calls.filter((call) => call.path === '/auth/me').length).toBeGreaterThan(0)
  })

  it('signs out from any route through the API', async () => {
    const { user, fetch } = renderAt('/audit', {
      permissions: [P.AUDIT_LOG_VIEW],
      routes: {
        'GET /audit/logs': page([]),
        'POST /auth/logout': ok({ revoked: true }),
      },
    })
    await shell()

    await user.click(await screen.findByRole('button', { name: /Test User/ }))
    await user.click(await screen.findByRole('menuitem', { name: /sign out/i }))

    await waitFor(() => fetch.requestFor('POST', '/auth/logout'))
    // And back to the sign-in form, rather than a shell with no data in it.
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
  })
})

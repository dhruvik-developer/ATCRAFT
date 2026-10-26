/**
 * The application shell.
 *
 * The shell is where over-exposure shows first, because it is the one component
 * every signed-in user renders on every route. Two failure modes matter and
 * both are invisible to the person most likely to look:
 *
 *  * **A section shown to someone who cannot use it.** Every link in it returns
 *    403, and an administrator - who holds every permission by construction -
 *    sees a perfectly working menu.
 *  * **A query fired by an ungated control.** The bell polls
 *    `GET /notifications/unread-count`, which declares
 *    `require_permission(NOTIFICATION_VIEW)`. Ungated it collected a 403 on
 *    every page load and again every minute, for every user without the right.
 *
 * The second one is pinned structurally rather than by assertion: these tests
 * do not route `/notifications/unread-count` unless the user holds the right,
 * and the harness throws on an unrouted request. A bell that polls when it
 * should not fails the test on the request itself.
 */

import { act, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ok, renderScreen, type RouteTable } from '@/test/harness'
import { P } from '@/lib/permissions'
import { STORAGE_KEYS } from '@/lib/constants'

import { AppLayout } from './app-layout'

// Menu focus/hover warms real chunks in the browser. Download behavior has its
// own tests; compiling those pages must not keep a drawer interaction pending.
vi.mock('@/app/route-loaders', () => ({ prefetchRoute: vi.fn() }))

/** `GET /notifications/unread-count` - the `UnreadCountRead` payload. */
function unreadCount(count: number, capped = false): RouteTable {
  return {
    'GET /notifications/unread-count': ok({ unread_count: count, is_capped: capped }),
  }
}

function renderLayout(permissions: string[], options: Parameters<typeof renderScreen>[1] = {}) {
  return renderScreen(<AppLayout />, { permissions, ...options })
}

/** The sidebar, once the session restore has populated the permission set. */
async function sidebar(): Promise<HTMLElement> {
  return screen.findByRole('navigation', { name: 'Main' })
}

describe('the sidebar shows only what the user can use', () => {
  it('hides a whole section the user holds no permission in', async () => {
    renderLayout([P.ITEM_VIEW])
    const nav = await sidebar()

    // The one right granted, and the section that right belongs to.
    expect(await within(nav).findByRole('link', { name: 'Items' })).toBeVisible()
    expect(within(nav).getByText('Engineering')).toBeVisible()

    // Everything else stays out of the menu. A store keeper offered a Finance
    // section can only ever collect a 403 from it.
    expect(within(nav).queryByText('Finance')).toBeNull()
    expect(within(nav).queryByRole('link', { name: 'Ledgers & Outstanding' })).toBeNull()
    expect(within(nav).queryByText('Administration')).toBeNull()
    expect(within(nav).queryByRole('link', { name: 'Users & Roles' })).toBeNull()
  })

  it('shows that same section once the right is granted', async () => {
    renderLayout([P.ITEM_VIEW, P.FINANCE_VIEW])
    const nav = await sidebar()

    // The other half of the gate. Asserting only the hidden case would pass
    // just as well against a sidebar that renders nothing at all.
    expect(await within(nav).findByRole('link', { name: 'Ledgers & Outstanding' })).toBeVisible()
    expect(within(nav).getByText('Finance')).toBeVisible()
  })

  it('filters the items inside a section, not only whole sections', async () => {
    renderLayout([P.WAREHOUSE_VIEW])
    const nav = await sidebar()

    // Inventory survives because one of its six items is permitted; the other
    // five must not come with it.
    expect(await within(nav).findByRole('link', { name: 'Warehouses' })).toBeVisible()
    expect(within(nav).getByText('Inventory')).toBeVisible()
    expect(within(nav).queryByRole('link', { name: 'Stock Balances' })).toBeNull()
    expect(within(nav).queryByRole('link', { name: 'Stock Ledger' })).toBeNull()
    expect(within(nav).queryByRole('link', { name: 'Storage Locations' })).toBeNull()
  })

  it('reveals an item on ANY of its permissions, not all of them', async () => {
    // Item Masters declares three rights. `canAny` is correct: a pricing clerk
    // who holds only `price.view` genuinely uses that screen - its Prices tab
    // is gated on exactly that - and switching this to `canAll` would hide it
    // from them with no error anywhere.
    renderLayout([P.PRICE_VIEW])
    const nav = await sidebar()

    expect(await within(nav).findByRole('link', { name: 'Item Masters' })).toBeVisible()
    expect(within(nav).queryByRole('link', { name: 'Items' })).toBeNull()
  })

  it('offers a user with no rights an empty menu rather than the whole one', async () => {
    renderLayout([])
    const nav = await sidebar()

    // The safe direction to fail in. A permission set that failed to load must
    // leave the menu empty, never open.
    await waitFor(() => expect(within(nav).queryAllByRole('link')).toHaveLength(0))
    expect(within(nav).queryByText('Administration')).toBeNull()
  })

  it('gives a superuser the whole menu without listing a single permission', async () => {
    // `can()` short-circuits on `is_superuser`, which is exactly why an ungated
    // control is invisible to the person reviewing it.
    renderLayout([], { user: { is_superuser: true }, routes: unreadCount(0) })
    const nav = await sidebar()

    expect(await within(nav).findByRole('link', { name: 'Users & Roles' })).toBeVisible()
    expect(within(nav).getByRole('link', { name: 'Ledgers & Outstanding' })).toBeVisible()
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).toBeVisible()
  })
})

describe('the notification bell', () => {
  it('is absent, and polls nothing, without notification.view', async () => {
    // `/notifications/unread-count` is deliberately not routed. The harness
    // throws on an unrouted request, so a bell that polls anyway fails here on
    // the request rather than on an assertion - which is the shape of the
    // original defect: a 403 collected every minute by every user without the
    // right, and never seen by an administrator who holds it.
    const { fetch } = renderLayout([P.ITEM_VIEW])
    await sidebar()

    expect(screen.queryByRole('link', { name: /notifications/i })).toBeNull()
    await waitFor(() =>
      expect(fetch.calls.map((call) => call.path)).not.toContain('/notifications/unread-count'),
    )
  })

  it('appears and states the unread count once the right is held', async () => {
    renderLayout([P.NOTIFICATION_VIEW], { routes: unreadCount(3) })

    // The count belongs in the accessible name, not only in the badge: a
    // coloured circle with a numeral in it says nothing to a screen reader.
    const bell = await screen.findByRole('link', { name: 'Notifications, 3 unread' })
    expect(bell).toHaveAttribute('href', '/notifications')
  })

  it('reads a capped count as a floor rather than an exact figure', async () => {
    renderLayout([P.NOTIFICATION_VIEW], { routes: unreadCount(500, true) })

    // `is_capped` means the API stopped counting, not that there are exactly
    // 500. "500 unread" would be a number the server never claimed.
    expect(await screen.findByRole('link', { name: 'Notifications, 500+ unread' })).toBeVisible()
  })

  it('marks a failed count instead of showing a bell that reads as "nothing new"', async () => {
    renderLayout([P.NOTIFICATION_VIEW], {
      routes: {
        'GET /notifications/unread-count': {
          __status: 503,
          success: false,
          error: { code: 'SERVICE_UNAVAILABLE', message: 'Notifications are unavailable' },
        },
      },
    })

    // An empty bell after a failed request is indistinguishable from an empty
    // inbox, so the user stops checking.
    expect(
      await screen.findByRole('link', {
        name: 'Notifications - the unread count could not be loaded',
      }),
    ).toBeVisible()
  })

  it('shows a plain bell, not a zero badge, when there is nothing waiting', async () => {
    renderLayout([P.NOTIFICATION_VIEW], { routes: unreadCount(0) })

    expect(await screen.findByRole('link', { name: 'Notifications' })).toBeVisible()
  })
})

describe('the global search', () => {
  /** Where the shell sent the user, read off the route it landed on. */
  function LocationProbe() {
    const location = useLocation()
    return <p data-testid="location">{location.pathname + location.search}</p>
  }

  function renderShellOverItems(permissions: string[]) {
    return renderScreen(
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={null} />
          <Route path="/items" element={<LocationProbe />} />
        </Route>
      </Routes>,
      { permissions },
    )
  }

  it('is absent without item.view, since the items list is all it can open', async () => {
    renderLayout([P.FINANCE_VIEW])
    await sidebar()

    expect(screen.queryByRole('searchbox', { name: 'Search items' })).toBeNull()
  })

  it('opens the items list filtered by the term', async () => {
    const { user } = renderShellOverItems([P.ITEM_VIEW])
    await sidebar()

    await user.type(await screen.findByRole('searchbox', { name: 'Search items' }), 'hinge{Enter}')

    // `useListQuery` reads `q` from the URL, so this is the whole hand-over.
    expect(await screen.findByTestId('location')).toHaveTextContent('/items?q=hinge')
  })

  it('goes nowhere on a blank term', async () => {
    const { user } = renderShellOverItems([P.ITEM_VIEW])
    await sidebar()

    await user.type(await screen.findByRole('searchbox', { name: 'Search items' }), '   {Enter}')

    expect(screen.queryByTestId('location')).toBeNull()
  })
})

describe('the account menu', () => {
  it('offers the profile and signs the user out through the API', async () => {
    const { user, fetch } = renderLayout([P.ITEM_VIEW], {
      routes: { 'POST /auth/logout': ok({ revoked: true }) },
    })
    await sidebar()

    await user.click(await screen.findByRole('button', { name: /Test User/ }))

    expect(await screen.findByRole('menuitem', { name: /profile & sessions/i })).toBeVisible()
    await user.click(screen.getByRole('menuitem', { name: /sign out/i }))

    // The server call is what revokes the session; clearing the tokens locally
    // only stops this tab from using them, and the session would stay live for
    // anyone holding the refresh token.
    await waitFor(() => fetch.requestFor('POST', '/auth/logout'))
  })
})

describe('the mobile navigation drawer', () => {
  afterEach(() => {
    localStorage.removeItem(STORAGE_KEYS.sidebar)
    vi.restoreAllMocks()
  })

  it('contains keyboard focus, closes with Escape and returns focus to its opener', async () => {
    // A collapsed desktop preference must not hide the mobile labels.
    localStorage.setItem(STORAGE_KEYS.sidebar, 'collapsed')
    const { user } = renderLayout([P.ITEM_VIEW])
    await sidebar()

    const opener = screen.getByRole('button', { name: 'Open navigation' })
    await user.click(opener)
    const drawer = await screen.findByRole('dialog', { name: 'Main navigation' })
    const close = within(drawer).getByRole('button', { name: 'Close navigation' })
    const items = within(drawer).getByRole('link', { name: 'Items' })
    expect(close).toHaveFocus()

    await user.tab()
    expect(items).toHaveFocus()
    await user.tab()
    expect(close).toHaveFocus()
    await user.tab({ shift: true })
    expect(items).toHaveFocus()

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(opener).toHaveFocus()
  })

  it('closes after opening the selected screen', async () => {
    const { user } = renderScreen(
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={null} />
          <Route path="/items" element={<p>Items destination</p>} />
        </Route>
      </Routes>,
      { permissions: [P.ITEM_VIEW] },
    )
    await sidebar()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const drawer = await screen.findByRole('dialog', { name: 'Main navigation' })

    await user.click(within(drawer).getByRole('link', { name: 'Items' }))

    expect(await screen.findByText('Items destination')).toBeVisible()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('releases the modal when the viewport grows to the desktop sidebar', async () => {
    const originalMatchMedia = window.matchMedia
    const desktop = Object.assign(new EventTarget(), {
      matches: false,
      media: '(min-width: 1024px)',
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
    }) as MediaQueryList
    vi.spyOn(window, 'matchMedia').mockImplementation((query) =>
      query === desktop.media ? desktop : originalMatchMedia(query),
    )
    const { user } = renderLayout([P.ITEM_VIEW])
    await sidebar()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(await screen.findByRole('dialog', { name: 'Main navigation' })).toBeVisible()

    act(() => {
      Object.defineProperty(desktop, 'matches', { value: true })
      desktop.dispatchEvent(new Event('change'))
    })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

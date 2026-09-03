/**
 * Users, roles and the permission matrix.
 *
 * This screen is the one place in the product where the affordances *are* the
 * subject matter, which makes a mis-gated control here worse than elsewhere: an
 * administrator reviewing access sees a menu built from their own permission
 * set, and a superuser holds everything by construction, so an over-exposed
 * action looks perfectly correct to the only person likely to look at it.
 *
 * The gate that earns most of this file is **Reset password**. It is enforced
 * by `POST /users/{id}/reset-password` as `user.reset_password`, not
 * `user.update`. Gating it on the edit right showed the action to people the
 * API refuses, and hid it from the helpdesk role that holds only the reset -
 * and every test that stubbed `can()` to true would have passed either way.
 *
 * The second theme is what the forms *send*. A password reset that posts
 * `new_password: ""` instead of omitting the field asks the server to set an
 * empty password rather than to generate one, and the difference is invisible
 * in a screenshot.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'
import { P } from '@/lib/permissions'

import UsersPage from './users-page'

beforeAll(() => {
  /**
   * Short-circuit the top-layer pseudo-classes.
   *
   * Floating UI's `isTopLayer` - reached whenever a Radix popper positions
   * itself, so on every menu, select and dialog this screen opens - asks
   * `element.matches(':modal')`. `nwsapi`, jsdom's selector engine, has no
   * implementation for that pseudo-class and takes roughly **1.2 seconds per
   * call** to decide so, which puts a single dropdown open past twenty seconds
   * and through vitest's default timeout.
   *
   * Nothing in jsdom is ever in the top layer, so false is both correct and
   * instant. This belongs in `src/test/setup.ts` next to the `matchMedia` and
   * `ResizeObserver` stubs; it is duplicated here only because that file is not
   * mine to edit.
   */
  const nativeMatches = Element.prototype.matches
  Element.prototype.matches = function patched(this: Element, selectors: string): boolean {
    if (selectors === ':modal' || selectors === ':popover-open') return false
    return nativeMatches.call(this, selectors)
  }
})

beforeEach(() => {
  /**
   * Undo the `pointer-events: none` Radix parks on `<body>`.
   *
   * `DismissableLayer` sets it while a modal menu or dialog is open and
   * restores it when the layer unmounts. A test that ends with a menu still
   * open is torn down by Testing Library's `cleanup` without the layer closing
   * first, and the style is left on `<body>`. userEvent refuses to click
   * through `pointer-events: none`, so the damage would land in the *next*
   * test, nowhere near its cause - hence clearing it before every test rather
   * than after the one that caused it.
   */
  document.body.style.pointerEvents = ''
})

// ------------------------------------------------------------- fixtures -----

/** The id the harness signs the test user in as. */
const SIGNED_IN_ID = '01a00000-0000-7000-8000-000000000001'

function userRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'u-priya',
    email: 'priya@atcraft.example',
    full_name: 'Priya Shah',
    employee_code: 'EMP-0041',
    is_active: true,
    is_superuser: false,
    must_change_password: false,
    vendor_id: null,
    branch_id: null,
    plant_id: null,
    department_id: null,
    last_login_at: '2026-08-30T09:15:00Z',
    locked_until: null,
    created_at: '2025-01-04T06:00:00Z',
    role_codes: ['STORES'],
    ...overrides,
  }
}

const STORES_ROLE = {
  id: 'r-stores',
  code: 'STORES',
  name: 'Stores',
  description: 'Goods in, goods out.',
  is_system: false,
  is_vendor_role: false,
  is_active: true,
  permission_count: 12,
  created_at: '2025-01-01T00:00:00Z',
}

/** Everything the users tab asks for on mount, all of it empty by default. */
function baseRoutes(rows: Record<string, unknown>[] = [userRow()]): RouteTable {
  return {
    'GET /users': page(rows),
    'GET /roles': page([STORES_ROLE]),
    'GET /organization/branches': page([]),
    'GET /organization/plants': page([]),
  }
}

function renderUsers(permissions: string[], routes: RouteTable = baseRoutes()) {
  return renderScreen(<UsersPage />, { permissions, routes })
}

/**
 * Wait until the grid has stopped rebuilding itself.
 *
 * The columns memo depends on the roles, branch and plant lookups, so the
 * moment those resolve TanStack Table is handed new column definitions and
 * re-creates every cell - replacing their DOM nodes. An element captured before
 * that point is detached by the time a test clicks it, and the click lands on
 * nothing: no error, no menu, and a failure that reads as a broken gate.
 *
 * The role badge only renders once `/roles` has answered, so waiting for it
 * settles the last of those dependencies.
 */
async function settled(): Promise<void> {
  await screen.findAllByRole('button', { name: STORES_ROLE.name })
}

/** The row-actions menu for one person, opened. */
async function openActions(
  user: ReturnType<typeof renderUsers>['user'],
  fullName: string,
): Promise<void> {
  await settled()
  await user.click(screen.getByRole('button', { name: `Actions for ${fullName}` }))
  await screen.findByRole('menu')
}

const READ_ONLY = [P.USER_VIEW, P.ROLE_VIEW]

// ---------------------------------------------------------------- tests -----

describe('the row-actions menu is built from the rights the API enforces', () => {
  it('offers Reset password to a user who holds user.reset_password', async () => {
    const { user } = renderUsers([...READ_ONLY, P.USER_UPDATE, P.USER_RESET_PASSWORD])
    await openActions(user, 'Priya Shah')

    expect(screen.getByRole('menuitem', { name: /reset password/i })).toBeVisible()
  })

  it('withholds it from a user who holds only user.update', async () => {
    // The shipped defect, pinned. `POST /users/{id}/reset-password` enforces
    // `user.reset_password`; gating the menu item on the edit right offered the
    // action to every account manager, and each of them got a 403 only after
    // typing a reason into the dialog.
    const { user } = renderUsers([...READ_ONLY, P.USER_UPDATE])
    await openActions(user, 'Priya Shah')

    expect(screen.getByRole('menuitem', { name: /edit details/i })).toBeVisible()
    expect(screen.queryByRole('menuitem', { name: /reset password/i })).toBeNull()
  })

  it('withholds Edit details from a helpdesk user who does not hold user.update', async () => {
    // The mirror image of the test above: the two rights are independent, so
    // holding one must not imply the other in either direction. The deactivate
    // right is granted alongside only because it is what makes the actions
    // column render - see the note on `settled` and the reset right below.
    const { user } = renderUsers([...READ_ONLY, P.USER_RESET_PASSWORD, P.USER_DEACTIVATE])
    await openActions(user, 'Priya Shah')

    expect(screen.getByRole('menuitem', { name: /reset password/i })).toBeVisible()
    expect(screen.queryByRole('menuitem', { name: /edit details/i })).toBeNull()
  })

  it('withholds Assign roles from a user who holds only the edit right', async () => {
    // Assigning a role is the privilege-escalation path on this screen - it is
    // what turns a storekeeper into an approver - so it carries its own right
    // rather than riding along with `user.update`.
    const { user } = renderUsers([...READ_ONLY, P.USER_UPDATE])
    await openActions(user, 'Priya Shah')

    expect(screen.queryByRole('menuitem', { name: /assign roles/i })).toBeNull()
  })

  it('offers Assign roles once permission.assign is held', async () => {
    const { user } = renderUsers([...READ_ONLY, P.PERMISSION_ASSIGN])
    await openActions(user, 'Priya Shah')

    expect(screen.getByRole('menuitem', { name: /assign roles/i })).toBeVisible()
  })

  it('offers no actions menu at all to a read-only reviewer', async () => {
    renderUsers(READ_ONLY)

    await settled()
    // An access review is a read. Rendering the trigger and emptying the menu
    // would leave a button that does nothing.
    expect(screen.queryByRole('button', { name: /^Actions for/ })).toBeNull()
  })

  it('refuses to offer the signed-in user their own deactivation', async () => {
    const { user } = renderUsers(
      [...READ_ONLY, P.USER_DEACTIVATE],
      baseRoutes([
        userRow({ id: SIGNED_IN_ID, full_name: 'Test User', email: 'tester@atcraft.example' }),
        userRow(),
      ]),
    )

    // Deactivating yourself revokes your own sessions mid-click, and on a
    // single-administrator tenant there is then nobody left who can undo it.
    await openActions(user, 'Test User')
    expect(screen.queryByRole('menuitem', { name: /deactivate/i })).toBeNull()

    await user.keyboard('{Escape}')
    await openActions(user, 'Priya Shah')
    expect(screen.getByRole('menuitem', { name: /deactivate/i })).toBeVisible()
  })
})

describe('the create button and the tabs', () => {
  it('offers New user only with user.create', async () => {
    renderUsers(READ_ONLY)
    await settled()

    expect(screen.queryByRole('button', { name: /new user/i })).toBeNull()
  })

  it('shows New user once the right is held', async () => {
    renderUsers([...READ_ONLY, P.USER_CREATE])
    expect(await screen.findByRole('button', { name: /new user/i })).toBeVisible()
  })

  it('hides the roles and matrix tabs without role.view', async () => {
    // Both tabs read `/roles` and the permission catalogue, which the API gates
    // on `role.view`. Offering the tab produces a page that can only 403.
    renderUsers([P.USER_VIEW])
    await settled()

    expect(screen.getByRole('tab', { name: /users/i })).toBeVisible()
    expect(screen.queryByRole('tab', { name: /^roles/i })).toBeNull()
    expect(screen.queryByRole('tab', { name: /permission matrix/i })).toBeNull()
  })

  it('shows them once role.view is granted', async () => {
    renderUsers(READ_ONLY)
    expect(await screen.findByRole('tab', { name: /^roles/i })).toBeVisible()
    expect(screen.getByRole('tab', { name: /permission matrix/i })).toBeVisible()
  })
})

describe('what the password reset actually sends', () => {
  /**
   * The edit right is granted only so the actions column exists.
   *
   * The column itself is guarded on `canUpdate || canDeactivate || canAssign`,
   * with `canResetPassword` left out of that list, so a role holding the reset
   * right alone renders no menu to reach the dialog through. The menu item is
   * gated correctly - the tests above pin that - but it is unreachable for the
   * one role the gate was written for. That is reported, not asserted here:
   * this suite must not encode the current behaviour as intended.
   */
  const RESET_RIGHTS = [...READ_ONLY, P.USER_UPDATE, P.USER_RESET_PASSWORD]

  async function openResetDialog(permissions = RESET_RIGHTS, routes = baseRoutes()) {
    const rendered = renderUsers(permissions, routes)
    await openActions(rendered.user, 'Priya Shah')
    await rendered.user.click(screen.getByRole('menuitem', { name: /reset password/i }))
    await screen.findByRole('heading', { name: 'Reset password for Priya Shah' })
    return rendered
  }

  it('omits new_password entirely when the field is left blank', async () => {
    const { user, fetch } = await openResetDialog(RESET_RIGHTS, {
      ...baseRoutes(),
      'POST /users/u-priya/reset-password': ok({
        user_id: 'u-priya',
        must_change_password: true,
        sessions_revoked: 2,
        temporary_password: 'Kx7-temp-Qz19',
      }),
    })

    await user.type(screen.getByLabelText(/^reason/i), 'Lost her phone')
    await user.click(screen.getByRole('button', { name: /^reset password$/i }))

    await waitFor(() => fetch.requestFor('POST', '/users/u-priya/reset-password'))
    const body = fetch.requestFor('POST', '/users/u-priya/reset-password') as Record<
      string,
      unknown
    >
    // Absent, not `""` and not `null`. "Generate one for me" and "set the
    // password to the empty string" are different requests, and only one of
    // them is what an empty field means.
    expect(body).toEqual({ reason: 'Lost her phone' })
    expect('new_password' in body).toBe(false)
  })

  it('sends the password verbatim when one is typed, and trims only the reason', async () => {
    const { user, fetch } = await openResetDialog(RESET_RIGHTS, {
      ...baseRoutes(),
      'POST /users/u-priya/reset-password': ok({
        user_id: 'u-priya',
        must_change_password: true,
        sessions_revoked: 1,
        temporary_password: null,
      }),
    })

    // The reason is padded on purpose and the password is not: the reason is
    // prose bound for the audit trail and is trimmed, while a password is an
    // opaque string that must reach the server byte for byte - trimming one
    // silently changes the credential the administrator is about to hand over.
    await user.type(screen.getByLabelText(/new password/i), 'Str0ng-Passw0rd!')
    await user.type(screen.getByLabelText(/^reason/i), '  Suspected shared login  ')
    await user.click(screen.getByRole('button', { name: /^reset password$/i }))

    await waitFor(() => fetch.requestFor('POST', '/users/u-priya/reset-password'))
    expect(fetch.requestFor('POST', '/users/u-priya/reset-password')).toEqual({
      new_password: 'Str0ng-Passw0rd!',
      reason: 'Suspected shared login',
    })
  })

  it('refuses a password the server would reject, without spending a request', async () => {
    const { user, fetch } = await openResetDialog()

    await user.type(screen.getByLabelText(/new password/i), 'short')
    await user.type(screen.getByLabelText(/^reason/i), 'Lost her phone')
    await user.click(screen.getByRole('button', { name: /^reset password$/i }))

    expect(await screen.findByText(/missing: at least 12 characters/i)).toBeVisible()
    // The reset revokes every session the account holds. Sending one the server
    // will refuse still costs the round trip, and a partially applied reset is
    // exactly what nobody wants to reason about.
    expect(fetch.calls.some((call) => call.path.endsWith('/reset-password'))).toBe(false)
  })

  it('demands a reason, because it is what the audit trail will carry', async () => {
    const { user, fetch } = await openResetDialog()

    await user.click(screen.getByRole('button', { name: /^reset password$/i }))

    expect(await screen.findByText(/at least 3 characters/i)).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/reset-password'))).toBe(false)
  })
})

describe('what the server says about the reset reaches the user', () => {
  // As above: the actions column is gated on update/deactivate/assign, so the
  // reset right on its own never renders a menu to open this dialog from.
  const RESET_RIGHTS = [...READ_ONLY, P.USER_UPDATE, P.USER_RESET_PASSWORD]

  async function submitReset(routes: RouteTable) {
    const rendered = renderUsers(RESET_RIGHTS, { ...baseRoutes(), ...routes })
    await openActions(rendered.user, 'Priya Shah')
    await rendered.user.click(screen.getByRole('menuitem', { name: /reset password/i }))
    await screen.findByRole('heading', { name: 'Reset password for Priya Shah' })
    await rendered.user.type(screen.getByLabelText(/^reason/i), 'Lost her phone')
    await rendered.user.click(screen.getByRole('button', { name: /^reset password$/i }))
    return rendered
  }

  it('puts a 422 field message on the field that caused it', async () => {
    await submitReset({
      'POST /users/u-priya/reset-password': fail(422, 'VALIDATION_ERROR', 'Invalid', {
        fields: { reason: ['A reason of at least ten characters is required'] },
      }),
    })

    // The mutation cache suppresses the toast for a 422 carrying field errors,
    // on the understanding that the form shows them inline. If this stops
    // working the user sees nothing at all.
    expect(
      await screen.findByText('A reason of at least ten characters is required'),
    ).toBeVisible()
  })

  it('keeps the dialog open on a 403 rather than reporting a reset that never happened', async () => {
    await submitReset({
      'POST /users/u-priya/reset-password': fail(
        403,
        'PERMISSION_DENIED',
        'You may not reset this account',
      ),
    })

    // The failure is announced through the global toast, which is mounted at
    // the application root and so is out of this screen's reach. What the
    // screen itself owes the user is not to look like it worked: the form is
    // still there, and the "temporary password" panel - the only thing that
    // means the reset happened - is not.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^reset password$/i })).toBeEnabled(),
    )
    expect(screen.getByLabelText(/^reason/i)).toBeVisible()
    expect(screen.queryByText(/shown once and never recoverable/i)).toBeNull()
  })

  it('shows a generated temporary password once, and says it cannot be recovered', async () => {
    await submitReset({
      'POST /users/u-priya/reset-password': ok({
        user_id: 'u-priya',
        must_change_password: true,
        sessions_revoked: 3,
        temporary_password: 'Kx7-temp-Qz19',
      }),
    })

    expect(await screen.findByText('Kx7-temp-Qz19')).toBeVisible()
    // Without this sentence an administrator closes the dialog expecting to
    // find the password again later, and the account is locked out instead.
    expect(screen.getByText(/shown once and never recoverable/i)).toBeVisible()
    // And the form is gone, so the reset cannot be fired a second time by
    // reflex - which would invalidate the password just handed over.
    expect(screen.queryByLabelText(/^reason/i)).toBeNull()
  })
})

describe('deactivation', () => {
  const DEACTIVATE_RIGHTS = [...READ_ONLY, P.USER_DEACTIVATE]

  async function openDeactivate(routes: RouteTable = {}) {
    const rendered = renderUsers(DEACTIVATE_RIGHTS, { ...baseRoutes(), ...routes })
    await openActions(rendered.user, 'Priya Shah')
    await rendered.user.click(screen.getByRole('menuitem', { name: /deactivate/i }))
    await screen.findByRole('heading', { name: 'Deactivate Priya Shah?' })
    return rendered
  }

  it('will not post without a reason, and says why it wants one', async () => {
    const { user, fetch } = await openDeactivate()

    await user.click(screen.getByRole('button', { name: 'Deactivate' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/at least 3 characters/i)
    // Six months on, this is the only answer to why the account was switched
    // off - so the request must not go without it.
    expect(fetch.calls.some((call) => call.path.endsWith('/deactivate'))).toBe(false)
  })

  it('posts the trimmed reason to the deactivate endpoint', async () => {
    const { user, fetch } = await openDeactivate({
      'POST /users/u-priya/deactivate': ok({ ...userRow(), is_active: false }),
    })

    await user.type(screen.getByLabelText(/^reason/i), '  Left the company  ')
    await user.click(screen.getByRole('button', { name: 'Deactivate' }))

    await waitFor(() => fetch.requestFor('POST', '/users/u-priya/deactivate'))
    expect(fetch.requestFor('POST', '/users/u-priya/deactivate')).toEqual({
      reason: 'Left the company',
    })
  })

  it('warns that live sessions are revoked before the click, not after', async () => {
    await openDeactivate()

    // Deactivation is not a soft flag: it signs the account out everywhere,
    // immediately. An administrator doing it to a shift worker mid-shift needs
    // to know that before pressing the button.
    expect(screen.getByText(/every live session this account holds is revoked/i)).toBeVisible()
  })

  it('offers activation, not deactivation, for an account already switched off', async () => {
    const { user } = renderUsers(
      DEACTIVATE_RIGHTS,
      baseRoutes([userRow({ is_active: false })]),
    )
    await openActions(user, 'Priya Shah')

    // Offering "Deactivate" on an inactive account puts the user through a
    // mandatory-reason dialog for a state change that is already in force.
    expect(screen.getByRole('menuitem', { name: /^activate/i })).toBeVisible()
    expect(screen.queryByRole('menuitem', { name: /deactivate/i })).toBeNull()
  })
})

describe('the states the list can be in', () => {
  it('says the list is empty rather than rendering an empty grid', async () => {
    renderUsers(READ_ONLY, { ...baseRoutes([]), 'GET /users': page([]) })

    expect(await screen.findByText('No users')).toBeVisible()
    expect(screen.getByText(/nothing matches the current filters/i)).toBeVisible()
  })

  it('offers the create action from the empty state, when the right is held', async () => {
    renderUsers([...READ_ONLY, P.USER_CREATE], { ...baseRoutes([]), 'GET /users': page([]) })

    await screen.findByText('No users')
    // A first-run tenant has no users but the person setting it up does have
    // `user.create`; an empty state with no way forward is a dead end.
    expect(screen.getAllByRole('button', { name: /new user/i }).length).toBeGreaterThan(0)
  })

  it('reports a failed load with the server message and a way to retry', async () => {
    renderUsers(READ_ONLY, {
      ...baseRoutes(),
      'GET /users': fail(503, 'SERVICE_UNAVAILABLE', 'The directory is temporarily unavailable'),
    })

    expect(await screen.findByText('Could not load this list')).toBeVisible()
    expect(screen.getByText('The directory is temporarily unavailable')).toBeVisible()
    // A failed list with no retry leaves a full page reload as the only option,
    // which also loses whatever filters were set.
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })

  it('renders what the API omits rather than dereferencing it', async () => {
    // `role_codes` is omitted, not emptied, for a user with no role, and
    // `last_login_at` is null for one who has never signed in. Both used to be
    // read straight through.
    renderUsers(
      READ_ONLY,
      baseRoutes([
        userRow({
          id: 'u-new',
          full_name: 'Never Signed In',
          email: 'new@atcraft.example',
          role_codes: undefined,
          last_login_at: null,
        }),
      ]),
    )

    // Re-queried on every attempt rather than captured once: the grid swaps its
    // cells when the lookups resolve, so a row held across an await is detached
    // by the time it is read.
    await waitFor(() => {
      const row = screen.getByText('Never Signed In').closest('tr')
      expect(row).not.toBeNull()
      expect(within(row!).getByText('No roles')).toBeVisible()
      expect(within(row!).getByText('Never')).toBeVisible()
    })
  })

  it('names a role by its name, and links the badge to what it grants', async () => {
    const { user } = renderUsers(READ_ONLY)

    // The grid carries role *codes*; the readable name comes from the roles
    // lookup. A code shown raw is how an access review turns into guesswork.
    const badge = await screen.findByRole('button', { name: 'Stores' })
    expect(badge).toHaveAttribute('title', 'Show what Stores grants')
    await user.click(badge)
    expect(await screen.findByRole('tab', { name: /permission matrix/i })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })
})

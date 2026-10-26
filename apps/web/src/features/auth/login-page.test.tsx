/**
 * The sign-in form.
 *
 * This is the one screen every user meets, and the only one whose failure modes
 * are entirely the user's problem: if the message is wrong they cannot tell a
 * mistyped password from a locked account, and if the button never re-enables
 * they cannot get in at all until they think to reload.
 *
 * Three properties are pinned here because each has a recorded cost:
 *
 *  * **The API's own message reaches the user verbatim.** A generic "sign-in
 *    failed" throws away the only text that distinguishes bad credentials from
 *    a disabled, locked or expired account.
 *  * **A 429 produces a finite, counting wait.** The fallback used to be zero,
 *    which disabled the submit button with nothing to re-enable it - and a
 *    disabled submit button also suppresses implicit submission, so Enter did
 *    nothing either. The form was dead until the page was reloaded.
 *  * **Sign-in is followed by `/auth/me`.** The token payload carries no
 *    permission set, so skipping that call leaves `can()` false for everything
 *    and the whole sidebar hidden until the next hard reload.
 */

import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { fail, meResponse, ok, renderScreen, type RouteBody, type RouteTable } from '@/test/harness'

import { LoginPage } from './login-page'

/**
 * The provider restores from a refresh token on every render, so a *signed out*
 * page needs the restore to be refused - and refused all the way down. The API
 * client answers a 401 with one transparent refresh, so leaving
 * `POST /auth/refresh` unrouted fails the test on the refresh attempt rather
 * than on the assertion.
 */
const SIGNED_OUT: RouteTable = {
  'GET /auth/me': fail(401, 'UNAUTHENTICATED', 'Not authenticated'),
  'POST /auth/refresh': fail(401, 'INVALID_TOKEN', 'Refresh token rejected'),
}

/** A successful `POST /auth/login` body, in the `TokenPair` shape. */
function tokenPair(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    mfa_required: false,
    access_token: 'a-token',
    token_type: 'bearer',
    expires_in: 900,
    refresh_expires_in: 86400,
    session_id: 's-1',
    user: {
      id: '01a00000-0000-7000-8000-000000000001',
      email: 'ops@atcraft.example',
      full_name: 'Ops User',
      company_id: '01a00000-0000-7000-8000-0000000000c0',
      is_superuser: false,
      must_change_password: false,
      ...overrides,
    },
  }
}

/** Where the router ended up, for the redirects this form owns. */
function LocationProbe() {
  const location = useLocation()
  return <p data-testid="location">{location.pathname + location.search}</p>
}

/**
 * Mounted at `/login`, exactly as the real router mounts it.
 *
 * Not a bare `<LoginPage />`: the page renders `<Navigate to={from}/>` the
 * moment it sees an authenticated user, so a copy left mounted after a
 * successful sign-in immediately re-navigates to `/` and overwrites wherever
 * `onSubmit` had just sent the user. Under the real route it unmounts instead.
 */
function renderLogin(routes: RouteTable = {}) {
  return renderScreen(
    <>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<p>elsewhere</p>} />
      </Routes>
      <LocationProbe />
    </>,
    { route: '/login', routes: { ...SIGNED_OUT, ...routes } },
  )
}

/** The submit button, once the session restore has settled on nobody. */
function signInButton() {
  return screen.findByRole('button', { name: /sign in/i })
}

const emailField = () => screen.getByRole('textbox', { name: /email/i })
/** Anchored: "Show password" also contains "password". See the naming test. */
const passwordField = () => screen.getByLabelText(/^password/i)

/**
 * Fill and submit.
 *
 * The wait on the submit button is not incidental: until the session restore
 * resolves the page renders a spinner and nothing else, so a synchronous query
 * for the email field finds an empty form and reports the wrong failure.
 */
async function signIn(
  user: ReturnType<typeof renderLogin>['user'],
  email: string,
  password: string,
): Promise<void> {
  const submit = await signInButton()
  await user.type(emailField(), email)
  await user.type(passwordField(), password)
  await user.click(submit)
}

describe('the form itself', () => {
  it('renders the sign-in form once the restore settles on nobody', async () => {
    renderLogin()
    // The heading is an <h1> deliberately: it is the only heading on the page,
    // and it must not change with the viewport - the brand panel beside it
    // disappears below `lg`, so a heading there would exist on a desktop and
    // not on a phone.
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible()
    expect(emailField()).toBeVisible()
  })

  it('gives the password field and its reveal toggle names that tell them apart', async () => {
    renderLogin()
    await signInButton()

    // Anchored on purpose. The field is "Password" and the toggle is "Show
    // password", so a case-insensitive substring query matches both - which is
    // what broke two Playwright specs that asked for /password/i and got two
    // elements back.
    expect(passwordField()).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Show password' })).toBeVisible()
  })

  it('reveals the password and renames the toggle, so its name states the next action', async () => {
    const { user } = renderLogin()
    await signInButton()

    await user.click(screen.getByRole('button', { name: 'Show password' }))
    expect(passwordField()).toHaveAttribute('type', 'text')
    // A toggle whose name did not change would tell a screen-reader user the
    // password is still hidden while it is on screen in plain text.
    expect(await screen.findByRole('button', { name: 'Hide password' })).toBeVisible()
  })
})

describe('what the form sends', () => {
  it('refuses to post an empty form, and says which field is missing', async () => {
    const { user, fetch } = renderLogin()
    await user.click(await signInButton())

    expect(await screen.findByText('Email is required.')).toBeVisible()
    expect(screen.getByText('Password is required.')).toBeVisible()
    // The point of validating here rather than at the server: an empty submit
    // must not spend one of the account's rate-limited login attempts.
    expect(fetch.calls.some((call) => call.path === '/auth/login')).toBe(false)
  })

  it('rejects a malformed email before it reaches the rate limiter', async () => {
    const { user, fetch } = renderLogin()
    await signIn(user, 'not-an-email', 'whatever')

    expect(await screen.findByText('Enter a valid email address.')).toBeVisible()
    expect(fetch.calls.some((call) => call.path === '/auth/login')).toBe(false)
  })

  it('posts exactly the credentials and nothing else', async () => {
    const { user, fetch } = renderLogin({
      'POST /auth/login': fail(401, 'INVALID_CREDENTIALS', 'Email or password is incorrect'),
    })

    await signIn(user, 'ops@atcraft.example', 'correct horse battery')

    await waitFor(() => fetch.requestFor('POST', '/auth/login'))
    // Exact equality, not a subset match: a stray field here is a credential
    // travelling further than it has to, and the endpoint rejects unknowns.
    expect(fetch.requestFor('POST', '/auth/login')).toEqual({
      email: 'ops@atcraft.example',
      password: 'correct horse battery',
    })
  })

  it('reads the permission set back from /auth/me before letting the user through', async () => {
    let meCalls = 0
    const { user, fetch } = renderLogin({
      // The restore is refused; the call `login` makes afterwards succeeds.
      'GET /auth/me': () => {
        meCalls += 1
        return meCalls === 1
          ? fail(401, 'UNAUTHENTICATED', 'Not authenticated')
          : meResponse(['item.view'])
      },
      'POST /auth/login': ok(tokenPair()),
    })

    await signIn(user, 'ops@atcraft.example', 'correct horse battery')

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/'))
    // The token payload carries no permission set. Without this second call the
    // user lands on a shell where `can()` answers false for everything, and
    // every gated control stays hidden until the next hard reload.
    expect(meCalls).toBeGreaterThanOrEqual(2)
    // And exactly one login attempt: a double submit here counts twice against
    // the lockout counter.
    expect(fetch.calls.filter((call) => call.path === '/auth/login')).toHaveLength(1)
  })

  it('sends a user who must change their password to the change-password screen', async () => {
    let meCalls = 0
    const { user } = renderLogin({
      'GET /auth/me': () => {
        meCalls += 1
        return meCalls === 1
          ? fail(401, 'UNAUTHENTICATED', 'Not authenticated')
          : meResponse([], { must_change_password: true })
      },
      'POST /auth/login': ok(tokenPair({ must_change_password: true })),
    })

    await signIn(user, 'temp@atcraft.example', 'the temporary one')

    // An administrator-issued temporary password is only safe because it is
    // forced out of use at once. Landing on the dashboard instead leaves it
    // live for as long as the user cares to ignore it.
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/change-password'),
    )
  })

  it('completes the TOTP challenge before creating a browser session', async () => {
    let meCalls = 0
    const { user, fetch } = renderLogin({
      'GET /auth/me': () => {
        meCalls += 1
        return meCalls === 1
          ? fail(401, 'UNAUTHENTICATED', 'Not authenticated')
          : meResponse(['item.view'])
      },
      'POST /auth/login': ok({
        mfa_required: true,
        challenge_token: 'signed-mfa-challenge',
        expires_in: 300,
      }),
      'POST /auth/mfa/verify': ok(tokenPair()),
    })

    await signIn(user, 'mfa@atcraft.example', 'correct horse battery')
    expect(await screen.findByRole('heading', { name: 'Authentication code' })).toBeVisible()
    await user.type(screen.getByLabelText(/Authentication code/), '123456')
    await user.click(screen.getByRole('button', { name: 'Verify and sign in' }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/'))
    expect(fetch.requestFor('POST', '/auth/mfa/verify')).toEqual({
      challenge_token: 'signed-mfa-challenge',
      code: '123456',
    })
  })

  it('blocks duplicate MFA verification and cancels it when returning to password sign-in', async () => {
    let meCalls = 0
    let passwordCalls = 0
    let finishVerification!: (response: RouteBody) => void
    let finishPassword!: (response: RouteBody) => void
    const { user, fetch } = renderLogin({
      'GET /auth/me': () =>
        ++meCalls === 1
          ? fail(401, 'UNAUTHENTICATED', 'Not authenticated')
          : meResponse(['item.view']),
      'POST /auth/login': () =>
        ++passwordCalls === 1
          ? ok({ mfa_required: true, challenge_token: 'signed-mfa-challenge', expires_in: 300 })
          : new Promise<RouteBody>((resolve) => {
              finishPassword = resolve
            }),
      'POST /auth/mfa/verify': () =>
        new Promise<RouteBody>((resolve) => {
          finishVerification = resolve
        }),
    })
    const originalFetch = globalThis.fetch
    let verificationSignal: AbortSignal | null | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith('/auth/mfa/verify')) verificationSignal = init?.signal
        return originalFetch(input, init)
      }),
    )
    await signIn(user, 'mfa@atcraft.example', 'correct horse battery')
    await user.type(screen.getByLabelText(/Authentication code/), '123456')
    await user.click(screen.getByRole('button', { name: 'Verify and sign in' }))
    const verify = screen.getByRole('button', { name: /Verify and sign in/ })
    expect(verify).toBeDisabled()
    fireEvent.submit(verify.closest('form')!)
    expect(fetch.calls.filter((call) => call.path === '/auth/mfa/verify')).toHaveLength(1)
    expect(verificationSignal?.aborted).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Back to password' }))
    expect(verificationSignal?.aborted).toBe(true)
    const submit = await signInButton()
    expect(submit).toBeEnabled()
    await user.click(submit)
    expect(submit).toBeDisabled()
    await act(async () => {
      finishVerification(fail(401, 'UNAUTHENTICATED', 'Old verification failed'))
    })
    expect(submit).toBeDisabled()
    expect(screen.queryByText('Old verification failed')).not.toBeInTheDocument()
    await act(async () => {
      finishPassword(ok(tokenPair()))
    })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/'))
  })
})

describe('what the server says reaches the user', () => {
  it('shows the API message verbatim, not a generic sign-in failure', async () => {
    const { user } = renderLogin({
      'POST /auth/login': fail(
        423,
        'ACCOUNT_LOCKED',
        'This account is locked until 14:30 after 5 failed attempts.',
      ),
    })

    await signIn(user, 'ops@atcraft.example', 'correct horse battery')

    // "Sign-in failed" would leave the user retrying a password that is right,
    // against an account that will refuse it for another quarter of an hour.
    expect(
      await screen.findByText('This account is locked until 14:30 after 5 failed attempts.'),
    ).toBeVisible()
  })

  it('tells the user when the account is refused rather than leaving the form silent', async () => {
    const { user } = renderLogin({
      'POST /auth/login': fail(403, 'ACCOUNT_DISABLED', 'This account has been deactivated.'),
    })

    await signIn(user, 'gone@atcraft.example', 'correct horse battery')

    expect(await screen.findByText('This account has been deactivated.')).toBeVisible()
    // Reported, not fatal: the form is still usable for a different account.
    expect(await signInButton()).toBeEnabled()
  })

  it('reports a transport failure as one, instead of blaming the credentials', async () => {
    const { user } = renderLogin({
      'POST /auth/login': () => {
        throw new TypeError('Failed to fetch')
      },
    })

    await signIn(user, 'ops@atcraft.example', 'correct horse battery')

    // A dropped connection shown as "incorrect password" sends the user off to
    // reset a password that was never wrong.
    expect(await screen.findByText(/could not reach the service/i)).toBeVisible()
  })
})

describe('the rate limit', () => {
  /** The seconds the button is counting down, read off its own label. */
  function countdownSeconds(button: HTMLElement): number {
    const match = /try again in (\d+)s/i.exec(button.textContent ?? '')
    expect(match, `expected a countdown label, got "${button.textContent}"`).not.toBeNull()
    return Number(match![1])
  }

  it('holds the form for the wait the API asked for', async () => {
    const { user } = renderLogin({
      'POST /auth/login': fail(
        429,
        'RATE_LIMITED',
        'Too many sign-in attempts from this address.',
        { retry_after_seconds: 45 },
      ),
    })

    await signIn(user, 'ops@atcraft.example', 'correct horse battery')

    const waiting = await screen.findByRole('button', { name: /try again in \d+s/i })
    expect(waiting).toBeDisabled()
    // Counted down from what the API said, not from an invented number: telling
    // the user to wait longer than the limiter actually holds them is a support
    // call, and telling them to wait less is another rejected attempt.
    expect(countdownSeconds(waiting)).toBeLessThanOrEqual(45)
    expect(countdownSeconds(waiting)).toBeGreaterThan(40)
    // The limiter's own message names the limit that was hit; the countdown
    // lives on the button, so together they say what happened and for how long.
    expect(screen.getByText('Too many sign-in attempts from this address.')).toBeVisible()
  })

  it('falls back to a finite wait when the API sends no usable one', async () => {
    const { user } = renderLogin({
      'POST /auth/login': fail(429, 'RATE_LIMITED', 'Too many attempts.', {
        retry_after_seconds: null,
      }),
    })

    await signIn(user, 'ops@atcraft.example', 'correct horse battery')

    const waiting = await screen.findByRole('button', { name: /try again in \d+s/i })
    // The regression this pins: the fallback was once zero, which disabled the
    // button with no countdown to re-enable it - and a disabled submit button
    // suppresses implicit submission too, so pressing Enter could not clear it
    // either. The only way out was a page reload.
    const seconds = countdownSeconds(waiting)
    expect(seconds).toBeGreaterThan(0)
    expect(seconds).toBeLessThanOrEqual(30)
  })
})

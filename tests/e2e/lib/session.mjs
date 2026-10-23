/**
 * Signing in, for the specs and the sweeps alike.
 *
 * Two facts about the API shape everything here:
 *
 * 1. **The seeded administrator cannot reach the application as seeded.**
 *    `src.core.seed` flags the account `must_change_password`, whatever
 *    password it was given, and `deps.py` then refuses every route except
 *    `/auth/me`, `/auth/change-password` and logout. The SPA mirrors that by
 *    routing to `/change-password` outside the shell. So a suite that signs in
 *    and waits for the navigation never sees it, and its failure - a timeout
 *    on a page whose heading reads "Choose a new password" - names none of
 *    this. `readyAdminToken` owns the bootstrap: it signs in, replaces the
 *    temporary password through the API, and signs in again.
 *
 * 2. **Login is rate limited per email: five attempts in five minutes.** A
 *    wrong guess counts and a success resets the counter, so a bootstrap tries
 *    the working password first and the seed password only on a 401. Every
 *    other login in a run succeeds and costs nothing.
 *
 * Plain JavaScript on purpose: the sweep scripts are `.mjs` and cannot import
 * TypeScript, while Playwright's loader imports this from the `.ts` specs
 * without complaint. `session.d.ts` beside it carries the types.
 */

import { ADMIN_EMAIL, ADMIN_PASSWORD, API_URL, BASE_URL, BOOTSTRAP_PASSWORD } from './env.mjs'

/**
 * @typedef {{ status: number, body: any }} ApiReply
 */

/**
 * POST JSON to the API and return the status with the parsed envelope.
 *
 * @param {string} path
 * @param {unknown} payload
 * @param {string} [token]
 * @returns {Promise<ApiReply>}
 */
export async function postJson(path, payload, token) {
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))
  return { status: response.status, body }
}

/**
 * One login attempt. Never throws on a refusal: the caller decides what a 401
 * or a 429 means for the run.
 *
 * @param {string} email
 * @param {string} password
 * @returns {Promise<ApiReply>}
 */
export function login(email, password) {
  return postJson('/auth/login', { email, password })
}

/** @param {ApiReply} reply */
function describe(reply) {
  const error = reply.body?.error
  return error
    ? `${reply.status} ${error.code ?? ''} ${error.message ?? ''}`.trim()
    : String(reply.status)
}

/**
 * A working administrator access token, bootstrapping the account on the way.
 *
 * Order of business:
 *
 *   - Sign in with `password`. That is the steady state, and the only login a
 *     run after the first ever makes.
 *   - On a 401, sign in with `bootstrapPassword` - the value the seed was run
 *     with. A fresh database is the only time this happens.
 *   - If the account is still on a temporary password, replace it with
 *     `password` through `/auth/change-password` (the one business route a
 *     temporary-password principal may call), then sign in again: the access
 *     token issued before the change stops working the moment it lands.
 *
 * Throws with the actual cause and the operator's next step when none of that
 * produces a session - including the case the API cannot resolve on its own,
 * where the seed was given `password` directly and the forced change refuses
 * to accept the same value.
 *
 * @param {{ email?: string, password?: string, bootstrapPassword?: string }} [account]
 * @returns {Promise<string>} the access token
 */
export async function readyAdminToken(account = {}) {
  const email = account.email ?? ADMIN_EMAIL
  const password = account.password ?? ADMIN_PASSWORD
  const bootstrapPassword = account.bootstrapPassword ?? BOOTSTRAP_PASSWORD

  let used = password
  let reply = await login(email, password)
  if (reply.status === 401 && bootstrapPassword && bootstrapPassword !== password) {
    used = bootstrapPassword
    reply = await login(email, bootstrapPassword)
  }

  if (reply.status === 429) {
    throw new Error(
      `${email} is rate limited at login (${describe(reply)}). LOGIN_PER_EMAIL allows five ` +
        'attempts per five minutes and a wrong password counts; wait for the window to close.',
    )
  }
  if (reply.status !== 200) {
    throw new Error(
      `could not sign in as ${email} (${describe(reply)}). Seed the database with ` +
        '`python -m src.core.seed --admin-password "$E2E_BOOTSTRAP_PASSWORD"` and set ' +
        'E2E_BOOTSTRAP_PASSWORD in tests/e2e/.env - the seed generates a password otherwise, ' +
        'and prints it once.',
    )
  }

  const { access_token: token, user } = reply.body.data
  if (!user?.must_change_password) return token

  if (used === password) {
    throw new Error(
      `${email} is on a temporary password that already equals E2E_ADMIN_PASSWORD, and the ` +
        'forced change refuses the same value. Seed with a distinct E2E_BOOTSTRAP_PASSWORD, or ' +
        'change the password once by hand.',
    )
  }
  const changed = await postJson(
    '/auth/change-password',
    { current_password: used, new_password: password },
    token,
  )
  if (changed.status !== 200) {
    throw new Error(
      `replacing the temporary password for ${email} failed (${describe(changed)}). ` +
        'E2E_ADMIN_PASSWORD must satisfy the password policy (12+ characters, mixed classes).',
    )
  }
  const again = await login(email, password)
  if (again.status !== 200) {
    throw new Error(`the new password was accepted but the next login failed (${describe(again)})`)
  }
  return again.body.data.access_token
}

/**
 * Sign a browser page in through the login form.
 *
 * Fields are addressed by role, never by label text: `getByLabel('Password')`
 * substring-matches the field's own "Show password" toggle as well as the
 * input, and resolves to two elements - a strict-mode violation, not a flake.
 *
 * Waits for the router to leave `/login`, and then refuses to return a page
 * parked on `/change-password`: nothing that follows can work there, and the
 * failure would otherwise surface as a timeout on the next screen.
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ email?: string, password?: string, allowTemporaryPassword?: boolean }} [account]
 */
export async function signInThroughForm(page, account = {}) {
  const email = account.email ?? ADMIN_EMAIL
  const password = account.password ?? ADMIN_PASSWORD

  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('textbox', { name: 'Email' }).fill(email)
  await page.getByRole('textbox', { name: 'Password' }).fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()

  try {
    await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 20_000 })
  } catch (error) {
    // The form shows the API's own refusal in an alert; surface that instead of
    // "waiting for navigation", which says nothing about why.
    const shown = await page
      .getByRole('alert')
      .first()
      .textContent({ timeout: 1_000 })
      .catch(() => null)
    throw new Error(
      `${email} did not get past the login form` + (shown ? `: ${shown.trim()}` : ''),
      { cause: error },
    )
  }

  if (
    new URL(page.url()).pathname.startsWith('/change-password') &&
    !account.allowTemporaryPassword
  ) {
    throw new Error(
      `${email} is still on a temporary password and was routed to /change-password. ` +
        'Bootstrap the account first: readyAdminToken() does it through the API.',
    )
  }
}

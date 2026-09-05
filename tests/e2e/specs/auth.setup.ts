import { expect, test as setup } from '@playwright/test'

import { ADMIN_EMAIL, ADMIN_PASSWORD } from '../lib/env.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

const AUTH_FILE = './.auth/admin.json'

/**
 * Signs in once and saves the browser storage state for every other spec.
 *
 * Logging in at the top of each test would add a real Argon2 verification per
 * spec — deliberately expensive work that says nothing about the feature under
 * test.
 *
 * The account is bootstrapped through the API first. As seeded, the
 * administrator is flagged `must_change_password`, the API refuses every
 * business route, and the SPA parks the session on `/change-password` outside
 * the shell - so the navigation this waits for never appeared, the setup timed
 * out, and every spec that depends on it was blocked. `readyAdminToken`
 * replaces the temporary password with `E2E_ADMIN_PASSWORD` on the first run
 * against a fresh database and is a single successful login on every run
 * after that. See `.env.example` for the seed command it expects.
 */
setup('authenticate as administrator', async ({ page }) => {
  await readyAdminToken()

  await signInThroughForm(page, { email: ADMIN_EMAIL, password: ADMIN_PASSWORD })

  // Wait for the shell, not just a URL change: the router redirects before the
  // session restore finishes, and asserting on the URL alone is flaky.
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeVisible({ timeout: 30_000 })

  await page.context().storageState({ path: AUTH_FILE })
})

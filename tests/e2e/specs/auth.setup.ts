import { expect, test as setup } from '@playwright/test'

const AUTH_FILE = './.auth/admin.json'

/**
 * Signs in once and saves the browser storage state for every other spec.
 *
 * Logging in at the top of each test would add a real Argon2 verification per
 * spec — deliberately expensive work that says nothing about the feature under
 * test.
 */
setup('authenticate as administrator', async ({ page }) => {
  const email = process.env.E2E_ADMIN_EMAIL ?? 'admin@atcraft.example'
  const password = process.env.E2E_ADMIN_PASSWORD ?? 'ChangeMe!Atcraft2026'

  await page.goto('/login')

  // Addressed by role, not by label. `getByLabel('Password')` substring-matches
  // the field's own "Show password" toggle as well as the input, and resolves to
  // two elements - which is a strict-mode violation, not a flake, so this spec
  // could never have got past its own first step.
  await page.getByRole('textbox', { name: 'Email' }).fill(email)
  await page.getByRole('textbox', { name: 'Password' }).fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()

  // Wait for the shell, not just a URL change: the router redirects before the
  // session restore finishes, and asserting on the URL alone is flaky.
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeVisible({ timeout: 30_000 })

  await page.context().storageState({ path: AUTH_FILE })
})

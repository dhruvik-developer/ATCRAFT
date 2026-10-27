import { defineConfig, devices } from '@playwright/test'

import { BASE_URL } from './lib/env.mjs'

/**
 * End-to-end configuration (master rules section 66).
 *
 * These tests run against a *running stack* — they are not unit tests with a
 * browser attached. Start the environment first:
 *
 * Follow the root README to start the API, frontend, PostgreSQL and Redis;
 * populate the installation with `make seed-local`. In this folder, `npm ci`
 * installs the test tooling and `npm test` checks types before Playwright.
 *
 * Configuration comes from `lib/env.mjs`, which loads `tests/e2e/.env` when it
 * exists - copy `.env.example` to get one - and is the same module the specs
 * and the sweep scripts read. See `.env.example` for how the seeded
 * administrator is bootstrapped past its forced password change.
 *
 * The suite covers the critical journeys only. E2E is the slowest and most
 * brittle layer, so it verifies that the modules are wired together correctly;
 * business-rule detail belongs in the backend unit and integration tests.
 */
export default defineConfig({
  testDir: './specs',
  outputDir: './test-results',
  timeout: 90_000,
  expect: { timeout: 15_000 },

  // Serial locally: the journeys share seeded master data and a parallel run
  // would have two specs racing for the same stock.
  fullyParallel: false,
  workers: 1,

  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,

  reporter: process.env.CI
    ? [['github'], ['html', { outputFolder: 'playwright-report', open: 'never' }]]
    : [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    // Authenticate once and save the storage state, rather than logging in at
    // the top of every spec.
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      // The saved state is a refresh cookie, and refresh tokens rotate: the
      // first context to present it gets a new one and the file's copy is
      // spent. A second context restored from the same file is a replay, which
      // revokes the whole family and lands on /login. So each spec restores
      // the file into ONE context in its beforeAll and shares that page, and
      // no spec uses the per-test `page` fixture. Keep it that way.
      use: { ...devices['Desktop Chrome'], storageState: './.auth/admin.json' },
      dependencies: ['setup'],
    },
  ],
})

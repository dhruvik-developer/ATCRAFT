import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end configuration (master rules section 66).
 *
 * These tests run against a *running stack* — they are not unit tests with a
 * browser attached. Start the environment first:
 *
 *     make dev-docker        (or: make dev, then make migrate && make seed)
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
  workers: process.env.CI ? 1 : 1,

  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,

  reporter: process.env.CI
    ? [['github'], ['html', { outputFolder: 'playwright-report', open: 'never' }]]
    : [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],

  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    // Authenticate once and reuse the storage state, rather than logging in at
    // the top of every spec.
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], storageState: './.auth/admin.json' },
      dependencies: ['setup'],
    },
  ],
})

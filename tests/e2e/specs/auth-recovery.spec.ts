/** A temporary API refusal must not discard a valid rotating-cookie session. */
import { expect, test } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { createNavigator } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

test.beforeAll(async () => {
  await readyAdminToken()
})

for (const failure of ['refresh rate limit', 'profile service outage'] as const) {
  test(`a ${failure} keeps the session available for an explicit retry`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    const navigation = createNavigator(page)
    await signInThroughForm(page)
    await expect(page.getByRole('button', { name: 'Open navigation', exact: true })).toBeVisible()
    await navigation.settle()

    const prefix = new URL(API_URL).pathname.replace(/\/$/, '')
    const blockedPath = `${prefix}/auth/${failure === 'refresh rate limit' ? 'refresh' : 'me'}`
    let unavailable = true
    let injected = 0
    let logins = 0
    let refreshed = 0
    let verified = 0
    page.on('request', (request) => {
      if (request.method() === 'POST' && new URL(request.url()).pathname === `${prefix}/auth/login`)
        logins += 1
    })
    page.on('response', (response) => {
      const path = new URL(response.url()).pathname
      if (response.status() === 200 && path === `${prefix}/auth/refresh`) refreshed += 1
      if (response.status() === 200 && path === `${prefix}/auth/me`) verified += 1
    })
    await page.route(`**${blockedPath}`, async (route) => {
      const request = route.request()
      // Allow the initial unauthenticated profile read to receive its genuine
      // 401. The profile-outage case occurs after the real refresh has rotated
      // the HttpOnly cookie, while the refresh429 case never reaches the server.
      const matches =
        failure === 'refresh rate limit'
          ? request.method() === 'POST'
          : request.method() === 'GET' && Boolean(request.headers().authorization)
      if (!unavailable || !matches) {
        await route.continue()
        return
      }
      injected += 1
      await route.fulfill({
        status: failure === 'refresh rate limit' ? 429 : 503,
        contentType: 'application/json',
        ...(failure === 'refresh rate limit' ? { headers: { 'Retry-After': '1' } } : {}),
        body: JSON.stringify({
          success: false,
          error: {
            code: failure === 'refresh rate limit' ? 'RATE_LIMITED' : 'SERVICE_UNAVAILABLE',
            message: 'The service is temporarily unavailable. Please try again.',
            details: failure === 'refresh rate limit' ? { retry_after_seconds: 1 } : {},
          },
        }),
      })
    })

    await page.goto(`${BASE_URL}/items`)
    await expect(
      page.getByRole('heading', { name: 'We could not restore your session', exact: true }),
    ).toBeVisible()
    await expect(page).toHaveURL(`${BASE_URL}/items`)
    await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Main', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Sign in instead', exact: true })).toBeVisible()
    expect(injected).toBeGreaterThan(0)
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
    ).toBeLessThanOrEqual(1)

    unavailable = false
    const retry = page.getByRole('button', { name: 'Try again', exact: true })
    await expect(retry).toBeEnabled()
    await retry.click()
    await expect(page.getByRole('button', { name: 'Open navigation', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Items', exact: true })).toBeVisible()
    await expect(page).toHaveURL(`${BASE_URL}/items`)
    expect(refreshed, 'The retry must retain and use the existing session credential').toBe(1)
    expect(verified).toBeGreaterThan(0)
    expect(logins, 'Session recovery must not submit credentials again').toBe(0)
    if (failure === 'refresh rate limit')
      expect(injected, 'Concurrent profile reads must share one failed refresh').toBe(1)
  })
}

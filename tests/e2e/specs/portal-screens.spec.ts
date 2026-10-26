/** Real browser coverage uses a bound principal for each external portal. */
import { expect, test } from '@playwright/test'

import { accountForAudience, portalIdentityPath } from '../lib/audiences.mjs'
import { API_URL, BASE_URL } from '../lib/env.mjs'
import { createNavigator, errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

const screens = {
  vendor: [
    '/vendor',
    '/vendor/orders',
    '/vendor/shipments',
    '/vendor/receipts',
    '/vendor/invoices',
    '/vendor/payments',
    '/profile',
    '/notifications?tab=preferences',
  ],
  dealer: [
    '/dealer',
    '/dealer/catalog',
    '/dealer/cart',
    '/dealer/wishlist',
    '/dealer/orders',
    '/dealer/invoices',
    '/dealer/returns',
    '/dealer/account',
    '/profile',
    '/notifications?tab=preferences',
  ],
} as const

for (const audience of ['vendor', 'dealer'] as const) {
  for (const width of [375, 768, 1440]) {
    test(`${audience} screens preserve scope and fit ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 768 ? 1024 : 900 })
      const account = accountForAudience(audience)
      const { navigate, settle } = createNavigator(page)
      const failures: string[] = []
      let restoringSession = false
      const restoration: string[] = []
      const apiPath = new URL(API_URL).pathname
      page.on('pageerror', (error) => failures.push(error.message))
      page.on('response', (response) => {
        const path = new URL(response.url()).pathname
        if (restoringSession) {
          if (path === `${apiPath}/auth/me` && response.status() === 401) {
            restoration.push('me401')
            return
          }
          if (path === `${apiPath}/auth/refresh` && response.status() === 200)
            restoration.push('refresh200')
          if (path === `${apiPath}/auth/me` && response.status() === 200) restoration.push('me200')
        }
        if (path.startsWith(`${apiPath}/`) && response.status() >= 400)
          failures.push(`HTTP ${response.status()} ${path}`)
      })
      await readyAdminToken(account)
      await signInThroughForm(page, account)
      await settle()
      for (const route of screens[audience]) {
        await navigate(route)
        await expect(page).toHaveURL(`${BASE_URL}${route}`)
        await expect(page.locator('main').getByRole('heading').first()).toBeVisible()
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
          `${route} overflows`,
        ).toBeLessThanOrEqual(1)
        expect(await errorToastFailures(page), route).toEqual([])
      }
      // A hard reload loses the memory-only access token. Prove the cookie is
      // refreshed and the portal identity restored before checking the redirect;
      // the SPA request tracker belongs to the previous document's requests.
      restoringSession = true
      await Promise.all([
        page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname === `${apiPath}/auth/refresh` &&
            response.status() === 200 &&
            response.request().method() === 'POST',
        ),
        page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname === `${apiPath}/auth/me` && response.status() === 200,
        ),
        page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname === `${apiPath}${portalIdentityPath(audience)}` &&
            response.status() === 200,
        ),
        page.goto(`${BASE_URL}/admin/users`),
      ])
      await expect(page).toHaveURL(`${BASE_URL}/${audience}`)
      await expect(page.locator('main').getByRole('heading').first()).toBeVisible()
      // Vite StrictMode can repeat profile reads on mount. Preserve the
      // ordered authentication phases and prove the rotating refresh cookie
      // was consumed exactly once; a later 401 still fails this assertion.
      expect(restoration.filter((event) => event === 'refresh200')).toHaveLength(1)
      expect(restoration.filter((event, index) => event !== restoration[index - 1])).toEqual([
        'me401',
        'refresh200',
        'me200',
      ])
      await expect(page.getByRole('navigation', { name: 'Main', exact: true })).toHaveCount(0)
      expect(await errorToastFailures(page)).toEqual([])
      expect(failures).toEqual([])
    })
  }
}

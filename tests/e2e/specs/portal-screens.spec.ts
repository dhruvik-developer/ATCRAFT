/** Real browser coverage uses a bound principal for each external portal. */
import { expect, test, type Page } from '@playwright/test'

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

async function expectPreferencesToFit(page: Page) {
  const table = page.getByRole('table', {
    name: 'Notification channels per event type',
    exact: true,
  })
  await expect(table).toBeVisible()
  const layout = await table.evaluate((element) => {
    const container = element.parentElement
    const main = element.closest('main')
    const lastColumn = element.querySelector('thead th:last-child')
    if (!container || !main || !lastColumn)
      throw new Error('Preferences table structure is missing')

    const measure = (node: Element) => {
      const box = node.getBoundingClientRect()
      const style = getComputedStyle(node)
      return {
        tag: node.tagName.toLowerCase(),
        id: node.id,
        classes: node.className,
        left: box.left,
        right: box.right,
        width: box.width,
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        display: style.display,
        minWidth: style.minWidth,
        overflowX: style.overflowX,
      }
    }
    const ancestors = []
    for (let node: Element | null = container; node; node = node.parentElement) {
      ancestors.push(measure(node))
    }
    const frame = measure(container)
    const previousScroll = container.scrollLeft
    // Prove the final channel stays reachable without clipping the table or page.
    container.scrollLeft = container.scrollWidth
    const reachedScroll = container.scrollLeft
    const lastColumnBox = lastColumn.getBoundingClientRect()
    const lastColumnPosition = {
      left: lastColumnBox.left,
      right: lastColumnBox.right,
    }
    container.scrollLeft = previousScroll

    return {
      pageOverflow: document.documentElement.scrollWidth - innerWidth,
      viewportWidth: innerWidth,
      frame,
      main: measure(main),
      table: measure(element),
      ancestors,
      reachedScroll,
      lastColumn: lastColumnPosition,
    }
  })
  const diagnostic = `Notification preferences layout: ${JSON.stringify(layout)}`
  expect(layout.pageOverflow, diagnostic).toBeLessThanOrEqual(1)
  expect(layout.frame.left, diagnostic).toBeGreaterThanOrEqual(Math.max(0, layout.main.left) - 1)
  expect(layout.frame.right, diagnostic).toBeLessThanOrEqual(
    Math.min(layout.viewportWidth, layout.main.right) + 1,
  )
  if (layout.frame.scrollWidth > layout.frame.clientWidth + 1) {
    expect(['auto', 'scroll'], diagnostic).toContain(layout.frame.overflowX)
    expect(layout.reachedScroll, diagnostic).toBeGreaterThan(0)
    const scrollRegion = page.getByRole('region', {
      name: 'Notification channel preferences',
      exact: true,
    })
    await scrollRegion.evaluate((element) => {
      element.scrollLeft = 0
    })
    await scrollRegion.focus()
    await expect(scrollRegion).toBeFocused()
    await page.keyboard.press('ArrowRight')
    await expect
      .poll(() => scrollRegion.evaluate((element) => element.scrollLeft))
      .toBeGreaterThan(0)
    await scrollRegion.evaluate((element) => {
      element.scrollLeft = 0
    })
  }
  expect(layout.lastColumn.left, diagnostic).toBeGreaterThanOrEqual(layout.frame.left - 1)
  expect(layout.lastColumn.right, diagnostic).toBeLessThanOrEqual(layout.frame.right + 1)
}

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
        if (route === '/notifications?tab=preferences') {
          await expectPreferencesToFit(page)
        } else {
          expect(
            await page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
            `${route} overflows`,
          ).toBeLessThanOrEqual(1)
        }
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

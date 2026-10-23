/** Browser regressions for layout and keyboard behavior jsdom cannot measure. */
import { expect, test, type Locator, type Page } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

const run = Date.now().toString(36)
const account = { email: `viewport.${run}@atcraft.example`, password: 'LayoutPass!Atcraft2026' }
const longName = 'UI readiness operator with a deliberately long display name '.repeat(3).trim()

test.beforeAll(async () => {
  const token = await readyAdminToken()
  const response = await fetch(`${API_URL}/users`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...account,
      full_name: longName,
      must_change_password: false,
      extra_permissions: ['dashboard.view', 'item.view', 'warehouse.view', 'inventory.view'],
    }),
  })
  expect(response.ok, `could not create layout account: ${response.status}`).toBeTruthy()
})

async function fitsViewport(page: Page, element: Locator) {
  await expect(element).toBeVisible()
  const box = await element.boundingBox()
  expect(box).not.toBeNull()
  const viewport = page.viewportSize()!
  expect(box!.x).toBeGreaterThanOrEqual(-1)
  expect(box!.y).toBeGreaterThanOrEqual(-1)
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width + 1)
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height + 1)
}

test('phone navigation contains focus, closes on route change, and releases on desktop', async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 667 })
  await signInThroughForm(page)
  const opener = page.getByRole('button', { name: 'Open navigation', exact: true })
  const drawer = page.getByRole('dialog', { name: 'Main navigation', exact: true })
  await opener.click()
  await fitsViewport(page, drawer)
  const close = drawer.getByRole('button', { name: 'Close navigation', exact: true })
  await expect(close).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  expect(await drawer.evaluate((element) => element.contains(document.activeElement))).toBeTruthy()
  await page.keyboard.press('Tab')
  await expect(close).toBeFocused()
  await page.screenshot({ path: 'readiness-artifacts/mobile-navigation.png' })
  await page.keyboard.press('Escape')
  await expect(drawer).toHaveCount(0)
  await expect(opener).toBeFocused()

  await opener.click()
  await drawer.locator('a[href="/items"]').click()
  await expect(page).toHaveURL(`${BASE_URL}/items`)
  await expect(drawer).toHaveCount(0)
  await page.screenshot({ path: 'readiness-artifacts/mobile-items.png' })
  await opener.click()
  await page.setViewportSize({ width: 1280, height: 800 })
  await expect(drawer).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: 'Main', exact: true })).toBeVisible()
})

test('a long account name fits a tablet and its final menu action fits a short screen', async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 })
  await signInThroughForm(page, account)
  await expect(page.locator('main')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768)
  const accountMenu = page
    .locator('header')
    .getByRole('button', { name: new RegExp(longName.slice(0, 30)) })
  await accountMenu.click()
  const menu = page.getByRole('menu')
  await fitsViewport(page, menu)
  await page.setViewportSize({ width: 667, height: 375 })
  await page.keyboard.press('End')
  await expect(menu.getByRole('menuitem').last()).toBeInViewport()
  await fitsViewport(page, menu)
  await page.screenshot({ path: 'readiness-artifacts/landscape-account-menu.png' })
})

test('a form select keeps its last option reachable in a short viewport', async ({ page }) => {
  await page.setViewportSize({ width: 667, height: 375 })
  await signInThroughForm(page)
  await page.goto(`${BASE_URL}/items`)
  await page.getByRole('button', { name: 'New item', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'New item', exact: true })
  await fitsViewport(page, dialog)
  await dialog.getByRole('combobox', { name: /^Base unit/ }).click()
  const list = page.getByRole('listbox')
  await expect(list.getByRole('option').first()).toBeVisible()
  await page.keyboard.press('End')
  await expect(list.getByRole('option').last()).toBeInViewport()
  await fitsViewport(page, list)
  await page.screenshot({ path: 'readiness-artifacts/landscape-select.png' })
})

test('the ageing chart screen reader table does not widen a phone page', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 667 })
  await signInThroughForm(page)
  await page.goto(`${BASE_URL}/finance`)
  await page.getByRole('tab', { name: 'Ageing', exact: true }).click()
  await expect(
    page.locator('table caption').filter({ hasText: 'Outstanding amount by party' }),
  ).toHaveCount(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
  await page.screenshot({ path: 'readiness-artifacts/mobile-ageing.png' })
})

test('readiness sweeps flag an invalid HTTP 200 response shown as an error toast', async ({
  page,
}) => {
  await signInThroughForm(page)
  await page.route('**/api/v1/products/items?**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: [] }),
    }),
  )
  await page.goto(`${BASE_URL}/items`)
  await expect(page.getByRole('alert').first()).toBeVisible()
  expect(await errorToastFailures(page)).toEqual(
    expect.arrayContaining([expect.stringContaining('invalid response')]),
  )
})

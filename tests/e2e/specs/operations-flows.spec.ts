/** Browser/API coverage for the procurement reports added in B5. */
import { expect, test } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

test('purchase-order summary runs by category from the report screen', async ({ page }) => {
  await readyAdminToken()
  await signInThroughForm(page)
  await page.goto(
    `${BASE_URL}/reports?report=PURCHASE_ORDER_SUMMARY&date_from=2026-09-01&date_to=2026-09-30`,
  )

  await expect(page.getByRole('heading', { name: 'Purchase order summary' })).toBeVisible()
  await page.getByRole('combobox', { name: 'Group by' }).click()
  await page.getByRole('option', { name: 'Category' }).click()

  const reportResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `${new URL(API_URL).pathname}/reports/purchase-order-summary` &&
      response.request().method() === 'GET',
  )
  await page.getByRole('button', { name: 'Run report' }).click()

  const response = await reportResponse
  expect(response.status()).toBe(200)
  expect(new URL(response.url()).searchParams.get('group_by')).toBe('category')
  expect(new URL(response.url()).searchParams.get('date_from')).toBe('2026-09-01')
  expect(new URL(response.url()).searchParams.get('date_to')).toBe('2026-09-30')
  expect(await errorToastFailures(page)).toEqual([])
})

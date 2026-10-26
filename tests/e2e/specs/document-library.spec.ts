/** Regression for nginx's implicit trailing-slash redirect at the API edge. */
import { expect, test } from '@playwright/test'

import { BASE_URL } from '../lib/env.mjs'
import { createNavigator, errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

test('document collection reaches the API without redirecting and later screens still load', async ({
  page,
  request,
}) => {
  // Use the browser origin deliberately. A direct API URL bypasses both nginx
  // hops and cannot catch the production-only 301 that changed the port.
  const collection = new URL('/api/v1/documents?limit=1', BASE_URL).href
  const anonymous = await request.get(collection, { maxRedirects: 0 })
  expect(anonymous.status()).toBe(401)
  expect(anonymous.headers().location).toBeUndefined()

  await readyAdminToken()
  const { navigate, settle } = createNavigator(page)
  await signInThroughForm(page)
  await settle()
  const listResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/documents',
  )
  await navigate('/documents')
  const response = await listResponse
  expect(response.status()).toBe(200)
  expect(response.request().redirectedFrom()).toBeNull()
  await expect(page.locator('main').getByRole('heading', { name: 'Documents', exact: true })).toBeVisible()
  expect(await errorToastFailures(page)).toEqual([])

  await navigate('/reports')
  await expect(page.locator('main').getByRole('heading', { name: 'Reports', exact: true })).toBeVisible()
  expect(await errorToastFailures(page)).toEqual([])
})

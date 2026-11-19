/** Verify form downloads are deferred using isolated identities and read-only fixtures. */
import { expect, test } from '@playwright/test'

import { BASE_URL } from '../lib/env.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

const screens = [
  {
    route: '/items',
    module: 'item-form-dialog',
    button: 'New item',
    permissions: ['item.view', 'item.create'],
  },
  {
    route: '/admin/users',
    module: 'user-dialogs',
    button: 'New user',
    permissions: ['user.view', 'user.create'],
  },
] as const

for (const screen of screens) {
  test(`${screen.route} downloads its form only on first use`, async ({
    page,
  }) => {
    await page
      .context()
      .addCookies([
        { name: 'atcraft_csrf', value: 'lazy-form-test', url: BASE_URL },
      ])
    const downloads: string[] = []
    const writes: string[] = []
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname
      if (path.includes(screen.module)) downloads.push(path)
    })
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname.replace('/api/v1', '')
      if (request.method() !== 'GET') writes.push(path)
      const data =
        path === '/auth/me'
          ? {
              user_id: '01a00000-0000-7000-8000-000000000001',
              company_id: '01a00000-0000-7000-8000-0000000000c0',
              company_name: 'Loading test',
              email: 'loading@example.test',
              full_name: 'Loading test',
              is_superuser: false,
              must_change_password: false,
              mfa_enabled: true,
              mfa_enrolment_required: false,
              roles: [],
              permissions: screen.permissions,
            }
          : []
      await route.fulfill({
        json: {
          success: true,
          data,
          message: 'Data loaded',
          pagination: {
            page: 1,
            limit: 25,
            total: 0,
            total_pages: 0,
            has_next: false,
            has_previous: false,
          },
        },
      })
    })

    await page.goto(`${BASE_URL}${screen.route}`)
    const open = page.getByRole('button', { name: screen.button, exact: true }).first()
    await expect(open).toBeVisible()
    await page.waitForLoadState('networkidle')
    expect(downloads).toEqual([])

    await open.click()
    const dialog = page.getByRole('dialog', {
      name: screen.button,
      exact: true,
    })
    await expect(dialog).toBeVisible()
    expect(downloads.length).toBeGreaterThan(0)
    const firstDownloads = downloads.length
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    await open.click()
    await expect(dialog).toBeVisible()
    expect(downloads).toHaveLength(firstDownloads)
    expect(writes).toEqual([])
  })
}

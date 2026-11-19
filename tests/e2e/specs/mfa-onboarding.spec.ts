/** Browser layout and routing regression; API policy is covered by the auth tests. */
import { expect, test } from '@playwright/test'

import { BASE_URL } from '../lib/env.mjs'

// This flow uses an isolated, simulated identity and never changes a real account.
test.use({ storageState: { cookies: [], origins: [] } })

for (const width of [320, 768, 1440]) {
  test(`required password and two-factor setup at ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await page
      .context()
      .addCookies([
        { name: 'atcraft_csrf', value: 'onboarding-test', url: BASE_URL },
      ])
    let passwordRequired = true
    let enrolled = false
    const calls: string[] = []
    const unexpected: string[] = []
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname.replace('/api/v1', '')
      calls.push(path)
      let data: unknown
      if (path === '/auth/me') {
        data = {
          user_id: '01a00000-0000-7000-8000-000000000001',
          company_id: '01a00000-0000-7000-8000-0000000000c0',
          company_name: 'Onboarding test',
          email: 'onboarding@example.test',
          full_name: 'Onboarding test',
          is_superuser: false,
          must_change_password: passwordRequired,
          mfa_enabled: enrolled,
          mfa_enrolment_required: !enrolled,
          roles: ['ADMIN'],
          permissions: [],
        }
      } else if (
        path === '/auth/change-password' &&
        request.method() === 'POST'
      ) {
        passwordRequired = false
        data = { revoked_sessions: 0 }
      } else if (path === '/auth/mfa/setup' && request.method() === 'POST') {
        data = {
          secret: 'JBSWY3DPEHPK3PXP',
          provisioning_uri:
            'otpauth://totp/ATCRAFT:test?secret=JBSWY3DPEHPK3PXP',
        }
      } else if (path === '/auth/mfa/confirm' && request.method() === 'POST') {
        expect(request.postDataJSON()).toEqual({ code: '123456' })
        enrolled = true
        data = { mfa_enabled: true, revoked_sessions: 0 }
      } else if (enrolled && path === '/notifications/channels') {
        data = { whatsapp_enabled: false, sms_enabled: false }
      } else if (enrolled && path === '/auth/sessions') {
        await route.fulfill({
          json: {
            success: true,
            data: [],
            message: 'Sessions loaded',
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
        return
      } else {
        unexpected.push(path)
        await route.fulfill({ status: 500, json: { success: false } })
        return
      }
      await route.fulfill({
        json: { success: true, data, message: 'Account updated' },
      })
    })

    await page.goto(`${BASE_URL}/profile`)
    await expect(
      page.getByRole('heading', { name: 'Choose a new password' }),
    ).toBeVisible()
    await page
      .getByLabel(/^Current password(?! for)/)
      .fill('Previous-Password-123!')
    await page.getByLabel(/^New password/).fill('Replacement-Password-123!')
    await page
      .getByLabel(/^Repeat new password/)
      .fill('Replacement-Password-123!')
    await page
      .getByRole('button', { name: 'Change password', exact: true })
      .click()

    await expect(page).toHaveURL(`${BASE_URL}/profile`)
    await expect(
      page.getByRole('heading', { name: 'Set up two-factor authentication' }),
    ).toBeVisible()
    const password = page.getByLabel(/Current password for two-factor setup/)
    await expect(password).toBeFocused()
    await expect(page.getByRole('navigation', { name: 'Main' })).toHaveCount(0)
    expect(
      calls.every((path) =>
        ['/auth/me', '/auth/change-password'].includes(path),
      ),
    ).toBe(true)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - innerWidth,
      ),
    ).toBeLessThanOrEqual(1)
    await page.screenshot({
      path: testInfo.outputPath('setup.png'),
      fullPage: true,
    })

    await password.fill('Replacement-Password-123!')
    await page
      .getByRole('button', { name: 'Set up two-factor', exact: true })
      .click()
    await expect(
      page.getByText('JBSWY3DPEHPK3PXP', { exact: true }),
    ).toBeVisible()
    await page.getByLabel(/Authenticator code/).fill('123456')
    await page
      .getByRole('button', { name: 'Enable two-factor', exact: true })
      .click()

    await expect(page).toHaveURL(`${BASE_URL}/profile`)
    await expect(
      page.getByRole('heading', { name: 'Your account' }),
    ).toBeVisible()
    await expect(page.getByText('Messaging settings unavailable')).toHaveCount(
      0,
    )
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  })
}

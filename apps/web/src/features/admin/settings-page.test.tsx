/**
 * The settings screen is a switch over a nullable stored value, so the tests
 * pair the two states that look alike on screen but answer different questions:
 * "the company chose off" and "no company choice is stored". The first sends
 * `false`, the second sends explicit `null` to withdraw the override, and the
 * page must not confuse them.
 *
 * The write is gated on `settings.manage`, not on the read right that admits the
 * page. A reader sees the value and cannot change it - and the test proves the
 * control is genuinely disabled rather than merely failing at the API.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { ok, renderScreen, type RouteTable } from '@/test/harness'

import SettingsPage from './settings-page'

function settings(autoPlan: boolean, isDefault: boolean) {
  return ok({ auto_plan_on_confirm: autoPlan, is_default: isDefault })
}

function renderSettings(permissions: string[], routes: RouteTable = {}) {
  return renderScreen(
    <>
      <SettingsPage />
      <Toaster />
    </>,
    {
      permissions,
      route: '/admin/settings',
      routes: { 'GET /sales/settings': settings(true, true), ...routes },
    },
  )
}

describe('settings', () => {
  it('shows the built-in default and refuses the write without settings.manage', async () => {
    renderSettings([P.SETTINGS_VIEW])

    const checkbox = await screen.findByRole('checkbox', {
      name: /Plan the shortfall automatically/,
    })
    expect(checkbox).toBeChecked()
    expect(checkbox).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/No company override is stored/)).toBeVisible()
  })

  it('stores an explicit off, then restores the default with null', async () => {
    const { user, fetch } = renderSettings([P.SETTINGS_VIEW, P.SETTINGS_MANAGE], {
      'PUT /sales/settings': ({ body }) =>
        (body as { auto_plan_on_confirm: boolean | null }).auto_plan_on_confirm === null
          ? settings(true, true)
          : settings(false, false),
    })

    const checkbox = await screen.findByRole('checkbox', {
      name: /Plan the shortfall automatically/,
    })
    await user.click(checkbox)
    expect(checkbox).not.toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Save' }))

    const writes = () =>
      fetch.calls.filter((call) => call.method === 'PUT' && call.path === '/sales/settings')

    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes().at(-1)!.body).toEqual({ auto_plan_on_confirm: false })
    expect(await screen.findByText('Automatic shortfall planning is off')).toBeVisible()
    expect(screen.getByText(/This company stores its own value \(off\)/)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Restore default' }))
    await waitFor(() => expect(writes()).toHaveLength(2))
    expect(writes().at(-1)!.body).toEqual({ auto_plan_on_confirm: null })
    expect(await screen.findByText('Built-in default restored')).toBeVisible()
  })
})

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

function approvalSettings(isDefault: boolean) {
  return ok({
    po_dual_approval_amount: '25000.0000',
    adjustment_approval_threshold: '25000.0000',
    internal_issue_approval_threshold: '25000.0000',
    requisition_auto_approve_limit: '0.0000',
    is_default: isDefault,
  })
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
      routes: {
        'GET /sales/settings': settings(true, true),
        'GET /audit/approval-settings': approvalSettings(true),
        ...routes,
      },
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
    expect(screen.getAllByText(/No company override is stored/).length).toBeGreaterThan(0)
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

  it('shows the company approval thresholds and saves a custom override', async () => {
    const { user, fetch } = renderSettings([P.SETTINGS_VIEW, P.SETTINGS_MANAGE], {
      'PUT /audit/approval-settings': ({ body }) => {
        const payload = body as {
          po_dual_approval_amount: string | null
          adjustment_approval_threshold: string | null
          internal_issue_approval_threshold: string | null
          requisition_auto_approve_limit: string | null
        }

        return ok({
          po_dual_approval_amount: payload.po_dual_approval_amount ?? '25000.0000',
          adjustment_approval_threshold: payload.adjustment_approval_threshold ?? '25000.0000',
          internal_issue_approval_threshold:
            payload.internal_issue_approval_threshold ?? '25000.0000',
          requisition_auto_approve_limit: payload.requisition_auto_approve_limit ?? '0.0000',
          is_default: false,
        })
      },
    })

    const poInput = await screen.findByRole('textbox', {
      name: /PO dual approval amount/i,
    })
    expect(
      screen.getByText(/zero stock-adjustment or internal-issue threshold requires approval/i),
    ).toBeVisible()
    await user.clear(poInput)
    await user.type(poInput, '35000')

    const writeButton = screen.getByRole('button', { name: /Save approval thresholds/i })
    await user.click(writeButton)

    const writes = () =>
      fetch.calls.filter(
        (call) => call.method === 'PUT' && call.path === '/audit/approval-settings',
      )

    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes().at(-1)!.body).toEqual({
      po_dual_approval_amount: '35000',
      adjustment_approval_threshold: '25000',
      internal_issue_approval_threshold: '25000',
      requisition_auto_approve_limit: '0',
    })
    expect(await screen.findByText('Approval thresholds updated')).toBeVisible()
  })

  it('keeps decimal precision and sends a blank field as null', async () => {
    const { user, fetch } = renderSettings([P.SETTINGS_VIEW, P.SETTINGS_MANAGE], {
      'PUT /audit/approval-settings': ({ body }) => {
        const payload = body as Record<string, string | null>
        return ok({
          po_dual_approval_amount: payload.po_dual_approval_amount ?? '25000.0000',
          adjustment_approval_threshold: payload.adjustment_approval_threshold ?? '25000.0000',
          internal_issue_approval_threshold:
            payload.internal_issue_approval_threshold ?? '25000.0000',
          requisition_auto_approve_limit: payload.requisition_auto_approve_limit ?? '0.0000',
          is_default: false,
        })
      },
    })

    const poInput = await screen.findByRole('textbox', {
      name: /PO dual approval amount/i,
    })
    await user.clear(poInput)
    await user.type(poInput, '0.0001')

    const adjustmentInput = screen.getByRole('textbox', {
      name: /Stock adjustment approval threshold/i,
    })
    await user.clear(adjustmentInput)

    await user.click(screen.getByRole('button', { name: /Save approval thresholds/i }))

    const writes = () =>
      fetch.calls.filter(
        (call) => call.method === 'PUT' && call.path === '/audit/approval-settings',
      )

    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes().at(-1)!.body).toEqual({
      po_dual_approval_amount: '0.0001',
      adjustment_approval_threshold: null,
      internal_issue_approval_threshold: '25000',
      requisition_auto_approve_limit: '0',
    })
  })

  it('refuses an amount with more than four decimal places', async () => {
    const { user } = renderSettings([P.SETTINGS_VIEW, P.SETTINGS_MANAGE])

    const poInput = await screen.findByRole('textbox', {
      name: /PO dual approval amount/i,
    })
    await user.clear(poInput)
    await user.type(poInput, '1.23456')

    expect(screen.getByRole('button', { name: /Save approval thresholds/i })).toBeDisabled()
    expect(
      await screen.findByText(/an amount of zero or more with up to 4 decimal places/),
    ).toBeVisible()
  })
})

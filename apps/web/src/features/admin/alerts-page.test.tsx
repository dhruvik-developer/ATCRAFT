import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import type { SchemaOf } from '@/types/api'
import { ok, renderScreen, type RouteTable } from '@/test/harness'

import AlertsPage from './alerts-page'

type AlertRule = SchemaOf<'NotificationAlertRuleRead'>

const WHOLE_MINUTES = 'Enter a duration that resolves to whole minutes, up to 3650 days.'

function alertRule(overrides: Partial<AlertRule> = {}): AlertRule {
  return {
    key: 'PO_NOT_ACKNOWLEDGED',
    notification_type: 'PURCHASE_ORDER_NOT_ACKNOWLEDGED',
    name: 'Purchase order not acknowledged',
    description: 'A sent purchase order has not been acknowledged by its vendor.',
    severity: 'WARNING',
    granularity: 'ENTITY',
    is_enabled: true,
    threshold_minutes: 1440,
    remind_every_minutes: 1440,
    is_customized: false,
    ...overrides,
  }
}

function renderAlerts(
  permissions: string[],
  alertRules: AlertRule[] = [alertRule()],
  routes: RouteTable = {},
) {
  return renderScreen(
    <>
      <AlertsPage />
      <Toaster />
    </>,
    {
      permissions,
      route: '/admin/alerts',
      routes: {
        'GET /notifications/alert-rules': ok(alertRules),
        ...routes,
      },
    },
  )
}

describe('smart alert settings', () => {
  it('lets settings viewers inspect rules without granting write controls', async () => {
    renderAlerts([P.SETTINGS_VIEW])

    expect(await screen.findByText('Purchase order not acknowledged')).toBeVisible()
    expect(screen.getByRole('checkbox', { name: 'Enable this alert' })).toBeDisabled()
    expect(
      screen.getByRole('spinbutton', {
        name: 'Alert after, Purchase order not acknowledged',
      }),
    ).toBeDisabled()
    expect(
      screen.getByRole('combobox', {
        name: 'Purchase order not acknowledged alert threshold unit',
      }),
    ).toHaveTextContent('days')
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('names each duration by its visible label first, then by its rule', async () => {
    // Speech input says the words on screen ("click Repeat reminder every"),
    // so the accessible name leads with them; the rule's name follows because
    // every card on the page carries the same two labels.
    renderAlerts([P.SETTINGS_VIEW])

    expect(
      await screen.findByRole('spinbutton', {
        name: 'Alert after, Purchase order not acknowledged',
      }),
    ).toBeVisible()
    expect(
      screen.getByRole('spinbutton', {
        name: 'Repeat reminder every, Purchase order not acknowledged',
      }),
    ).toBeVisible()
  })

  it('submits only changed rules and preserves untouched rules', async () => {
    const unchanged = alertRule({
      key: 'QC_PENDING',
      notification_type: 'QC_INSPECTION_PENDING',
      name: 'QC pending',
      description: 'An inspection is waiting for a verdict.',
      threshold_minutes: 120,
      remind_every_minutes: 60,
    })
    const { user, fetch } = renderAlerts(
      [P.SETTINGS_VIEW, P.SETTINGS_MANAGE],
      [alertRule(), unchanged],
      { 'PUT /notifications/alert-rules': ok([alertRule(), unchanged]) },
    )

    const threshold = await screen.findByRole('spinbutton', {
      name: 'Alert after, Purchase order not acknowledged',
    })
    await user.clear(threshold)
    await user.type(threshold, '0.0625')
    await user.click(screen.getByRole('button', { name: 'Save changes (1)' }))

    await waitFor(() => {
      expect(
        fetch.calls.filter(
          (call) => call.method === 'PUT' && call.path === '/notifications/alert-rules',
        ),
      ).toHaveLength(1)
    })
    expect(fetch.calls.at(-1)?.body).toEqual({
      rules: [
        {
          key: 'PO_NOT_ACKNOWLEDGED',
          is_enabled: true,
          threshold_minutes: 90,
          remind_every_minutes: 1440,
        },
      ],
    })
    expect(await screen.findByText('Alert rules saved')).toBeVisible()
  })

  it('blocks saving an incomplete numeric setting', async () => {
    renderAlerts([P.SETTINGS_VIEW, P.SETTINGS_MANAGE])

    const threshold = await screen.findByRole('spinbutton', {
      name: 'Alert after, Purchase order not acknowledged',
    })
    fireEvent.change(threshold, { target: { value: '' } })

    expect(screen.getByText(WHOLE_MINUTES)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('refuses a duration past the ten-year cap the server enforces', async () => {
    renderAlerts([P.SETTINGS_VIEW, P.SETTINGS_MANAGE])

    const threshold = await screen.findByRole('spinbutton', {
      name: 'Alert after, Purchase order not acknowledged',
    })
    fireEvent.change(threshold, { target: { value: '3651' } })

    expect(screen.getByText(WHOLE_MINUTES)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()

    fireEvent.change(threshold, { target: { value: '3650' } })

    expect(screen.queryByText(WHOLE_MINUTES)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes (1)' })).toBeEnabled()
  })
})

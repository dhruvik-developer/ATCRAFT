import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { MrpScheduleSettingsCard } from './mrp-schedule-settings-card'

const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'

interface ScheduleSettings {
  mrp_scheduled_enabled: boolean
  horizon_days: number
  warehouse_ids: string[] | null
  respect_safety_stock: boolean
}

const initialSettings: ScheduleSettings = {
  mrp_scheduled_enabled: true,
  horizon_days: 90,
  warehouse_ids: null,
  respect_safety_stock: true,
}

const warehousePage = page([
  {
    id: WAREHOUSE_ID,
    code: 'PLANT-1',
    name: 'Main plant',
  },
])

function renderSchedule(permissions: string[], routes: RouteTable = {}) {
  return renderScreen(
    <>
      <MrpScheduleSettingsCard />
      <Toaster />
    </>,
    {
      permissions,
      route: '/mrp',
      routes: {
        'GET /mrp/schedule-settings': ok(initialSettings),
        'GET /warehouses': warehousePage,
        ...routes,
      },
    },
  )
}

describe('MRP schedule settings', () => {
  it('lets MRP viewers inspect settings without the run permission', async () => {
    renderSchedule([P.MRP_VIEW])

    expect(await screen.findByRole('checkbox', { name: 'Enable nightly planning' })).toBeChecked()
    expect(screen.getByRole('textbox', { name: 'Horizon (days)' })).toHaveValue('90')
    expect(screen.getByRole('textbox', { name: 'Horizon (days)' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save schedule' })).not.toBeInTheDocument()
    expect(screen.getByText(/can view these settings but cannot change them/i)).toBeVisible()
  })

  it('saves the company schedule, warehouse scope and safety-stock choice', async () => {
    const { user, fetch } = renderSchedule([P.MRP_VIEW, P.MRP_RUN, P.WAREHOUSE_VIEW], {
      'PUT /mrp/schedule-settings': ({ body }) => ok(body as ScheduleSettings),
    })

    const enabled = await screen.findByRole('checkbox', { name: 'Enable nightly planning' })
    await user.click(enabled)

    const horizon = screen.getByRole('textbox', { name: 'Horizon (days)' })
    await user.clear(horizon)
    await user.type(horizon, '365')

    await user.click(screen.getByRole('combobox', { name: 'Warehouse scope' }))
    await user.click(screen.getByRole('option', { name: /PLANT-1 Main plant/ }))
    await user.click(screen.getByRole('checkbox', { name: 'Protect safety stock' }))
    await user.click(screen.getByRole('button', { name: 'Save schedule' }))

    await waitFor(() => {
      expect(
        fetch.calls.filter(
          (call) => call.method === 'PUT' && call.path === '/mrp/schedule-settings',
        ),
      ).toHaveLength(1)
    })
    expect(fetch.requestFor('PUT', '/mrp/schedule-settings')).toEqual({
      mrp_scheduled_enabled: false,
      horizon_days: 365,
      warehouse_ids: [WAREHOUSE_ID],
      respect_safety_stock: false,
    })
    expect(
      await screen.findByText(/next scheduled run will use these company settings/i),
    ).toBeVisible()
  })

  it('blocks invalid horizons and retains a way to discard edits', async () => {
    const { user } = renderSchedule([P.MRP_VIEW, P.MRP_RUN])

    const horizon = await screen.findByRole('textbox', { name: 'Horizon (days)' })
    await user.clear(horizon)
    await user.type(horizon, '0')

    expect(await screen.findByText(/must be between 1 and 1095 days/i)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save schedule' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(horizon).toHaveValue('90')
  })
})

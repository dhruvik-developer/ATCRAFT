/**
 * The cost pool a work centre's machine hours are charged to.
 *
 * A work centre that names a cost centre keeps that centre from being
 * switched off (`_assert_may_deactivate`), so the value matters beyond the
 * form: an edit by someone shown no picker must send back the pool the centre
 * already has, never the null that would quietly release it.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import { WorkCenterFormDialog } from './routing-dialogs'
import type { WorkCenter } from './routing-model'

const STAMP = '2026-09-01T09:00:00Z'

const MACHINING = {
  id: 'cc-mach',
  code: 'CC-MACH',
  name: 'Machining',
  cost_centre_type: 'PRODUCTION',
  parent_id: null,
  plant_id: null,
  head_user_id: null,
  description: null,
  is_active: true,
  created_at: STAMP,
  updated_at: STAMP,
}

const CNC: WorkCenter = {
  id: 'wc-cnc',
  code: 'WC-CNC',
  name: 'CNC turning',
  description: null,
  plant_id: null,
  department_id: null,
  cost_centre_id: 'cc-mach',
  capacity_per_hour: '40.0000',
  cost_per_hour: '650.0000',
  setup_cost: '250.0000',
  working_hours_per_day: '16.0000',
  is_active: true,
  created_at: STAMP,
  updated_at: STAMP,
}

describe("a work centre's cost pool", () => {
  it('is sent with a new centre, chosen by an account that may read centres', async () => {
    const { user, fetch } = renderScreen(<WorkCenterFormDialog open onOpenChange={() => {}} />, {
      permissions: [P.WORK_CENTER_MANAGE, P.COST_CENTRE_VIEW],
      routes: {
        'GET /organization/cost-centres': page([MACHINING]),
        'POST /routing/work-centers': ok(CNC),
      },
    })

    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'WC-CNC')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'CNC turning')
    await user.click(within(dialog).getByRole('combobox', { name: 'Cost centre' }))
    await user.click(await screen.findByRole('option', { name: 'CC-MACH - Machining' }))
    await user.click(within(dialog).getByRole('button', { name: /create work centre/i }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/routing/work-centers'))) as Record<
      string,
      unknown
    >
    expect(body.code).toBe('WC-CNC')
    expect(body.cost_centre_id).toBe('cc-mach')
  })

  it('travels back unchanged from an account shown no picker', async () => {
    const { user, fetch } = renderScreen(
      <WorkCenterFormDialog open onOpenChange={() => {}} workCenter={CNC} />,
      {
        permissions: [P.WORK_CENTER_MANAGE],
        routes: { 'PATCH /routing/work-centers/wc-cnc': ok(CNC) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('combobox', { name: 'Cost centre' })).not.toBeInTheDocument()
    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'CNC turning cell')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/routing/work-centers/wc-cnc'),
    )) as Record<string, unknown>
    expect(body).not.toHaveProperty('code')
    expect(body.cost_centre_id).toBe('cc-mach')
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })
})

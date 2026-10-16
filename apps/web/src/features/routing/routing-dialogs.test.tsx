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

import { RoutingFormDialog, WorkCenterFormDialog } from './routing-dialogs'
import { workableMachinesOf, type Machine, type WorkCenter } from './routing-model'

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

/**
 * The machine picker on a routing operation.
 *
 * A routing step may name the machine it runs on, but only a workable machine
 * of its own work centre - the API refuses a retired machine and a machine
 * from any other cell, and an outsourced step runs on the vendor's equipment.
 */
const MILL: WorkCenter = {
  ...CNC,
  id: 'wc-mill',
  code: 'WC-MILL',
  name: 'Milling',
}

const PRESS: WorkCenter = {
  ...CNC,
  id: 'wc-press',
  code: 'WC-PRESS',
  name: 'Pressing',
}

function machine(id: string, code: string, workCenterId: string, status: Machine['status']) {
  return {
    id,
    code,
    name: `${code} name`,
    work_center_id: workCenterId,
    machine_type: null,
    capacity_per_hour: '10.0000',
    cost_per_hour: '100.0000',
    status,
    last_maintenance_date: null,
    next_maintenance_date: null,
    is_active: status !== 'RETIRED',
    created_at: STAMP,
    updated_at: STAMP,
  } satisfies Machine
}

const LATHE = machine('mc-lathe', 'MC-LATHE', MILL.id, 'AVAILABLE')
const RETIRED = machine('mc-old', 'MC-OLD', MILL.id, 'RETIRED')
const PRESS_MACHINE = machine('mc-press', 'MC-PRESS', PRESS.id, 'AVAILABLE')

const ITEM = { id: 'it000000-0000-7000-8000-000000000002', code: 'BRACKET', name: 'Bracket' }

function renderRoutingForm() {
  return renderScreen(<RoutingFormDialog open onOpenChange={() => {}} />, {
    permissions: [P.ROUTING_VIEW, P.ROUTING_MANAGE, P.MACHINE_VIEW],
    routes: {
      'GET /routing/work-centers': page([MILL, PRESS]),
      'GET /routing/machines': page([LATHE, RETIRED, PRESS_MACHINE]),
      'GET /products/items': page([ITEM]),
      'POST /routing': ok({ id: 'rt-1', code: 'RT-1' }),
    },
  })
}

async function fillTheMinimum(
  user: ReturnType<typeof renderRoutingForm>['user'],
  dialog: HTMLElement,
): Promise<void> {
  const type = async (selector: string, value: string) => {
    const field = dialog.querySelector(selector)
    if (!(field instanceof HTMLElement)) throw new Error(`missing ${selector}`)
    await user.type(field, value)
  }
  await type('#routing-code', 'RT-1')
  await user.click(within(dialog).getByRole('combobox', { name: 'Item produced' }))
  await user.click(await screen.findByRole('option', { name: /BRACKET/ }))
  await type('#op-code-0', 'MILL')
  await type('#op-name-0', 'Mill the face')
}

describe('the operation machine picker', () => {
  it('offers only workable machines of the row work centre and sends the choice', async () => {
    const { user, fetch } = renderRoutingForm()
    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)

    await user.click(within(dialog).getByRole('combobox', { name: 'Work centre' }))
    await user.click(await screen.findByRole('option', { name: /WC-MILL/ }))

    await user.click(within(dialog).getByRole('combobox', { name: 'Machine' }))
    expect(await screen.findByRole('option', { name: 'MC-LATHE - MC-LATHE name' }))
    expect(screen.queryByRole('option', { name: /MC-OLD/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /MC-PRESS/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'MC-LATHE - MC-LATHE name' }))

    await user.click(within(dialog).getByRole('button', { name: 'Create routing' }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/routing'))) as {
      operations: { machine_id: string | null }[]
    }
    expect(body.operations[0]!.machine_id).toBe(LATHE.id)
  })

  it('clears the machine when the work centre changes', async () => {
    const { user, fetch } = renderRoutingForm()
    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)

    await user.click(within(dialog).getByRole('combobox', { name: 'Work centre' }))
    await user.click(await screen.findByRole('option', { name: /WC-MILL/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Machine' }))
    await user.click(await screen.findByRole('option', { name: /MC-LATHE/ }))

    await user.click(within(dialog).getByRole('combobox', { name: 'Work centre' }))
    await user.click(await screen.findByRole('option', { name: /WC-PRESS/ }))

    await user.click(within(dialog).getByRole('button', { name: 'Create routing' }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/routing'))) as {
      operations: { machine_id: string | null; work_center_id: string }[]
    }
    expect(body.operations[0]!.work_center_id).toBe(PRESS.id)
    expect(body.operations[0]!.machine_id).toBeNull()
  })

  it('disables the picker and drops the machine when the step is outsourced', async () => {
    const { user } = renderRoutingForm()
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Work centre' }))
    await user.click(await screen.findByRole('option', { name: /WC-MILL/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Machine' }))
    await user.click(await screen.findByRole('option', { name: /MC-LATHE/ }))

    await user.click(within(dialog).getByRole('checkbox', { name: 'Job work' }))
    expect(within(dialog).getByRole('combobox', { name: 'Machine' })).toBeDisabled()
  })
})

describe('workableMachinesOf', () => {
  it('keeps active machines of the centre and drops retired ones', () => {
    expect(workableMachinesOf([LATHE, RETIRED, PRESS_MACHINE], MILL.id)).toEqual([LATHE])
  })
})

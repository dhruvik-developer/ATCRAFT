/**
 * The routings register, reached from its list.
 *
 * The row opens a routing on a pointer click, but a `<tr onClick>` is not in
 * the tab order, and the row's only other control is Edit - offered on a draft,
 * and only to `routing.manage`. So the code is a real button for the same
 * panel, or an approved routing's operations could not be read from the
 * keyboard at all.
 */

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody, type RouteResponder } from '@/test/harness'

import type { RoutingDetail, WorkCenter } from './routing-model'
import RoutingPage from './routing-page'

const STAMP = '2026-09-01T09:00:00Z'

const ROUTING: RoutingDetail = {
  id: 'rt-1',
  code: 'RT-001',
  name: 'Hinge body',
  item_id: 'item-1',
  version: 2,
  status: 'APPROVED',
  is_default: true,
  effective_from: '2026-09-01',
  effective_to: null,
  approved_by_id: null,
  approved_at: null,
  created_at: STAMP,
  updated_at: STAMP,
  notes: null,
  operations: [],
}

/** The list, plus everything the detail panel reads once it opens. */
const ROUTES = {
  'GET /routing': page([ROUTING]),
  'GET /routing/rt-1': ok(ROUTING),
  'GET /routing/rt-1/cost': ok({ operations: [] }),
  'GET /routing/work-centers': page([]),
  'GET /routing/machines': page([]),
}

describe('opening a routing from the list', () => {
  it('opens the routing from its code with the keyboard', async () => {
    const { user } = renderScreen(<RoutingPage />, {
      permissions: [P.ROUTING_VIEW],
      routes: ROUTES,
    })

    const code = await screen.findByRole('button', { name: 'RT-001' })
    code.focus()
    await user.keyboard('{Enter}')

    expect(await screen.findByRole('dialog', { name: 'RT-001 - Hinge body' })).toBeVisible()
  })

  it('still opens the routing from a click anywhere on the row', async () => {
    const { user } = renderScreen(<RoutingPage />, {
      permissions: [P.ROUTING_VIEW],
      routes: ROUTES,
    })

    await user.click(await screen.findByText('Hinge body'))

    expect(await screen.findByRole('dialog', { name: 'RT-001 - Hinge body' })).toBeVisible()
  })
})

/**
 * Editing one draft routing after another.
 *
 * The edit dialog stays mounted between openings, and `PATCH /routing/{id}`
 * replaces the operation list wholesale. The dialog used to skip resetting its
 * form while the routing it opened on had not arrived, and enabled Save as soon
 * as the read stopped loading - so when the next routing's read failed, Save
 * sent the routing edited last, its name and its operations, onto the one on
 * screen, replacing that routing's operations with another's.
 */
const LATHE: WorkCenter = {
  id: 'wc-lathe',
  code: 'WC-LATHE',
  name: 'Lathe cell',
  description: null,
  plant_id: null,
  department_id: null,
  cost_centre_id: null,
  capacity_per_hour: '40.0000',
  cost_per_hour: '100.0000',
  setup_cost: '0.0000',
  working_hours_per_day: '8.0000',
  is_active: true,
  created_at: STAMP,
  updated_at: STAMP,
}

function draft(id: string, code: string, name: string, operationName: string): RoutingDetail {
  return {
    ...ROUTING,
    id,
    code,
    name,
    status: 'DRAFT',
    is_default: false,
    operations: [
      {
        id: `${id}-op10`,
        routing_id: id,
        sequence: 10,
        operation_code: 'OP10',
        operation_name: operationName,
        work_center_id: LATHE.id,
        machine_id: null,
        setup_time_minutes: '10.0000',
        run_time_per_unit_minutes: '1.0000',
        queue_time_minutes: '0.0000',
        move_time_minutes: '0.0000',
        process_cost: '0.0000',
        is_outsourced: false,
        job_work_vendor_id: null,
        qc_required: false,
        notes: null,
      },
    ],
  }
}

const ALPHA = draft('rt-a', 'RT-A', 'Alpha route', 'Alpha turning')
const BETA = draft('rt-b', 'RT-B', 'Beta route', 'Beta drilling')
const READ_FAILED = fail(503, 'SERVICE_UNAVAILABLE', 'The routing register is unavailable')

/** The register, with Alpha readable and Beta answered by `betaRead`. */
function renderDrafts(betaRead: RouteResponder) {
  return renderScreen(
    <>
      <RoutingPage />
      <Toaster />
    </>,
    {
      permissions: [P.ROUTING_VIEW, P.ROUTING_MANAGE, P.WORK_CENTER_VIEW],
      errorNets: true,
      allowUnrouted: true,
      routes: {
        'GET /routing': page([ALPHA, BETA]),
        'GET /routing/rt-a': ok(ALPHA),
        'GET /routing/rt-b': betaRead,
        'GET /routing/work-centers': page([LATHE]),
        'GET /routing/machines': page([]),
        'GET /products/items/item-1': ok({ id: 'item-1', code: 'HINGE', name: 'Hinge body' }),
        'PATCH /routing/rt-b': ({ body }) => ok({ ...BETA, ...(body as object) }),
      },
    },
  )
}

/** Edit Alpha, then cancel it: the dialog closes but stays mounted with Alpha's form. */
async function editAlphaThenCancel(user: ReturnType<typeof renderDrafts>['user']) {
  await user.click(await screen.findByRole('button', { name: 'Edit routing RT-A' }))
  const dialog = await screen.findByRole('dialog')
  await waitFor(() =>
    expect(within(dialog).getByLabelText('Operation')).toHaveValue('Alpha turning'),
  )
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
}

describe('editing the next routing after the previous one', () => {
  it('shows a failed read in place of the fields, never the routing edited last, and saves nothing', async () => {
    const { user, fetch } = renderDrafts(() => READ_FAILED)
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit routing RT-B' }))
    const dialog = await screen.findByRole('dialog')

    expect(await within(dialog).findByText('Could not load this routing')).toBeVisible()
    expect(within(dialog).getByText(/The routing register is unavailable/)).toBeVisible()
    expect(screen.queryByDisplayValue('Alpha route')).toBeNull()
    expect(screen.queryByDisplayValue('Alpha turning')).toBeNull()
    expect(within(dialog).queryByLabelText('Operation')).toBeNull()
    const save = within(dialog).getByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()
    await user.click(save)
    // Said once, in the dialog: the read opts out of the shell's toast.
    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(within(toasts).queryAllByRole('alert')).toHaveLength(0)
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })

  it('fills the form from the routing it opened once a retry reads it, and saves that one', async () => {
    let reads = 0
    const { user, fetch } = renderDrafts(() => (++reads === 1 ? READ_FAILED : ok(BETA)))
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit routing RT-B' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(await within(dialog).findByRole('button', { name: 'Try again' }))

    await waitFor(() =>
      expect(within(dialog).getByLabelText('Operation')).toHaveValue('Beta drilling'),
    )
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Beta route')
    expect(reads).toBe(2)
    const save = within(dialog).getByRole('button', { name: 'Save changes' })
    expect(save).toBeEnabled()
    await user.click(save)

    const body = (await waitFor(() => fetch.requestFor('PATCH', '/routing/rt-b'))) as {
      name: string
      operations: { operation_name: string; work_center_id: string }[]
    }
    expect(body.name).toBe('Beta route')
    expect(body.operations).toEqual([
      expect.objectContaining({ operation_name: 'Beta drilling', work_center_id: LATHE.id }),
    ])
  })

  it('holds its own list row, never the routing edited last, while its operations load', async () => {
    let answerRead!: (response: RouteBody) => void
    const { user, fetch } = renderDrafts(
      () =>
        new Promise<RouteBody>((resolve) => {
          answerRead = resolve
        }),
    )
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit routing RT-B' }))
    const dialog = await screen.findByRole('dialog')

    // Unlike the vendor and transporter forms, this one is drawn while it loads.
    expect(await within(dialog).findByText('Loading the current operations')).toBeVisible()
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Beta route')
    expect(screen.queryByDisplayValue('Alpha route')).toBeNull()
    expect(screen.queryByDisplayValue('Alpha turning')).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled()

    // A complete operation typed into the blank row, then submitted by a path
    // other than Save, would replace Beta's real operations with it.
    await user.type(within(dialog).getByLabelText('Code', { selector: '#op-code-0' }), 'OP99')
    await user.type(within(dialog).getByLabelText('Operation'), 'Typed while loading')
    await user.click(within(dialog).getByRole('combobox', { name: 'Work centre' }))
    await user.click(await screen.findByRole('option', { name: /WC-LATHE/ }))
    const form = dialog.querySelector('form')
    expect(form).not.toBeNull()
    await act(async () => {
      fireEvent.submit(form as HTMLFormElement)
      // Long enough for the resolver's validation to settle either way.
      await new Promise((settle) => setTimeout(settle, 50))
    })
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)

    await act(async () => {
      answerRead(ok(BETA))
    })
    await waitFor(() =>
      expect(within(dialog).getByLabelText('Operation')).toHaveValue('Beta drilling'),
    )
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeEnabled()
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })
})

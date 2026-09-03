/**
 * Assembly orders: the register and the order panel over it.
 *
 * The panel is where this file earns its keep, and the reason is one line of
 * `assembly-model`: **`isCompletable` is IN_PROGRESS and nothing else.**
 * `ASSEMBLY_ORDER_LIFECYCLE` has an edge COMPONENTS_ISSUED -> IN_PROGRESS and an
 * edge IN_PROGRESS -> COMPLETED, and none from COMPONENTS_ISSUED straight to
 * COMPLETED. Offering Complete on a kitted-but-unstarted order walks the
 * operator through quantities, a reason and a serial per tracked line, and then
 * `assert_transition` refuses the lot. So every status is asserted for exactly
 * what it offers.
 *
 * The other half is that **completion is a different right from every other
 * write on the order**: `POST /assembly/{id}/complete` declares
 * `assembly.complete` while approve, release, issue, close and cancel all
 * declare `assembly_order.manage`. The two are therefore always granted
 * separately here, never together, because granting both is precisely what
 * hides a wrong gate.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import type { AssemblyComponent, AssemblyOrderRow, AssemblyStatus } from './assembly-model'
import AssemblyPage from './assembly-page'

// ------------------------------------------------------------- fixtures ----

function assemblyOrder(overrides: Partial<AssemblyOrderRow> = {}): AssemblyOrderRow {
  return {
    id: 'asm-1',
    code: 'ASM-0001',
    status: 'RELEASED',
    version: 1,
    item_id: 'item-aaa-0001',
    bom_id: 'bom-1',
    warehouse_id: 'wh-1',
    output_warehouse_id: 'wh-2',
    planned_quantity: '10.0000',
    assembled_quantity: '4.0000',
    rejected_quantity: '0.0000',
    assembly_line: null,
    planned_start: '2026-03-01',
    planned_end: '2026-03-03',
    actual_start: null,
    actual_end: null,
    requires_qc: false,
    qc_inspection_id: null,
    qc_held_at: null,
    packing_order_id: null,
    packed_at: null,
    source_type: 'MANUAL',
    source_id: null,
    cancellation_reason: null,
    notes: null,
    ...overrides,
  }
}

function assemblyComponent(overrides: Partial<AssemblyComponent> = {}): AssemblyComponent {
  return {
    id: 'ac-1',
    assembly_order_id: 'asm-1',
    line_number: 1,
    item_id: 'item-bbb-0001',
    uom_id: 'uom-1',
    required_quantity: '100.0000',
    issued_quantity: '40.0000',
    consumed_quantity: '0.0000',
    is_serial_tracked_component: false,
    consumed_serial_ids: [],
    reservation_id: null,
    notes: null,
    ...overrides,
  }
}

/** Everything the panel reads when one order is opened. */
function panelRoutes(
  order: AssemblyOrderRow,
  components: AssemblyComponent[] = [assemblyComponent()],
) {
  return {
    'GET /assembly': page([order]),
    [`GET /assembly/${order.id}`]: ok(order),
    [`GET /assembly/${order.id}/components`]: ok(components),
  }
}

/**
 * Open one order's panel.
 *
 * The row is re-queried at the moment of the click rather than held across the
 * await: the grid rebuilds its cells when the list query settles, and a node
 * captured before that is detached by the time the click is dispatched.
 */
async function openPanel(
  user: ReturnType<typeof renderScreen>['user'],
  code = 'ASM-0001',
): Promise<void> {
  await screen.findByText(code)
  await user.click(screen.getByRole('button', { name: 'Open' }))
  await screen.findByRole('dialog')
}

/** Render the register with one order in the given state and open its panel. */
async function openOrderIn(status: AssemblyStatus, permissions: string[]) {
  const order = assemblyOrder({ status })
  const rendered = renderScreen(<AssemblyPage />, {
    permissions: [P.ASSEMBLY_ORDER_VIEW, ...permissions],
    routes: panelRoutes(order),
  })
  await openPanel(rendered.user)
  return rendered
}

// --------------------------------------------------------------- register --

describe('the assembly register', () => {
  it('reads a rejected quantity as a decimal, not as the string it arrived as', async () => {
    // A NUMERIC(18,4) column serialises as "0.0000", which never equals the
    // literal '0' - so a string comparison marked every untouched order as
    // having rejected output, in red, on every row of the page.
    renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW],
      routes: {
        'GET /assembly': page([
          assemblyOrder({ planned_quantity: '10.0000', assembled_quantity: '4.0000' }),
          assemblyOrder({
            id: 'asm-2',
            code: 'ASM-0002',
            planned_quantity: '20.0000',
            assembled_quantity: '5.0000',
            rejected_quantity: '2.5000',
          }),
        ]),
      },
    })

    await screen.findByText('ASM-0001')
    // The genuine rejection is shown as a figure...
    expect(screen.getByText('2.5')).toBeVisible()
    // ...and the "0.0000" is not: no cell anywhere renders a zero.
    expect(screen.queryByText('0')).toBeNull()
  })

  it('filters on the planned start, under the names the endpoint answers', async () => {
    // The toolbar owns `date_from`/`date_to` in the URL, but the endpoint takes
    // `planned_from`/`planned_to`; an unknown parameter is dropped in silence,
    // so under the URL's own names the range picker did nothing whatsoever.
    const seen: { url?: URL } = {}
    renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW],
      route: '/assembly?date_from=2026-03-01&date_to=2026-03-31&status=IN_PROGRESS',
      routes: {
        'GET /assembly': ({ url }) => {
          seen.url = url
          return page([assemblyOrder()])
        },
      },
    })
    await screen.findByText('ASM-0001')

    const sent = seen.url!.searchParams
    expect(sent.get('planned_from')).toBe('2026-03-01')
    expect(sent.get('planned_to')).toBe('2026-03-31')
    expect(sent.get('status')).toBe('IN_PROGRESS')
    expect(sent.get('date_from')).toBeNull()
  })

  it('says an empty register is empty, and a refused one refused', async () => {
    const empty = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW],
      routes: { 'GET /assembly': page([]) },
    })
    expect(await screen.findByText('No assembly orders found')).toBeVisible()
    empty.unmount()

    renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW],
      routes: {
        'GET /assembly': fail(403, 'PERMISSION_DENIED', 'Assembly orders are not yours to see'),
      },
    })
    expect(await screen.findByText('Could not load this list')).toBeVisible()
    expect(screen.getByText('Assembly orders are not yours to see')).toBeVisible()
  })
})

// -------------------------------------------------------------- lifecycle --

describe('what the order panel offers, status by status', () => {
  it('offers issuing from RELEASED, and completion nowhere near it', async () => {
    await openOrderIn('RELEASED', [P.ASSEMBLY_ORDER_MANAGE, P.ASSEMBLY_COMPLETE])

    expect(screen.getByRole('button', { name: /issue components/i })).toBeVisible()
    // Released but not started: there is no edge from here to COMPLETED.
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
  })

  it('still offers only issuing on a fully kitted COMPONENTS_ISSUED order', async () => {
    // The state machine has no COMPONENTS_ISSUED -> COMPLETED edge. Both rights
    // are granted here so that a Complete button appearing could only be a
    // lifecycle mistake, never a permission one.
    await openOrderIn('COMPONENTS_ISSUED', [P.ASSEMBLY_ORDER_MANAGE, P.ASSEMBLY_COMPLETE])

    expect(screen.getByRole('button', { name: /issue components/i })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
  })

  it('offers completion from IN_PROGRESS - to someone holding assembly.complete', async () => {
    await openOrderIn('IN_PROGRESS', [P.ASSEMBLY_COMPLETE])

    expect(screen.getByRole('button', { name: 'Complete' })).toBeVisible()
    // Issuing is the manage right, which this account does not hold.
    expect(screen.queryByRole('button', { name: /issue components/i })).toBeNull()
  })

  it('refuses completion to the manage right, which is a different right', async () => {
    // `assembly_order.manage` covers approve, release, issue, close and cancel.
    // It does not cover completion, and completion is the one that writes the
    // ledger: components leave stock and the finished item is received.
    await openOrderIn('IN_PROGRESS', [P.ASSEMBLY_ORDER_MANAGE])

    expect(screen.getByRole('button', { name: /issue components/i })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
  })

  it('offers nothing on a DRAFT, which has nothing reserved yet', async () => {
    await openOrderIn('DRAFT', [P.ASSEMBLY_ORDER_MANAGE, P.ASSEMBLY_COMPLETE])

    expect(screen.queryByRole('button', { name: /issue components/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
  })

  it('offers nothing on a COMPLETED order either', async () => {
    await openOrderIn('COMPLETED', [P.ASSEMBLY_ORDER_MANAGE, P.ASSEMBLY_COMPLETE])

    expect(screen.queryByRole('button', { name: /issue components/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
  })
})

// ------------------------------------------------------ issuing components --

describe('issuing components', () => {
  it('sends the running total for every line, including the ones left at zero', async () => {
    // `issue_components` *assigns* `issued_quantity` rather than adding to it,
    // and it issues a line's full requirement for any line the request omits.
    // Both together mean the figure is the running total on the bench and every
    // line has to be sent: sending the outstanding balance replaced a line that
    // had 40 of 100 issued with 60, and omitting a cleared line kitted it in
    // full behind the operator's back.
    const order = assemblyOrder({ status: 'RELEASED' })
    const { user, fetch } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        ...panelRoutes(order, [
          assemblyComponent(),
          assemblyComponent({
            id: 'ac-2',
            line_number: 2,
            item_id: 'item-ccc-0002',
            required_quantity: '5.0000',
            issued_quantity: '0.0000',
          }),
        ]),
        'POST /assembly/asm-1/issue-components': ok({ ...order, status: 'COMPONENTS_ISSUED' }),
      },
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: /issue components/i }))

    // Pre-filled with the requirement, not with what is outstanding.
    const first = await screen.findByLabelText('Line 1 - item-bbb')
    expect(first).toHaveValue('100')

    const second = screen.getByLabelText('Line 2 - item-ccc')
    await user.clear(second)
    await user.type(second, '0')
    await user.click(screen.getByRole('button', { name: 'Issue components' }))

    await waitFor(() => fetch.requestFor('POST', '/assembly/asm-1/issue-components'))
    expect(fetch.requestFor('POST', '/assembly/asm-1/issue-components')).toEqual({
      reason: 'Component issue to assembly order',
      lines: [
        { line_number: 1, issued_quantity: '100' },
        { line_number: 2, issued_quantity: '0' },
      ],
    })
  })

  it('lands a 422 on the line the server named', async () => {
    const order = assemblyOrder({ status: 'RELEASED' })
    const { user } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        ...panelRoutes(order),
        'POST /assembly/asm-1/issue-components': fail(422, 'VALIDATION_ERROR', 'Invalid', {
          fields: { 'lines.0.quantity': ['Above the reserved quantity for this line'] },
        }),
      },
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: /issue components/i }))
    await screen.findByLabelText('Line 1 - item-bbb')
    await user.click(screen.getByRole('button', { name: 'Issue components' }))

    expect(
      await screen.findByText('Above the reserved quantity for this line'),
    ).toBeVisible()
  })
})

// --------------------------------------------------------- completing one --

describe('completing an assembly', () => {
  const inProgress = () =>
    assemblyOrder({
      status: 'IN_PROGRESS',
      planned_quantity: '10.0000',
      assembled_quantity: '4.0000',
    })

  it('defaults to the balance of the plan and names only the serialised lines', async () => {
    // A line that is not serial tracked has nothing to name and is refused
    // outright if it names anything, so the payload carries the tracked lines
    // and only those.
    const order = inProgress()
    const { user, fetch } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_COMPLETE],
      routes: {
        ...panelRoutes(order, [
          assemblyComponent(),
          assemblyComponent({
            id: 'ac-2',
            line_number: 2,
            item_id: 'item-ccc-0002',
            is_serial_tracked_component: true,
          }),
        ]),
        'POST /assembly/asm-1/complete': ok({
          order: { ...order, status: 'COMPLETED' },
          consume_movement_count: 2,
          output_quantity: '6.0000',
          output_held_for_qc: false,
          qc_inspection_created: false,
        }),
      },
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: 'Complete' }))

    // Planned less already assembled - the balance, not the whole plan. Queried
    // by accessible name rather than by label text: a required Label appends an
    // `aria-hidden` asterisk, which the accessible name computation drops and a
    // raw label-text match does not.
    expect(await screen.findByRole('textbox', { name: 'Assembled' })).toHaveValue('6')
    await user.click(screen.getByRole('button', { name: 'Complete assembly' }))

    await waitFor(() => fetch.requestFor('POST', '/assembly/asm-1/complete'))
    expect(fetch.requestFor('POST', '/assembly/asm-1/complete')).toEqual({
      assembled_quantity: '6',
      rejected_quantity: '0',
      reason: 'Assembly completed',
      components: [{ line_number: 2, serial_ids: [] }],
    })
  })

  it('says why a completion cannot name serials rather than failing at the API', async () => {
    // The account holds `assembly.complete` and not `inventory.view`, so the
    // serial register is unreadable. The API counts the serials it is given
    // against what was produced and refuses the whole completion on a mismatch,
    // which is a poor way to discover you were never able to name them.
    const order = inProgress()
    const { user } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_COMPLETE],
      routes: panelRoutes(order, [
        assemblyComponent({ is_serial_tracked_component: true }),
      ]),
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: 'Complete' }))

    expect(
      await screen.findByText(/Serial numbers are not readable by your account/),
    ).toBeVisible()
  })

  it('shows a refusal that names no field, which the toast layer stays silent on', async () => {
    // The mutation cache suppresses the toast for a 422 carrying field errors,
    // on the understanding that the form shows them. A serial-count or
    // issued-quantity failure names no field this form renders, so the dialog
    // has to surface it or nobody sees anything at all.
    const order = inProgress()
    const { user } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_COMPLETE],
      routes: {
        ...panelRoutes(order),
        'POST /assembly/asm-1/complete': fail(
          409,
          'INSUFFICIENT_ISSUED_QUANTITY',
          'Line 1 has 40 issued and the completion needs 60',
        ),
      },
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: 'Complete' }))
    await screen.findByRole('textbox', { name: 'Assembled' })
    await user.click(screen.getByRole('button', { name: 'Complete assembly' }))

    expect(
      await screen.findByText('Line 1 has 40 issued and the completion needs 60'),
    ).toBeVisible()
  })
})

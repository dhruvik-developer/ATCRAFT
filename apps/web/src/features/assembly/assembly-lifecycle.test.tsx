/**
 * Assembly lifecycle commands: exact request bodies and their gates.
 *
 * Approve, release, start, close and cancel each post `{ reason }` to their
 * own edge, and delete carries the reason on the query string; every one of
 * them sits behind `assembly_order.manage`, so the affordances are asserted
 * with and without it. Release additionally shows the live shortage read the
 * API refuses on: a release is refused while any line is short, so the
 * operator is told before anything is promised.
 *
 * The QC verdict has two faces. With a linked inspection the button opens
 * that inspection's own sheet in quality - the inspection is the one release
 * path, and the sheet is what decides it. With no inspection the verdict is
 * recorded on the order itself, accepted plus rejected, which must account
 * for everything on hold.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen, type RouteTable } from '@/test/harness'

import AssemblyPage from './assembly-page'
import type {
  AssemblyComponent,
  AssemblyComponentAvailability,
  AssemblyOrderRow,
  AssemblyStatus,
} from './assembly-model'

function assemblyOrder(overrides: Partial<AssemblyOrderRow> = {}): AssemblyOrderRow {
  return {
    id: 'asm-1',
    code: 'ASM-0001',
    status: 'DRAFT',
    version: 1,
    item_id: 'item-aaa-0001',
    bom_id: 'bom-1',
    warehouse_id: 'wh-1',
    output_warehouse_id: 'wh-2',
    planned_quantity: '10.0000',
    assembled_quantity: '0.0000',
    rejected_quantity: '0.0000',
    assembly_line: null,
    planned_start: '2026-03-01',
    planned_end: '2026-03-03',
    actual_start: null,
    actual_end: null,
    priority: 100,
    requires_qc: false,
    qc_inspection_id: null,
    qc_held_at: null,
    output_batch_id: null,
    output_serial_ids: null,
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
    issued_quantity: '0.0000',
    consumed_quantity: '0.0000',
    is_serial_tracked_component: false,
    is_batch_tracked_component: false,
    issued_batch_id: null,
    consumed_batch_id: null,
    consumed_serial_ids: [],
    reservation_id: null,
    notes: null,
    ...overrides,
  }
}

function availability(
  overrides: Partial<AssemblyComponentAvailability> = {},
): AssemblyComponentAvailability {
  return {
    line_number: 1,
    item_id: 'item-bbb-0001',
    required_quantity: '100.0000',
    issued_quantity: '0.0000',
    is_reserved: false,
    available_quantity: '60.0000',
    shortage_quantity: '40.0000',
    ...overrides,
  }
}

function panelRoutes(order: AssemblyOrderRow, routes: RouteTable = {}) {
  return {
    'GET /assembly': page([order]),
    [`GET /assembly/${order.id}`]: ok(order),
    [`GET /assembly/${order.id}/components`]: ok([assemblyComponent()]),
    ...routes,
  }
}

async function openPanel(
  user: ReturnType<typeof renderScreen>['user'],
  code = 'ASM-0001',
): Promise<void> {
  await screen.findByText(code)
  await user.click(screen.getByRole('button', { name: 'Open' }))
  await screen.findByRole('dialog')
}

/** Open the panel with the order in the given state, and start one command. */
async function openCommand(
  status: AssemblyStatus,
  permissions: string[],
  button: string | RegExp,
  routes: RouteTable = {},
) {
  const order = assemblyOrder({ status })
  const rendered = renderScreen(<AssemblyPage />, {
    permissions: [P.ASSEMBLY_ORDER_VIEW, ...permissions],
    routes: panelRoutes(order, routes),
  })
  await openPanel(rendered.user)
  await rendered.user.click(screen.getByRole('button', { name: button }))
  return rendered
}

async function confirmWithReason(
  user: ReturnType<typeof renderScreen>['user'],
  confirm: string | RegExp,
  reason: string,
): Promise<void> {
  // By accessible name rather than label text: a required Label appends an
  // `aria-hidden` asterisk that a raw label-text match does not drop.
  await user.type(await screen.findByRole('textbox', { name: 'Reason' }), reason)
  await user.click(screen.getByRole('button', { name: confirm }))
}

// ------------------------------------------------------------ transitions --

describe('assembly lifecycle commands', () => {
  it.each([
    { status: 'DRAFT', button: 'Approve', path: 'approve', confirm: 'Approve order' },
    { status: 'APPROVED', button: 'Release', path: 'release', confirm: 'Release and reserve' },
    { status: 'COMPONENTS_ISSUED', button: 'Start', path: 'start', confirm: 'Start assembly' },
    { status: 'COMPLETED', button: 'Close order', path: 'close', confirm: 'Close order' },
    { status: 'APPROVED', button: 'Cancel', path: 'cancel', confirm: 'Cancel order' },
  ] as const)(
    '$button posts only the reason to its own edge',
    async ({ status, button, path, confirm }) => {
      const order = assemblyOrder({ status })
      const { user, fetch } = renderScreen(<AssemblyPage />, {
        permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
        routes: {
          ...panelRoutes(order, {
            'GET /assembly/asm-1/availability': ok([availability({ shortage_quantity: '0.0000' })]),
          }),
          [`POST /assembly/asm-1/${path}`]: ok({ ...order }),
        },
      })
      await openPanel(user)
      await user.click(screen.getByRole('button', { name: button }))
      await confirmWithReason(user, confirm, 'Bench free, moving ahead')

      await waitFor(() => fetch.requestFor('POST', `/assembly/asm-1/${path}`))
      expect(fetch.requestFor('POST', `/assembly/asm-1/${path}`)).toEqual({
        reason: 'Bench free, moving ahead',
      })
    },
  )

  it('deletes a draft with the reason on the query string, then closes the panel', async () => {
    const seen: { url?: URL } = {}
    const order = assemblyOrder({ status: 'DRAFT' })
    const { user } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        ...panelRoutes(order),
        'DELETE /assembly/asm-1': ({ url }: { url: URL }) => {
          seen.url = url
          return ok(null)
        },
      },
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await confirmWithReason(user, 'Delete draft', 'Raised against the wrong item')

    await waitFor(() => expect(seen.url).toBeDefined())
    expect(seen.url!.searchParams.get('reason')).toBe('Raised against the wrong item')
  })

  it('refuses to send a command without its audited reason', async () => {
    const { user, fetch } = await openCommand('DRAFT', [P.ASSEMBLY_ORDER_MANAGE], 'Approve', {
      'POST /assembly/asm-1/approve': ok(assemblyOrder({ status: 'APPROVED' })),
    })

    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    expect(await screen.findByText(/at least 3 characters/)).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/approve'))).toBe(false)
  })

  it('shows no lifecycle command without the manage right', async () => {
    const order = assemblyOrder({ status: 'APPROVED' })
    const rendered = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_COMPLETE],
      routes: panelRoutes(order),
    })
    await openPanel(rendered.user)

    for (const name of [
      'Approve',
      'Release',
      'Start',
      'Close order',
      'Cancel',
      'Edit draft',
      'Delete',
    ]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
  })
})

// ---------------------------------------------------------------- release --

describe('releasing to the bench', () => {
  it('shows what the release would still be short of, read live', async () => {
    const { user, fetch } = await openCommand('APPROVED', [P.ASSEMBLY_ORDER_MANAGE], 'Release', {
      'GET /assembly/asm-1/availability': ok([availability()]),
      'POST /assembly/asm-1/release': ok(assemblyOrder({ status: 'RELEASED' })),
    })

    // Promisable stock covers 60 of the 100 required: the shortage is stated
    // before the bench is promised anything.
    expect(await screen.findByText('short 40 of 100')).toBeVisible()
    await confirmWithReason(user, 'Release and reserve', 'Full kit on the shelf')

    await waitFor(() => fetch.requestFor('POST', '/assembly/asm-1/release'))
    expect(fetch.requestFor('POST', '/assembly/asm-1/release')).toEqual({
      reason: 'Full kit on the shelf',
    })
  })

  it('says when every component is covered rather than showing a bare form', async () => {
    await openCommand('APPROVED', [P.ASSEMBLY_ORDER_MANAGE], 'Release', {
      'GET /assembly/asm-1/availability': ok([availability({ shortage_quantity: '0.0000' })]),
      'POST /assembly/asm-1/release': ok(assemblyOrder({ status: 'RELEASED' })),
    })

    expect(await screen.findByText(/every component is covered/i)).toBeVisible()
  })
})

// ---------------------------------------------------------------- verdict --

describe('recording the QC verdict', () => {
  const held = () =>
    assemblyOrder({
      status: 'QC_PENDING',
      requires_qc: true,
      assembled_quantity: '10.0000',
      qc_inspection_id: 'insp-1',
      qc_held_at: '2026-03-05T10:00:00Z',
    })

  it('opens the linked inspection sheet instead of a second verdict form', async () => {
    const order = held()
    // The sheet's header reads the row it was opened with; the readings come
    // from the detail read, which a refusal here proves was attempted.
    const inspection = {
      id: 'insp-1',
      code: 'QC-0007',
      item_id: order.item_id,
      warehouse_id: order.output_warehouse_id,
      status: 'PENDING',
      qc_stage: 'ASSEMBLY',
      reference_type: 'assembly_order',
      reference_id: order.id,
      reference_code: order.code,
      inspected_quantity: '10.0000',
      sample_size: '10.0000',
      accepted_quantity: '0.0000',
      rejected_quantity: '0.0000',
      rework_quantity: '0.0000',
      hold_quantity: '10.0000',
      scrap_quantity: '0.0000',
      result: 'PENDING',
      return_required: false,
      inspected_at: null,
      inspector_user_id: null,
      vendor_id: null,
      batch_id: null,
      template_id: null,
      template_version: null,
      created_at: '2026-03-05T10:00:00Z',
      parameters: [],
      results: [],
    }
    const rendered = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.QC_DECIDE],
      routes: {
        ...panelRoutes(order),
        'GET /quality/inspections/insp-1': ok(inspection),
      },
    })
    await openPanel(rendered.user)
    await rendered.user.click(screen.getByRole('button', { name: /qc verdict/i }))

    // The sheet opens on the linked inspection and reads it: no second
    // verdict form is offered for a hold quality already holds.
    expect(await screen.findByText('Inspect QC-0007')).toBeVisible()
    expect(screen.queryByText(/no inspection covers this hold/i)).toBeNull()
    await waitFor(() =>
      expect(
        rendered.fetch.calls.some(
          (call) => call.method === 'GET' && call.path === '/quality/inspections/insp-1',
        ),
      ).toBe(true),
    )
  })

  it('records the verdict on the order itself when no inspection covers the hold', async () => {
    const order = assemblyOrder({
      status: 'QC_PENDING',
      requires_qc: true,
      assembled_quantity: '10.0000',
      qc_inspection_id: null,
      qc_held_at: '2026-03-05T10:00:00Z',
    })
    const { user, fetch } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.QC_DECIDE],
      routes: {
        ...panelRoutes(order),
        'POST /assembly/asm-1/qc-result': ok({ ...order, status: 'COMPLETED' }),
      },
    })
    await openPanel(user)
    await user.click(screen.getByRole('button', { name: /qc verdict/i }))

    // Defaults to accepting everything assembled; rejecting is the deliberate edit.
    expect(await screen.findByRole('textbox', { name: 'Accepted' })).toHaveValue('10.0000')
    await user.type(screen.getByRole('textbox', { name: 'Reason' }), 'Gauges passed on all ten')
    await user.click(screen.getByRole('button', { name: 'Record verdict' }))

    await waitFor(() => fetch.requestFor('POST', '/assembly/asm-1/qc-result'))
    expect(fetch.requestFor('POST', '/assembly/asm-1/qc-result')).toEqual({
      accepted_quantity: '10.0000',
      rejected_quantity: '0',
      reason: 'Gauges passed on all ten',
    })
  })

  it('shows no verdict without the decide right', async () => {
    const rendered = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: panelRoutes(held()),
    })
    await openPanel(rendered.user)

    expect(screen.queryByRole('button', { name: /qc verdict/i })).toBeNull()
  })
})

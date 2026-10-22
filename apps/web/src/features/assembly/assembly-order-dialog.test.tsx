/**
 * Raising and amending an assembly order: exact request bodies and gates.
 *
 * Creation posts the narrowed choice - a manufacturable item, an optional
 * recipe from its approved assembly or production structures, the two
 * warehouses, the schedule, the priority the reservations will inherit, and
 * whether the output needs inspection - and omits everything left empty
 * rather than sending nulls the API would read as explicit clears. Amendment
 * carries no item and no recipe: changing what is being built is a different
 * order, not an edit.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import AssemblyPage from './assembly-page'
import type { AssemblyOrderRow } from './assembly-model'

const ITEM = { id: 'item-lock-1', code: 'LOCK-SET-01', name: 'Mortise lock set', is_active: true }
const WAREHOUSES = [
  { id: 'wh-1', code: 'MAIN', name: 'Main store' },
  { id: 'wh-2', code: 'FG', name: 'Finished goods' },
]
const BOMS = [
  {
    id: 'bom-1',
    code: 'BOM-LOCK-01',
    item_id: ITEM.id,
    version: 2,
    revision: null,
    status: 'APPROVED',
    bom_type: 'ASSEMBLY',
    is_default: true,
    output_quantity: '1.0000',
    base_uom_id: 'uom-1',
    effective_from: '2026-01-01',
    effective_to: null,
    parent_bom_id: null,
    approved_by_id: null,
    approved_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'bom-phantom',
    code: 'BOM-LOCK-PH',
    item_id: ITEM.id,
    version: 1,
    revision: null,
    status: 'APPROVED',
    bom_type: 'PHANTOM',
    is_default: false,
    output_quantity: '1.0000',
    base_uom_id: 'uom-1',
    effective_from: '2026-01-01',
    effective_to: null,
    parent_bom_id: null,
    approved_by_id: null,
    approved_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
]

function draft(overrides: Partial<AssemblyOrderRow> = {}): AssemblyOrderRow {
  return {
    id: 'asm-1',
    code: 'ASM-0001',
    status: 'DRAFT',
    version: 1,
    item_id: ITEM.id,
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

const lookups = {
  'GET /warehouses': page(WAREHOUSES),
  'GET /products/items': page([ITEM]),
  [`GET /products/items/${ITEM.id}`]: ok(ITEM),
  'GET /bom': page(BOMS),
}

async function openCreate(user: ReturnType<typeof renderScreen>['user']) {
  // The action is gated on the signed-in account's rights, so it appears once
  // the session resolves - clicking before that races the auth read.
  await user.click(await screen.findByRole('button', { name: /new assembly order/i }))
  return screen.findByRole('dialog')
}

describe('raising an assembly order', () => {
  it('narrows the item search to what the endpoint would accept', async () => {
    const seen: { url?: URL } = {}
    const { user } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        'GET /assembly': page([]),
        'GET /warehouses': page(WAREHOUSES),
        'GET /products/items': ({ url }: { url: URL }) => {
          seen.url = url
          return page([ITEM])
        },
        'GET /bom': page(BOMS),
      },
    })
    await openCreate(user)

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Item to assemble' }))
    await screen.findByRole('option', { name: /LOCK-SET-01/ })

    // Inactive items and ones not flagged manufacturable are refused at save,
    // so the search never offers them.
    expect(seen.url!.searchParams.get('can_manufacture')).toBe('true')
  })

  it('posts the narrowed choice and omits everything left empty', async () => {
    const { user, fetch } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        'GET /assembly': page([]),
        ...lookups,
        'POST /assembly': ok(draft()),
        // Saving opens the created order's panel, which reads it back.
        'GET /assembly/asm-1': ok(draft()),
        'GET /assembly/asm-1/components': ok([]),
      },
    })
    const dialog = await openCreate(user)

    await user.click(within(dialog).getByRole('combobox', { name: 'Item to assemble' }))
    await user.click(await screen.findByRole('option', { name: /LOCK-SET-01/ }))

    await user.click(within(dialog).getByRole('combobox', { name: 'Component warehouse' }))
    await user.click(await screen.findByRole('option', { name: 'MAIN - Main store' }))

    await user.type(within(dialog).getByRole('textbox', { name: /planned quantity/i }), '25')
    await user.click(within(dialog).getByRole('button', { name: 'Raise order' }))

    await waitFor(() => fetch.requestFor('POST', '/assembly'))
    // The recipe, the output warehouse, the line, the dates and the notes were
    // left empty, so none is sent: the server applies its own defaults.
    expect(fetch.requestFor('POST', '/assembly')).toEqual({
      item_id: ITEM.id,
      planned_quantity: '25',
      warehouse_id: 'wh-1',
      priority: 100,
      requires_qc: false,
    })
    // Saving lands on the created order's panel rather than back on the list.
    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(await screen.findByText('ASM-0001')).toBeVisible()
  })

  it('requires the item, the quantity and the component warehouse', async () => {
    const { user, fetch } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: { 'GET /assembly': page([]), ...lookups },
    })
    await openCreate(user)

    await user.click(screen.getByRole('button', { name: 'Raise order' }))

    expect(await screen.findByText('Choose the item to assemble.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('shows no create action without the manage right', async () => {
    renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW],
      routes: { 'GET /assembly': page([]) },
    })
    // The register settles first: asserting absence before the session
    // resolves would pass without proving anything.
    await screen.findByText('No assembly orders found')

    expect(screen.queryByRole('button', { name: /new assembly order/i })).toBeNull()
  })
})

describe('amending a draft', () => {
  it('sends the editable fields and never the item or the recipe', async () => {
    const order = draft()
    const { user, fetch } = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        'GET /assembly': page([order]),
        [`GET /assembly/${order.id}`]: ok(order),
        [`GET /assembly/${order.id}/components`]: ok([]),
        ...lookups,
        [`PATCH /assembly/${order.id}`]: ok({ ...order, planned_quantity: '25.0000' }),
      },
    })
    await screen.findByText('ASM-0001')
    await user.click(screen.getByRole('button', { name: 'Open' }))
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'Edit draft' }))

    const quantity = await screen.findByRole('textbox', { name: /planned quantity/i })
    await user.clear(quantity)
    await user.type(quantity, '25')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => fetch.requestFor('PATCH', '/assembly/asm-1'))
    const body = fetch.requestFor('PATCH', '/assembly/asm-1') as Record<string, unknown>
    expect(body.planned_quantity).toBe('25')
    expect(body).not.toHaveProperty('item_id')
    expect(body).not.toHaveProperty('bom_id')
  })
})

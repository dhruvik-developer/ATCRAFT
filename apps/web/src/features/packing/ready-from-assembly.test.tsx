/**
 * Ready from assembly: the packing hand-over for a completed assembly.
 *
 * The panel lists assemblies whose output is packed nowhere yet; packing one
 * posts the assembly, the quantity and the customer to
 * `POST /packing/from-assembly`, which raises the packing order and marks the
 * assembly packed against it in one transaction. The customer comes from the
 * assembly's source when it was raised for a sales order, so it is named here
 * only otherwise - and the suite pins both shapes.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { ReadyForPackingRow } from '@/features/assembly/assembly-model'
import PackingPage from './packing-page'

function readyRow(overrides: Partial<ReadyForPackingRow> = {}): ReadyForPackingRow {
  return {
    assembly_order_id: 'asm-1',
    code: 'ASM-0001',
    item_id: 'item-lock-1',
    output_warehouse_id: 'wh-2',
    assembled_quantity: '10.0000',
    available_quantity: '10.0000',
    completed_at: '2026-03-05T10:00:00Z',
    requires_qc: false,
    source_type: 'MANUAL',
    source_id: null,
    ...overrides,
  }
}

const routes = {
  'GET /packing': page([]),
  'GET /assembly/ready-for-packing': page([readyRow()]),
}

describe('ready from assembly', () => {
  it('lists completed assemblies whose output is packed nowhere yet', async () => {
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW, P.PACKING_MANAGE],
      routes,
    })

    expect(await screen.findByText('Ready from assembly')).toBeVisible()
    expect(screen.getByText('ASM-0001')).toBeVisible()
  })

  it('raises the packing order and claims the assembly in one call', async () => {
    const created = {
      id: 'pk-9',
      code: 'PK-0009',
      sales_order_id: null,
      customer_id: 'cust-1',
      warehouse_id: 'wh-2',
      status: 'DRAFT',
      planned_date: '2026-03-06',
      packed_date: null,
      packed_by_id: null,
      total_cartons: 0,
      total_gross_weight: '0.000',
      total_net_weight: '0.000',
      dispatch_id: null,
      handed_over_at: null,
      notes: null,
      version: 1,
      lines: [],
    }
    const { user, fetch } = renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW, P.PACKING_MANAGE],
      routes: {
        ...routes,
        'GET /customers': page([{ id: 'cust-1', code: 'C-001', name: 'Arch Hardware' }]),
        'POST /packing/from-assembly': ok(created),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Pack' }))
    await screen.findByRole('dialog')

    // A manual assembly names no sales order, so the customer must be named
    // here rather than resolved from a source.
    await user.click(screen.getByRole('combobox', { name: 'Customer' }))
    await user.click(await screen.findByRole('option', { name: /C-001/ }))
    await user.click(screen.getByRole('button', { name: 'Raise packing order' }))

    await waitFor(() => fetch.requestFor('POST', '/packing/from-assembly'))
    expect(fetch.requestFor('POST', '/packing/from-assembly')).toEqual({
      assembly_order_id: 'asm-1',
      customer_id: 'cust-1',
      quantity: '10.0000',
    })
  })

  it('leaves the customer to the sales order when the assembly was raised for one', async () => {
    const { user, fetch } = renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW, P.PACKING_MANAGE],
      routes: {
        'GET /packing': page([]),
        'GET /assembly/ready-for-packing': page([
          readyRow({ source_type: 'SALES_ORDER', source_id: 'so-1' }),
        ]),
        'POST /packing/from-assembly': ok({ id: 'pk-9', code: 'PK-0009' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Pack' }))
    await screen.findByRole('dialog')
    // No customer named: the order's customer comes from the source order.
    await user.click(screen.getByRole('button', { name: 'Raise packing order' }))

    await waitFor(() => fetch.requestFor('POST', '/packing/from-assembly'))
    expect(fetch.requestFor('POST', '/packing/from-assembly')).toEqual({
      assembly_order_id: 'asm-1',
      quantity: '10.0000',
    })
  })

  it('shows no pack action without the manage right', async () => {
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW],
      routes,
    })

    await screen.findByText('ASM-0001')
    expect(screen.queryByRole('button', { name: 'Pack' })).toBeNull()
  })

  it('stays out of the way when nothing is ready', async () => {
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW, P.PACKING_MANAGE],
      routes: {
        'GET /packing': page([]),
        'GET /assembly/ready-for-packing': page([]),
      },
    })

    expect(await screen.findByText('No packing orders')).toBeVisible()
    expect(screen.queryByText('Ready from assembly')).toBeNull()
  })
})

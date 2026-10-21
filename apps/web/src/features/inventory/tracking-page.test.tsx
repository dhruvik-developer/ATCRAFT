/**
 * Batch and serial tracking.
 *
 * Two failures matter most on this screen. A reader who cannot manage lots must
 * not be offered a lifecycle action that the API will refuse, and a trace must
 * show the ledger rows for the unit that was clicked - not a generic "history"
 * panel. Both are held here at the fetch boundary.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import TrackingPage from './tracking-page'
import type { StockBatchRow, StockSerialRow } from './types'

const ITEM = {
  id: 'it000000-0000-7000-8000-000000000001',
  code: 'BRASS-ROD-12',
  name: 'Brass rod 12mm',
}
const WAREHOUSE = {
  id: 'wh000000-0000-7000-8000-000000000001',
  code: 'MAIN',
  name: 'Main store',
}

function batch(overrides: Partial<StockBatchRow> = {}): StockBatchRow {
  return {
    id: 'ba000000-0000-7000-8000-000000000001',
    item_id: ITEM.id,
    batch_number: 'L2026-07',
    supplier_batch_number: 'SUP-77',
    vendor_id: null,
    manufactured_on: '2026-07-01',
    expires_on: '2027-07-01',
    received_on: '2026-07-04',
    initial_quantity: '100.0000',
    unit_cost: '42.5000',
    status: 'ACTIVE',
    notes: null,
    created_at: '2026-07-04T09:00:00Z',
    item_code: ITEM.code,
    item_name: ITEM.name,
    on_hand_quantity: '80.0000',
    available_quantity: '65.0000',
    in_transit_quantity: '5.0000',
    ...overrides,
  }
}

function serial(overrides: Partial<StockSerialRow> = {}): StockSerialRow {
  return {
    id: 'se000000-0000-7000-8000-000000000001',
    item_id: ITEM.id,
    serial_number: 'SN-0001',
    batch_id: 'ba000000-0000-7000-8000-000000000001',
    warehouse_id: WAREHOUSE.id,
    location_id: null,
    status: 'IN_STOCK',
    received_on: '2026-07-04',
    unit_cost: '42.5000',
    notes: null,
    created_at: '2026-07-04T09:00:00Z',
    item_code: ITEM.code,
    item_name: ITEM.name,
    batch_number: 'L2026-07',
    warehouse_code: WAREHOUSE.code,
    ...overrides,
  }
}

const BATCHES = 'GET /inventory/batches'
const SERIALS = 'GET /inventory/serials'

describe('batch tracking', () => {
  it('lets an inventory reader see lots but not change their lifecycle', async () => {
    renderScreen(<TrackingPage />, {
      route: '/inventory/tracking',
      permissions: [P.INVENTORY_VIEW],
      routes: { [BATCHES]: page([batch()]) },
    })

    expect(await screen.findByText('L2026-07')).toBeInTheDocument()
    expect(screen.getByText(/SUP-77/)).toBeInTheDocument()
    expect(screen.getByText('65')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /register batch/i })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /actions for batch L2026-07/i }),
    ).not.toBeInTheDocument()
  })

  it('offers only the lifecycle edges a lot status has, and records a reason', async () => {
    const { user, fetch } = renderScreen(<TrackingPage />, {
      route: '/inventory/tracking',
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_BATCH_MANAGE],
      routes: {
        [BATCHES]: page([
          batch({ id: 'b-active', batch_number: 'L-ACTIVE', status: 'ACTIVE' }),
          batch({ id: 'b-quarantine', batch_number: 'L-HOLD', status: 'QUARANTINED' }),
          batch({ id: 'b-consumed', batch_number: 'L-EMPTY', status: 'CONSUMED' }),
        ]),
        'POST /inventory/batches/b-active/quarantine': ok(batch({ status: 'QUARANTINED' })),
      },
    })

    await screen.findByText('L-ACTIVE')
    expect(
      screen.queryByRole('button', { name: /actions for batch L-EMPTY/i }),
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /actions for batch L-ACTIVE/i }))
    expect(await screen.findByRole('menuitem', { name: 'Quarantine' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Expire' })).toBeInTheDocument()
    await user.click(screen.getByRole('menuitem', { name: 'Quarantine' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/moves to QC hold/i)).toBeInTheDocument()
    await user.type(
      within(dialog).getByRole('textbox', { name: /^reason/i }),
      'Damaged seal found at goods-in',
    )
    await user.click(within(dialog).getByRole('button', { name: /quarantine lot/i }))

    expect(fetch.requestFor('POST', '/inventory/batches/b-active/quarantine')).toEqual({
      reason: 'Damaged seal found at goods-in',
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('does not offer activating a lot that is already active', async () => {
    const { user } = renderScreen(<TrackingPage />, {
      route: '/inventory/tracking',
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_BATCH_MANAGE],
      routes: { [BATCHES]: page([batch({ status: 'ACTIVE' })]) },
    })

    await screen.findByText('L2026-07')
    await user.click(screen.getByRole('button', { name: /actions for batch L2026-07/i }))
    expect(await screen.findByRole('menuitem', { name: 'Quarantine' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Activate' })).not.toBeInTheDocument()
  })
})

describe('serial tracking', () => {
  it('loads the unit history only when a serial is traced', async () => {
    const { user, fetch } = renderScreen(<TrackingPage />, {
      route: '/inventory/tracking?tab=serials',
      permissions: [P.INVENTORY_VIEW],
      routes: {
        [SERIALS]: page([serial()]),
        'GET /inventory/serials/se000000-0000-7000-8000-000000000001': ok({
          serial: serial(),
          movements: [
            {
              id: 'tx-1',
              item_id: ITEM.id,
              item_code: ITEM.code,
              item_name: ITEM.name,
              warehouse_id: WAREHOUSE.id,
              warehouse_code: WAREHOUSE.code,
              location_id: null,
              batch_id: 'ba000000-0000-7000-8000-000000000001',
              serial_id: 'se000000-0000-7000-8000-000000000001',
              transaction_type: 'PURCHASE_RECEIPT',
              stock_state: 'AVAILABLE',
              quantity: '1.0000',
              unit_cost: '42.5000',
              value: '42.5000',
              reference_type: 'GRN',
              reference_id: null,
              reference_code: 'GRN/2026-27/00009',
              reason: 'Supplier delivery',
              occurred_at: '2026-07-04T09:15:00Z',
              created_at: '2026-07-04T09:15:00Z',
            },
          ],
        }),
      },
    })

    expect(await screen.findByText('SN-0001')).toBeInTheDocument()
    expect(screen.getByText('MAIN')).toBeInTheDocument()
    expect(fetch.calls.map((call) => call.path)).not.toContain(
      '/inventory/serials/se000000-0000-7000-8000-000000000001',
    )

    await user.click(screen.getByRole('button', { name: /trace serial SN-0001/i }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('GRN/2026-27/00009')).toBeInTheDocument()
    expect(within(dialog).getByText('Purchase Receipt')).toBeInTheDocument()
    expect(within(dialog).queryByText(/unit cost/i)).not.toBeInTheDocument()
  })

  it('shows unit cost only to a valuation reader', async () => {
    renderScreen(<TrackingPage />, {
      route: '/inventory/tracking?tab=serials',
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_VALUATION_VIEW],
      routes: { [SERIALS]: page([serial()]) },
    })

    await screen.findByText('SN-0001')
    expect(screen.getByText('Unit cost')).toBeInTheDocument()
    expect(screen.getByText(/42\.50/)).toBeInTheDocument()
  })
})

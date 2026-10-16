/**
 * The daily bookings tab: one date across all open orders, with an inline
 * Record on every row that may still take output.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import { DailyBookingsPanel } from './daily-bookings-panel'
import { localTodayISO } from './planning-model'

const TODAY = localTodayISO()

const ORDER = {
  id: 'po-1',
  code: 'PO-0001',
  status: 'IN_PROCESS',
  item_id: 'it-1',
  warehouse_id: 'wh-1',
  plant_id: null,
  cost_centre_id: null,
  planned_quantity: '100.0000',
  produced_quantity: '30.0000',
  rejected_quantity: '2.0000',
  scrap_quantity: '0.0000',
  planned_start_date: TODAY,
  planned_end_date: TODAY,
  actual_start_date: TODAY,
  actual_end_date: null,
  priority: 100,
  source_type: 'MANUAL',
  source_id: null,
  requires_qc: false,
  created_at: `${TODAY}T09:00:00Z`,
}

const ENTRY = {
  id: 'pe-1',
  production_order_id: ORDER.id,
  work_order_id: null,
  entry_date: TODAY,
  shift: 'A',
  shift_id: null,
  operator_user_id: null,
  machine_id: null,
  produced_quantity: '20.0000',
  rejected_quantity: '1.0000',
  rejection_reason: 'Scored face',
  downtime_minutes: '0.0000',
  notes: null,
  output_transaction_id: 'txn-1',
  qc_inspection_id: null,
  qc_inspection_code: null,
  qc_hold_open: false,
  created_at: `${TODAY}T14:00:00Z`,
}

function bookings(routes: Record<string, unknown> = {}) {
  return renderScreen(<DailyBookingsPanel />, {
    permissions: [P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_ENTRY_CREATE],
    routes: {
      'GET /production': page([ORDER]),
      'GET /production/entries': page([ENTRY]),
      ...routes,
    },
  })
}

describe('the daily bookings tab', () => {
  it('totals the date bookings under each open order', async () => {
    const { fetch } = bookings()

    expect(await screen.findByText('PO-0001')).toBeVisible()
    expect(await screen.findByText('20')).toBeVisible()
    expect(await screen.findByText(/1 booking/)).toBeVisible()

    // One query for the whole date, narrowed to it - not one per order.
    const calls = fetch.calls.filter((call) => call.path === '/production/entries')
    expect(calls).toHaveLength(1)
  })

  it('records through the booking dialog against the row order', async () => {
    const { user, fetch } = bookings({
      'GET /routing/roster/operators': page([]),
      [`POST /production/${ORDER.id}/entries`]: ok({ message: 'Booked 5 on PO-0001.' }),
    })

    await user.click(await screen.findByRole('button', { name: 'Record' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/produced/i), '5')
    await user.click(within(dialog).getByRole('button', { name: /record production/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/production/${ORDER.id}/entries`),
    )) as Record<string, unknown>
    expect(body.produced_quantity).toBe('5')
  })
})

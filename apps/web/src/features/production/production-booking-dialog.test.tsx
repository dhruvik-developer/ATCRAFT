import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import { RecordProductionDialog } from './production-dialogs'

const shifts = ['A', 'B'].map((code) => ({
  id: `shift-${code}`,
  code,
  name: code === 'A' ? 'Morning' : 'Afternoon',
  start_time: code === 'A' ? '06:00:00' : '14:00:00',
  end_time: code === 'A' ? '14:00:00' : '22:00:00',
  crosses_midnight: false,
  break_minutes: 0,
  sort_order: 1,
  is_active: true,
}))
const routes = {
  'GET /routing/shifts': page(shifts),
  'GET /routing/roster/operators': page([{ id: 'operator-1', full_name: 'Neha Patel' }]),
  'GET /routing/machines': page([{ id: 'machine-1', code: 'MILL', name: 'Mill', is_active: true }]),
  'GET /production/job-1/work-orders': page([
    { id: 'operation-1', code: 'WO-1', operation_name: 'Milling', work_center_id: 'cell-1' },
  ]),
  'POST /production/job-1/entries': ok({ message: 'Downtime recorded', output_movement: null }),
}

function booking(permissions: string[], extra = {}) {
  return renderScreen(
    <RecordProductionDialog
      open
      onOpenChange={vi.fn()}
      orderId="job-1"
      orderCode="PO-1"
      itemId="item-1"
    />,
    { permissions, routes: { ...routes, ...extra } },
  )
}

describe('governed production bookings', () => {
  it('books downtime against the chosen operation and shift without user.view', async () => {
    const { user, fetch } = booking([P.PRODUCTION_ENTRY_CREATE, P.WORK_ORDER_VIEW])
    await screen.findByRole('option', { name: 'WO-1 - Milling' })
    await user.selectOptions(await screen.findByLabelText('Work order'), 'operation-1')
    fireEvent.change(screen.getByLabelText(/^Date/), { target: { value: '2026-10-12' } })
    await user.click(screen.getByLabelText('Shift', { exact: true }))
    await user.click(await screen.findByRole('option', { name: /B.*Afternoon/ }))
    await user.click(screen.getByLabelText('Operator', { exact: true }))
    await user.click(await screen.findByRole('option', { name: 'Neha Patel' }))
    await user.type(screen.getByLabelText('Downtime (minutes)'), '15.5')
    await user.click(screen.getByRole('button', { name: 'Record production' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/production/job-1/entries')).toEqual({
        entry_date: '2026-10-12',
        shift_id: 'shift-B',
        work_order_id: 'operation-1',
        operator_user_id: 'operator-1',
        machine_id: null,
        produced_quantity: '0',
        rejected_quantity: '0',
        rejection_reason: null,
        downtime_minutes: '15.5',
      }),
    )
  })

  it('prefills the planned shift and machine while keeping the chosen operation', async () => {
    const { user, fetch } = booking(
      [P.PRODUCTION_ENTRY_CREATE, P.WORK_ORDER_VIEW, P.SHIFT_ROSTER_VIEW, P.MACHINE_VIEW],
      {
        'GET /routing/roster/assignments': page([
          { shift_id: 'shift-A', machine_id: 'machine-1', work_center_id: 'cell-1' },
        ]),
      },
    )
    await screen.findByRole('option', { name: 'WO-1 - Milling' })
    await user.selectOptions(await screen.findByLabelText('Work order'), 'operation-1')
    fireEvent.change(screen.getByLabelText(/^Date/), { target: { value: '2026-10-12' } })
    await user.click(screen.getByLabelText('Operator', { exact: true }))
    await user.click(await screen.findByRole('option', { name: 'Neha Patel' }))
    await screen.findByText(/Prefilled from the roster for 2026-10-12/)
    await user.type(screen.getByLabelText('Downtime (minutes)'), '20')
    await user.click(screen.getByRole('button', { name: 'Record production' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/production/job-1/entries')).toMatchObject({
        shift_id: 'shift-A',
        machine_id: 'machine-1',
        work_order_id: 'operation-1',
        produced_quantity: '0',
        rejected_quantity: '0',
        downtime_minutes: '20',
      }),
    )
  })
})

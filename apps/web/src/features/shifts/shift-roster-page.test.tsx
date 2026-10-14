import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import ShiftRosterPage from './shift-roster-page'

const shift = {
  id: 'shift-a',
  code: 'A',
  name: 'Morning',
  start_time: '06:00:00',
  end_time: '14:00:00',
  crosses_midnight: false,
  break_minutes: 0,
  sort_order: 1,
  is_active: true,
  created_at: '2026-10-12T00:00:00Z',
  updated_at: '2026-10-12T00:00:00Z',
}
const centre = { id: 'cell-1', code: 'CELL', name: 'Assembly cell', is_active: true }
const routes = {
  'GET /routing/shifts': page([shift]),
  'GET /routing/work-centers': page([centre]),
  'GET /routing/machines': page([]),
  'GET /routing/roster/assignments': page([]),
  'GET /routing/roster/operators': page([
    { id: 'operator-1', full_name: 'Neha Patel', employee_code: 'OP-001' },
  ]),
}

describe('shift and roster permissions', () => {
  it('offers master view without roster writes', async () => {
    renderScreen(<ShiftRosterPage />, { permissions: [P.SHIFT_VIEW], routes })
    expect(await screen.findByText('Morning')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'New shift' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Day board' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Assign' })).toBeNull()
  })
  it('offers roster view without master or roster writes', async () => {
    renderScreen(<ShiftRosterPage />, { permissions: [P.SHIFT_ROSTER_VIEW], routes })
    expect(await screen.findByRole('tab', { name: 'Day board' })).toBeVisible()
    expect(screen.queryByRole('tab', { name: 'Shifts' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Bulk assign' })).toBeNull()
  })
  it('renders historical roster projections when master directories are unavailable', async () => {
    renderScreen(<ShiftRosterPage />, {
      permissions: [P.SHIFT_ROSTER_VIEW],
      routes: {
        ...routes,
        'GET /routing/shifts': page([]),
        'GET /routing/work-centers': fail(403, 'FORBIDDEN', 'Missing permission'),
        'GET /routing/machines': fail(403, 'FORBIDDEN', 'Missing permission'),
        'GET /routing/roster/assignments': page([
          {
            id: 'assignment-1',
            shift_id: shift.id,
            shift_code: shift.code,
            work_center_id: centre.id,
            work_center_code: centre.code,
            work_center_name: centre.name,
            machine_id: 'machine-1',
            machine_code: 'MILL-1',
            operator_user_id: 'operator-1',
            operator_name: 'Neha Patel',
            roster_date: '2026-10-12',
            status: 'PLANNED',
          },
        ]),
      },
    })
    expect((await screen.findAllByText('Neha Patel')).length).toBe(2)
    expect(screen.getByRole('rowheader', { name: /CELL.*MILL-1/ })).toBeVisible()
  })
  it('creates a local night shift with the midnight flag derived by the server', async () => {
    const { user, fetch } = renderScreen(<ShiftRosterPage />, {
      permissions: [P.SHIFT_VIEW, P.SHIFT_MANAGE],
      routes: { ...routes, 'POST /routing/shifts': ok(shift) },
    })
    await user.click(await screen.findByRole('button', { name: 'New shift' }))
    await user.type(screen.getByLabelText('Code'), 'C')
    await user.type(screen.getByLabelText('Name'), 'Night')
    await user.clear(screen.getByLabelText('Start time'))
    await user.type(screen.getByLabelText('Start time'), '22:00')
    await user.clear(screen.getByLabelText('End time'))
    await user.type(screen.getByLabelText('End time'), '06:00')
    await user.click(screen.getByRole('button', { name: 'Save shift' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/routing/shifts')).toEqual({
        code: 'C',
        name: 'Night',
        start_time: '22:00',
        end_time: '06:00',
        break_minutes: 0,
        sort_order: 0,
        is_active: true,
      }),
    )
  })
  it('posts the selected cell and operator and surfaces a conflict', async () => {
    const { user, fetch } = renderScreen(<ShiftRosterPage />, {
      permissions: [P.SHIFT_ROSTER_VIEW, P.SHIFT_ROSTER_MANAGE],
      routes: {
        ...routes,
        'POST /routing/roster/assignments': fail(
          422,
          'BUSINESS_RULE_VIOLATION',
          'That assignment overlaps another for the same operator or machine',
          {
            conflicts: [
              {
                resource: 'operator',
                message: 'Neha Patel is already on A at CELL on 2026-10-12.',
              },
              { resource: 'machine', message: 'Machine MILL is already staffed on A at CELL.' },
            ],
          },
        ),
      },
    })
    await user.click((await screen.findAllByRole('button', { name: 'Assign' }))[0]!)
    const form = within(screen.getByRole('dialog'))
    await user.selectOptions(await form.findByLabelText('Work centre'), 'cell-1')
    await user.selectOptions(form.getByLabelText('Shift'), 'shift-a')
    await user.selectOptions(await form.findByLabelText('Operator'), 'operator-1')
    await user.click(screen.getByRole('button', { name: 'Save assignments' }))
    expect(await screen.findByText(/That assignment overlaps/)).toBeVisible()
    expect(screen.getByText('Neha Patel is already on A at CELL on 2026-10-12.')).toBeVisible()
    expect(screen.getByText('Machine MILL is already staffed on A at CELL.')).toBeVisible()
    expect(fetch.requestFor('POST', '/routing/roster/assignments')).toMatchObject({
      shift_id: 'shift-a',
      work_center_id: 'cell-1',
      machine_id: null,
      operator_user_id: 'operator-1',
      notes: null,
    })
  })
})

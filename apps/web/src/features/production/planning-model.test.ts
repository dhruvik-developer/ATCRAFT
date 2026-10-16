/**
 * The planning board's date and overlap arithmetic, without a browser.
 *
 * Day columns are browser-local dates and clash badges compare loaded
 * operations; both are pure functions of the rows, so they are pinned here
 * rather than through the page.
 */

import { describe, expect, it } from 'vitest'

import type { WorkOrder } from './work-order-model'
import {
  addDaysISO,
  boardWeek,
  clashingWorkOrders,
  groupCapacityByCentre,
  touchedColumns,
  windowsOverlap,
  type CapacityDay,
} from './planning-model'

function day(overrides: Partial<CapacityDay> = {}): CapacityDay {
  return {
    work_center_id: 'wc-1',
    work_center_code: 'MILL-01',
    work_center_name: 'CNC milling',
    date: '2026-10-12',
    planned_minutes: '60.0000',
    capacity_minutes: '480.0000',
    capacity_source: 'STANDARD',
    over_capacity: false,
    ...overrides,
  }
}

function operation(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 'wo-1',
    code: 'WO-0001',
    production_order_id: 'po-1',
    sequence: 10,
    operation_code: 'MILL',
    operation_name: 'Mill the face',
    work_center_id: 'wc-1',
    machine_id: null,
    operator_user_id: null,
    status: 'PENDING',
    planned_quantity: '10.0000',
    completed_quantity: '0.0000',
    rejected_quantity: '0.0000',
    planned_setup_minutes: '30.0000',
    planned_run_minutes: '60.0000',
    planned_start_at: '2026-10-12T06:00:00Z',
    planned_end_at: '2026-10-12T07:30:00Z',
    actual_start_at: null,
    actual_end_at: null,
    shift_id: null,
    shift: null,
    notes: null,
    work_center_cost_per_hour: '250.0000',
    work_center_setup_cost: '100.0000',
    process_cost: '0.0000',
    is_outsourced: false,
    ...overrides,
  }
}

describe('the board week', () => {
  it('covers seven dates from the chosen start', () => {
    expect(boardWeek('2026-10-12')).toHaveLength(7)
    expect(boardWeek('2026-10-12')[0]).toBe('2026-10-12')
    expect(boardWeek('2026-10-12')[6]).toBe('2026-10-18')
  })

  it('adds days across month boundaries', () => {
    expect(addDaysISO('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDaysISO('2026-10-12', -7)).toBe('2026-10-05')
  })
})

describe('touchedColumns', () => {
  const week = boardWeek('2026-10-12')

  it('files a short window under exactly one column', () => {
    const row = operation({
      planned_start_at: '2026-10-12T12:00:00Z',
      planned_end_at: '2026-10-12T13:00:00Z',
    })
    expect(touchedColumns(row, week)).toHaveLength(1)
  })

  it('files a 25-hour window under exactly two adjacent columns', () => {
    const row = operation({
      planned_start_at: '2026-10-12T12:00:00Z',
      planned_end_at: '2026-10-13T13:00:00Z',
    })
    const columns = touchedColumns(row, week)
    expect(columns).toHaveLength(2)
    expect(columns[1]! - columns[0]!).toBe(1)
  })

  it('ignores operations with no planned window', () => {
    expect(
      touchedColumns(operation({ planned_start_at: null, planned_end_at: null }), week),
    ).toEqual([])
  })
})

describe('windowsOverlap', () => {
  it('treats back-to-back windows as sequencing, not a clash', () => {
    expect(
      windowsOverlap(
        ['2026-10-12T06:00:00Z', '2026-10-12T08:00:00Z'],
        ['2026-10-12T08:00:00Z', '2026-10-12T10:00:00Z'],
      ),
    ).toBe(false)
    expect(
      windowsOverlap(
        ['2026-10-12T06:00:00Z', '2026-10-12T08:00:00Z'],
        ['2026-10-12T07:00:00Z', '2026-10-12T10:00:00Z'],
      ),
    ).toBe(true)
  })

  it('needs both ends on both sides', () => {
    expect(windowsOverlap([null, null], ['2026-10-12T07:00:00Z', '2026-10-12T10:00:00Z'])).toBe(
      false,
    )
  })
})

describe('clashingWorkOrders', () => {
  it('badges two operations sharing a machine and an hour', () => {
    const first = operation({ id: 'wo-1' })
    const second = operation({
      id: 'wo-2',
      code: 'WO-0002',
      machine_id: 'mc-1',
      planned_start_at: '2026-10-12T06:30:00Z',
      planned_end_at: '2026-10-12T08:00:00Z',
    })
    const withMachine = { ...first, machine_id: 'mc-1' }
    expect(clashingWorkOrders([withMachine, second])).toEqual(new Set(['wo-1', 'wo-2']))
  })

  it('ignores different machines and unscheduled rows', () => {
    const first = operation({ id: 'wo-1', machine_id: 'mc-1' })
    const second = operation({ id: 'wo-2', code: 'WO-0002', machine_id: 'mc-2' })
    const floating = operation({
      id: 'wo-3',
      code: 'WO-0003',
      machine_id: 'mc-1',
      planned_start_at: null,
      planned_end_at: null,
    })
    expect(clashingWorkOrders([first, second, floating])).toEqual(new Set())
  })
})

describe('groupCapacityByCentre', () => {
  it('groups days under their centre in code order', () => {
    const rows = [
      day({ work_center_id: 'wc-2', work_center_code: 'PRESS-01', work_center_name: 'Pressing' }),
      day(),
      day({ date: '2026-10-13' }),
    ]
    const groups = groupCapacityByCentre(rows)
    expect(groups.map((group) => group.code)).toEqual(['MILL-01', 'PRESS-01'])
    expect(groups[0]!.days).toHaveLength(2)
  })
})

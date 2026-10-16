/**
 * Wire shapes and board arithmetic for the production planning board.
 *
 * The board reads two endpoints over the same week: `GET /production/capacity`
 * for the heat strip (planned minutes per centre per day against rostered or
 * standard capacity) and `GET /production/work-orders` for the timeline
 * blocks. Both are fetched whole - every page, bounded by the week and the
 * centre filter - because a timeline that silently drops its eighth row is
 * worse than one that takes two requests to fill.
 *
 * Two deliberate approximations, both stated so they can be revisited:
 *
 * 1. **Day columns are browser-local days.** Work-order windows are UTC
 *    instants and the capacity buckets are company-local days; the browser
 *    does not know the company timezone, so a chip is filed under the day its
 *    instant falls on where the planner sits. For the plants this serves those
 *    are the same day.
 * 2. **Overlap badges cover the loaded week only.** Two operations clashing
 *    outside the visible week do not badge each other; the API still
 *    warns-or-refuses on the PATCH itself, which sees everything.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'
import type { Machine } from '@/features/routing/routing-model'

import type { WorkOrder } from './work-order-model'

// ------------------------------------------------------------ query keys ----

/**
 * Planning reads live documents, so its keys sit under the production prefix:
 * anything that invalidates `queryKeys.production.all` - a booking, a release,
 * an assignment - picks these lists up too.
 */
export const planningKeys = {
  capacity: (params: unknown) => [...queryKeys.production.all, 'capacity', params] as const,
  board: (params: unknown) => [...queryKeys.production.all, 'planning-board', params] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /production/capacity` (`ProductionCapacityDay`). */
export type CapacityDay = SchemaOf<'ProductionCapacityDay'>

/** `capacity_source` on the row: rostered minutes, or the standard day. */
export type CapacitySource = SchemaOf<'CapacitySource'>

// -------------------------------------------------------------- week days ----

/** This week's board is seven local dates; the API takes them as `YYYY-MM-DD`. */
export const BOARD_DAYS = 7

/** Today's local date as `YYYY-MM-DD` - the board's default first column. */
export function localTodayISO(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

/** Add whole days to a `YYYY-MM-DD`, staying in date arithmetic, never instants. */
export function addDaysISO(iso: string, delta: number): string {
  const [year, month, day] = iso.split('-').map(Number)
  const date = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, day ?? 1))
  date.setUTCDate(date.getUTCDate() + delta)
  return date.toISOString().slice(0, 10)
}

/** The seven dates the board shows starting from `startISO`. */
export function boardWeek(startISO: string): string[] {
  return Array.from({ length: BOARD_DAYS }, (_, offset) => addDaysISO(startISO, offset))
}

/** Short weekday + day/month for a column header: "Mon 19". */
export function columnLabel(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number)
  const date = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, day ?? 1))
  const weekday = date.toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' })
  return `${weekday} ${day}`
}

// -------------------------------------------------------- timeline maths ----

/** Start of the browser-local day as a UTC instant, for bucketing windows. */
function dayStartMs(iso: string): number {
  const [year, month, day] = iso.split('-').map(Number)
  const local = new Date(year ?? 1970, (month ?? 1) - 1, day ?? 1)
  return local.getTime()
}

/**
 * Which board columns (0-based) a work order touches.
 *
 * A block with no planned window touches none and is listed under the board
 * instead of drawn on it: unscheduled work is invisible to capacity, and the
 * queue is where it waits to be planned.
 */
export function touchedColumns(workOrder: WorkOrder, week: readonly string[]): number[] {
  if (!workOrder.planned_start_at || !workOrder.planned_end_at) return []
  const start = Date.parse(workOrder.planned_start_at)
  const end = Date.parse(workOrder.planned_end_at)
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return []
  const columns: number[] = []
  week.forEach((iso, index) => {
    const opens = dayStartMs(iso)
    const closes = opens + 24 * 60 * 60 * 1000
    if (start < closes && end > opens) columns.push(index)
  })
  return columns
}

/** Half-open window overlap on instants - the same rule the API applies. */
export function windowsOverlap(
  first: readonly [string | null, string | null],
  second: readonly [string | null, string | null],
): boolean {
  if (!first[0] || !first[1] || !second[0] || !second[1]) return false
  const starts = [Date.parse(first[0]), Date.parse(second[0])]
  const ends = [Date.parse(first[1]), Date.parse(second[1])]
  if (starts.some((value) => Number.isNaN(value)) || ends.some((value) => Number.isNaN(value))) {
    return false
  }
  const [aStart, bStart] = starts as [number, number]
  const [aEnd, bEnd] = ends as [number, number]
  return aStart < bEnd && bStart < aEnd
}

/**
 * Work-order ids whose planned window overlaps another loaded operation on
 * the same machine.
 *
 * Only operations naming a machine with both ends planned can clash; the rest
 * are invisible to this check exactly as they are to the API's. Computed over
 * the loaded week, so the badge says "clashes with something on this board".
 */
export function clashingWorkOrders(rows: readonly WorkOrder[]): Set<string> {
  const clashing = new Set<string>()
  const placed = rows.filter((row) => row.machine_id && row.planned_start_at && row.planned_end_at)
  for (let left = 0; left < placed.length; left++) {
    for (let right = left + 1; right < placed.length; right++) {
      const a = placed[left]
      const b = placed[right]
      if (!a || !b) continue
      if (
        a.machine_id === b.machine_id &&
        windowsOverlap(
          [a.planned_start_at, a.planned_end_at],
          [b.planned_start_at, b.planned_end_at],
        )
      ) {
        clashing.add(a.id)
        clashing.add(b.id)
      }
    }
  }
  return clashing
}

// -------------------------------------------------------------- fetching ----

const BOARD_PAGE_LIMIT = LOOKUP_LIST_LIMIT

/**
 * Every page of a board endpoint, in order.
 *
 * The board cannot page honestly - page two of the operations is still this
 * week's work - so it reads to the end. Bounded in practice: one week of one
 * cell is a handful of rows, and the loop gives up after ten pages rather
 * than following a runaway cursor.
 */
export async function fetchAllPages<T>(path: string, params: Record<string, unknown>) {
  const rows: T[] = []
  let page = 1
  for (;;) {
    const response = await api.getPage<T>(path, {
      params: { ...params, page, limit: BOARD_PAGE_LIMIT },
    })
    rows.push(...response.data)
    if (!response.pagination.has_next || page >= 10) return rows
    page += 1
  }
}

/** Capacity rows grouped by centre, in centre-code order with days in order. */
export function groupCapacityByCentre(
  rows: readonly CapacityDay[],
): { centreId: string; code: string; name: string; days: CapacityDay[] }[] {
  const groups = new Map<
    string,
    { centreId: string; code: string; name: string; days: CapacityDay[] }
  >()
  for (const row of rows) {
    const group = groups.get(row.work_center_id) ?? {
      centreId: row.work_center_id,
      code: row.work_center_code,
      name: row.work_center_name,
      days: [],
    }
    group.days.push(row)
    groups.set(row.work_center_id, group)
  }
  return [...groups.values()].sort((a, b) => a.code.localeCompare(b.code))
}

/** Fetch the capacity board with the shared transactional freshness. */
export function useCapacityBoard(params: Record<string, unknown>, enabled = true) {
  return useQuery({
    queryKey: planningKeys.capacity(params),
    queryFn: () => fetchAllPages<CapacityDay>('/production/capacity', params),
    staleTime: staleTimes.transactional,
    refetchOnWindowFocus: true,
    enabled,
  })
}

/** Fetch the week's operations with the same freshness. */
export function useBoardWorkOrders(params: Record<string, unknown>, enabled = true) {
  return useQuery({
    queryKey: planningKeys.board(params),
    queryFn: () => fetchAllPages<WorkOrder>('/production/work-orders', params),
    staleTime: staleTimes.transactional,
    refetchOnWindowFocus: true,
    enabled,
  })
}

/**
 * The machine register for the machine view and chip labels.
 *
 * One bounded fetch for the whole board rather than one per centre: the board
 * already holds every centre it draws, so the register is filtered in memory
 * to those cells. Retired and inactive machines stay in the list - unlike the
 * pickers, this is a reading view, and an operation already planned on a since
 * retired machine must still draw on its row rather than vanish.
 */
const MACHINE_REGISTER_PARAMS = {
  limit: LOOKUP_LIST_LIMIT,
  with_total: false,
  sort_by: 'code',
  sort_dir: 'asc',
} as const

export function useBoardMachines(enabled = true) {
  return useQuery({
    queryKey: queryKeys.routing.machines(MACHINE_REGISTER_PARAMS),
    queryFn: () => fetchAllPages<Machine>('/routing/machines', { ...MACHINE_REGISTER_PARAMS }),
    staleTime: staleTimes.reference,
    enabled,
  })
}

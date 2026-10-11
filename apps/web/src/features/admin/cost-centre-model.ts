/**
 * Cost centres: the wire shapes, and the tree arithmetic the master tab and
 * every picker share.
 *
 * A cost centre is where money is charged - a die-casting shop, a sales zone -
 * and the centres nest, at most six levels deep (`MAX_COST_CENTRE_DEPTH`, the
 * department limit; docs/assumptions.md §B1-5). Everything here is derived from
 * the generated contract rather than restated, so a field the API drops stops
 * compiling instead of rendering as a blank.
 *
 * The helpers are pure on purpose. A picker built from the full reference list
 * (`useCostCentreOptions`) has to decide three things locally - the order the
 * tree reads in, which rows may be offered, and which may not become a parent -
 * and each of those is a rule worth pinning in a test of its own rather than
 * inside a rendered select.
 */

import type { CostCentreOption } from '@/hooks/use-organisation-options'
import type { SchemaOf } from '@/types/api'

export type CostCentreRow = SchemaOf<'CostCentreRead'>
export type CostCentreCreate = SchemaOf<'CostCentreCreate'>
export type CostCentreUpdate = SchemaOf<'CostCentreUpdate'>

/**
 * How a centre is grouped on a report (`CostCentreType`).
 *
 * Reporting only - nothing in the system branches on it (docs/database.md,
 * "Cost centres"), so the form may offer all four freely. `satisfies` keeps the
 * list honest against the contract: a member the API drops stops compiling.
 */
export type CostCentreType = SchemaOf<'CostCentreType'>

export const COST_CENTRE_TYPES = [
  'PRODUCTION',
  'SERVICE',
  'ADMINISTRATION',
  'SELLING',
] as const satisfies readonly CostCentreType[]

/** One row of a tree, with how far down it sits. The roots are depth 0. */
export interface TreeEntry<T> {
  row: T
  depth: number
}

/**
 * Rows in depth-first order, siblings by code - the order a tree reads in.
 *
 * A row whose parent is not in the set (a list cut off at the lookup limit) is
 * placed at the top rather than dropped: an option nobody can see is worse than
 * one shown one level too high. The server refuses cycles, but a row that never
 * reaches a root is still listed at the end, for the same reason.
 */
export function inTreeOrder<T extends CostCentreOption>(rows: readonly T[]): TreeEntry<T>[] {
  const ids = new Set(rows.map((row) => row.id))
  const children = new Map<string | null, T[]>()
  for (const row of rows) {
    const parent = row.parent_id !== null && ids.has(row.parent_id) ? row.parent_id : null
    const siblings = children.get(parent)
    if (siblings) siblings.push(row)
    else children.set(parent, [row])
  }
  for (const siblings of children.values()) {
    siblings.sort((a, b) => a.code.localeCompare(b.code))
  }

  const ordered: TreeEntry<T>[] = []
  const placed = new Set<string>()
  const visit = (parent: string | null, depth: number) => {
    for (const row of children.get(parent) ?? []) {
      if (placed.has(row.id)) continue
      placed.add(row.id)
      ordered.push({ row, depth })
      visit(row.id, depth + 1)
    }
  }
  visit(null, 0)

  for (const row of rows) {
    if (!placed.has(row.id)) {
      placed.add(row.id)
      ordered.push({ row, depth: 0 })
    }
  }
  return ordered
}

/**
 * A centre and everything beneath it - what may not become its parent.
 *
 * The server refuses a move beneath the centre's own descendant
 * (`assert_parent_allowed`); withholding those options is not a list query, it
 * spares the user a choice whose only answer is a refusal.
 */
export function subtreeIds(rows: readonly CostCentreOption[], rootId: string): Set<string> {
  const children = new Map<string, string[]>()
  for (const row of rows) {
    if (row.parent_id === null) continue
    const siblings = children.get(row.parent_id)
    if (siblings) siblings.push(row.id)
    else children.set(row.parent_id, [row.id])
  }

  const found = new Set<string>([rootId])
  const queue = [rootId]
  while (queue.length > 0) {
    const current = queue.shift() as string
    for (const child of children.get(current) ?? []) {
      if (found.has(child)) continue
      found.add(child)
      queue.push(child)
    }
  }
  return found
}

/**
 * What a picker may offer, in tree order.
 *
 * Active centres, because a new allocation to an inactive one is refused - plus
 * the one the record already carries, active or not. A document keeps its
 * centre after deactivation (docs/assumptions.md §B1-3); leaving that centre
 * out of its own edit form would make the next save look like a deliberate
 * "unallocated".
 */
export function pickableCostCentres<T extends CostCentreOption>(
  rows: readonly T[],
  current: string | null,
  exclude?: ReadonlySet<string>,
): TreeEntry<T>[] {
  return inTreeOrder(rows).filter(
    ({ row }) => (row.is_active || row.id === current) && !exclude?.has(row.id),
  )
}

/** "CC-MACH - Machining", marked when the centre is no longer offered. */
export function costCentreLabel(
  row: Pick<CostCentreOption, 'code' | 'name' | 'is_active'>,
): string {
  return `${row.code} - ${row.name}${row.is_active ? '' : ' (inactive)'}`
}

/**
 * The tree arithmetic every cost-centre picker shares.
 *
 * Pure rules, pinned on their own because each is a decision a rendered select
 * would hide: the order the tree reads in, which rows may be offered, and which
 * may not become a parent.
 */

import { describe, expect, it } from 'vitest'

import type { CostCentreOption } from '@/hooks/use-organisation-options'

import { costCentreLabel, inTreeOrder, pickableCostCentres, subtreeIds } from './cost-centre-model'

function node(
  id: string,
  code: string,
  parent_id: string | null = null,
  is_active = true,
): CostCentreOption {
  return { id, code, name: code.toLowerCase(), parent_id, is_active }
}

// Listed out of order on purpose: the API pages by code, which does not put a
// child beneath its parent.
const ROWS = [
  node('sales', 'CC-SALES'),
  node('cnc', 'CC-MACH-CNC', 'mach'),
  node('prod', 'CC-PROD'),
  node('pack', 'CC-PACK', 'prod'),
  node('mach', 'CC-MACH', 'prod'),
  node('west', 'CC-SALES-WEST', 'sales', false),
]

const order = (entries: { row: CostCentreOption; depth: number }[]) =>
  entries.map(({ row, depth }) => `${depth}:${row.code}`)

describe('inTreeOrder', () => {
  it('reads depth first, each level by code', () => {
    expect(order(inTreeOrder(ROWS))).toEqual([
      '0:CC-PROD',
      '1:CC-MACH',
      '2:CC-MACH-CNC',
      '1:CC-PACK',
      '0:CC-SALES',
      '1:CC-SALES-WEST',
    ])
  })

  it('lists a row whose parent is missing at the top rather than losing it', () => {
    // A list cut off at the lookup limit can hold a child without its parent.
    expect(order(inTreeOrder([node('cnc', 'CC-MACH-CNC', 'mach')]))).toEqual(['0:CC-MACH-CNC'])
  })

  it('still lists rows caught in a cycle the server should never have allowed', () => {
    const cycle = [node('a', 'CC-A', 'b'), node('b', 'CC-B', 'a')]
    expect(inTreeOrder(cycle).map(({ row }) => row.code)).toEqual(['CC-A', 'CC-B'])
  })
})

describe('subtreeIds', () => {
  it('is the centre and everything beneath it, at any depth', () => {
    expect(subtreeIds(ROWS, 'prod')).toEqual(new Set(['prod', 'mach', 'cnc', 'pack']))
    expect(subtreeIds(ROWS, 'cnc')).toEqual(new Set(['cnc']))
  })
})

describe('pickableCostCentres', () => {
  it('offers active centres only', () => {
    const codes = pickableCostCentres(ROWS, null).map(({ row }) => row.code)
    expect(codes).not.toContain('CC-SALES-WEST')
    expect(codes).toContain('CC-SALES')
  })

  it('keeps the inactive centre a record already carries, so a save cannot drop it', () => {
    const codes = pickableCostCentres(ROWS, 'west').map(({ row }) => row.code)
    expect(codes).toContain('CC-SALES-WEST')
  })

  it("withholds what is excluded, such as a centre's own subtree as its parent", () => {
    const codes = pickableCostCentres(ROWS, 'prod', subtreeIds(ROWS, 'mach')).map(
      ({ row }) => row.code,
    )
    expect(codes).toEqual(['CC-PROD', 'CC-PACK', 'CC-SALES'])
  })
})

describe('costCentreLabel', () => {
  it('reads as code and name, and says when a centre is no longer offered', () => {
    expect(costCentreLabel({ code: 'CC-MACH', name: 'Machining', is_active: true })).toBe(
      'CC-MACH - Machining',
    )
    expect(costCentreLabel({ code: 'CC-OLD', name: 'Old zone', is_active: false })).toBe(
      'CC-OLD - Old zone (inactive)',
    )
  })
})

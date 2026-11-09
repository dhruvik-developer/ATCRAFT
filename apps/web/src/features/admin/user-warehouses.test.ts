/**
 * The warehouse assignment rules the user form applies before it saves.
 *
 * They mirror `assert_can_assign_warehouses` (docs/assumptions.md §WH-1): the
 * API is the authority and refuses with 403 SCOPE_DENIED, so what is pinned
 * here is only that the form never offers a save that refusal is certain to
 * meet - and never blocks one the API would accept.
 */

import { describe, expect, it } from 'vitest'

import {
  mayMoveWarehouse,
  mustChooseWarehouse,
  sameIds,
  warehouseAssignmentChange,
  warehouseReach,
} from './user-warehouses'

const MAIN = 'wh-main'
const NORTH = 'wh-north'
const SOUTH = 'wh-south'

describe('warehouseReach', () => {
  it('is every warehouse for an administrator with no assignment', () => {
    expect(warehouseReach({ is_superuser: false, allowed_warehouse_ids: [] })).toBeNull()
  })

  it('is every warehouse for a superuser, whatever their own list says', () => {
    // The API's warehouse scope exempts a superuser before it reads the list.
    expect(warehouseReach({ is_superuser: true, allowed_warehouse_ids: [MAIN] })).toBeNull()
  })

  it('is exactly the assignment of a limited administrator', () => {
    const reach = warehouseReach({ is_superuser: false, allowed_warehouse_ids: [MAIN, NORTH] })
    expect(reach).toEqual(new Set([MAIN, NORTH]))
    expect(mayMoveWarehouse(reach, MAIN)).toBe(true)
    expect(mayMoveWarehouse(reach, SOUTH)).toBe(false)
  })

  it('leaves the decision to the API when nobody is signed in', () => {
    expect(warehouseReach(null)).toBeNull()
    expect(mayMoveWarehouse(null, SOUTH)).toBe(true)
  })
})

describe('mustChooseWarehouse', () => {
  const limited = new Set([MAIN, NORTH])

  it('never holds an unrestricted administrator to a choice', () => {
    expect(mustChooseWarehouse(null, null, [])).toBe(false)
    expect(mustChooseWarehouse(null, [MAIN], [])).toBe(false)
  })

  it('makes a limited administrator name a warehouse for a new account', () => {
    // An empty list is every warehouse, which they may not give.
    expect(mustChooseWarehouse(limited, null, [])).toBe(true)
    expect(mustChooseWarehouse(limited, null, [NORTH])).toBe(false)
  })

  it('refuses to let them empty a limited account, which would free it', () => {
    expect(mustChooseWarehouse(limited, [MAIN], [])).toBe(true)
  })

  it('lets them save an unrestricted account they cannot limit, left as it was', () => {
    // Nothing moves, so the API needs no authority - an edit to the phone
    // number of an unrestricted colleague must not be blocked.
    expect(mustChooseWarehouse(limited, [], [])).toBe(false)
  })
})

describe('warehouseAssignmentChange', () => {
  it('always sends the list on a create, an empty one included', () => {
    expect(warehouseAssignmentChange(null, [])).toEqual({ allowed_warehouse_ids: [] })
    expect(warehouseAssignmentChange(null, [MAIN])).toEqual({ allowed_warehouse_ids: [MAIN] })
  })

  it('leaves an unmoved assignment out of an edit, whatever the order', () => {
    expect(warehouseAssignmentChange([MAIN, NORTH], [NORTH, MAIN])).toEqual({})
    expect(warehouseAssignmentChange([], [])).toEqual({})
  })

  it('sends the whole list when an edit moves any of it', () => {
    expect(warehouseAssignmentChange([MAIN], [MAIN, SOUTH])).toEqual({
      allowed_warehouse_ids: [MAIN, SOUTH],
    })
    // Emptying the list frees the account; it is sent, not dropped.
    expect(warehouseAssignmentChange([MAIN], [])).toEqual({ allowed_warehouse_ids: [] })
  })

  it('hands back a copy, not the form state itself', () => {
    const chosen = [MAIN]
    const sent = warehouseAssignmentChange(null, chosen).allowed_warehouse_ids
    expect(sent).toEqual(chosen)
    expect(sent).not.toBe(chosen)
  })
})

describe('sameIds', () => {
  it('compares membership, not order', () => {
    expect(sameIds([MAIN, NORTH], [NORTH, MAIN])).toBe(true)
    expect(sameIds([MAIN], [MAIN, NORTH])).toBe(false)
    expect(sameIds([MAIN, NORTH], [MAIN, SOUTH])).toBe(false)
  })
})

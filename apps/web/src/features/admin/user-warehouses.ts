/**
 * An account's warehouse assignment, as the user form edits it.
 *
 * `allowed_warehouse_ids` is authority on a second axis (docs/assumptions.md
 * §WH-1). An administrator assigned to some warehouses may give an account, or
 * take away from it, only warehouses they are assigned to themselves - and
 * because an empty assignment means every warehouse, they can neither leave an
 * account unrestricted nor limit one that is. An administrator with no
 * assignment, and a superuser, are not limited: the same two exemptions the
 * API's warehouse scope makes (`src/common/warehouse_scope.py`), which is also
 * why `GET /warehouses` lists every warehouse to them and only their own to
 * anyone else.
 *
 * The API enforces all of it, answering 403 SCOPE_DENIED. These rules only keep
 * the form from offering a save that refusal is certain to meet.
 */

import type { CurrentUser } from '@/features/auth/auth-context'

/** The warehouses an administrator may give or take away; null for every one. */
export type WarehouseReach = ReadonlySet<string> | null

/**
 * What the signed-in administrator may move.
 *
 * Nobody reaches the user form signed out; were it ever rendered so, it would
 * offer every warehouse and leave the decision to the API, which makes it.
 */
export function warehouseReach(
  administrator: Pick<CurrentUser, 'is_superuser' | 'allowed_warehouse_ids'> | null,
): WarehouseReach {
  if (!administrator || administrator.is_superuser) return null
  if (administrator.allowed_warehouse_ids.length === 0) return null
  return new Set(administrator.allowed_warehouse_ids)
}

/** Whether this administrator may give, or take away, one warehouse. */
export function mayMoveWarehouse(reach: WarehouseReach, warehouseId: string): boolean {
  return reach === null || reach.has(warehouseId)
}

/** The same set of ids, whatever the order. */
export function sameIds(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  const set = new Set(left)
  return right.every((id) => set.has(id))
}

/**
 * Whether a save must name at least one warehouse before it is sent.
 *
 * `stored` is the account's assignment before this save, or null for an
 * account being created. Only a limited administrator ever must: for them an
 * empty list would make the account unrestricted. The one empty list they may
 * send is the one the account already had - an unrestricted account they
 * cannot limit, left as it was, which moves nothing.
 */
export function mustChooseWarehouse(
  reach: WarehouseReach,
  stored: readonly string[] | null,
  chosen: readonly string[],
): boolean {
  if (reach === null || chosen.length > 0) return false
  return stored === null || stored.length > 0
}

/**
 * The assignment a save sends.
 *
 * Always on a create. On an edit only when it moved: an unchanged list needs no
 * authority, and re-sending the one read when the form opened would put back
 * a change another administrator made in the meantime.
 */
export function warehouseAssignmentChange(
  stored: readonly string[] | null,
  chosen: readonly string[],
): { allowed_warehouse_ids?: string[] } {
  if (stored !== null && sameIds(stored, chosen)) return {}
  return { allowed_warehouse_ids: [...chosen] }
}

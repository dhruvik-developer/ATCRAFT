/**
 * Wire shapes, query keys and vocabulary for storage locations read across
 * every warehouse.
 *
 * The warehouses screen answers "what is inside this warehouse". This module
 * backs the other question - "where is bin A-14", and "are the racks in all six
 * plants coded the way the labelling standard says" - so the shapes here are
 * derived from the generated contract rather than retyped. `warehouse-model.ts`
 * carries a hand-written `StorageLocation` for the panel nested under one
 * warehouse; a second hand-written copy would be a second opinion, and `tsc`
 * cannot referee two opinions about what the server sends.
 *
 * Three properties of the contract shape the screen above it:
 *
 * 1. **A location row is not denormalised.** `warehouse_id` and
 *    `parent_location_id` arrive as bare ids. The warehouse name therefore comes
 *    from the single `/warehouses/options` call the pickers already make, and
 *    the parent is resolved only for the one row the user opens - a lookup per
 *    row is exactly the N+1 this codebase forbids.
 * 2. **The list can show a row the detail endpoint denies.** `include_deleted`
 *    returns retired locations, while `GET /warehouses/locations/{id}` goes
 *    through `get_or_404`, which scopes soft-deleted rows out. So the detail
 *    panel renders the row it was handed and only *asks* the API about the
 *    parent - see `useLocationDetail`.
 * 3. **Sorting is a server whitelist.** `StorageLocationRepository.sortable_fields`
 *    is `code`, `name`, `location_type`, `is_active`, `created_at`, `updated_at`
 *    and nothing else, so any column outside that set must declare
 *    `enableSorting: false` rather than send a `sort_by` the API will ignore.
 *
 * Capacity and the weight and volume limits are NUMERIC strings. They are handed
 * straight to `formatQuantity`, which converts through decimal.js; `Number()`
 * never touches one.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { formatQuantity } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import { LOCATION_TYPES as WAREHOUSE_MODULE_LOCATION_TYPES } from './warehouse-model'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no storage-location branch, so
 * the keys live here in the same hierarchical shape: invalidating
 * `locationKeys.all` clears every cross-warehouse list and every resolved
 * parent at once.
 *
 * Deliberately *not* under the `warehouses` branch. The panel on the warehouses
 * screen caches its locations under `queryKeys.warehouses.detail(id)`, and a
 * location created there invalidates that branch only - this list picks the new
 * row up on its next mount or from the table's own refresh control, which is
 * the right trade for a five-minute master stale time.
 */
export const locationKeys = {
  all: ['storage-locations'] as const,
  list: (params: unknown) => ['storage-locations', 'list', params] as const,
  detail: (id: string) => ['storage-locations', 'detail', id] as const,
}

// ------------------------------------------------------------ vocabulary ----

/** `StorageLocationType` - the physical shape of a storage position. */
export type LocationType = SchemaOf<'StorageLocationType'>

/**
 * The options the type filter offers.
 *
 * Reused from the warehouse module rather than retyped, and annotated against
 * the generated union: the annotation is what proves the neighbouring tuple
 * still agrees with the contract, and it fails to compile the day a member is
 * renamed on the server.
 */
export const LOCATION_TYPES: readonly LocationType[] = WAREHOUSE_MODULE_LOCATION_TYPES

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /warehouses/locations` (`StorageLocationRead`).
 *
 * The same schema serves the detail endpoint, so a row from the list is a
 * complete location and the detail panel needs no second fetch of itself.
 */
export type StorageLocationRow = SchemaOf<'StorageLocationRead'>

// -------------------------------------------------------------- derived -----

/**
 * Retired means soft-deleted, which is not the same as deactivated.
 *
 * A deactivated location still exists and can be switched back on; a retired one
 * has been withdrawn after the server checked it holds no stock and has no live
 * sub-locations, and only appears at all when `include_deleted` is asked for.
 * The list has to say which of the two it is looking at.
 */
export const isRetired = (location: StorageLocationRow): boolean => Boolean(location.deleted_at)

/**
 * Capacity as it should read in a grid, or `null` when none is recorded.
 *
 * `null` and `0` are different statements - "nobody has measured this bin"
 * against "this bin takes nothing" - so an unrecorded capacity returns null for
 * the caller to render as unset, and a recorded zero is printed as the zero it
 * is. Flattening both into one dash is how a capacity constraint disappears
 * from an audit.
 */
export function describeCapacity(location: StorageLocationRow): string | null {
  if (location.capacity_quantity === null) return null
  return formatQuantity(location.capacity_quantity, location.capacity_uom ?? undefined)
}

/** A weight or volume ceiling with its unit, or `null` when none is recorded. */
export function describeLimit(value: string | null, unit: string): string | null {
  return value === null ? null : formatQuantity(value, unit)
}

// ---------------------------------------------------------------- queries ---

/**
 * One storage location by id - `GET /warehouses/locations/{location_id}`,
 * which the router gates on `LOCATION_VIEW`.
 *
 * The screen uses it for the two places an id has to become a name: the parent
 * of the location the user opened, and the parent named by the
 * `parent_location_id` filter. Both read this key, so the pair costs one
 * request.
 *
 * Callers must not ask about the parent of a *retired* location. Retiring is
 * refused while live sub-locations remain (`delete_location` in the warehouses
 * service), so a live row's parent is guaranteed live and therefore visible
 * here - but a retired row's parent may itself be retired, and this endpoint
 * answers 404 for those. The global query cache turns an unexpected 404 into a
 * "Could not load data" toast, which is a poor way to say "the parent went with
 * it"; `enabled` is how that case is kept out.
 */
export function useLocationDetail(locationId: string | null, enabled = true) {
  return useQuery({
    queryKey: locationKeys.detail(locationId ?? ''),
    queryFn: () => api.get<StorageLocationRow>(`/warehouses/locations/${locationId}`),
    enabled: enabled && Boolean(locationId),
    staleTime: staleTimes.master,
  })
}

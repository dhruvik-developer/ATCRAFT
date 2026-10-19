/**
 * Where the demand behind a reservation is managed.
 *
 * One spelling shared by the reservations table, the post-decision alternatives
 * panel and the reallocation dialog: the same demand type must not resolve to
 * two different routes depending on which screen you opened it from.
 */

import type { DemandType } from './types'

export const DEMAND_ROUTES: Record<DemandType, ((id: string) => string) | null> = {
  SALES_ORDER: (id) => '/sales-orders/' + id,
  PRODUCTION_ORDER: (id) => '/production/' + id,
  ASSEMBLY_ORDER: () => '/assembly',
  JOB_WORK_ORDER: () => '/job-work',
  TRANSFER: () => '/inventory/movements',
  SPARE: null,
}

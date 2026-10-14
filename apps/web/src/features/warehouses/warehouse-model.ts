/**
 * Warehouse and storage-location shapes as the UI consumes them, plus the two
 * pickers the create/edit dialog needs.
 *
 * `code` is absent from every update payload on purpose: the API refuses to
 * change it, because it is printed on bin labels and quoted on documents that
 * have already been issued.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString } from '@/types/api'

/** Const tuples so one list drives the type, the options and the zod enum. */
export const WAREHOUSE_TYPES = [
  'RAW_MATERIAL',
  'COMPONENT',
  'WIP',
  'ASSEMBLY',
  'FINISHED_GOODS',
  'PACKING',
  'SCRAP',
  'RETURN',
  'QC_HOLD',
  'JOB_WORK',
  'IN_TRANSIT',
  'CONSUMABLES',
] as const
export type WarehouseType = (typeof WAREHOUSE_TYPES)[number]

export const LOCATION_TYPES = ['RACK', 'BIN', 'SHELF', 'FLOOR', 'STAGING'] as const
export type LocationType = (typeof LOCATION_TYPES)[number]

/** One row of `GET /warehouses`. */
export interface Warehouse {
  id: string
  code: string
  name: string
  warehouse_type: WarehouseType
  branch_id: string | null
  plant_id: string | null
  parent_warehouse_id: string | null
  manager_user_id: string | null

  address_line1: string | null
  address_line2: string | null
  city: string | null
  state: string | null
  state_code: string | null
  postal_code: string | null
  country: string
  phone: string | null
  email: string | null

  is_active: boolean
  is_default: boolean
  allow_negative_stock: boolean
  notes: string | null

  /** Live storage locations, batched by the API for the whole page. */
  location_count: number

  created_at: string
  updated_at: string
  deleted_at: string | null
}

/** One row of `GET /warehouses/{id}/locations`. */
export interface StorageLocation {
  id: string
  warehouse_id: string
  code: string
  name: string
  location_type: LocationType
  parent_location_id: string | null
  is_active: boolean
  capacity_quantity: DecimalString | null
  capacity_uom: string | null
  max_weight_kg: DecimalString | null
  max_volume_m3: DecimalString | null
  barcode: string | null
  notes: string | null
  created_at: string
  updated_at: string
}

export interface WarehouseOption {
  id: string
  code: string
  name: string
  warehouse_type: WarehouseType
  is_default: boolean
}

/** Active warehouses for the parent picker. */
export function useWarehouseOptions(enabled: boolean) {
  return useQuery({
    queryKey: [...queryKeys.warehouses.all, 'options'],
    queryFn: () => api.get<WarehouseOption[]>('/warehouses/options'),
    staleTime: staleTimes.reference,
    enabled,
  })
}

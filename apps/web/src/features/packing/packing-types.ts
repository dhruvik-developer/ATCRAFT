/**
 * Wire shapes for packing orders, cartons and the packing-list job.
 *
 * Weights and quantities are decimal strings (docs/api.md, "Numbers"). A
 * shipment weight that is a hundredth out is a freight bill that does not
 * reconcile, so nothing here converts one to a JS number for arithmetic.
 *
 * Three shapes of this module drive how the screens are built:
 *
 * * Cartons are not part of the order payload. `GET /packing/{id}` returns the
 *   header and its lines; the cartons are their own paged endpoint.
 * * A carton's contents are replaced, never appended to. `PUT .../contents`
 *   takes the whole set and a reason, and gives the previous set back to the
 *   order's lines inside one transaction.
 * * A carton is a physical box that has to be paid for: sealing it issues the
 *   packing material from stock, which is why `packing_material_item_id` is
 *   required when the box is opened.
 */

import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * Packing has no entry in the shared registry yet.
 *
 * The keys are still rooted at `packing` and shaped like the ones in
 * lib/query-client, so they behave identically: invalidating `packingKeys.all`
 * sweeps every list, detail and job watcher at once.
 */
export const packingKeys = {
  all: ['packing'] as const,
  list: (params: unknown) => ['packing', 'list', params] as const,
  detail: (id: string) => ['packing', 'detail', id] as const,
  cartons: (id: string) => ['packing', 'cartons', id] as const,
  cartonLabel: (orderId: string, cartonId: string) =>
    ['packing', 'cartons', orderId, cartonId, 'label'] as const,
  cartonScan: (barcode: string) => ['packing', 'carton-scan', barcode] as const,
  materials: (warehouseId: string, search = '') =>
    ['packing', 'materials', warehouseId, search] as const,
  packingListJob: (jobId: string) => ['packing', 'packing-list-job', jobId] as const,
}

// ------------------------------------------------------------ vocabulary ----

export const PACKING_STATUSES = [
  'DRAFT',
  'IN_PROGRESS',
  'PACKED',
  'HANDED_OVER',
  'CLOSED',
  'CANCELLED',
] as const
export type PackingStatus = (typeof PACKING_STATUSES)[number]

export const CARTON_TYPES = [
  'BOX',
  'CARTON',
  'CRATE',
  'PALLET',
  'BAG',
  'DRUM',
  'SHRINK_WRAP',
] as const
export type CartonType = (typeof CARTON_TYPES)[number]

/**
 * Cartons may only be changed while the order is IN_PROGRESS.
 *
 * Not DRAFT: `CARTON_EDITABLE_STATUSES` in the packing module is exactly
 * `(IN_PROGRESS,)`, so a draft order has to be started before a box is opened.
 */
export const isPackable = (status: string): boolean => status === 'IN_PROGRESS'

export const isStartable = (status: string): boolean => status === 'DRAFT'
export const isCompletable = (status: string): boolean => status === 'IN_PROGRESS'
export const isHandOverable = (status: string): boolean => status === 'PACKED'
export const isCloseable = (status: string): boolean => status === 'HANDED_OVER'
export const isPackingCancellable = (status: string): boolean =>
  status === 'DRAFT' || status === 'IN_PROGRESS'

// --------------------------------------------------------------- packing ----

/** One row of `GET /packing` (`PackingOrderSummary`). */
export interface PackingOrderRow {
  id: string
  code: string
  sales_order_id: string | null
  customer_id: string
  warehouse_id: string
  status: PackingStatus
  planned_date: string
  packed_date: string | null
  packed_by_id: string | null
  total_cartons: number
  total_gross_weight: DecimalString
  total_net_weight: DecimalString
  dispatch_id: string | null
  handed_over_at: string | null
  notes: string | null
  version: number
}

/** One line of `GET /packing/{id}` (`PackingOrderLineRead`). */
export interface PackingOrderLine {
  id: string
  packing_order_id: string
  line_number: number
  sales_order_line_id: string | null
  item_id: string
  quantity_to_pack: DecimalString
  packed_quantity: DecimalString
  uom_id: string
}

/** `GET /packing/{id}` (`PackingOrderDetail`). Cartons are read separately. */
export interface PackingOrderDetail extends PackingOrderRow {
  lines: PackingOrderLine[]
}

/** One content row of a carton (`CartonContentRead`). */
export interface CartonContent {
  id: string
  carton_id: string
  packing_order_line_id: string
  item_id: string
  quantity: DecimalString
  net_weight_kg: DecimalString
  batch_id: string | null
  is_serial_tracked_item: boolean
  /** One per unit; the units themselves, not their printed numbers. */
  serial_ids: string[]
}

/** One row of `GET /packing/{id}/cartons` (`CartonDetail`). */
export interface Carton {
  id: string
  packing_order_id: string
  carton_number: number
  carton_type: CartonType
  barcode: string
  length_cm: DecimalString | null
  width_cm: DecimalString | null
  height_cm: DecimalString | null
  tare_weight_kg: DecimalString
  net_weight_kg: DecimalString
  gross_weight_kg: DecimalString
  packing_material_item_id: string
  packing_material_quantity: DecimalString
  material_transaction_id: string | null
  is_sealed: boolean
  sealed_at: string | null
  contents: CartonContent[]
}

/** One content row of `CartonCreate` / `CartonContentsReplace` (`CartonContentIn`). */
export interface CartonContentIn {
  /** The packing order's line number, not the line's id. */
  line_number: number
  quantity: DecimalString
  batch_id?: string | null
  serial_ids: string[]
  net_weight_kg: DecimalString
}

/** One row of `GET /packing/packing-materials` (`PackingMaterialRow`). */
export interface PackingMaterialRow {
  item_id: string
  code: string
  name: string
  base_uom_id: string
  available_quantity: DecimalString
}

/** `POST /packing/{id}/cartons/{carton_id}/seal` (`SealResult`). */
export interface SealResult {
  carton: Carton
  material_transaction_id: string
  packing_material_item_id: string
  packing_material_quantity: DecimalString
}

/** `POST /packing/{id}/complete` (`PackingCompletionResult`). */
export interface PackingCompletionResult {
  order: PackingOrderDetail
  total_cartons: number
  total_gross_weight: DecimalString
  total_net_weight: DecimalString
  short_packed_line_numbers: number[]
}

/** `GET /packing/{id}/cartons/{carton_id}/label` (`CartonLabel`). */
export type CartonLabel = SchemaOf<'CartonLabel'>

/** `POST /packing/scan` (`CartonScanRequest`). */
export type CartonScanRequest = SchemaOf<'CartonScanRequest'>

/** `GET` or `POST /packing/scan` (`CartonScanResult`). */
export type CartonScanResult = SchemaOf<'CartonScanResult'>

// ------------------------------------------------------------------ job -----

/**
 * `GET /reports/jobs/{job_id}` (`ReportJobRead`).
 *
 * Rendering a packing list is a PDF build, which is exactly the kind of work
 * docs/api.md says never holds a connection open: `GET /packing/{id}/packing-list`
 * returns 202 with a job id, and the finished artefact arrives as a document to
 * download.
 */
export interface ReportJob {
  id: string
  report_key: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  row_count: number
  document_id: string | null
  download_url: string | null
  error_message: string | null
  generated_at: string | null
  created_at: string
}

export function isJobInFlight(status: string | null | undefined): boolean {
  return status === 'QUEUED' || status === 'RUNNING'
}

export function isJobFailed(status: string | null | undefined): boolean {
  return status === 'FAILED'
}

export function isJobComplete(status: string | null | undefined): boolean {
  return status === 'COMPLETED'
}

/** Quantities on the carton content form. Empty is treated as zero. */
export const QUANTITY_INPUT = /^$|^\d{1,14}(\.\d{1,4})?$/

/** Weights and dimensions, to three places. */
export const MEASURE_INPUT = /^$|^\d{1,10}(\.\d{1,3})?$/

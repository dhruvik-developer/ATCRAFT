/**
 * Wire shapes and vocabulary for the vendor master.
 *
 * Mirrors `src/modules/vendors/schemas.py`. Declared in the feature folder
 * rather than in `types/api.ts` because these are the payloads of one module,
 * not part of the transport contract every screen shares.
 *
 * Money arrives as a decimal string and is never turned into a JS number for
 * arithmetic (docs/api.md, "Numbers").
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

/**
 * A vendor holds several of these at once - the same firm may supply raw
 * material and also do job work - so the list screen renders a badge per type
 * rather than a single "type" column.
 */
export const VENDOR_TYPES = [
  'RAW_MATERIAL_SUPPLIER',
  'COMPONENT_SUPPLIER',
  'PACKING_MATERIAL_SUPPLIER',
  'JOB_WORK',
  'TRANSPORT',
  'SERVICE',
] as const
export type VendorType = (typeof VENDOR_TYPES)[number]

/** Portal role of a named contact at the vendor. */
export const VENDOR_CONTACT_ROLES = ['OWNER', 'MANAGER', 'DISPATCH', 'QC', 'ACCOUNTS'] as const
export type VendorContactRole = (typeof VENDOR_CONTACT_ROLES)[number]

/** The 0-5 scale the API's rating engine and its check constraint agree on. */
export const MAX_VENDOR_RATING = 5

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /vendors`, and the payload of `GET /vendors/{id}`. */
export interface VendorRow {
  id: string
  code: string
  name: string
  legal_name: string | null
  vendor_types: string[]

  gstin: string | null
  pan: string | null
  msme_number: string | null

  payment_terms_days: number
  credit_limit: DecimalString | null
  lead_time_days: number
  currency: string

  /** Null means never rated. An unrated vendor is unknown, not bad. */
  rating: DecimalString | null
  rating_updated_at: string | null
  rating_sample_size: number

  is_approved: boolean
  approved_at: string | null
  approved_by_id: string | null
  is_active: boolean

  bank_name: string | null
  bank_branch: string | null
  bank_account_name: string | null
  /** The API returns only the last four digits to every screen but payments. */
  bank_account_number_masked: string | null
  bank_ifsc: string | null
  bank_swift: string | null

  address_line1: string | null
  address_line2: string | null
  city: string | null
  state: string | null
  state_code: string | null
  postal_code: string | null
  country: string

  contact_person: string | null
  email: string | null
  phone: string | null
  mobile: string | null
  website: string | null
  notes: string | null

  contact_count: number
  category_count: number

  created_at: string
  updated_at: string
  deleted_at: string | null
}

/** Slim projection from `GET /vendors/options`, used by the pickers. */
export interface VendorOption {
  id: string
  code: string
  name: string
  vendor_types: string[]
  is_approved: boolean
  rating: DecimalString | null
  lead_time_days: number
}

/** One row of `GET /vendors/{id}/contacts`. */
export interface VendorContact {
  id: string
  vendor_id: string
  name: string
  designation: string | null
  contact_role: VendorContactRole
  email: string | null
  phone: string | null
  mobile: string | null
  is_primary: boolean
  is_active: boolean
  notes: string | null
}

/** One row of `GET /vendors/{id}/categories`. */
export interface VendorCategoryLink {
  id: string
  vendor_id: string
  item_category_id: string
  item_category_code: string | null
  item_category_name: string | null
  is_preferred: boolean
  notes: string | null
}

/**
 * The answer to both `GET /vendors/{id}/rating` (compute, store nothing) and
 * `POST /vendors/{id}/rating` (compute and store).
 *
 * Derived from the contract rather than mirrored by hand, unlike the rest of
 * this file: it is the one payload the screen both reads and writes, and a
 * retyped copy of a shape you write to is a second opinion `tsc` cannot check
 * against the server (`types/api.ts`). `applied` is the difference between the
 * two verbs - a preview always answers false.
 */
export type VendorRating = SchemaOf<'VendorRatingRead'>

/**
 * The entire body of `POST /vendors/{id}/rating`: a measurement window, and
 * nothing else.
 *
 * There is no on-time figure and no rejection figure to send, deliberately -
 * the API reads both off goods receipts and QC inspections, because a buyer who
 * can type their own vendor's score is a sourcing control that does not work.
 * So the recording dialog has exactly one input, and every dimension of the
 * evaluation is read back off `VendorRating` instead of being entered.
 */
export type VendorRatingRequest = SchemaOf<'VendorRatingRequest'>

/**
 * Bounds of `window_days`, from the contract's own schema (minimum 7, maximum
 * 1095, default 180). Outside them the request is a 422 before it reaches the
 * rating engine, so the picker never offers a window the API will not measure.
 */
export const RATING_WINDOW_MIN_DAYS = 7
export const RATING_WINDOW_MAX_DAYS = 1095
export const DEFAULT_RATING_WINDOW_DAYS = 180

/**
 * The windows worth offering.
 *
 * A free-text number of days would be a validation problem for no gain: what a
 * buyer actually wants to ask is "how has this vendor done recently" versus
 * "how has it done over the whole relationship", and the answer changes with
 * the window rather than with the exact day count. Every entry sits inside the
 * contract's bounds above.
 */
export const RATING_WINDOW_CHOICES: readonly { days: number; label: string }[] = [
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last quarter (90 days)' },
  { days: DEFAULT_RATING_WINDOW_DAYS, label: 'Last half year (180 days)' },
  { days: 365, label: 'Last year (365 days)' },
  { days: 730, label: 'Last two years (730 days)' },
  { days: RATING_WINDOW_MAX_DAYS, label: 'Whole window (1095 days)' },
]

/**
 * How the two measured dimensions combine into the 0-5 score, as percentages.
 *
 * Mirrors `DELIVERY_WEIGHT` and `QUALITY_WEIGHT` in
 * `src/modules/vendors/service.py`. Explanatory copy only - the client computes
 * no part of a rating - but a score presented without saying what it weighs is
 * a number nobody can argue with, and this one decides who gets an order.
 */
export const RATING_WEIGHTS = { delivery: 60, quality: 40 } as const

// ------------------------------------------------------------- lifecycle ----

/**
 * `VendorState` in the service, derived rather than stored.
 *
 * There is no status column. `vendor_state()` reads `deleted_at`, then
 * `is_active`, then `is_approved`, and the precedence is the point: a retired
 * row that still carries `is_approved` from before its retirement must never
 * read as usable.
 */
export type VendorState = 'PENDING_APPROVAL' | 'APPROVED' | 'SUSPENDED' | 'ARCHIVED'

export function vendorState(
  vendor: Pick<VendorRow, 'deleted_at' | 'is_active' | 'is_approved'>,
): VendorState {
  if (vendor.deleted_at) return 'ARCHIVED'
  if (!vendor.is_active) return 'SUSPENDED'
  return vendor.is_approved ? 'APPROVED' : 'PENDING_APPROVAL'
}

/**
 * Retirement is reachable from every live state, and from none other.
 *
 * `VENDOR_LIFECYCLE` carries a Retire edge out of PENDING_APPROVAL, APPROVED
 * and SUSPENDED alike - so suspending first is not a precondition, and offering
 * "Retire" beside "Suspend" is not a trap. Out of ARCHIVED there is no edge at
 * all: `DELETE /vendors/{id}` resolves the row with `get_or_404`, which
 * excludes retired rows, so a second retirement is a 404 rather than a refusal
 * anyone can act on.
 *
 * The list only asks for live rows today, which makes this a guard rather than
 * a filter - but `GET /vendors` takes `include_deleted`, so it need not stay
 * that way, and a status comparison inlined in JSX would not survive that.
 */
export const acceptsRetirement = (state: VendorState): boolean => state !== 'ARCHIVED'

/**
 * Rating a retired vendor is refused for the same reason, one layer down:
 * `recompute_rating` resolves the vendor with `get_or_404` before it measures
 * anything. Suspended and unapproved vendors *are* ratable, and deliberately so
 * - the delivery history that would justify reapproving one is exactly what the
 * rating summarises.
 */
export const acceptsRating = (state: VendorState): boolean => state !== 'ARCHIVED'

// --------------------------------------------------------------- lookups ----

/**
 * Vendor names for the procurement grids.
 *
 * `PurchaseOrderSummary` and `GoodsReceiptSummary` carry `vendor_id` and
 * nothing else, so every screen that lists a document has to name the supplier
 * itself. `GET /vendors/options` is the projection the API publishes for
 * exactly this - bounded server-side at 500 rows and index-served - so it is
 * one cached reference query per session, not a download of the vendor master.
 *
 * `approvedOnly` is false by default here: an order raised months ago on a
 * vendor whose approval has since been withdrawn still has to show a name.
 * A vendor that has been suspended outright is not in the list at all, and the
 * caller renders a dash rather than a UUID.
 *
 * A failure resolves to an empty list rather than propagating: a missing
 * supplier name is decoration on an order list, and should not replace a
 * working screen with an error state.
 */
export function useVendorOptions(enabled = true, approvedOnly = false) {
  return useQuery({
    queryKey: [...queryKeys.vendors.all, 'options', approvedOnly] as const,
    queryFn: async (): Promise<VendorOption[]> => {
      try {
        return await api.get<VendorOption[]>('/vendors/options', {
          params: { approved_only: approvedOnly },
        })
      } catch {
        return []
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

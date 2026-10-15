/**
 * Wire shapes and vocabulary for quality control.
 *
 * The wire shapes are DERIVED from the generated contract rather than retyped.
 * A hand-written interface compiles perfectly while disagreeing with the API,
 * which is precisely how a screen comes to render nothing; deriving turns that
 * disagreement into a compile error. Only the client-side vocabulary and the
 * form helpers below are genuinely local.
 *
 * Quantities are decimal strings end to end (docs/api.md, "Numbers"). Nothing
 * here turns one into a JS number - a measured value of 0.1 + 0.2 that reads as
 * 0.30000000000000004 against a 0.30 upper limit would reject good material.
 *
 * Three properties of the current contract shape everything above it:
 *
 * 1. **Inspection rows are not denormalised.** They carry `item_id`,
 *    `vendor_id`, `warehouse_id` and `batch_id` - no codes, no names, no UoM.
 *    The screens therefore show what the row actually contains and link out by
 *    id rather than rendering blanks.
 * 2. **The verdict is the server's, not the form's.** `POST .../decide` takes
 *    the five quantities and a reason; it does not take a result. The API
 *    derives the result from how the quantity was split, and returns it along
 *    with what it moved in stock.
 * 3. **`POST .../results` will not accept an empty measurement set.**
 *    `RecordResultsRequest.measurements` is `minItems: 1`, so an inspection
 *    raised without a template has no readings to record and must go straight
 *    to the decision. See `inspection-dialog.tsx`.
 */

import { toDecimal } from '@/lib/format'
import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

/** Where in the flow the inspection sits (`QcStage`). */
export type QcStage = SchemaOf<'QcStage'>

/** Display order for the stage filter. Typed against the contract's enum. */
export const QC_STAGES: readonly QcStage[] = [
  'INCOMING',
  'JOB_WORK',
  'IN_PROCESS',
  'ASSEMBLY',
  'FINAL',
  'RETURN',
]

/**
 * The verdict (`InspectionResult`).
 *
 * `PENDING` is a member of the wire enum - an undecided inspection carries it
 * rather than a null - so it is listed here, and excluded from the filter's
 * decided-outcome choices separately.
 */
export type QcResult = SchemaOf<'InspectionResult'>

export const QC_RESULTS: readonly QcResult[] = [
  'PENDING',
  'ACCEPTED',
  'REWORK',
  'REJECTED',
  'HOLD',
  'SCRAP',
]

/** The outcomes a completed inspection can carry, for the result filter. */
export const QC_DECIDED_RESULTS: readonly Exclude<QcResult, 'PENDING'>[] = QC_RESULTS.filter(
  (value): value is Exclude<QcResult, 'PENDING'> => value !== 'PENDING',
)

export type QcInspectionStatus = SchemaOf<'InspectionStatus'>

export const QC_INSPECTION_STATUSES: readonly QcInspectionStatus[] = [
  'PENDING',
  'IN_PROGRESS',
  'ACCEPTED',
  'REWORK',
  'REJECTED',
  'HOLD',
  'SCRAP',
  'CLOSED',
]

export type QcParameterType = SchemaOf<'ParameterType'>

export const QC_PARAMETER_TYPES = ['NUMERIC', 'BOOLEAN', 'TEXT', 'SELECT'] as const

/**
 * An inspection the API will still accept a decision on.
 *
 * Not merely the undecided ones. `decide_inspection` gates on
 * `(*OPEN_STATUSES, *REOPENABLE_STATUSES)` (apps/api/src/modules/quality),
 * where OPEN is `PENDING | IN_PROGRESS` and REOPENABLE is `REWORK | HOLD` -
 * "decided statuses whose quantity is still sitting in the QC hold pool, and
 * can therefore still be re-decided out of it".
 *
 * Listing only the open two hid the action on exactly the inspections that
 * most need it: a lot put on hold or sent for rework is stock sitting in the
 * QC pool waiting for somebody to settle it, and the UI offered no way to.
 */
export const isInspectable = (status: string): boolean =>
  status === 'PENDING' || status === 'IN_PROGRESS' || status === 'REWORK' || status === 'HOLD'

/**
 * `DecideRequest.rejection_reason` on `POST .../decide`: 3..2000 characters,
 * wider than the audit `reason` beside it, which carries the shared
 * `REASON_MIN_LENGTH`..`REASON_MAX_LENGTH`. Anything outside the band is a
 * 422 against a field the dialog does render, so it is caught before it is
 * sent.
 */
export const REJECTION_REASON_MAX_LENGTH = 2000

// ------------------------------------------------------------ parameters ----

/** One row of `qc_template_parameters` (`TemplateParameterRead`). */
export type QcTemplateParameter = SchemaOf<'TemplateParameterRead'>

/** A recorded measurement (`InspectionResultRead`). */
export type QcResultValue = SchemaOf<'InspectionResultRead'>

/**
 * The server's reading of the measurements so far (`ParameterEvaluationRead`).
 *
 * This is authoritative: the dialog gives immediate feedback on a numeric that
 * leaves its band, but whether the lot is condemned is the API's call.
 */
export type QcParameterEvaluation = SchemaOf<'ParameterEvaluationRead'>

/** One measurement as `POST .../results` wants it (`MeasurementIn`). */
export type QcMeasurementIn = SchemaOf<'MeasurementIn'>

// ----------------------------------------------------------- inspections ----

/**
 * One row of `GET /quality/pending` and `GET /quality/inspections`
 * (`InspectionSummary`).
 */
export type QcInspectionRow = SchemaOf<'InspectionSummary'>

/** `GET /quality/inspections/{id}` (`InspectionDetail`). */
export type QcInspectionDetail = SchemaOf<'InspectionDetail'>

/** One verdict in the decision history (`DecisionHistoryRead`). */
export type QcDecisionHistory = SchemaOf<'DecisionHistoryRead'>

/** One rework cycle (`ReworkRecordRead`). */
export type QcReworkRecord = SchemaOf<'ReworkRecordRead'>

/** `ReworkStatus` on the wire. */
export type QcReworkStatus = SchemaOf<'ReworkStatus'>

export const QC_REWORK_STATUSES = ['OPEN', 'IN_PROGRESS', 'DONE'] as const

/** `ReworkOutcome` on the wire. */
export type QcReworkOutcome = SchemaOf<'ReworkOutcome'>

export const QC_REWORK_OUTCOMES = ['REWORKED_OK', 'SCRAPPED', 'RETURNED'] as const

/** `POST /quality/inspections/{id}/results` (`RecordResultsResponse`). */
export type QcResultsResponse = SchemaOf<'RecordResultsResponse'>

/**
 * `POST /quality/inspections/{id}/decide` (`DecisionResponse`).
 *
 * The quantity fields are what the decision actually moved in stock, which is
 * not the same as what was typed into the form: accepted quantity is released,
 * scrap is written off, and rejected material may be held pending a return.
 */
export type QcDecisionResponse = SchemaOf<'DecisionResponse'>

/** `POST /documents` (`DocumentRead`), for an inspection photo. */
export type UploadedDocument = SchemaOf<'DocumentRead'>

// --------------------------------------------------------------- reports ----

/** One period of `GET /quality/reports/item/{item_id}` (`QualityPeriodRow`). */
export type ItemQualityPoint = SchemaOf<'QualityPeriodRow'>

/** `GET /quality/reports/item/{item_id}` (`ItemQualityHistory`). */
export type ItemQualityHistory = SchemaOf<'ItemQualityHistory'>

/** One item line of a vendor's record (`VendorQualityItemRow`). */
export type VendorQualityItemRow = SchemaOf<'VendorQualityItemRow'>

/** `GET /quality/reports/vendor/{vendor_id}` (`VendorQualitySummary`). */
export type VendorQualitySummary = SchemaOf<'VendorQualitySummary'>

// --------------------------------------------------------------- helpers ----

/**
 * Where the document that raised this inspection lives.
 *
 * An inspector who cannot see the receipt behind a queue entry has to trust the
 * code on the label, which is exactly how a rejection gets booked against the
 * wrong consignment. Receipts and job-work orders have list screens rather than
 * detail routes, so those are opened pre-searched on the code.
 */
export function referenceLink(
  referenceType: string | null,
  referenceId: string | null,
  referenceCode: string | null,
): string | null {
  const code = referenceCode ? encodeURIComponent(referenceCode) : null
  switch (referenceType) {
    case 'goods_receipt':
      return code ? `/procurement/goods-receipts?q=${code}` : '/procurement/goods-receipts'
    case 'purchase_order':
      return referenceId ? `/procurement/purchase-orders/${referenceId}` : null
    case 'production_order':
    case 'production_entry':
      return referenceId ? `/production/${referenceId}` : null
    case 'work_order':
      return '/production/work-orders'
    case 'packing_order':
      return '/packing'
    case 'assembly_order':
      return code ? `/assembly?q=${code}` : '/assembly'
    case 'job_work_order':
    case 'job_work_receipt':
      return code ? `/job-work?q=${code}` : '/job-work'
    case 'dispatch':
      return code ? `/dispatch?q=${code}` : '/dispatch'
    default:
      return null
  }
}

/** Whole days the material has been waiting for a decision. */
export function ageInDays(raisedAt: string | null): number | null {
  if (!raisedAt) return null
  const raised = new Date(raisedAt)
  if (Number.isNaN(raised.getTime())) return null
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  return Math.max(0, Math.round((startOfDay(new Date()) - startOfDay(raised)) / 86_400_000))
}

/** Human-readable specification, e.g. `12.5 - 13.5 mm` or `<= 0.2 mm`. */
export function specLabel(parameter: {
  parameter_type: string
  min_value: string | null
  max_value: string | null
  unit: string | null
}): string | null {
  if (parameter.parameter_type !== 'NUMERIC') return null
  const suffix = parameter.unit ? ` ${parameter.unit}` : ''
  if (parameter.min_value !== null && parameter.max_value !== null) {
    return `${parameter.min_value} - ${parameter.max_value}${suffix}`
  }
  if (parameter.min_value !== null) return `>= ${parameter.min_value}${suffix}`
  if (parameter.max_value !== null) return `<= ${parameter.max_value}${suffix}`
  return null
}

/**
 * Whether a recorded reading breaches the specification.
 *
 * Immediate in-form feedback only. The binding judgement is the API's
 * `ParameterEvaluationRead`, which this deliberately does not try to reproduce.
 *
 * Compared with decimal.js rather than `Number`: a 0.1 tolerance band evaluated
 * in binary floating point rejects readings that are exactly on the limit.
 */
export function isOutOfSpec(
  parameter: { parameter_type: string; min_value: string | null; max_value: string | null },
  value: string,
): boolean {
  if (parameter.parameter_type !== 'NUMERIC') return false
  const trimmed = value.trim()
  if (trimmed === '' || !NUMERIC_READING.test(trimmed)) return false
  const reading = toDecimal(trimmed)
  if (parameter.min_value !== null && reading.lessThan(toDecimal(parameter.min_value))) return true
  if (parameter.max_value !== null && reading.greaterThan(toDecimal(parameter.max_value)))
    return true
  return false
}

/** Readings may be negative (a deviation) and are recorded to six places. */
export const NUMERIC_READING = /^-?\d{1,14}(\.\d{1,6})?$/

/** Quantities on the disposition. Empty is treated as zero by the form. */
export const QUANTITY_INPUT = /^$|^\d{1,14}(\.\d{1,4})?$/

/**
 * A recorded answer that looks like a failure.
 *
 * Only NUMERIC and BOOLEAN can be judged here. A SELECT parameter carries its
 * permitted answers but no marking of which of them count as a failure - that
 * classification lives in the API's evaluation - so a valid choice is never
 * flagged locally.
 */
export function hasFailed(
  parameter: { parameter_type: string; min_value: string | null; max_value: string | null },
  value: string,
): boolean {
  const trimmed = value.trim()
  if (trimmed === '') return false
  if (parameter.parameter_type === 'BOOLEAN') return trimmed === 'FAIL'
  if (parameter.parameter_type === 'SELECT') return false
  return isOutOfSpec(parameter, trimmed)
}

/**
 * Wire shapes and vocabulary for QC inspection specifications - "templates".
 *
 * A template is the check sheet an inspection is judged against, and it is
 * versioned for a reason: `qc_inspections.template_id` names the exact row that
 * was in force, so a past pass or fail stays reproducible. That makes editing a
 * template and cutting a new version two different acts:
 *
 * * **Amending** (`PATCH`) rewrites the specification in place. The API allows
 *   it only while no inspection has applied that version - afterwards the edit
 *   would restate what those inspections were measured against, and
 *   `service.update_template` refuses with `REFERENCED_RECORD_IN_USE`.
 * * **Versioning** (`POST .../versions`) writes a *new row* carrying the same
 *   `code` and `version + 1`, copies the check sheet unless a replacement is
 *   sent, and retires the row it forked from. Nothing already recorded moves.
 *
 * Two consequences shape every screen above this file:
 *
 * 1. **A version is a row, not a sub-resource.** Versions of one specification
 *    are separate rows of `GET /quality/templates` sharing a `code`. There is
 *    no versions endpoint and no `code` filter - only a `search` that matches
 *    code or name - so the history is assembled as documented on
 *    `qcTemplateKeys.versions`.
 * 2. **`TemplateSummary` carries no check sheet and no description.** The list
 *    row states scope, stage, sampling and version; the characteristics only
 *    arrive with `GET /quality/templates/{id}` (`TemplateDetail`), which is why
 *    the list opens a detail rather than pretending a template is a name and a
 *    date.
 *
 * Everything on the wire is DERIVED from the generated contract rather than
 * retyped. A hand-written interface compiles perfectly while disagreeing with
 * the API, which is precisely how a screen comes to render nothing.
 */

import { formatNumber, formatQuantity, toDecimal } from '@/lib/format'
import type { DecimalString, SchemaOf } from '@/types/api'

import type { QcParameterType, QcStage, QcTemplateParameter } from './quality-types'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has a `quality` branch for
 * inspections but none for specifications, so the keys live here in the same
 * hierarchical shape: invalidating `qcTemplateKeys.all` clears every template
 * list, detail and version history at once.
 */
export const qcTemplateKeys = {
  all: ['qc-templates'] as const,
  list: (params: unknown) => ['qc-templates', 'list', params] as const,
  detail: (id: string) => ['qc-templates', 'detail', id] as const,
  /**
   * Every revision of one code.
   *
   * Keyed by `code` rather than by id because the whole point is that the
   * revisions are different rows: forking v3 into v4 must invalidate the
   * history v3's dialog is showing, and only the code is common to both.
   */
  versions: (code: string) => ['qc-templates', 'versions', code] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /quality/templates` (`TemplateSummary`). */
export type QcTemplateRow = SchemaOf<'TemplateSummary'>

/** `GET /quality/templates/{id}` (`TemplateDetail`) - the summary plus the sheet. */
export type QcTemplateDetail = SchemaOf<'TemplateDetail'>

/** `POST /quality/templates` (`TemplateCreate`). */
export type QcTemplateCreate = SchemaOf<'TemplateCreate'>

/** `PATCH /quality/templates/{id}` (`TemplateUpdate`). */
export type QcTemplateUpdate = SchemaOf<'TemplateUpdate'>

/** `POST /quality/templates/{id}/versions` (`TemplateVersionRequest`). */
export type QcTemplateVersionRequest = SchemaOf<'TemplateVersionRequest'>

/** One check-sheet row as the write endpoints want it (`TemplateParameterIn`). */
export type QcTemplateParameterIn = SchemaOf<'TemplateParameterIn'>

// ------------------------------------------------------------ vocabulary ----

/** How much of a lot is actually measured (`SamplingPlan`). */
export type QcSamplingPlan = SchemaOf<'SamplingPlan'>

/**
 * Display order for the plan filter and the form.
 *
 * `satisfies` rather than a plain annotation, so the literal members survive
 * for the form's enum while a plan renamed in the contract still fails to
 * compile here.
 */
export const QC_SAMPLING_PLANS = [
  'ALL',
  'PERCENTAGE',
  'FIXED_SAMPLE',
  'AQL',
] as const satisfies readonly QcSamplingPlan[]

/**
 * What each plan means, transcribed from `SamplingPlan` in
 * apps/api/src/modules/quality/models.py.
 *
 * Shown next to the choice because the difference between a percentage and a
 * statistical plan is not guessable from four words of enum, and choosing the
 * wrong one silently changes how much of every future lot gets measured.
 */
export const SAMPLING_PLAN_HINT: Record<QcSamplingPlan, string> = {
  ALL: 'Every piece in the lot is measured.',
  PERCENTAGE: 'A proportion of the lot, rounded up, never below one piece.',
  FIXED_SAMPLE: 'A constant number of pieces whatever the lot size.',
  AQL: 'ISO 2859-1 general inspection level II. The sample grows with the lot, far more slowly than a percentage.',
}

/**
 * How the plans read on screen.
 *
 * `humanise` would render `AQL` as "Aql", which is a standard nobody would
 * recognise spelled that way.
 */
export const SAMPLING_PLAN_LABEL: Record<QcSamplingPlan, string> = {
  ALL: 'Every piece',
  PERCENTAGE: 'Percentage of the lot',
  FIXED_SAMPLE: 'Fixed sample',
  AQL: 'AQL (ISO 2859-1)',
}

/**
 * How each characteristic type reads on screen.
 *
 * The contract's names are what the evaluator branches on, not what an
 * inspector recognises at a bench: "SELECT" names a control rather than a
 * characteristic, and nobody describes a go/no-go gauge check as "BOOLEAN".
 */
export const PARAMETER_TYPE_LABEL: Record<QcParameterType, string> = {
  NUMERIC: 'Measured value',
  BOOLEAN: 'Pass or fail',
  TEXT: 'Free text',
  SELECT: 'Chosen value',
}

/** Which plans carry a figure, and which one. */
export const planNeedsPercentage = (plan: QcSamplingPlan): boolean => plan === 'PERCENTAGE'
export const planNeedsSampleSize = (plan: QcSamplingPlan): boolean => plan === 'FIXED_SAMPLE'

/**
 * Where a template applies, narrowest first.
 *
 * `item_id` and `item_category_id` are mutually exclusive on the wire, and both
 * null means the stage-wide default. At most one *active* template may claim
 * each of these, which is what makes resolving the specification for an
 * incoming lot deterministic - and why activating a second one is refused.
 */
export type QcTemplateScope = 'ITEM' | 'CATEGORY' | 'STAGE'

export function scopeOf(template: {
  item_id: string | null
  item_category_id: string | null
}): QcTemplateScope {
  if (template.item_id) return 'ITEM'
  if (template.item_category_id) return 'CATEGORY'
  return 'STAGE'
}

export const SCOPE_LABEL: Record<QcTemplateScope, string> = {
  ITEM: 'One item',
  CATEGORY: 'An item category',
  STAGE: 'Stage default',
}

/**
 * The sampling plan as a sentence, with its figure.
 *
 * The percentage goes through `formatQuantity` rather than `formatPercent`:
 * the column is `NUMERIC(9,4)` and a plan means exactly the number it states,
 * so a 12.75% plan must not be rounded to 12.8% and a 5% plan must not be
 * padded to 5.00%. `formatQuantity` trims trailing zeros without rounding.
 */
export function samplingLabel(template: {
  sampling_plan: QcSamplingPlan
  sample_percentage: DecimalString | null
  sample_size: number | null
}): string {
  switch (template.sampling_plan) {
    case 'PERCENTAGE':
      return template.sample_percentage === null
        ? 'Percentage of the lot'
        : `${formatQuantity(template.sample_percentage)}% of the lot`
    case 'FIXED_SAMPLE':
      return template.sample_size === null
        ? 'Fixed sample'
        : `${formatNumber(template.sample_size)} pieces per lot`
    case 'AQL':
      return 'AQL (ISO 2859-1, level II)'
    case 'ALL':
      return 'Every piece'
  }
}

/** `QC-014 v3` - how a specification is named wherever it is referred to. */
export const templateTitle = (template: { code: string; version: number }): string =>
  `${template.code} v${template.version}`

// ------------------------------------------------------------- the bounds ---

// Transcribed from the constrained scalars in
// apps/api/src/modules/quality/schemas.py and models.py. They are here so the
// forms refuse a value before it becomes a 422 against a field the user can see.
export const CODE_MIN_LENGTH = 2
export const CODE_MAX_LENGTH = 64
export const NAME_MIN_LENGTH = 2
export const NAME_MAX_LENGTH = 255
export const DESCRIPTION_MAX_LENGTH = 2000
export const PARAMETER_NAME_MAX_LENGTH = 120
export const ALLOWED_VALUE_MAX_LENGTH = 64
export const UNIT_MAX_LENGTH = 24
export const MAX_PARAMETERS_PER_TEMPLATE = 200
export const MAX_SAMPLE_SIZE = 1_000_000

/** `Measurement` is `NUMERIC(18,6)`, and a specification limit may be negative. */
export const MEASUREMENT = /^-?\d{1,12}(\.\d{1,6})?$/

/** `SamplePercentage` is `NUMERIC(9,4)`, bounded 0 < p <= 100. */
export const PERCENTAGE = /^\d{1,3}(\.\d{1,4})?$/

/** `sample_size` is a whole count of pieces, not a decimal. */
export const SAMPLE_SIZE = /^\d{1,7}$/

/** Compared with decimal.js: `Number('0.1') + Number('0.2') > 0.3` is true. */
export function isPercentageInRange(value: string): boolean {
  const percentage = toDecimal(value)
  return percentage.greaterThan(0) && percentage.lessThanOrEqualTo(100)
}

/**
 * A tolerance band the wrong way round.
 *
 * Both limits are optional - a one-sided specification is normal - so this is
 * only a defect when both are present. Compared as decimals because `'9' > '10'`
 * is true for strings, which would reject a perfectly good 9..10 mm band.
 */
export function toleranceIsInverted(min: string, max: string): boolean {
  const lower = min.trim()
  const upper = max.trim()
  if (lower === '' || upper === '') return false
  if (!MEASUREMENT.test(lower) || !MEASUREMENT.test(upper)) return false
  return toDecimal(lower).greaterThan(toDecimal(upper))
}

// ------------------------------------------------------- the check sheet ----

/**
 * One characteristic as the form holds it.
 *
 * Every field is a string or a boolean, because that is what an `<input>`
 * produces and because a measurement must never round-trip through a JS number:
 * a 0.1 tolerance limit read back as 0.30000000000000004 rejects good material.
 * `sequence` is absent deliberately - it is the row's position in the sheet,
 * assigned on submit, so reordering cannot leave two rows claiming the same
 * number (which the API rejects outright).
 */
export interface QcParameterDraft {
  parameter_name: string
  parameter_type: QcParameterType
  unit: string
  min_value: string
  max_value: string
  target_value: string
  /** One permitted answer per line. `allowed_values` is a list on the wire. */
  allowed_values: string
  is_critical: boolean
  is_mandatory: boolean
}

export function emptyParameterDraft(): QcParameterDraft {
  return {
    parameter_name: '',
    parameter_type: 'NUMERIC',
    unit: '',
    min_value: '',
    max_value: '',
    target_value: '',
    allowed_values: '',
    is_critical: false,
    is_mandatory: true,
  }
}

export function draftFromParameter(parameter: QcTemplateParameter): QcParameterDraft {
  return {
    parameter_name: parameter.parameter_name,
    parameter_type: parameter.parameter_type,
    unit: parameter.unit ?? '',
    min_value: parameter.min_value ?? '',
    max_value: parameter.max_value ?? '',
    target_value: parameter.target_value ?? '',
    allowed_values: joinAllowedValues(parameter.allowed_values),
    is_critical: parameter.is_critical,
    is_mandatory: parameter.is_mandatory,
  }
}

/** A newline per value: a comma is a legitimate character in "Blue, dark". */
export function splitAllowedValues(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

export function joinAllowedValues(values: string[] | null): string {
  return values === null ? '' : values.join('\n')
}

/**
 * The single place the type/specification agreement is enforced.
 *
 * `TemplateParameterIn` validates it server-side too, and rejects the whole
 * request: a tolerance band typed against a BOOLEAN, or an allowed-value list
 * on a NUMERIC, is a 422. The form keeps a hidden leftover when the type is
 * switched - so switching back does not mean retyping the band - and this
 * function is what guarantees the leftover never reaches the wire.
 *
 * `is_mandatory` is forced true for a critical characteristic for the same
 * reason the API insists on it: one that may be skipped never rejects anything.
 */
export function draftToWire(draft: QcParameterDraft, sequence: number): QcTemplateParameterIn {
  const isNumeric = draft.parameter_type === 'NUMERIC'
  const hasValueList = draft.parameter_type === 'SELECT' || draft.parameter_type === 'TEXT'
  const values = hasValueList ? splitAllowedValues(draft.allowed_values) : []
  const unit = draft.unit.trim()
  const bound = (value: string): string | null => (value.trim() === '' ? null : value.trim())

  return {
    sequence,
    parameter_name: draft.parameter_name.trim(),
    parameter_type: draft.parameter_type,
    unit: unit === '' ? null : unit,
    min_value: isNumeric ? bound(draft.min_value) : null,
    max_value: isNumeric ? bound(draft.max_value) : null,
    target_value: isNumeric ? bound(draft.target_value) : null,
    allowed_values: values.length > 0 ? values : null,
    is_critical: draft.is_critical,
    is_mandatory: draft.is_critical || draft.is_mandatory,
  }
}

/**
 * Whether the sheet in the form differs from the one that was loaded.
 *
 * Load-bearing on an amend, not a nicety. `TemplateUpdate` decides whether an
 * edit "changes meaning" from the *keys present in the payload*, not from
 * whether their values differ - so sending an untouched `parameters` array
 * turns a rename into a specification change and earns a refusal on any
 * template that has ever been inspected against.
 *
 * Compared through `draftToWire`, so trailing whitespace and a leftover bound
 * hidden by a type switch do not count as a change - they cannot reach the API
 * either.
 */
export function sheetChanged(before: QcParameterDraft[], after: QcParameterDraft[]): boolean {
  const onWire = (rows: QcParameterDraft[]) =>
    JSON.stringify(rows.map((row, index) => draftToWire(row, index + 1)))
  return onWire(before) !== onWire(after)
}

/** How many characteristics can reject the lot on their own. */
export const criticalCount = (parameters: QcTemplateParameter[]): number =>
  parameters.filter((parameter) => parameter.is_critical).length

/**
 * Whether a stage restored from a bookmarked URL is one the contract defines.
 *
 * A stray `?qc_stage=RECEIVING` would otherwise be forwarded to the list
 * endpoint and answered with a 422 on a read the user did nothing to provoke.
 */
export const isQcStage = (value: string, stages: readonly QcStage[]): value is QcStage =>
  (stages as readonly string[]).includes(value)

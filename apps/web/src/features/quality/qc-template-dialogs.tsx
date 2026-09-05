/**
 * Reading and writing an inspection specification.
 *
 * Four dialogs, because a template is not a record with one Save:
 *
 * * **Detail** shows the check sheet and the revision history. A template
 *   reduced to a name and a date says nothing - the characteristics, their
 *   limits and which of them can reject the lot on their own *are* the
 *   specification.
 * * **Amend** rewrites this version in place. The API permits that only while
 *   no inspection has been judged against it, so the form separates what is
 *   always editable (name, description, whether it is selected) from what
 *   restates the specification, and says which of the two is about to happen.
 * * **New version** forks the row: same code, `version + 1`, the previous
 *   revision retired. Nothing already recorded moves.
 * * **Retire** is the soft delete. It takes the specification out of every list
 *   for good; the row survives only so past inspections still name what they
 *   followed.
 *
 * Every write here is gated on `P.QC_TEMPLATE_MANAGE`, which is what
 * `apps/api/src/modules/quality/router.py` declares on all four of
 * `POST /quality/templates`, `PATCH`, `POST .../versions` and `DELETE`. It is
 * deliberately *not* `qc.inspect` or `qc.decide`: the person who measures a
 * part and the person who writes the specification it is measured against are
 * different roles (section 38). The gating here is UX only - the API
 * authorises each request independently.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, GitBranch, Pencil, Plus, ShieldAlert, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import {
  Controller,
  useFieldArray,
  useForm,
  useWatch,
  type Control,
  type FieldErrors,
  type UseFormRegister,
} from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import { ConfirmDialog, DescriptionList, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Checkbox,
  Input,
  Label,
  Separator,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { useCategoryOptions } from '@/features/items/item-model'
import { ApiError, api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDateTime, humanise, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  ALLOWED_VALUE_MAX_LENGTH,
  CODE_MAX_LENGTH,
  CODE_MIN_LENGTH,
  DESCRIPTION_MAX_LENGTH,
  MAX_PARAMETERS_PER_TEMPLATE,
  MAX_SAMPLE_SIZE,
  MEASUREMENT,
  NAME_MAX_LENGTH,
  NAME_MIN_LENGTH,
  PARAMETER_NAME_MAX_LENGTH,
  PARAMETER_TYPE_LABEL,
  PERCENTAGE,
  QC_SAMPLING_PLANS,
  SAMPLE_SIZE,
  SAMPLING_PLAN_HINT,
  SAMPLING_PLAN_LABEL,
  SCOPE_LABEL,
  UNIT_MAX_LENGTH,
  criticalCount,
  draftFromParameter,
  draftToWire,
  emptyParameterDraft,
  isPercentageInRange,
  isQcStage,
  planNeedsPercentage,
  planNeedsSampleSize,
  qcTemplateKeys,
  samplingLabel,
  scopeOf,
  sheetChanged,
  splitAllowedValues,
  templateTitle,
  toleranceIsInverted,
  type QcParameterDraft,
  type QcTemplateCreate,
  type QcTemplateDetail,
  type QcTemplateRow,
  type QcTemplateScope,
  type QcTemplateUpdate,
  type QcTemplateVersionRequest,
} from './qc-template-model'
import { QC_PARAMETER_TYPES, QC_STAGES, specLabel, type QcTemplateParameter } from './quality-types'

/** How many revisions of one code the history panel will show. */
const VERSION_HISTORY_LIMIT = 50

const SCOPES = ['ITEM', 'CATEGORY', 'STAGE'] as const satisfies readonly QcTemplateScope[]

// =============================================================================
// The form
// =============================================================================

/**
 * One characteristic.
 *
 * The rules mirror `TemplateParameterIn`'s model validator exactly, because the
 * API rejects the *whole* request when one row disagrees with its own type - a
 * forty-row sheet bounced for a stray tolerance on a yes/no question, with no
 * indication of which row. Catching it here puts the message on the field.
 */
const parameterSchema = z
  .object({
    parameter_name: z
      .string()
      .trim()
      .min(1, 'Name the characteristic')
      .max(PARAMETER_NAME_MAX_LENGTH, `At most ${PARAMETER_NAME_MAX_LENGTH} characters`),
    parameter_type: z.enum(QC_PARAMETER_TYPES),
    unit: z.string().trim().max(UNIT_MAX_LENGTH, `At most ${UNIT_MAX_LENGTH} characters`),
    min_value: z.string().trim(),
    max_value: z.string().trim(),
    target_value: z.string().trim(),
    // Not trimmed: the newlines separate the permitted answers.
    allowed_values: z.string(),
    is_critical: z.boolean(),
    is_mandatory: z.boolean(),
  })
  .superRefine((row, ctx) => {
    if (row.parameter_type === 'NUMERIC') {
      for (const field of ['min_value', 'max_value', 'target_value'] as const) {
        const value = row[field]
        if (value !== '' && !MEASUREMENT.test(value)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: [field],
            message: 'Up to 12 digits and 6 decimal places',
          })
        }
      }
      if (toleranceIsInverted(row.min_value, row.max_value)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['max_value'],
          message: 'The maximum is below the minimum',
        })
      }
    }

    if (row.parameter_type === 'SELECT' || row.parameter_type === 'TEXT') {
      const values = splitAllowedValues(row.allowed_values)
      if (row.parameter_type === 'SELECT' && values.length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['allowed_values'],
          message: 'A chosen-value characteristic needs the answers it accepts',
        })
      }
      if (values.some((value) => value.length > ALLOWED_VALUE_MAX_LENGTH)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['allowed_values'],
          message: `Each answer is at most ${ALLOWED_VALUE_MAX_LENGTH} characters`,
        })
      }
      if (new Set(values).size !== values.length) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['allowed_values'],
          message: 'The answers repeat',
        })
      }
    }
  })

const templateFormShape = z.object({
  // Immutable after creation - `TemplateUpdate` has no `code` - so it is
  // validated only on the create path, in `schemaFor`.
  code: z.string().trim(),
  name: z
    .string()
    .trim()
    .min(NAME_MIN_LENGTH, `At least ${NAME_MIN_LENGTH} characters`)
    .max(NAME_MAX_LENGTH, `At most ${NAME_MAX_LENGTH} characters`),
  description: z
    .string()
    .trim()
    .max(DESCRIPTION_MAX_LENGTH, `At most ${DESCRIPTION_MAX_LENGTH} characters`),
  /** Required only when forking a version; that is where the audit needs it. */
  reason: z.string().trim(),
  scope: z.enum(SCOPES),
  item_id: z.string(),
  item_category_id: z.string(),
  // `z.custom` rather than a second copy of the stage list: `QC_STAGES` is
  // already typed against the contract's `QcStage`, and a duplicated tuple here
  // would be the one that goes stale.
  qc_stage: z.custom<(typeof QC_STAGES)[number]>(
    (value) => (typeof value === 'string' ? isQcStage(value, QC_STAGES) : false),
    { message: 'Choose the inspection stage' },
  ),
  sampling_plan: z.enum(QC_SAMPLING_PLANS),
  sample_percentage: z.string().trim(),
  sample_size: z.string().trim(),
  is_active: z.boolean(),
  parameters: z
    .array(parameterSchema)
    .min(1, 'A specification with no characteristics cannot inspect anything')
    .max(MAX_PARAMETERS_PER_TEMPLATE, `At most ${MAX_PARAMETERS_PER_TEMPLATE} characteristics`),
})

type TemplateFormValues = z.infer<typeof templateFormShape>

/** Which act the form is performing. Drives the copy and the extra rules. */
type FormMode = 'CREATE' | 'AMEND' | 'VERSION'

/**
 * The schema for one mode.
 *
 * A factory rather than a `mode` field inside the form, so all three modes
 * share one values type - which is what lets the check-sheet editor below be a
 * plain component instead of a generic one wrapped in casts.
 */
function schemaFor(mode: FormMode) {
  return templateFormShape.superRefine((values, ctx) => {
    if (mode === 'CREATE') {
      if (values.code.length < CODE_MIN_LENGTH || values.code.length > CODE_MAX_LENGTH) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['code'],
          message: `Between ${CODE_MIN_LENGTH} and ${CODE_MAX_LENGTH} characters`,
        })
      }
    }

    if (mode === 'VERSION') {
      if (values.reason.length < REASON_MIN_LENGTH || values.reason.length > REASON_MAX_LENGTH) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['reason'],
          message: `Between ${REASON_MIN_LENGTH} and ${REASON_MAX_LENGTH} characters`,
        })
      }
    }

    if (values.scope === 'ITEM' && values.item_id === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['item_id'],
        message: 'Choose the item this specification governs',
      })
    }
    if (values.scope === 'CATEGORY' && values.item_category_id === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['item_category_id'],
        message: 'Choose the category this specification governs',
      })
    }

    if (planNeedsPercentage(values.sampling_plan)) {
      if (
        !PERCENTAGE.test(values.sample_percentage) ||
        !isPercentageInRange(values.sample_percentage)
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['sample_percentage'],
          message: 'A percentage above 0 and at most 100, to 4 decimal places',
        })
      }
    }
    if (planNeedsSampleSize(values.sampling_plan)) {
      const size = Number(values.sample_size)
      if (!SAMPLE_SIZE.test(values.sample_size) || size < 1 || size > MAX_SAMPLE_SIZE) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['sample_size'],
          message: `A whole number of pieces between 1 and ${MAX_SAMPLE_SIZE}`,
        })
      }
    }

    // "Two parameters share a name; a check sheet cannot ask twice." The API
    // compares them trimmed and case-folded, so this does too - otherwise
    // "Length" and "length" pass here and are refused there.
    const seen = new Map<string, number>()
    values.parameters.forEach((row, index) => {
      const key = row.parameter_name.trim().toLowerCase()
      if (key === '') return
      if (seen.has(key)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['parameters', index, 'parameter_name'],
          message: 'Another characteristic already asks this',
        })
      } else {
        seen.set(key, index)
      }
    })
  })
}

function emptyFormValues(): TemplateFormValues {
  return {
    code: '',
    name: '',
    description: '',
    reason: '',
    scope: 'ITEM',
    item_id: '',
    item_category_id: '',
    qc_stage: 'INCOMING',
    sampling_plan: 'ALL',
    sample_percentage: '',
    sample_size: '',
    is_active: true,
    parameters: [emptyParameterDraft()],
  }
}

function formValuesFrom(detail: QcTemplateDetail, sheet: QcParameterDraft[]): TemplateFormValues {
  return {
    code: detail.code,
    name: detail.name,
    description: detail.description ?? '',
    reason: '',
    scope: scopeOf(detail),
    item_id: detail.item_id ?? '',
    item_category_id: detail.item_category_id ?? '',
    qc_stage: detail.qc_stage,
    sampling_plan: detail.sampling_plan,
    sample_percentage: detail.sample_percentage ?? '',
    sample_size: detail.sample_size === null ? '' : String(detail.sample_size),
    is_active: detail.is_active,
    parameters: sheet,
  }
}

// ------------------------------------------------------------- payloads -----

/** A sample size is a count of pieces, not money - an integer is correct. */
const asSampleSize = (value: string): number => Number.parseInt(value, 10)

function createPayload(values: TemplateFormValues): QcTemplateCreate {
  return {
    code: values.code,
    name: values.name,
    description: values.description === '' ? null : values.description,
    item_id: values.scope === 'ITEM' ? values.item_id : null,
    item_category_id: values.scope === 'CATEGORY' ? values.item_category_id : null,
    qc_stage: values.qc_stage,
    sampling_plan: values.sampling_plan,
    // `TemplateCreate` refuses a figure that does not belong to the plan
    // ("sample_percentage applies to the PERCENTAGE plan only"), so the one the
    // plan does not use is cleared rather than sent as typed.
    sample_percentage: planNeedsPercentage(values.sampling_plan) ? values.sample_percentage : null,
    sample_size: planNeedsSampleSize(values.sampling_plan)
      ? asSampleSize(values.sample_size)
      : null,
    is_active: values.is_active,
    parameters: values.parameters.map((row, index) => draftToWire(row, index + 1)),
  }
}

function versionPayload(values: TemplateFormValues): QcTemplateVersionRequest {
  return {
    reason: values.reason,
    name: values.name,
    // Sent as typed, empty string included. `null` on this endpoint means
    // "copy the description the source version had", so emptying the box and
    // sending null would bring back the text the user just deleted.
    description: values.description,
    // The plan is always sent, even unchanged: `create_template_version` reads
    // `sample_percentage` and `sample_size` from the payload only when
    // `sampling_plan` is present, and otherwise copies the source's figures. A
    // percentage edited on its own would silently be discarded.
    sampling_plan: values.sampling_plan,
    sample_percentage: planNeedsPercentage(values.sampling_plan) ? values.sample_percentage : null,
    sample_size: planNeedsSampleSize(values.sampling_plan)
      ? asSampleSize(values.sample_size)
      : null,
    parameters: values.parameters.map((row, index) => draftToWire(row, index + 1)),
    // Not offered as a choice. A new version inherits the source's scope, and
    // two active templates on one scope are refused by `_assert_scope_is_free`
    // and by the partial unique index behind it - so `retire_previous: false`
    // cannot succeed in the only situation anyone would ask for it, namely
    // while the source is still active. The dialog states the retirement as a
    // consequence instead of offering a switch that always fails.
    retire_previous: true,
  }
}

/** Equal as decimals: `'5'`, `'5.0'` and `'5.0000'` are the same 5%. */
function sameDecimal(formValue: string, stored: string | null): boolean {
  if (formValue === '') return stored === null
  if (stored === null) return false
  return toDecimal(formValue).equals(toDecimal(stored))
}

interface AmendDiff {
  payload: QcTemplateUpdate
  /**
   * Whether the payload carries a key the API classes as "meaning" - scope,
   * stage, sampling or the check sheet.
   */
  restatesSpecification: boolean
  hasChanges: boolean
}

/**
 * What actually changed, and only that.
 *
 * This is load-bearing rather than tidy. `update_template` decides whether an
 * edit restates the specification from `payload.model_fields_set` - the keys
 * *present* in the body, not the ones whose values differ. Sending the whole
 * form therefore turns "fix a typo in the name" into a specification change and
 * gets it refused on every template that has ever been inspected against.
 */
function diffTemplate(
  values: TemplateFormValues,
  detail: QcTemplateDetail,
  baseline: QcParameterDraft[],
): AmendDiff {
  const presentation: QcTemplateUpdate = {}
  if (values.name !== detail.name) presentation.name = values.name
  if (values.description !== (detail.description ?? '')) {
    presentation.description = values.description === '' ? null : values.description
  }
  if (values.is_active !== detail.is_active) presentation.is_active = values.is_active

  const specification: QcTemplateUpdate = {}
  const nextItem = values.scope === 'ITEM' ? values.item_id : null
  const nextCategory = values.scope === 'CATEGORY' ? values.item_category_id : null
  if (nextItem !== detail.item_id) specification.item_id = nextItem
  if (nextCategory !== detail.item_category_id) specification.item_category_id = nextCategory
  if (values.qc_stage !== detail.qc_stage) specification.qc_stage = values.qc_stage

  const planChanged = values.sampling_plan !== detail.sampling_plan
  const nextPercentage = planNeedsPercentage(values.sampling_plan) ? values.sample_percentage : ''
  const nextSize = planNeedsSampleSize(values.sampling_plan) ? values.sample_size : ''
  const storedSize = detail.sample_size === null ? '' : String(detail.sample_size)
  if (planChanged) specification.sampling_plan = values.sampling_plan
  // The figure travels with the plan whenever the plan moves: the server
  // validates the merged row, and a PERCENTAGE plan arriving without its
  // percentage is refused as an incomplete plan.
  if (planChanged || !sameDecimal(nextPercentage, detail.sample_percentage)) {
    specification.sample_percentage = nextPercentage === '' ? null : nextPercentage
  }
  if (planChanged || nextSize !== storedSize) {
    specification.sample_size = nextSize === '' ? null : asSampleSize(nextSize)
  }

  if (sheetChanged(baseline, values.parameters)) {
    specification.parameters = values.parameters.map((row, index) => draftToWire(row, index + 1))
  }

  const restatesSpecification = Object.keys(specification).length > 0
  return {
    payload: { ...presentation, ...specification },
    restatesSpecification,
    hasChanges: restatesSpecification || Object.keys(presentation).length > 0,
  }
}

/**
 * The failures that have no field to sit on.
 *
 * A per-field 422 is rendered against its control by `applyFieldErrors`, and
 * the mutation cache stays quiet for those on purpose. Everything else - a
 * duplicate code, a scope already claimed, a frozen specification, and a
 * model-level 422 whose path is the whole body - would otherwise be a toast
 * that vanishes while the user is still looking at the form that caused it.
 */
function formError(error: unknown): string | null {
  if (error === null || error === undefined) return null
  if (!(error instanceof ApiError)) return 'Something went wrong. The change was not saved.'
  const root = error.fieldErrors._root
  if (root && root.length > 0) return root.join('. ')
  if (error.status === 422 && Object.keys(error.fieldErrors).length > 0) return null
  return error.message
}

/** The refusal that means "this version is frozen; fork it instead". */
const isFrozenSpecification = (error: unknown): boolean =>
  error instanceof ApiError && error.code === 'REFERENCED_RECORD_IN_USE'

// =============================================================================
// The check sheet editor
// =============================================================================

function ParameterRow({
  index,
  count,
  control,
  register,
  errors,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  index: number
  count: number
  control: Control<TemplateFormValues>
  register: UseFormRegister<TemplateFormValues>
  errors: FieldErrors<TemplateFormValues>
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}) {
  const parameterType = useWatch({ control, name: `parameters.${index}.parameter_type` })
  const isCritical = useWatch({ control, name: `parameters.${index}.is_critical` })
  const rowErrors = errors.parameters?.[index]

  const isNumeric = parameterType === 'NUMERIC'
  const hasValueList = parameterType === 'SELECT' || parameterType === 'TEXT'
  const position = `characteristic ${index + 1}`

  return (
    <div className={cn('rounded-md border p-3', isCritical && 'border-destructive/40')}>
      <div className="flex flex-wrap items-start gap-2">
        <span
          className="mt-2 w-6 shrink-0 text-center text-xs tabular text-muted-foreground"
          aria-hidden
        >
          {index + 1}
        </span>

        <div className="min-w-[10rem] flex-1">
          <Label htmlFor={`qc-param-name-${index}`} className="sr-only">
            {`Name of ${position}`}
          </Label>
          <Input
            id={`qc-param-name-${index}`}
            placeholder="What is measured, e.g. Overall length"
            {...register(`parameters.${index}.parameter_name`)}
            aria-invalid={Boolean(rowErrors?.parameter_name)}
          />
          <FieldError message={rowErrors?.parameter_name?.message} />
        </div>

        <div className="w-full shrink-0 sm:w-[9.5rem]">
          <Label htmlFor={`qc-param-type-${index}`} className="sr-only">
            {`Type of ${position}`}
          </Label>
          <Controller
            control={control}
            name={`parameters.${index}.parameter_type`}
            render={({ field }) => (
              // A leftover limit or answer list left behind by a type switch is
              // dropped by `draftToWire`, never sent - so switching type and
              // back does not mean retyping the band.
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id={`qc-param-type-${index}`} aria-label={`Type of ${position}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {QC_PARAMETER_TYPES.map((type) => (
                    <SelectItem key={type} value={type}>
                      {PARAMETER_TYPE_LABEL[type]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-0.5 pt-0.5">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={index === 0}
            aria-label={`Move ${position} earlier`}
            onClick={onMoveUp}
          >
            <ArrowUp />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={index === count - 1}
            aria-label={`Move ${position} later`}
            onClick={onMoveDown}
          >
            <ArrowDown />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            // The API will not accept a specification with no characteristics,
            // so the last row cannot be removed.
            disabled={count === 1}
            aria-label={`Remove ${position}`}
            onClick={onRemove}
          >
            <Trash2 />
          </Button>
        </div>
      </div>

      {isNumeric && (
        <div className="mt-3 grid gap-3 pl-0 sm:grid-cols-4 sm:pl-8">
          <div>
            <Label htmlFor={`qc-param-min-${index}`} className="text-xs">
              Minimum
            </Label>
            <Input
              id={`qc-param-min-${index}`}
              inputMode="decimal"
              placeholder="-"
              {...register(`parameters.${index}.min_value`)}
              aria-invalid={Boolean(rowErrors?.min_value)}
              className="mt-1 text-right tabular"
            />
            <FieldError message={rowErrors?.min_value?.message} />
          </div>
          <div>
            <Label htmlFor={`qc-param-max-${index}`} className="text-xs">
              Maximum
            </Label>
            <Input
              id={`qc-param-max-${index}`}
              inputMode="decimal"
              placeholder="-"
              {...register(`parameters.${index}.max_value`)}
              aria-invalid={Boolean(rowErrors?.max_value)}
              className="mt-1 text-right tabular"
            />
            <FieldError message={rowErrors?.max_value?.message} />
          </div>
          <div>
            <Label htmlFor={`qc-param-target-${index}`} className="text-xs">
              Target
            </Label>
            <Input
              id={`qc-param-target-${index}`}
              inputMode="decimal"
              placeholder="-"
              {...register(`parameters.${index}.target_value`)}
              aria-invalid={Boolean(rowErrors?.target_value)}
              className="mt-1 text-right tabular"
            />
            <FieldError message={rowErrors?.target_value?.message} />
          </div>
          <div>
            <Label htmlFor={`qc-param-unit-${index}`} className="text-xs">
              Unit
            </Label>
            <Input
              id={`qc-param-unit-${index}`}
              placeholder="mm"
              {...register(`parameters.${index}.unit`)}
              aria-invalid={Boolean(rowErrors?.unit)}
              className="mt-1"
            />
            <FieldError message={rowErrors?.unit?.message} />
          </div>
        </div>
      )}

      {hasValueList && (
        <div className="mt-3 pl-0 sm:pl-8">
          <Label htmlFor={`qc-param-values-${index}`} className="text-xs">
            {parameterType === 'SELECT' ? 'Answers it accepts' : 'Suggested answers (optional)'}
          </Label>
          <Textarea
            id={`qc-param-values-${index}`}
            rows={3}
            // One per line rather than comma-separated: a comma is a legitimate
            // character in an answer such as "Blue, dark".
            placeholder={'One per line\nPass\nFail'}
            {...register(`parameters.${index}.allowed_values`)}
            aria-invalid={Boolean(rowErrors?.allowed_values)}
            className="mt-1"
          />
          <FieldError message={rowErrors?.allowed_values?.message} />
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 pl-0 sm:pl-8">
        <div className="flex items-center gap-2">
          <Controller
            control={control}
            name={`parameters.${index}.is_critical`}
            render={({ field }) => (
              <Checkbox
                id={`qc-param-critical-${index}`}
                checked={field.value}
                onCheckedChange={(checked) => field.onChange(checked === true)}
              />
            )}
          />
          <Label htmlFor={`qc-param-critical-${index}`} className="text-xs font-normal">
            Critical - one failure condemns the lot
          </Label>
        </div>

        <div className="flex items-center gap-2">
          <Controller
            control={control}
            name={`parameters.${index}.is_mandatory`}
            render={({ field }) => (
              <Checkbox
                id={`qc-param-mandatory-${index}`}
                // A critical characteristic that may be skipped never rejects
                // anything, so the API refuses the pair outright. The box is
                // shown ticked and locked rather than letting the form collect
                // a combination that is guaranteed to be sent back.
                checked={isCritical ? true : field.value}
                disabled={isCritical}
                onCheckedChange={(checked) => field.onChange(checked === true)}
              />
            )}
          />
          <Label htmlFor={`qc-param-mandatory-${index}`} className="text-xs font-normal">
            Must be recorded
          </Label>
        </div>
      </div>
    </div>
  )
}

function ParameterSheetEditor({
  control,
  register,
  errors,
}: {
  control: Control<TemplateFormValues>
  register: UseFormRegister<TemplateFormValues>
  errors: FieldErrors<TemplateFormValues>
}) {
  const { fields, append, remove, move } = useFieldArray({ control, name: 'parameters' })

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Label>Characteristics</Label>
          <p className="mt-0.5 text-xs text-muted-foreground">
            The order is the order they are worked on the shop floor.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={fields.length >= MAX_PARAMETERS_PER_TEMPLATE}
          onClick={() => append(emptyParameterDraft())}
        >
          <Plus />
          Add characteristic
        </Button>
      </div>

      {fields.map((field, index) => (
        <ParameterRow
          key={field.id}
          index={index}
          count={fields.length}
          control={control}
          register={register}
          errors={errors}
          onRemove={() => remove(index)}
          onMoveUp={() => move(index, index - 1)}
          onMoveDown={() => move(index, index + 1)}
        />
      ))}

      <FieldError message={errors.parameters?.message ?? errors.parameters?.root?.message} />
    </div>
  )
}

// =============================================================================
// Shared form sections
// =============================================================================

function ScopeFields({
  control,
  errors,
}: {
  control: Control<TemplateFormValues>
  errors: FieldErrors<TemplateFormValues>
}) {
  const { can } = useAuth()
  // Categories are a bounded reference set, cached for the session. The picker
  // for items is a server-side search instead - the item master runs to tens of
  // thousands of rows and must never be downloaded to be filtered here.
  const categories = useCategoryOptions(can(P.CATEGORY_VIEW))
  const scope = useWatch({ control, name: 'scope' })

  return (
    <div className="space-y-3">
      <div>
        <Label htmlFor="qc-template-scope">Applies to</Label>
        <Controller
          control={control}
          name="scope"
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger id="qc-template-scope" className="mt-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SCOPES.map((option) => (
                  <SelectItem key={option} value={option}>
                    {SCOPE_LABEL[option]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        />
        <p className="mt-1 text-xs text-muted-foreground">
          An item-specific specification beats a category one, which beats the stage default. Only
          one active template may claim each scope, so resolving the specification for an incoming
          lot has a single answer.
        </p>
      </div>

      {scope === 'ITEM' && (
        <div>
          <Controller
            control={control}
            name="item_id"
            render={({ field }) => (
              <ItemPicker
                id="qc-template-item"
                label="Item"
                value={field.value || null}
                onChange={(itemId) => field.onChange(itemId ?? '')}
                placeholder="Search the item master"
                invalid={Boolean(errors.item_id)}
              />
            )}
          />
          <FieldError message={errors.item_id?.message} />
        </div>
      )}

      {scope === 'CATEGORY' && (
        <div>
          <Label htmlFor="qc-template-category" required>
            Item category
          </Label>
          <Controller
            control={control}
            name="item_category_id"
            render={({ field }) => (
              <Select
                value={field.value}
                onValueChange={field.onChange}
                disabled={!can(P.CATEGORY_VIEW)}
              >
                <SelectTrigger
                  id="qc-template-category"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.item_category_id)}
                >
                  <SelectValue placeholder="Choose a category" />
                </SelectTrigger>
                <SelectContent>
                  {(categories.data ?? []).map((category) => (
                    <SelectItem key={category.id} value={category.id}>
                      {category.code} - {category.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          {!can(P.CATEGORY_VIEW) && (
            <p className="mt-1 text-xs text-muted-foreground">
              Choosing a category needs the category catalogue, which your account cannot read. Use
              an item or the stage default instead.
            </p>
          )}
          <FieldError message={errors.item_category_id?.message} />
        </div>
      )}
    </div>
  )
}

function SamplingFields({
  control,
  register,
  errors,
}: {
  control: Control<TemplateFormValues>
  register: UseFormRegister<TemplateFormValues>
  errors: FieldErrors<TemplateFormValues>
}) {
  const plan = useWatch({ control, name: 'sampling_plan' })

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="qc-template-plan">Sampling plan</Label>
          <Controller
            control={control}
            name="sampling_plan"
            render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id="qc-template-plan" className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {QC_SAMPLING_PLANS.map((option) => (
                    <SelectItem key={option} value={option}>
                      {SAMPLING_PLAN_LABEL[option]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
        </div>

        {planNeedsPercentage(plan) && (
          <div>
            <Label htmlFor="qc-template-percentage" required>
              Percentage of the lot
            </Label>
            <Input
              id="qc-template-percentage"
              inputMode="decimal"
              {...register('sample_percentage')}
              aria-invalid={Boolean(errors.sample_percentage)}
              className="mt-1.5 text-right tabular"
            />
            <FieldError message={errors.sample_percentage?.message} />
          </div>
        )}

        {planNeedsSampleSize(plan) && (
          <div>
            <Label htmlFor="qc-template-sample-size" required>
              Pieces per lot
            </Label>
            <Input
              id="qc-template-sample-size"
              inputMode="numeric"
              {...register('sample_size')}
              aria-invalid={Boolean(errors.sample_size)}
              className="mt-1.5 text-right tabular"
            />
            <FieldError message={errors.sample_size?.message} />
          </div>
        )}
      </div>

      <p className="text-xs text-muted-foreground">{SAMPLING_PLAN_HINT[plan]}</p>
    </div>
  )
}

function StageField({
  control,
  errors,
}: {
  control: Control<TemplateFormValues>
  errors: FieldErrors<TemplateFormValues>
}) {
  return (
    <div>
      <Label htmlFor="qc-template-stage">Inspection stage</Label>
      <Controller
        control={control}
        name="qc_stage"
        render={({ field }) => (
          <Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger id="qc-template-stage" className="mt-1.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {QC_STAGES.map((stage) => (
                <SelectItem key={stage} value={stage}>
                  {humanise(stage)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      />
      {/* A stage restored from a hand-edited URL is the only way this can fail,
          and a form that refuses to submit without saying why is worse than the
          bad value. */}
      <FieldError message={errors.qc_stage?.message} />
    </div>
  )
}

/** A skeleton the width of the fields it stands in for. */
function FormSkeleton() {
  return (
    <div className="space-y-4" aria-hidden>
      <Skeleton className="h-9 w-full" />
      <Skeleton className="h-9 w-2/3" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-24 w-full" />
    </div>
  )
}

// =============================================================================
// Create and amend
// =============================================================================

/**
 * Write a new specification, or amend an existing version of one.
 *
 * Both are `P.QC_TEMPLATE_MANAGE` - `POST /quality/templates` and
 * `PATCH /quality/templates/{id}` declare the same right.
 */
export function QcTemplateFormDialog({
  open,
  onOpenChange,
  templateId,
  onForkInstead,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Null creates version 1; an id amends that exact version. */
  templateId: string | null
  /** Offered when the API refuses the edit because the version is in use. */
  onForkInstead: (templateId: string) => void
}) {
  const queryClient = useQueryClient()
  const mode: FormMode = templateId === null ? 'CREATE' : 'AMEND'
  /**
   * The check sheet the form was filled from, tagged with the template it came
   * from.
   *
   * The tag is not decoration. `reset` happens in an effect, so there is a frame
   * in which the freshly fetched template is in hand and the form still holds
   * the previous one's values - and a diff taken across those two would post an
   * edit nobody typed. Everything that can write is held back until the tag
   * matches the template on screen.
   */
  const [filledFrom, setFilledFrom] = useState<{ id: string; sheet: QcParameterDraft[] } | null>(
    null,
  )

  const detail = useQuery({
    queryKey: qcTemplateKeys.detail(templateId ?? ''),
    queryFn: () => api.get<QcTemplateDetail>(`/quality/templates/${templateId}`),
    enabled: open && templateId !== null,
    staleTime: staleTimes.master,
  })

  const resolver = useMemo(() => zodResolver(schemaFor(mode)), [mode])
  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<TemplateFormValues>({ resolver, defaultValues: emptyFormValues() })

  const loaded = detail.data
  useEffect(() => {
    if (!open) return
    if (mode === 'CREATE') {
      reset(emptyFormValues())
      setFilledFrom(null)
      return
    }
    if (!loaded) return
    const sheet = (loaded.parameters ?? []).map(draftFromParameter)
    reset(formValuesFrom(loaded, sheet))
    setFilledFrom({ id: loaded.id, sheet })
  }, [open, mode, loaded, reset])

  // `watch()` re-renders the dialog on every keystroke, which is the price of
  // telling the user *before* they submit which of the two acts they are about
  // to perform. Affordable because the sheet is bounded at
  // MAX_PARAMETERS_PER_TEMPLATE and a real one runs to tens of rows, not two
  // hundred; nothing else on the screen depends on it.
  const filled = loaded !== undefined && filledFrom?.id === loaded.id ? filledFrom : null
  const diff = loaded && filled ? diffTemplate(watch(), loaded, filled.sheet) : null

  const mutation = useMutation({
    mutationFn: (submitted: TemplateFormValues) => {
      if (mode === 'CREATE') {
        return api.post<QcTemplateDetail>('/quality/templates', createPayload(submitted))
      }
      if (!loaded || !filled) {
        // Unreachable while the submit button is gated on `canSubmit`; a PATCH
        // built from a form that was never filled would be an edit nobody typed.
        throw new Error('The specification is still loading.')
      }
      return api.patch<QcTemplateDetail>(
        `/quality/templates/${templateId}`,
        diffTemplate(submitted, loaded, filled.sheet).payload,
      )
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: qcTemplateKeys.all })
      toast.success(
        mode === 'CREATE' ? success.created('Specification') : success.updated('Specification'),
        `${templateTitle(saved)} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const message = formError(mutation.error)
  const awaitingDetail = mode === 'AMEND' && !filled
  const canSubmit = mode === 'CREATE' || (diff?.hasChanges ?? false)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {mode === 'CREATE'
              ? 'New inspection specification'
              : `Amend ${loaded ? templateTitle(loaded) : 'specification'}`}
          </DialogTitle>
          <DialogDescription>
            {mode === 'CREATE'
              ? 'Created as version 1. Inspections raised for this scope and stage will be judged against the characteristics below.'
              : 'Name, description and whether it is selected can always be changed. Anything that restates what the specification means can only be changed while no inspection has been judged against this version.'}
          </DialogDescription>
        </DialogHeader>

        {awaitingDetail ? (
          <DialogBody>
            {detail.error ? (
              <Alert variant="destructive">
                <AlertTitle>Could not load this specification</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            ) : (
              <FormSkeleton />
            )}
          </DialogBody>
        ) : (
          <form
            onSubmit={handleSubmit((submitted) => mutation.mutate(submitted))}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-5">
              {message && (
                <Alert variant="destructive">
                  <AlertTitle>
                    {isFrozenSpecification(mutation.error)
                      ? 'This version is frozen'
                      : mode === 'CREATE'
                        ? 'The specification was not created'
                        : 'The change was not saved'}
                  </AlertTitle>
                  <AlertDescription>{message}</AlertDescription>
                  {isFrozenSpecification(mutation.error) && templateId && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-3"
                      onClick={() => {
                        onOpenChange(false)
                        onForkInstead(templateId)
                      }}
                    >
                      <GitBranch />
                      Raise a new version instead
                    </Button>
                  )}
                </Alert>
              )}

              <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)]">
                <div>
                  <Label htmlFor="qc-template-code" required={mode === 'CREATE'}>
                    Code
                  </Label>
                  <Input
                    id="qc-template-code"
                    {...register('code')}
                    // The code identifies the specification across every one of
                    // its versions, and `TemplateUpdate` has no field for it.
                    readOnly={mode === 'AMEND'}
                    aria-invalid={Boolean(errors.code)}
                    autoComplete="off"
                    className={cn('mt-1.5 font-mono', mode === 'AMEND' && 'bg-muted')}
                  />
                  <FieldError message={errors.code?.message} />
                </div>
                <div>
                  <Label htmlFor="qc-template-name" required>
                    Name
                  </Label>
                  <Input
                    id="qc-template-name"
                    {...register('name')}
                    aria-invalid={Boolean(errors.name)}
                    className="mt-1.5"
                  />
                  <FieldError message={errors.name?.message} />
                </div>
              </div>

              <div>
                <Label htmlFor="qc-template-description">Description</Label>
                <Textarea
                  id="qc-template-description"
                  rows={2}
                  placeholder="What this specification is for, and anything the inspector should know before starting."
                  {...register('description')}
                  aria-invalid={Boolean(errors.description)}
                  className="mt-1.5"
                />
                <FieldError message={errors.description?.message} />
              </div>

              <div className="flex items-start gap-2">
                <Controller
                  control={control}
                  name="is_active"
                  render={({ field }) => (
                    <Checkbox
                      id="qc-template-active"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                      className="mt-0.5"
                    />
                  )}
                />
                <div>
                  <Label htmlFor="qc-template-active">Selected for new inspections</Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Leave this off to stage a specification without displacing the one currently in
                    force. An inactive template is never resolved for an incoming lot.
                  </p>
                </div>
              </div>

              <Separator />

              {mode === 'AMEND' && (
                <Alert variant={diff?.restatesSpecification ? 'warning' : 'info'}>
                  <AlertTitle>
                    {diff?.restatesSpecification
                      ? 'You are restating this specification'
                      : 'Everything below defines what this specification means'}
                  </AlertTitle>
                  <AlertDescription>
                    {diff?.restatesSpecification
                      ? 'Scope, stage, sampling and the characteristics decide what past inspections were judged against. The API will refuse this edit if any inspection has already applied this version - raise a new version instead, and the record of what was measured stays intact.'
                      : 'Changing any of it is only accepted while no inspection has applied this version.'}
                  </AlertDescription>
                  {/* The refusal alert above already offers the fork, and one
                      dialog does not need the same button twice. */}
                  {templateId && !isFrozenSpecification(mutation.error) && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-3"
                      onClick={() => {
                        onOpenChange(false)
                        onForkInstead(templateId)
                      }}
                    >
                      <GitBranch />
                      Raise a new version instead
                    </Button>
                  )}
                </Alert>
              )}

              <StageField control={control} errors={errors} />
              <ScopeFields control={control} errors={errors} />
              <SamplingFields control={control} register={register} errors={errors} />

              <Separator />

              <ParameterSheetEditor control={control} register={register} errors={errors} />
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={mutation.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" loading={mutation.isPending} disabled={!canSubmit}>
                {mode === 'CREATE'
                  ? 'Create specification'
                  : diff?.restatesSpecification
                    ? 'Save specification change'
                    : 'Save details'}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

// =============================================================================
// Fork a new version
// =============================================================================

/**
 * Fork a used specification into the next revision.
 *
 * `POST /quality/templates/{id}/versions`, `P.QC_TEMPLATE_MANAGE`. The check
 * sheet arrives pre-filled with the current one so that changing a single
 * tolerance does not mean retyping forty rows, and the whole sheet is sent, so
 * a reorder made here is what gets written.
 */
export function QcTemplateVersionDialog({
  open,
  onOpenChange,
  templateId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  templateId: string | null
}) {
  const queryClient = useQueryClient()
  /**
   * Which template the form was filled from.
   *
   * `reset` runs in an effect, so the fetched source and the form's contents
   * disagree for one frame. Posting in that frame would fork the specification
   * with whatever the previous source left behind, which is exactly the kind of
   * write nothing downstream can distinguish from a deliberate one.
   */
  const [filledFrom, setFilledFrom] = useState<string | null>(null)

  const detail = useQuery({
    queryKey: qcTemplateKeys.detail(templateId ?? ''),
    queryFn: () => api.get<QcTemplateDetail>(`/quality/templates/${templateId}`),
    enabled: open && templateId !== null,
    staleTime: staleTimes.master,
  })

  const resolver = useMemo(() => zodResolver(schemaFor('VERSION')), [])
  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<TemplateFormValues>({ resolver, defaultValues: emptyFormValues() })

  const loaded = detail.data
  useEffect(() => {
    if (!open || !loaded) return
    reset(formValuesFrom(loaded, (loaded.parameters ?? []).map(draftFromParameter)))
    setFilledFrom(loaded.id)
  }, [open, loaded, reset])

  const isReady = loaded !== undefined && filledFrom === loaded.id

  const mutation = useMutation({
    mutationFn: (submitted: TemplateFormValues) =>
      api.post<QcTemplateDetail>(
        `/quality/templates/${templateId}/versions`,
        versionPayload(submitted),
      ),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: qcTemplateKeys.all })
      toast.success(
        success.created(templateTitle(created)),
        'The previous version is retired. Inspections already recorded still name the version they followed.',
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const message = formError(mutation.error)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {loaded ? `New version of ${loaded.code}` : 'New version of this specification'}
          </DialogTitle>
          <DialogDescription>
            {loaded
              ? `Version ${loaded.version} is retired and version ${loaded.version + 1} takes its place. Inspections already judged against v${loaded.version} keep pointing at it, so what they measured stays reproducible.`
              : 'A fork writes the next revision and retires the current one.'}
          </DialogDescription>
        </DialogHeader>

        {!isReady ? (
          <DialogBody>
            {detail.error ? (
              <Alert variant="destructive">
                <AlertTitle>Could not load the version being forked</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            ) : (
              <FormSkeleton />
            )}
          </DialogBody>
        ) : (
          <form
            onSubmit={handleSubmit((submitted) => mutation.mutate(submitted))}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-5">
              {message && (
                <Alert variant="destructive">
                  <AlertTitle>The version was not created</AlertTitle>
                  <AlertDescription>{message}</AlertDescription>
                </Alert>
              )}

              <div>
                <Label htmlFor="qc-version-reason" required>
                  Why is the specification changing?
                </Label>
                <Input
                  id="qc-version-reason"
                  placeholder="Drawing revision C: bore tolerance tightened"
                  {...register('reason')}
                  aria-invalid={Boolean(errors.reason)}
                  autoComplete="off"
                  className="mt-1.5"
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Recorded on the audit trail against both versions. It is the only place the reason
                  for a specification change is kept.
                </p>
                <FieldError message={errors.reason?.message} />
              </div>

              <Separator />

              <div>
                <Label htmlFor="qc-version-name" required>
                  Name
                </Label>
                <Input
                  id="qc-version-name"
                  {...register('name')}
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>

              <div>
                <Label htmlFor="qc-version-description">Description</Label>
                <Textarea
                  id="qc-version-description"
                  rows={2}
                  {...register('description')}
                  aria-invalid={Boolean(errors.description)}
                  className="mt-1.5"
                />
                <FieldError message={errors.description?.message} />
              </div>

              <SamplingFields control={control} register={register} errors={errors} />

              <Alert variant="info">
                <AlertTitle>Scope and stage carry over unchanged</AlertTitle>
                <AlertDescription>
                  {loaded
                    ? `This version stays the ${humanise(loaded.qc_stage)} specification for the same ${SCOPE_LABEL[scopeOf(loaded)].toLowerCase()}. Moving a specification to a different scope is an amendment, not a revision.`
                    : 'A revision keeps the scope and stage of the version it forks from.'}
                </AlertDescription>
              </Alert>

              <Separator />

              <ParameterSheetEditor control={control} register={register} errors={errors} />
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={mutation.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" loading={mutation.isPending}>
                {loaded ? `Create version ${loaded.version + 1}` : 'Create new version'}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

// =============================================================================
// Retire
// =============================================================================

/**
 * Retire a specification.
 *
 * `DELETE /quality/templates/{id}`, `P.QC_TEMPLATE_MANAGE`. The reason travels
 * in the query string, not a body: a DELETE body is legal but several clients
 * and proxies drop it, and the reason is not optional.
 */
export function QcTemplateRetireDialog({
  template,
  onOpenChange,
}: {
  template: Pick<QcTemplateRow, 'id' | 'code' | 'version' | 'name'> | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    if (template) {
      setReason('')
      setTouched(false)
    }
  }, [template])

  const mutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string; label: string }) =>
      // The reason travels in the query string rather than a body: a DELETE
      // body is legal but several HTTP clients and proxies drop it.
      api.delete(`/quality/templates/${id}`, { params: { reason: text } }),
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: qcTemplateKeys.all })
      toast.success(
        success.retired('Specification'),
        `${variables.label} is no longer resolved for new inspections.`,
      )
      onOpenChange(false)
    },
  })

  const tooShort = reason.trim().length < REASON_MIN_LENGTH

  return (
    <ConfirmDialog
      open={Boolean(template)}
      onOpenChange={onOpenChange}
      title="Retire this specification?"
      description={
        template
          ? `${templateTitle(template)} - ${template.name}. It stops being resolved for new inspections and disappears from this list. Inspections already recorded still name it, which is why the row is retired rather than deleted. To change a specification that is in use, raise a new version instead.`
          : undefined
      }
      confirmLabel="Retire specification"
      destructive
      loading={mutation.isPending}
      onConfirm={() => {
        if (!template) return
        if (tooShort) {
          setTouched(true)
          return
        }
        mutation.mutate({
          id: template.id,
          text: reason.trim(),
          label: `${templateTitle(template)} - ${template.name}`,
        })
      }}
    >
      <div>
        <Label htmlFor="qc-template-retire-reason" required>
          Reason
        </Label>
        <Input
          id="qc-template-retire-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={REASON_MAX_LENGTH}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={touched && tooShort}
        />
        {touched && tooShort && (
          <FieldError message={`Record at least ${REASON_MIN_LENGTH} characters of reason`} />
        )}
      </div>
    </ConfirmDialog>
  )
}

// =============================================================================
// Detail
// =============================================================================

function ParameterTable({ parameters }: { parameters: QcTemplateParameter[] }) {
  if (parameters.length === 0) {
    return (
      <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
        This version carries no characteristics, so an inspection against it has nothing to record.
        Raise a new version to give it a check sheet.
      </p>
    )
  }

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <caption className="sr-only">Characteristics measured by this specification</caption>
        <thead className="bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              #
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Characteristic
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Type
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Specification
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Judgement
            </th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {parameters.map((parameter) => {
            const band = specLabel(parameter)
            const answers = parameter.allowed_values ?? []
            return (
              <tr key={parameter.id}>
                <td className="px-3 py-2 tabular text-muted-foreground">{parameter.sequence}</td>
                <td className="px-3 py-2">
                  <span className="font-medium">{parameter.parameter_name}</span>
                  {parameter.unit && (
                    <span className="ml-1 text-xs text-muted-foreground">({parameter.unit})</span>
                  )}
                </td>
                <td className="px-3 py-2 text-muted-foreground">
                  {PARAMETER_TYPE_LABEL[parameter.parameter_type]}
                </td>
                <td className="px-3 py-2">
                  {band && <span className="tabular">{band}</span>}
                  {parameter.target_value !== null && (
                    <span className="block text-xs text-muted-foreground">
                      Target {parameter.target_value}
                      {parameter.unit ? ` ${parameter.unit}` : ''}
                    </span>
                  )}
                  {answers.length > 0 && (
                    <span className="block text-xs text-muted-foreground">
                      {answers.join(' / ')}
                    </span>
                  )}
                  {!band && parameter.target_value === null && answers.length === 0 && (
                    <span className="text-muted-foreground">Recorded, not judged</span>
                  )}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {parameter.is_critical && (
                      <Badge variant="destructive">
                        <ShieldAlert className="h-3 w-3" aria-hidden />
                        Critical
                      </Badge>
                    )}
                    {parameter.is_mandatory ? (
                      <Badge variant="secondary">Must record</Badge>
                    ) : (
                      <Badge variant="muted">Optional</Badge>
                    )}
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Every revision of one code.
 *
 * There is no versions endpoint and no `code` filter on the list - only a
 * `search` that matches code *or* name with an ILIKE - so the history is a
 * narrow server-side search with an exact-code check on what comes back. That
 * check discards a template whose *name* happens to contain this code; it is
 * not a substitute for server-side filtering, and the query is bounded, sorted
 * by version and capped.
 *
 * Versions retired by `DELETE` carry a `deleted_at` and are excluded by the
 * repository, so they never appear here. A version superseded by a fork is only
 * deactivated, which is why the history can show inactive rows.
 */
function VersionHistory({
  template,
  onOpenVersion,
}: {
  template: QcTemplateDetail
  onOpenVersion: (id: string) => void
}) {
  const params = {
    search: template.code,
    limit: VERSION_HISTORY_LIMIT,
    with_total: false,
    sort_by: 'version',
    sort_dir: 'desc',
  }

  const history = useQuery({
    queryKey: qcTemplateKeys.versions(template.code),
    queryFn: () => api.getPage<QcTemplateRow>('/quality/templates', { params }),
    staleTime: staleTimes.master,
  })

  if (history.isLoading) {
    return <Skeleton className="h-16 w-full" aria-hidden />
  }
  if (history.error) {
    return (
      <p className="text-sm text-muted-foreground">
        The revision history could not be loaded: {history.error.message}
      </p>
    )
  }

  const versions = (history.data?.data ?? []).filter((row) => row.code === template.code)

  if (versions.length === 0) {
    // Only reachable if the search page filled up with other codes before
    // reaching this one, since the version being shown is itself a row of it.
    return (
      <p className="rounded-md border border-dashed p-4 text-center text-sm text-muted-foreground">
        No other revision of {template.code} could be found.
      </p>
    )
  }

  return (
    <ul className="divide-y rounded-md border">
      {versions.map((version) => (
        <li key={version.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
          {/* Below `sm` the version and name take a whole line (basis-full)
              with the date beneath the name, and the badge and control drop to
              a second line; from `sm` up the basis returns to 0 and the row is
              the single line it always was. */}
          <span className="flex min-w-0 flex-1 basis-full items-center gap-3 sm:basis-0">
            <span className="w-9 shrink-0 tabular text-sm font-medium">v{version.version}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">{version.name}</span>
              <span className="block text-xs text-muted-foreground sm:hidden">
                {formatDateTime(version.updated_at)}
              </span>
            </span>
          </span>
          {version.is_active ? (
            <Badge variant="success">In force</Badge>
          ) : (
            <Badge variant="muted">Superseded</Badge>
          )}
          <span className="hidden w-40 shrink-0 text-xs text-muted-foreground sm:block">
            {formatDateTime(version.updated_at)}
          </span>
          {version.id === template.id ? (
            <span className="ml-auto w-16 shrink-0 text-right text-xs text-muted-foreground">
              Shown
            </span>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto w-16 shrink-0"
              onClick={() => onOpenVersion(version.id)}
            >
              Open
            </Button>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * One specification, in full: what it covers, what it measures, and every
 * revision it has been through.
 *
 * Reading is `P.QC_VIEW`, which is what `GET /quality/templates/{id}` declares;
 * the three actions in the footer are `P.QC_TEMPLATE_MANAGE`, which is what
 * their endpoints declare. They are separate rights on purpose - an inspector
 * reads the specification constantly and writes it never.
 */
export function QcTemplateDetailDialog({
  templateId,
  onOpenChange,
  onOpenVersion,
  onAmend,
  onFork,
  onRetire,
}: {
  templateId: string | null
  onOpenChange: (open: boolean) => void
  onOpenVersion: (id: string) => void
  onAmend: (id: string) => void
  onFork: (id: string) => void
  onRetire: (template: QcTemplateDetail) => void
}) {
  const { can } = useAuth()
  const canManage = can(P.QC_TEMPLATE_MANAGE)

  const detail = useQuery({
    queryKey: qcTemplateKeys.detail(templateId ?? ''),
    queryFn: () => api.get<QcTemplateDetail>(`/quality/templates/${templateId}`),
    enabled: templateId !== null,
    staleTime: staleTimes.master,
  })

  const template = detail.data
  const parameters = template?.parameters ?? []

  return (
    <Dialog open={templateId !== null} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {template
              ? `${templateTitle(template)} - ${template.name}`
              : 'Inspection specification'}
          </DialogTitle>
          <DialogDescription>
            An inspection records against one version of a specification, so this check sheet is the
            statement every result raised under it was judged by.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {detail.isLoading && (
            <div className="space-y-3" aria-hidden>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-24 w-full" />
            </div>
          )}

          {!detail.isLoading && detail.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this specification</AlertTitle>
              <AlertDescription>{detail.error.message}</AlertDescription>
            </Alert>
          )}

          {template && (
            <>
              <DescriptionList
                columns={2}
                items={[
                  { label: 'Stage', value: humanise(template.qc_stage) },
                  {
                    label: 'Applies to',
                    value:
                      template.item_id !== null ? (
                        // The row carries ids and no codes or names, so this
                        // links to the item master rather than rendering a
                        // resolved label the contract does not send.
                        <Link
                          to={`/items/${template.item_id}`}
                          className="font-mono text-xs hover:underline"
                        >
                          {template.item_id.slice(0, 8)}
                        </Link>
                      ) : template.item_category_id !== null ? (
                        <span className="font-mono text-xs">
                          {template.item_category_id.slice(0, 8)}
                        </span>
                      ) : (
                        SCOPE_LABEL.STAGE
                      ),
                  },
                  { label: 'Sampling', value: samplingLabel(template) },
                  {
                    label: 'Selected for new inspections',
                    value: template.is_active ? (
                      <Badge variant="success">In force</Badge>
                    ) : (
                      <Badge variant="muted">Superseded or held back</Badge>
                    ),
                  },
                  {
                    label: 'Characteristics',
                    value: `${parameters.length} (${criticalCount(parameters)} critical)`,
                  },
                  { label: 'Last changed', value: formatDateTime(template.updated_at) },
                ]}
              />

              {template.description && (
                <p className="whitespace-pre-line text-sm text-muted-foreground">
                  {template.description}
                </p>
              )}

              {criticalCount(parameters) > 0 && (
                <Alert variant="warning">
                  <AlertTitle>
                    {criticalCount(parameters) === 1
                      ? 'One characteristic can condemn the lot on its own'
                      : `${criticalCount(parameters)} characteristics can condemn the lot on their own`}
                  </AlertTitle>
                  <AlertDescription>
                    A critical failure rejects the consignment whatever the rest of the sheet says,
                    which is why a critical characteristic can never be optional.
                  </AlertDescription>
                </Alert>
              )}

              <div className="space-y-2">
                <h3 className="text-sm font-medium">Check sheet</h3>
                <ParameterTable parameters={parameters} />
              </div>

              <div className="space-y-2">
                <h3 className="text-sm font-medium">Revisions</h3>
                <VersionHistory template={template} onOpenVersion={onOpenVersion} />
              </div>
            </>
          )}
        </DialogBody>

        {template && canManage && (
          <DialogFooter>
            <Button variant="outline" onClick={() => onRetire(template)}>
              <Trash2 />
              Retire
            </Button>
            <Button variant="outline" onClick={() => onAmend(template.id)}>
              <Pencil />
              Amend this version
            </Button>
            <Button onClick={() => onFork(template.id)}>
              <GitBranch />
              New version
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

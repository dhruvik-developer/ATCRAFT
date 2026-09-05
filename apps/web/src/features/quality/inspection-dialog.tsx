/**
 * The inspection sheet.
 *
 * This is the screen quality actually works in, so it is built around the two
 * things that go wrong when inspection lives on paper:
 *
 * 1. **A reading that breaches the specification is not obvious.** Every
 *    numeric parameter shows its limits next to the box and turns destructive
 *    the moment the value leaves the band - the inspector is told while the
 *    part is still in their hand, not after the lot has been dispositioned.
 * 2. **The five disposition buckets do not add up.** Accepted, rejected,
 *    rework, hold and scrap must account for exactly the inspected quantity.
 *    A running remainder is shown, and the form refuses to submit until it is
 *    zero, because a lot that is 3 pieces short in the system is 3 pieces of
 *    unexplained stock on the floor.
 *
 * A critical parameter is different in kind, not degree: one failure condemns
 * the lot. The dialog says so before anything is entered, and once a critical
 * reading fails it stops offering "accepted" as a decision at all.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, Image as ImageIcon, ShieldAlert, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import { DescriptionList, FieldError, StatusBadge } from '@/components/shared'
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
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

import {
  NUMERIC_READING,
  QC_PARAMETER_TYPES,
  QUANTITY_INPUT,
  REJECTION_REASON_MAX_LENGTH,
  hasFailed,
  isOutOfSpec,
  referenceLink,
  specLabel,
  type QcDecisionResponse,
  type QcInspectionDetail,
  type QcInspectionRow,
  type QcParameterEvaluation,
  type QcResultsResponse,
  type UploadedDocument,
} from './quality-types'

// ------------------------------------------------------------------ form ----

const parameterSchema = z.object({
  parameter_id: z.string(),
  sequence: z.number(),
  parameter_name: z.string(),
  parameter_type: z.enum(QC_PARAMETER_TYPES),
  unit: z.string().nullable(),
  min_value: z.string().nullable(),
  max_value: z.string().nullable(),
  target_value: z.string().nullable(),
  allowed_values: z.array(z.string()).nullable(),
  is_critical: z.boolean(),
  is_mandatory: z.boolean(),

  value: z.string(),
  remark: z.string().trim().max(255, 'At most 255 characters'),
  document_id: z.string().nullable(),
  /** Known only for a photo attached in this session; the wire carries an id. */
  document_name: z.string().nullable(),
})

const schema = z
  .object({
    /** Carried in the form so the sum rule is expressible in the schema. */
    inspected_quantity: z.string(),
    /** False for an inspector who may record readings but not decide. */
    decide: z.boolean(),
    /**
     * False for a decider who may not record readings. `POST .../results`
     * needs `qc.inspect`, so a reading typed by an account without it would be
     * refused before the decision that account is entitled to record is ever
     * reached - the inputs are read-only and their rules are not applied.
     */
    record: z.boolean(),
    /**
     * Required by the API on every decision - it is the audit reason, not a
     * free-text note, and the request is refused without it.
     *
     * `DecideRequest` bounds it to 3..255 characters. The form used to allow up
     * to 500 and to accept a single character, so both ends produced a 422 on a
     * field the operator had filled in and no message anywhere.
     */
    reason: z.string().trim().max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
    rejection_reason: z
      .string()
      .trim()
      .max(REJECTION_REASON_MAX_LENGTH, `At most ${REJECTION_REASON_MAX_LENGTH} characters`),
    accepted_quantity: z.string().trim().regex(QUANTITY_INPUT, 'Up to 4 decimal places'),
    rejected_quantity: z.string().trim().regex(QUANTITY_INPUT, 'Up to 4 decimal places'),
    rework_quantity: z.string().trim().regex(QUANTITY_INPUT, 'Up to 4 decimal places'),
    hold_quantity: z.string().trim().regex(QUANTITY_INPUT, 'Up to 4 decimal places'),
    scrap_quantity: z.string().trim().regex(QUANTITY_INPUT, 'Up to 4 decimal places'),
    remarks: z.string().trim().max(2000, 'At most 2000 characters'),
    parameters: z.array(parameterSchema),
  })
  .superRefine((values, ctx) => {
    // Readings are checked only when this account can change them: an error on
    // a read-only input is one the user has no way to clear. The API still
    // refuses a decision whose mandatory readings are missing, and that
    // rejection reaches the screen through the mutation cache's toast.
    if (values.record) {
      values.parameters.forEach((parameter, index) => {
        const reading = parameter.value.trim()

        if (parameter.is_mandatory && reading === '') {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['parameters', index, 'value'],
            message: 'This reading is required',
          })
          return
        }
        if (reading === '') return

        if (parameter.parameter_type === 'NUMERIC' && !NUMERIC_READING.test(reading)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['parameters', index, 'value'],
            message: 'Enter a measurement, up to 6 decimal places',
          })
        }

        if (
          parameter.parameter_type === 'SELECT' &&
          parameter.allowed_values !== null &&
          !parameter.allowed_values.includes(reading)
        ) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['parameters', index, 'value'],
            message: 'Choose one of the listed answers',
          })
        }
      })
    }

    // Everything below is the disposition, which only a decision submits.
    if (!values.decide) return

    if (values.reason.length < REASON_MIN_LENGTH) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['reason'],
        message:
          values.reason === ''
            ? 'Say why this decision is being taken - it is recorded in the audit trail'
            : `The API records this in the audit trail and needs at least ${REASON_MIN_LENGTH} characters`,
      })
    }

    // `rejection_reason` is optional, but the API bounds it to 3..2000 when it
    // is present - an empty one is omitted rather than sent as a short string.
    if (values.rejection_reason !== '' && values.rejection_reason.length < REASON_MIN_LENGTH) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['rejection_reason'],
        message: `At least ${REASON_MIN_LENGTH} characters, or leave it empty`,
      })
    }

    const inspected = toDecimal(values.inspected_quantity)
    const total = toDecimal(values.accepted_quantity)
      .plus(toDecimal(values.rejected_quantity))
      .plus(toDecimal(values.rework_quantity))
      .plus(toDecimal(values.hold_quantity))
      .plus(toDecimal(values.scrap_quantity))

    if (!total.equals(inspected)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['accepted_quantity'],
        message: `The five quantities must add up to the ${formatQuantity(
          values.inspected_quantity,
        )} inspected - they currently total ${formatQuantity(total.toFixed())}`,
      })
    }

    const criticalFailure = values.parameters.some(
      (parameter) => parameter.is_critical && hasFailed(parameter, parameter.value),
    )

    if (criticalFailure) {
      if (toDecimal(values.accepted_quantity).greaterThan(0)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['accepted_quantity'],
          message: 'A critical parameter failed - none of this lot can be accepted',
        })
      }
    }

    const condemned = toDecimal(values.rejected_quantity).plus(toDecimal(values.scrap_quantity))
    if (condemned.greaterThan(0) && values.remarks.trim() === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['remarks'],
        message: 'Say why material was rejected or scrapped - this drives the vendor rating',
      })
    }
  })

type InspectionFormValues = z.infer<typeof schema>

const BOOLEAN_ANSWERS = [
  { value: 'PASS', label: 'Pass' },
  { value: 'FAIL', label: 'Fail' },
] as const

const DISPOSITION_FIELDS = [
  { name: 'accepted_quantity', label: 'Accepted', hint: 'Becomes free stock' },
  { name: 'rejected_quantity', label: 'Rejected', hint: 'Returned to the vendor' },
  { name: 'rework_quantity', label: 'Rework', hint: 'Goes back for correction' },
  { name: 'hold_quantity', label: 'Hold', hint: 'Stays in QC hold' },
  { name: 'scrap_quantity', label: 'Scrap', hint: 'Written off' },
] as const

function emptyValues(): InspectionFormValues {
  return {
    inspected_quantity: '0',
    decide: false,
    record: false,
    reason: '',
    rejection_reason: '',
    accepted_quantity: '',
    rejected_quantity: '',
    rework_quantity: '',
    hold_quantity: '',
    scrap_quantity: '',
    remarks: '',
    parameters: [],
  }
}

/**
 * A stored measurement, back into the single string the form edits.
 *
 * The API stores a reading in one of three typed columns according to the
 * parameter's type rather than as one text value, so the right column has to be
 * picked on the way in and set on the way out.
 */
function readingOf(
  parameterType: string,
  previous:
    | {
        measured_value_numeric: string | null
        measured_value_bool: boolean | null
        measured_value_text: string | null
      }
    | undefined,
): string {
  if (!previous) return ''
  if (parameterType === 'NUMERIC') return previous.measured_value_numeric ?? ''
  if (parameterType === 'BOOLEAN') {
    if (previous.measured_value_bool === null) return ''
    return previous.measured_value_bool ? 'PASS' : 'FAIL'
  }
  return previous.measured_value_text ?? ''
}

function valuesFrom(
  detail: QcInspectionDetail,
  canDecide: boolean,
  canInspect: boolean,
): InspectionFormValues {
  const recorded = new Map((detail.results ?? []).map((result) => [result.parameter_id, result]))

  return {
    inspected_quantity: detail.inspected_quantity,
    decide: canDecide,
    record: canInspect,
    reason: '',
    rejection_reason: '',
    // Pre-filling "everything accepted" would be a nudge towards passing the
    // lot without measuring it, so the buckets start empty.
    accepted_quantity: '',
    rejected_quantity: '',
    rework_quantity: '',
    hold_quantity: '',
    scrap_quantity: '',
    remarks: '',
    parameters: (detail.parameters ?? []).map((parameter) => {
      const previous = recorded.get(parameter.id)
      return {
        parameter_id: parameter.id,
        sequence: parameter.sequence,
        parameter_name: parameter.parameter_name,
        parameter_type: parameter.parameter_type,
        unit: parameter.unit,
        min_value: parameter.min_value,
        max_value: parameter.max_value,
        target_value: parameter.target_value,
        allowed_values: parameter.allowed_values,
        is_critical: parameter.is_critical,
        is_mandatory: parameter.is_mandatory,
        value: readingOf(parameter.parameter_type, previous),
        remark: previous?.remark ?? '',
        document_id: previous?.photo_document_id ?? null,
        // The wire carries only the document id, so a photo attached in an
        // earlier session shows as attached without its filename.
        document_name: null,
      }
    }),
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

// ---------------------------------------------------------------- dialog ----

export function InspectionDialog({
  inspection,
  open,
  onOpenChange,
}: {
  inspection: QcInspectionRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [uploadingIndex, setUploadingIndex] = useState<number | null>(null)
  // The API's verdict on the readings so far, kept after a save-without-decide
  // so the inspector can see it before dispositioning.
  const [evaluation, setEvaluation] = useState<QcParameterEvaluation | null>(null)

  // Gating is UX only - the API authorises the results post and the decision
  // independently of anything this component believes.
  const canDecide = can(P.QC_DECIDE)
  // `POST .../results` is `qc.inspect` and `POST .../decide` is `qc.decide`;
  // the dialog opens on either, so each half is gated on its own right.
  const canInspect = can(P.QC_INSPECT)
  const canUpload = can(P.DOCUMENT_UPLOAD)

  const inspectionId = inspection?.id ?? ''

  const detail = useQuery({
    queryKey: queryKeys.quality.detail(inspectionId),
    queryFn: () => api.get<QcInspectionDetail>(`/quality/inspections/${inspectionId}`),
    enabled: open && Boolean(inspectionId),
    staleTime: staleTimes.transactional,
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setValue,
    setError,
    watch,
    formState: { errors },
  } = useForm<InspectionFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  const { fields } = useFieldArray({ control, name: 'parameters' })

  // Rebuilt only when a different inspection's template arrives - resetting on
  // every background refetch would wipe readings already typed.
  const loadedId = detail.data?.id ?? null
  useEffect(() => {
    if (!detail.data) return
    reset(valuesFrom(detail.data, canDecide, canInspect))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedId, canDecide, canInspect, reset])

  useEffect(() => {
    if (open) return
    reset(emptyValues())
    setUploadingIndex(null)
    setEvaluation(null)
  }, [open, reset])

  const uploadPhoto = useMutation({
    mutationFn: async ({ index, file }: { index: number; file: File }) => {
      const body = new FormData()
      body.append('file', file)
      body.append('entity_type', 'qc_inspection')
      body.append('entity_id', inspectionId)
      // The upload form field is `category`, from a fixed vocabulary; there is
      // no free-text `document_type`.
      body.append('category', 'ATTACHMENT')
      const document = await api.post<UploadedDocument>('/documents', body)
      return { index, document }
    },
    onSuccess: ({ index, document }) => {
      setValue(`parameters.${index}.document_id`, document.id, { shouldDirty: true })
      setValue(`parameters.${index}.document_name`, document.file_name, { shouldDirty: true })
      toast.success('Photo attached', `${document.file_name} is attached to this parameter.`)
    },
    onSettled: () => setUploadingIndex(null),
  })

  const submit = useMutation({
    mutationFn: async (values: InspectionFormValues) => {
      const measurements = values.parameters
        .filter(
          (parameter) =>
            parameter.value.trim() !== '' ||
            parameter.remark.trim() !== '' ||
            parameter.document_id !== null,
        )
        .map((parameter) => {
          const reading = parameter.value.trim()
          return {
            parameter_id: parameter.parameter_id,
            // One typed column per parameter type, not a single text value.
            measured_value_numeric:
              parameter.parameter_type === 'NUMERIC' && reading !== '' ? reading : null,
            measured_value_bool:
              parameter.parameter_type === 'BOOLEAN' && reading !== '' ? reading === 'PASS' : null,
            measured_value_text:
              parameter.parameter_type === 'TEXT' || parameter.parameter_type === 'SELECT'
                ? orNull(reading)
                : null,
            remark: orNull(parameter.remark),
            photo_document_id: parameter.document_id,
          }
        })

      /**
       * `RecordResultsRequest.measurements` is `minItems: 1`.
       *
       * An inspection raised without a template has no parameters, so there is
       * nothing to measure and this list is empty - and the panel above tells
       * the inspector, correctly, that they can still record a disposition.
       * Posting `measurements: []` anyway earns a 422 whose field path is
       * `measurements`, which no control on this form renders, and which the
       * mutation cache suppresses precisely because a 422 with field errors is
       * assumed to be shown inline. The decision then never happens and nothing
       * appears on screen. So the readings are posted only when there are
       * readings; otherwise the disposition goes straight to `/decide`.
       *
       * The same applies to an account holding `qc.decide` without
       * `qc.inspect`: `/results` would answer 403 and the decision it is
       * entitled to record would never be reached, so the readings post is
       * skipped and the API's own mandatory-reading rule is the backstop.
       */
      const saved =
        values.record && measurements.length > 0
          ? await api.post<QcResultsResponse>(`/quality/inspections/${inspectionId}/results`, {
              measurements,
              remarks: orNull(values.remarks),
            })
          : null

      if (!values.decide) {
        if (!saved) {
          // Nothing measured and no decision asked for: there is no request to
          // make, and saying so beats a silent no-op.
          return { decided: false as const, code: inspection?.code ?? '', evaluation: null }
        }
        return {
          decided: false as const,
          code: inspection?.code ?? '',
          evaluation: saved.evaluation,
        }
      }

      // No `result` on the wire: the API derives the verdict from how the
      // quantity was split, and `reason` is mandatory.
      const decision = await api.post<QcDecisionResponse>(
        `/quality/inspections/${inspectionId}/decide`,
        {
          reason: values.reason,
          // Quantities stay decimal strings on the wire; the API parses them.
          accepted_quantity: values.accepted_quantity.trim() || '0',
          rejected_quantity: values.rejected_quantity.trim() || '0',
          rework_quantity: values.rework_quantity.trim() || '0',
          hold_quantity: values.hold_quantity.trim() || '0',
          scrap_quantity: values.scrap_quantity.trim() || '0',
          rejection_reason: orNull(values.rejection_reason),
          remarks: orNull(values.remarks),
        },
      )
      return { decided: true as const, code: decision.inspection.code, decision }
    },
    onSuccess: (outcome) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })

      if (!outcome.decided) {
        if (!outcome.evaluation) {
          toast.warning(
            'Nothing to save',
            'This inspection has no parameters to measure. Record the disposition to decide it.',
          )
          return
        }
        // The server evaluates the readings and says what it would do with the
        // lot; that is worth more to the inspector than "saved".
        const { recommended_result, is_conclusive } = outcome.evaluation
        setEvaluation(outcome.evaluation)
        toast.success(
          success.saved('Readings'),
          is_conclusive
            ? `Recorded. On these readings the lot reads as ${humanise(recommended_result)}.`
            : 'Recorded. Not every mandatory parameter has a reading yet.',
        )
        return
      }

      // A decision moves material out of QC hold into free stock, damaged or
      // scrap, so balances and what can be promised are both stale now.
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      // The receipt and its purchase order carry the QC status too.
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })

      const { decision, code } = outcome
      // What actually moved, which is not the same as what was typed: this is
      // the line the inspector needs to reconcile against the floor.
      const moved = [
        `${formatQuantity(decision.released_quantity)} released`,
        `${formatQuantity(decision.scrapped_quantity)} scrapped`,
        `${formatQuantity(decision.still_on_hold_quantity)} still on hold`,
      ].join(', ')
      toast.success(
        `${code} decided as ${humanise(decision.inspection.result)}`,
        decision.return_required ? `${moved}. This lot has to go back to the vendor.` : `${moved}.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const parameters = watch('parameters')
  const criticalFailures = parameters.filter(
    (parameter) => parameter.is_critical && hasFailed(parameter, parameter.value),
  )
  const hasCriticalParameters = parameters.some((parameter) => parameter.is_critical)

  const inspectedQuantity = detail.data?.inspected_quantity ?? inspection?.inspected_quantity ?? '0'
  const dispositionTotal = toDecimal(watch('accepted_quantity'))
    .plus(toDecimal(watch('rejected_quantity')))
    .plus(toDecimal(watch('rework_quantity')))
    .plus(toDecimal(watch('hold_quantity')))
    .plus(toDecimal(watch('scrap_quantity')))
  const remainder = toDecimal(inspectedQuantity).minus(dispositionTotal)

  const reference = inspection
    ? referenceLink(inspection.reference_type, inspection.reference_id, inspection.reference_code)
    : null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{inspection ? `Inspect ${inspection.code}` : 'Inspection'}</DialogTitle>
          <DialogDescription>
            Work through the template, then split the inspected quantity across the five
            dispositions. Nothing moves in stock until the decision is submitted.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => submit.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            {inspection && (
              <div className="rounded-lg border bg-muted/30 p-4">
                <DescriptionList
                  columns={4}
                  items={[
                    {
                      label: 'Stage',
                      value: <StatusBadge status={inspection.qc_stage} />,
                    },
                    {
                      label: 'Item',
                      // The row carries an id only - no code, no name.
                      value: (
                        <Link
                          to={`/items/${inspection.item_id}`}
                          className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
                        >
                          {inspection.item_id.slice(0, 8)}
                          <ExternalLink className="h-3 w-3" aria-hidden />
                        </Link>
                      ),
                    },
                    {
                      label: 'Inspected quantity',
                      value: (
                        <span className="tabular font-medium">
                          {formatQuantity(inspectedQuantity)}
                        </span>
                      ),
                    },
                    {
                      label: 'Against',
                      value: reference ? (
                        <Link
                          to={reference}
                          className="inline-flex items-center gap-1 hover:underline"
                        >
                          {inspection.reference_code ?? humanise(inspection.reference_type)}
                          <ExternalLink className="h-3 w-3" aria-hidden />
                        </Link>
                      ) : (
                        (inspection.reference_code ?? '-')
                      ),
                    },
                    {
                      label: 'Sample size',
                      value: (
                        <span className="tabular">{formatQuantity(inspection.sample_size)}</span>
                      ),
                    },
                    {
                      label: 'Batch',
                      value: inspection.batch_id ? (
                        <span className="font-mono text-xs">{inspection.batch_id.slice(0, 8)}</span>
                      ) : (
                        'Not batch tracked'
                      ),
                    },
                    { label: 'Raised', value: formatDate(inspection.created_at) },
                    {
                      label: 'Template',
                      value:
                        inspection.template_id === null
                          ? 'None'
                          : `Version ${inspection.template_version ?? 1}`,
                    },
                  ]}
                />
              </div>
            )}

            {detail.isLoading && (
              <div className="space-y-2" aria-hidden>
                <Skeleton className="h-24 w-full" />
                <Skeleton className="h-24 w-full" />
                <Skeleton className="h-24 w-full" />
              </div>
            )}

            {!detail.isLoading && detail.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load the inspection template</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            )}

            {!detail.isLoading && !detail.error && fields.length === 0 && (
              <Alert variant="warning">
                <AlertTitle>No template parameters</AlertTitle>
                <AlertDescription>
                  This inspection has no parameters attached, so there is nothing to measure. You
                  can still record a disposition and a remark.
                </AlertDescription>
              </Alert>
            )}

            {hasCriticalParameters && criticalFailures.length === 0 && (
              <Alert variant="warning">
                <ShieldAlert aria-hidden />
                <AlertTitle>This template has critical parameters</AlertTitle>
                <AlertDescription>
                  A critical parameter is pass-or-fail for the whole lot. Recording a failure
                  against one of them rejects the entire inspected quantity - none of it can be
                  accepted, however good the other readings are.
                </AlertDescription>
              </Alert>
            )}

            {evaluation && (
              <Alert variant={evaluation.has_critical_failure ? 'destructive' : 'info'}>
                <AlertTitle>
                  On the saved readings this lot reads as {humanise(evaluation.recommended_result)}
                </AlertTitle>
                <AlertDescription>
                  {evaluation.critical_failures && evaluation.critical_failures.length > 0 && (
                    <p>Critical failures: {evaluation.critical_failures.join(', ')}.</p>
                  )}
                  {evaluation.failures && evaluation.failures.length > 0 && (
                    <p>Out of specification: {evaluation.failures.join(', ')}.</p>
                  )}
                  {evaluation.missing_mandatory && evaluation.missing_mandatory.length > 0 && (
                    <p>Still to measure: {evaluation.missing_mandatory.join(', ')}.</p>
                  )}
                  {!evaluation.is_conclusive && (
                    <p>This is not yet conclusive - the lot cannot be judged on these readings.</p>
                  )}
                </AlertDescription>
              </Alert>
            )}

            {criticalFailures.length > 0 && (
              <Alert variant="destructive">
                <AlertTriangle aria-hidden />
                <AlertTitle>
                  Critical failure on{' '}
                  {criticalFailures.map((parameter) => parameter.parameter_name).join(', ')}
                </AlertTitle>
                <AlertDescription>
                  The whole lot is rejected. Accepted must be zero and the decision must be
                  rejected, scrapped or held.
                </AlertDescription>
              </Alert>
            )}

            {/* ------------------------------------------------ parameters -- */}
            {fields.length > 0 && (
              <div className="space-y-3">
                <h3 className="text-sm font-medium">Parameters</h3>

                {fields.map((field, index) => {
                  const parameter = parameters[index]
                  const value = parameter?.value ?? ''
                  const outOfSpec = parameter ? isOutOfSpec(parameter, value) : false
                  const failed = parameter ? hasFailed(parameter, value) : false
                  const spec = specLabel(field)
                  // A photo attached in an earlier session comes back as an id
                  // with no filename, so it is labelled generically.
                  const attachedId = parameter?.document_id ?? null
                  const attachedName =
                    parameter?.document_name ?? (attachedId ? 'Photo attached' : null)

                  return (
                    <div
                      key={field.id}
                      className={cn(
                        'rounded-lg border p-3',
                        field.is_critical && 'border-l-4 border-l-destructive',
                        failed && 'border-destructive bg-destructive/5',
                      )}
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="font-medium">
                            <span className="tabular mr-2 text-muted-foreground">
                              {field.sequence}
                            </span>
                            {field.parameter_name}
                          </p>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {field.is_critical && (
                            <Badge variant="destructive">
                              <ShieldAlert className="h-3 w-3" aria-hidden />
                              Critical
                            </Badge>
                          )}
                          {field.is_mandatory && <Badge variant="muted">Required</Badge>}
                          {spec && <Badge variant="secondary">Spec {spec}</Badge>}
                          {field.target_value !== null && (
                            <Badge variant="outline">Target {field.target_value}</Badge>
                          )}
                        </div>
                      </div>

                      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        <div>
                          <Label htmlFor={`qc-value-${index}`} required={field.is_mandatory}>
                            {field.parameter_type === 'NUMERIC' ? 'Measured value' : 'Observation'}
                          </Label>

                          {field.parameter_type === 'NUMERIC' && (
                            <>
                              <Input
                                id={`qc-value-${index}`}
                                {...register(`parameters.${index}.value`)}
                                inputMode="decimal"
                                readOnly={!canInspect}
                                autoComplete="off"
                                className={cn('mt-1.5', outOfSpec && 'border-destructive')}
                                aria-invalid={
                                  outOfSpec || Boolean(errors.parameters?.[index]?.value)
                                }
                                aria-describedby={`qc-spec-${index}`}
                              />
                              <p
                                id={`qc-spec-${index}`}
                                className={cn(
                                  'mt-1 text-xs',
                                  outOfSpec
                                    ? 'font-medium text-destructive'
                                    : 'text-muted-foreground',
                                )}
                              >
                                {outOfSpec
                                  ? `Out of specification - allowed ${spec ?? 'range'}`
                                  : spec
                                    ? `Allowed ${spec}`
                                    : 'No limits set on this parameter'}
                              </p>
                            </>
                          )}

                          {field.parameter_type === 'BOOLEAN' && (
                            <Controller
                              control={control}
                              name={`parameters.${index}.value`}
                              render={({ field: control_ }) => (
                                <div
                                  role="radiogroup"
                                  aria-label={field.parameter_name}
                                  className="mt-1.5 inline-flex rounded-md border border-input p-0.5"
                                >
                                  {BOOLEAN_ANSWERS.map((answer) => (
                                    <label
                                      key={answer.value}
                                      className={cn(
                                        'rounded-sm px-4 py-1.5 text-sm font-medium transition-colors',
                                        canInspect
                                          ? 'cursor-pointer'
                                          : 'cursor-not-allowed opacity-70',
                                        'focus-within:ring-2 focus-within:ring-ring',
                                        control_.value === answer.value
                                          ? answer.value === 'FAIL'
                                            ? 'bg-destructive text-destructive-foreground'
                                            : 'bg-success text-success-foreground'
                                          : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                                      )}
                                    >
                                      <input
                                        type="radio"
                                        className="sr-only"
                                        name={`qc-boolean-${index}`}
                                        value={answer.value}
                                        checked={control_.value === answer.value}
                                        disabled={!canInspect}
                                        onChange={() => control_.onChange(answer.value)}
                                        onBlur={control_.onBlur}
                                      />
                                      {answer.label}
                                    </label>
                                  ))}
                                </div>
                              )}
                            />
                          )}

                          {field.parameter_type === 'SELECT' && (
                            <Controller
                              control={control}
                              name={`parameters.${index}.value`}
                              render={({ field: control_ }) => (
                                <Select
                                  value={control_.value}
                                  onValueChange={control_.onChange}
                                  disabled={!canInspect}
                                >
                                  <SelectTrigger
                                    id={`qc-value-${index}`}
                                    className={cn('mt-1.5', failed && 'border-destructive')}
                                    aria-label={field.parameter_name}
                                  >
                                    <SelectValue placeholder="Choose an answer" />
                                  </SelectTrigger>
                                  <SelectContent>
                                    {(field.allowed_values ?? []).map((option) => (
                                      <SelectItem key={option} value={option}>
                                        {option}
                                      </SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                              )}
                            />
                          )}

                          {field.parameter_type === 'TEXT' && (
                            <Input
                              id={`qc-value-${index}`}
                              {...register(`parameters.${index}.value`)}
                              readOnly={!canInspect}
                              autoComplete="off"
                              className="mt-1.5"
                              aria-invalid={Boolean(errors.parameters?.[index]?.value)}
                            />
                          )}

                          <FieldError message={errors.parameters?.[index]?.value?.message} />
                        </div>

                        <div>
                          <Label htmlFor={`qc-remarks-${index}`}>Remark</Label>
                          <Input
                            id={`qc-remarks-${index}`}
                            {...register(`parameters.${index}.remark`)}
                            readOnly={!canInspect}
                            autoComplete="off"
                            className="mt-1.5"
                            aria-invalid={Boolean(errors.parameters?.[index]?.remark)}
                          />
                          <FieldError message={errors.parameters?.[index]?.remark?.message} />
                        </div>

                        {/* photo_document_id is only persisted through the
                            results post, so the control needs both rights. */}
                        {canUpload && canInspect && (
                          <div>
                            <Label htmlFor={`qc-photo-${index}`}>Photo</Label>
                            {attachedName ? (
                              <div className="mt-1.5 flex h-9 items-center gap-2 rounded-md border border-input px-3 text-sm">
                                <ImageIcon
                                  className="h-4 w-4 shrink-0 text-muted-foreground"
                                  aria-hidden
                                />
                                <span className="min-w-0 flex-1 truncate">{attachedName}</span>
                                <button
                                  type="button"
                                  className="rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                  aria-label={`Remove the photo on ${field.parameter_name}`}
                                  onClick={() => {
                                    setValue(`parameters.${index}.document_id`, null)
                                    setValue(`parameters.${index}.document_name`, null)
                                  }}
                                >
                                  <X className="h-4 w-4" />
                                </button>
                              </div>
                            ) : (
                              <Input
                                id={`qc-photo-${index}`}
                                type="file"
                                accept="image/*"
                                className="mt-1.5 py-1.5 text-xs"
                                disabled={uploadingIndex !== null}
                                onChange={(event) => {
                                  const file = event.target.files?.[0]
                                  // The input is cleared either way: the value the
                                  // form keeps is the uploaded document's id.
                                  event.target.value = ''
                                  if (!file) return
                                  setUploadingIndex(index)
                                  uploadPhoto.mutate({ index, file })
                                }}
                              />
                            )}
                            {uploadingIndex === index && (
                              <p className="mt-1 text-xs text-muted-foreground" role="status">
                                Uploading...
                              </p>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {/* ----------------------------------------------- disposition -- */}
            {canDecide && (
              <div className="space-y-3 rounded-lg border p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-medium">Disposition</h3>
                  <p
                    className={cn(
                      'text-sm tabular',
                      remainder.isZero() ? 'text-success' : 'text-warning',
                    )}
                    role="status"
                  >
                    {remainder.isZero()
                      ? `All ${formatQuantity(inspectedQuantity)} accounted for`
                      : remainder.greaterThan(0)
                        ? `${formatQuantity(remainder.toFixed())} still to allocate`
                        : `${formatQuantity(remainder.abs().toFixed())} over the inspected quantity`}
                  </p>
                </div>

                <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
                  {DISPOSITION_FIELDS.map((entry) => (
                    <div key={entry.name}>
                      <Label htmlFor={`qc-${entry.name}`}>{entry.label}</Label>
                      <Input
                        id={`qc-${entry.name}`}
                        {...register(entry.name)}
                        inputMode="decimal"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors[entry.name])}
                        aria-describedby={`qc-${entry.name}-hint`}
                      />
                      <p
                        id={`qc-${entry.name}-hint`}
                        className="mt-1 text-xs text-muted-foreground"
                      >
                        {entry.hint}
                      </p>
                      <FieldError message={errors[entry.name]?.message} />
                    </div>
                  ))}
                </div>

                {/* There is no decision to choose: the API derives the result
                    from how the quantity was split above and returns it. What
                    it needs instead is the audited reason for the split. */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="qc-reason" required>
                      Reason for this decision
                    </Label>
                    <Input
                      id="qc-reason"
                      {...register('reason')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.reason)}
                      aria-describedby="qc-reason-hint"
                    />
                    <p id="qc-reason-hint" className="mt-1 text-xs text-muted-foreground">
                      Recorded against this inspection in the audit trail.
                    </p>
                    <FieldError message={errors.reason?.message} />
                  </div>

                  <div>
                    <Label htmlFor="qc-rejection-reason">Rejection reason</Label>
                    <Input
                      id="qc-rejection-reason"
                      {...register('rejection_reason')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.rejection_reason)}
                      aria-describedby="qc-rejection-reason-hint"
                    />
                    <p id="qc-rejection-reason-hint" className="mt-1 text-xs text-muted-foreground">
                      The defect itself. Drives the vendor rating.
                    </p>
                    <FieldError message={errors.rejection_reason?.message} />
                  </div>
                </div>

                <div>
                  <Label htmlFor="qc-remarks">Inspection remarks</Label>
                  <Textarea
                    id="qc-remarks"
                    {...register('remarks')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.remarks)}
                  />
                  <FieldError message={errors.remarks?.message} />
                </div>
              </div>
            )}

            {canDecide && !canInspect && fields.length > 0 && (
              <Alert variant="info">
                <AlertTitle>Readings are recorded by an inspector</AlertTitle>
                <AlertDescription>
                  Your account can disposition this lot but not record its measurements - that needs
                  the qc.inspect right. The readings above are shown as recorded; only the decision
                  below is sent.
                </AlertDescription>
              </Alert>
            )}

            {!canDecide && (
              <Alert variant="info">
                <AlertTitle>Readings only</AlertTitle>
                <AlertDescription>
                  Your account can record measurements but not decide a lot. Somebody with the
                  decision right has to disposition it before the material leaves QC hold.
                </AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submit.isPending}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              loading={submit.isPending}
              disabled={detail.isLoading || Boolean(detail.error)}
            >
              {canDecide ? 'Submit inspection' : 'Save readings'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

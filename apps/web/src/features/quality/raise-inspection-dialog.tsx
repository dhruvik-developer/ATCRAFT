/**
 * Raise an inspection by hand.
 *
 * Most inspections are raised automatically by the receipt, booking or packing
 * start that puts the stock on hold. This dialog is for a supervisor pulling a
 * lot off the shelf: the stage, the lot, how much of it, and whether raising
 * the inspection moves that quantity from available into QC hold. Leaving
 * `hold_stock` off is for lots an upstream document has already held - holding
 * them again would take the quantity out of availability twice.
 *
 * Lazy-loaded with the detail dialog so neither lands in the quality page's
 * first-load chunk.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { FieldError } from '@/components/shared'
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
  Checkbox,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { describeError, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { QC_STAGES, type QcInspectionRow } from './quality-types'

/** Reference types a hand-raised inspection may quote. */
const REFERENCE_TYPES = [
  'manual',
  'goods_receipt',
  'production_entry',
  'work_order',
  'packing_order',
  'assembly_order',
  'job_work_receipt',
  'dispatch',
] as const

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/

const schema = z
  .object({
    qc_stage: z.enum(['INCOMING', 'IN_PROCESS', 'FINAL', 'RETURN', 'JOB_WORK', 'ASSEMBLY']),
    item_id: z.string().min(1, validation.choose('the item')),
    warehouse_id: z.string().min(1, validation.choose('the warehouse')),
    batch_id: z.string().trim(),
    inspected_quantity: z
      .string()
      .trim()
      .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
      .refine((value) => value !== '' && Number(value) > 0, 'Quantity must be more than zero'),
    hold_stock: z.boolean(),
    template_id: z.string(),
    reference_type: z.enum(REFERENCE_TYPES),
    reference_code: z.string().trim().max(64, 'At most 64 characters'),
    reference_id: z.string().trim(),
    reason: z
      .string()
      .trim()
      .min(REASON_MIN_LENGTH, `At least ${REASON_MIN_LENGTH} characters`)
      .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
    remarks: z.string().trim().max(2000, 'At most 2000 characters'),
  })
  .superRefine((values, ctx) => {
    if (values.batch_id !== '' && !UUID.test(values.batch_id)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['batch_id'],
        message: 'A batch id, or empty',
      })
    }
    if (values.reference_id !== '' && !UUID.test(values.reference_id)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['reference_id'],
        message: 'An id, or empty',
      })
    }
  })

type RaiseFormValues = z.infer<typeof schema>

function emptyValues(): RaiseFormValues {
  return {
    qc_stage: 'IN_PROCESS',
    item_id: '',
    warehouse_id: '',
    batch_id: '',
    inspected_quantity: '',
    hold_stock: true,
    template_id: SELECT_NONE,
    reference_type: 'manual',
    reference_code: '',
    reference_id: '',
    reason: '',
    remarks: '',
  }
}

interface TemplateChoice {
  id: string
  code: string
  name: string
}

export function RaiseInspectionDialog({
  open,
  onOpenChange,
  onRaised,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onRaised?: (inspection: QcInspectionRow) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<RaiseFormValues>({ resolver: zodResolver(schema), defaultValues: emptyValues() })

  useEffect(() => {
    if (open) reset(emptyValues())
  }, [open, reset])

  const itemId = watch('item_id')
  const stage = watch('qc_stage')

  // The template is optional: naming one inspects against that specification,
  // leaving it empty inspects against the inspector's judgement. Scoped to
  // the item and stage so the list stays short.
  const templates = useQuery({
    queryKey: ['quality', 'raise-templates', itemId, stage] as const,
    queryFn: () =>
      api.getPage<TemplateChoice>('/quality/templates', {
        params: { item_id: itemId, qc_stage: stage, is_active: true, limit: 50 },
      }),
    enabled: open && Boolean(itemId),
    staleTime: staleTimes.master,
  })
  const templateRows = templates.data?.data ?? []

  const raise = useMutation({
    mutationFn: (values: RaiseFormValues) =>
      api.post<QcInspectionRow>('/quality/inspections', {
        item_id: values.item_id,
        warehouse_id: values.warehouse_id,
        qc_stage: values.qc_stage,
        inspected_quantity: values.inspected_quantity.trim(),
        batch_id: values.batch_id.trim() === '' ? null : values.batch_id.trim(),
        hold_stock: values.hold_stock,
        template_id: values.template_id === SELECT_NONE ? null : values.template_id,
        reference_type: values.reference_type,
        reference_code: values.reference_code.trim() === '' ? null : values.reference_code.trim(),
        reference_id: values.reference_id.trim() === '' ? null : values.reference_id.trim(),
        reason: values.reason.trim(),
        remarks: values.remarks.trim() === '' ? null : values.remarks.trim(),
      }),
    onSuccess: (inspection) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
      toast.success(success.created('Inspection'), `${inspection.code} raised for inspection.`)
      onOpenChange(false)
      onRaised?.(inspection)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Raise an inspection</DialogTitle>
          <DialogDescription>
            Pull a lot off the shelf and put it through quality control.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => raise.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <Controller
                  control={control}
                  name="qc_stage"
                  render={({ field }) => (
                    <>
                      <Label htmlFor="raise-qc-stage">Stage</Label>
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger id="raise-qc-stage" className="mt-1.5" aria-label="Stage">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {QC_STAGES.map((value) => (
                            <SelectItem key={value} value={value}>
                              {humanise(value)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </>
                  )}
                />
                <FieldError message={errors.qc_stage?.message} />
              </div>

              <div className="sm:col-span-2">
                <Controller
                  control={control}
                  name="item_id"
                  render={({ field }) => (
                    <ItemPicker
                      id="raise-item"
                      label="Item"
                      value={field.value || null}
                      onChange={(value) => field.onChange(value ?? '')}
                      placeholder="Search the item master"
                      invalid={Boolean(errors.item_id)}
                    />
                  )}
                />
                <FieldError message={errors.item_id?.message} />
              </div>

              <div className="sm:col-span-2">
                <Controller
                  control={control}
                  name="warehouse_id"
                  render={({ field }) => (
                    <WarehouseSelect
                      id="raise-warehouse"
                      label="Warehouse"
                      value={field.value}
                      onChange={field.onChange}
                    />
                  )}
                />
                <FieldError message={errors.warehouse_id?.message} />
              </div>

              <div>
                <Label htmlFor="raise-quantity">Quantity</Label>
                <Input
                  id="raise-quantity"
                  inputMode="decimal"
                  {...register('inspected_quantity')}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.inspected_quantity)}
                />
                <FieldError message={errors.inspected_quantity?.message} />
              </div>

              <div>
                <Label htmlFor="raise-batch">Batch id (optional)</Label>
                <Input
                  id="raise-batch"
                  {...register('batch_id')}
                  placeholder="Required for batch-tracked items"
                  className="mt-1.5 font-mono text-xs"
                  aria-invalid={Boolean(errors.batch_id)}
                />
                <FieldError message={errors.batch_id?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="raise-template">Specification (optional)</Label>
                <Controller
                  control={control}
                  name="template_id"
                  render={({ field }) => (
                    <Select
                      value={field.value || SELECT_NONE}
                      onValueChange={field.onChange}
                      disabled={!itemId || templates.isLoading}
                    >
                      <SelectTrigger
                        id="raise-template"
                        className="mt-1.5"
                        aria-label="Specification"
                      >
                        <SelectValue
                          placeholder={!itemId ? 'Choose an item first' : 'Inspector judgement'}
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>Inspector judgement</SelectItem>
                        {templateRows.map((template) => (
                          <SelectItem key={template.id} value={template.id}>
                            {template.code} - {template.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.template_id?.message} />
              </div>

              <div>
                <Controller
                  control={control}
                  name="reference_type"
                  render={({ field }) => (
                    <>
                      <Label htmlFor="raise-reference-type">Against</Label>
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger
                          id="raise-reference-type"
                          className="mt-1.5"
                          aria-label="Against"
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {REFERENCE_TYPES.map((value) => (
                            <SelectItem key={value} value={value}>
                              {humanise(value)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="raise-reference-code">Reference code (optional)</Label>
                <Input
                  id="raise-reference-code"
                  {...register('reference_code')}
                  placeholder="e.g. the work order code"
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.reference_code)}
                />
                <FieldError message={errors.reference_code?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="raise-reference-id">Reference id (optional)</Label>
                <Input
                  id="raise-reference-id"
                  {...register('reference_id')}
                  placeholder="The document this inspection is raised against"
                  className="mt-1.5 font-mono text-xs"
                  aria-invalid={Boolean(errors.reference_id)}
                />
                <FieldError message={errors.reference_id?.message} />
              </div>

              <div className="sm:col-span-2 flex items-start gap-2">
                <Controller
                  control={control}
                  name="hold_stock"
                  render={({ field }) => (
                    <Checkbox
                      id="raise-hold-stock"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <div>
                  <Label htmlFor="raise-hold-stock" className="font-normal">
                    Move the quantity into QC hold
                  </Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Leave off when an upstream document has already held the stock.
                  </p>
                </div>
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="raise-reason">Reason</Label>
                <Textarea
                  id="raise-reason"
                  {...register('reason')}
                  rows={2}
                  maxLength={REASON_MAX_LENGTH}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.reason)}
                />
                <FieldError message={errors.reason?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="raise-remarks">Remarks (optional)</Label>
                <Textarea
                  id="raise-remarks"
                  {...register('remarks')}
                  rows={2}
                  maxLength={2000}
                  className="mt-1.5"
                  aria-invalid={Boolean(errors.remarks)}
                />
                <FieldError message={errors.remarks?.message} />
              </div>
            </div>

            {raise.error && (
              <Alert variant="destructive">
                <AlertDescription>{describeError(raise.error)}</AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={raise.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={raise.isPending} disabled={!can(P.QC_INSPECT)}>
              Raise inspection
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

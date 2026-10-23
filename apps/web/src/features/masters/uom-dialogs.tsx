/**
 * Units of measure and the conversions between them.
 *
 * A wrong conversion is the most expensive mistake this screen can make: it
 * does not fail, it quietly multiplies or divides every quantity derived from
 * it - a goods receipt, a BOM explosion, a stock valuation. So the whole design
 * of these dialogs is about the direction of the factor.
 *
 * `factor` means **how many `to` units make ONE `from` unit**. The create form
 * never shows a bare number: it renders the equation as the operator types, and
 * the inverse beside it, because the resolver uses the inverse too - a pair
 * stored once is live in both directions (`UomGraph.factor`).
 *
 * `UomConversionCheckDialog` exists for the same reason. It asks the server to
 * convert a real quantity and prints what the server said. Nothing about it is
 * computed in the browser, so it cannot agree with a mistake this screen made.
 *
 * Two things the contract does not let these dialogs do, and the reasons:
 * - **A unit's code and measurement family cannot be edited.** `UomUpdate`
 *   carries neither. Moving a unit between families would orphan every
 *   conversion in both.
 * - **A conversion cannot be edited, only removed and re-entered.** There is no
 *   PATCH on `/products/uom-conversions/{id}`, which is the safer contract:
 *   an in-place factor change would silently re-value stock already booked
 *   through it.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { ConfirmDialog, FieldError } from '@/components/shared'
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
  Checkbox,
  Input,
  Label,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise, toDecimal } from '@/lib/format'
import { describeError, success, validation } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  conversionStatement,
  FACTOR_PATTERN,
  formatFactor,
  inverseFactor,
  MAX_UOM_DECIMALS,
  mastersKeys,
  MIN_UOM_DECIMALS,
  UOM_CATEGORIES,
  type ConversionResult,
  type ConversionRow,
  type UomRow,
} from './masters-model'

// -------------------------------------------------------------- the unit ----

const uomSchema = z.object({
  code: z
    .string()
    .trim()
    .min(1, validation.required('Code'))
    .max(16, 'Code is at most 16 characters'),
  name: z
    .string()
    .trim()
    .min(1, validation.required('Name'))
    .max(64, 'Name is at most 64 characters'),
  uom_category: z.enum(UOM_CATEGORIES),
  is_base: z.boolean(),
  decimal_places: z
    .string()
    .trim()
    .regex(/^\d$/, 'A whole number of decimal places')
    .refine(
      (value) => Number(value) >= MIN_UOM_DECIMALS && Number(value) <= MAX_UOM_DECIMALS,
      `Between ${MIN_UOM_DECIMALS} and ${MAX_UOM_DECIMALS} decimal places`,
    ),
  is_active: z.boolean(),
})

type UomFormValues = z.infer<typeof uomSchema>

function emptyUom(): UomFormValues {
  return {
    code: '',
    name: '',
    uom_category: 'COUNT',
    is_base: false,
    decimal_places: '4',
    is_active: true,
  }
}

function valuesFromUom(uom: UomRow): UomFormValues {
  return {
    code: uom.code,
    name: uom.name,
    uom_category: uom.uom_category,
    is_base: uom.is_base,
    decimal_places: String(uom.decimal_places),
    is_active: uom.is_active,
  }
}

export function UomFormDialog({
  open,
  onOpenChange,
  uom,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  uom?: UomRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(uom)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<UomFormValues>({
    resolver: zodResolver(uomSchema),
    defaultValues: emptyUom(),
  })

  const editingId = uom?.id ?? null
  useEffect(() => {
    if (open) reset(uom ? valuesFromUom(uom) : emptyUom())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: UomFormValues) => {
      const decimals = Number.parseInt(values.decimal_places, 10)

      if (uom) {
        return api.patch<UomRow>(`/products/uoms/${uom.id}`, {
          name: values.name.trim(),
          decimal_places: decimals,
          is_base: values.is_base,
          is_active: values.is_active,
        })
      }

      return api.post<UomRow>('/products/uoms', {
        code: values.code.trim(),
        name: values.name.trim(),
        uom_category: values.uom_category,
        is_base: values.is_base,
        decimal_places: decimals,
        is_active: values.is_active,
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      // The item form's unit picker reads the shared branch; a unit added here
      // must not stay invisible to it for the reference-data stale window.
      void queryClient.invalidateQueries({ queryKey: queryKeys.uoms.all })
      toast.success(
        isEdit ? success.updated('Unit') : success.created('Unit'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  // Clearing the base flag is refused outright by the service: a family without
  // a base unit leaves every conversion in it unresolvable. The way to change
  // it is to promote another unit, so the control is locked rather than
  // offering a press that can only be rejected.
  const baseIsLocked = Boolean(uom?.is_base)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${uom?.code}` : 'New unit of measure'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code and the measurement family are fixed - moving a unit between families would orphan every conversion in both.'
              : 'A unit belongs to one measurement family. Conversions only ever relate two units of the same family.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="uom-code" required>
                  Code
                </Label>
                <Input
                  id="uom-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="uom-name" required>
                  Name
                </Label>
                <Input
                  id="uom-name"
                  {...register('name')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>

              <div>
                <Label htmlFor="uom-category" required>
                  Measurement family
                </Label>
                <Controller
                  control={control}
                  name="uom_category"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange} disabled={isEdit}>
                      <SelectTrigger id="uom-category" className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {UOM_CATEGORIES.map((entry) => (
                          <SelectItem key={entry} value={entry}>
                            {humanise(entry)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.uom_category?.message} />
              </div>

              <div>
                <Label htmlFor="uom-decimals" required>
                  Decimal places
                </Label>
                <Input
                  id="uom-decimals"
                  type="number"
                  min={MIN_UOM_DECIMALS}
                  max={MAX_UOM_DECIMALS}
                  step={1}
                  {...register('decimal_places')}
                  aria-invalid={Boolean(errors.decimal_places)}
                  className="mt-1.5"
                />
                <FieldError message={errors.decimal_places?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  How finely a quantity in this unit may be stated. Nought for a unit that is only
                  ever whole, such as pieces.
                </p>
              </div>
            </div>

            <div className="flex items-start gap-2.5">
              <Controller
                control={control}
                name="is_base"
                render={({ field }) => (
                  <Checkbox
                    id="uom-base"
                    checked={field.value}
                    disabled={baseIsLocked}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <div>
                <Label htmlFor="uom-base" className="cursor-pointer font-normal">
                  Base unit of this family
                </Label>
                <p className="text-xs text-muted-foreground">
                  {baseIsLocked
                    ? 'This is already the base unit. A family must keep one, so promote another unit instead of clearing this.'
                    : 'Every conversion in a family resolves through its base unit. A family has exactly one.'}
                </p>
              </div>
            </div>

            <div className="flex items-start gap-2.5">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox
                    id="uom-active"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <div>
                <Label htmlFor="uom-active" className="cursor-pointer font-normal">
                  Available on new items
                </Label>
                <p className="text-xs text-muted-foreground">
                  Deactivation is refused while active items still measure in this unit.
                </p>
              </div>
            </div>

            <FieldError message={errors.root?.message} />
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
              {isEdit ? 'Save changes' : 'Create unit'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------- the conversion ----

const conversionSchema = z
  .object({
    from_uom_id: z.string().min(1, validation.choose('the unit being converted from')),
    to_uom_id: z.string().min(1, validation.choose('the unit being converted to')),
    factor: z
      .string()
      .trim()
      .min(1, validation.required('Factor'))
      .regex(FACTOR_PATTERN, 'Up to 10 digits and 8 decimal places, no sign')
      .refine((value) => toDecimal(value).greaterThan(0), 'The factor must be greater than nought'),
  })
  .refine((values) => values.from_uom_id !== values.to_uom_id, {
    path: ['to_uom_id'],
    message: 'The two units must be different',
  })

type ConversionFormValues = z.infer<typeof conversionSchema>

/**
 * Add a conversion between two units.
 *
 * The second select offers only the first unit's own measurement family. That
 * is not a list filter applied in the browser to dodge a server query - the
 * unit master is reference data already loaded whole for both selects, and
 * `GET /products/uoms` publishes no family parameter to narrow it with. Kilos
 * to pieces is not a unit conversion at all (it is an item-specific weight),
 * and the service refuses it, so it is not offered.
 */
export function ConversionFormDialog({
  open,
  onOpenChange,
  units,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The tenant's units, already loaded by the tab behind this dialog. */
  units: readonly UomRow[]
}) {
  const queryClient = useQueryClient()

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<ConversionFormValues>({
    resolver: zodResolver(conversionSchema),
    defaultValues: { from_uom_id: '', to_uom_id: '', factor: '' },
  })

  useEffect(() => {
    if (open) reset({ from_uom_id: '', to_uom_id: '', factor: '' })
  }, [open, reset])

  const fromId = watch('from_uom_id')
  const toId = watch('to_uom_id')
  const factor = watch('factor')

  const fromUnit = units.find((unit) => unit.id === fromId) ?? null
  const toUnit = units.find((unit) => unit.id === toId) ?? null

  const targets = useMemo(
    () =>
      fromUnit
        ? units.filter(
            (unit) => unit.uom_category === fromUnit.uom_category && unit.id !== fromUnit.id,
          )
        : [],
    [units, fromUnit],
  )

  // Changing the source family can strand a target chosen from the old one,
  // which would post a pair the service rejects for a reason the form already
  // knows.
  useEffect(() => {
    if (toId && !targets.some((unit) => unit.id === toId)) {
      setValue('to_uom_id', '', { shouldValidate: false })
    }
  }, [targets, toId, setValue])

  const mutation = useMutation({
    mutationFn: (values: ConversionFormValues) =>
      api.post<ConversionRow>('/products/uom-conversions', {
        from_uom_id: values.from_uom_id,
        to_uom_id: values.to_uom_id,
        // A decimal string all the way to the wire; never parsed into a double.
        factor: values.factor.trim(),
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.uoms.all })
      toast.success(
        success.created('Conversion'),
        conversionStatement(fromUnit?.code ?? '', toUnit?.code ?? '', saved.factor),
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const inverse = FACTOR_PATTERN.test(factor.trim()) ? inverseFactor(factor.trim()) : null
  const canPreview = Boolean(fromUnit && toUnit && inverse)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New unit conversion</DialogTitle>
          <DialogDescription>
            One factor, one direction, stated as an equation. The pair is stored once and resolves
            both ways, so there is no need to enter the reverse.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid items-end gap-3 sm:grid-cols-[1fr_auto_1fr]">
              <div>
                <Label htmlFor="conversion-from" required>
                  From unit
                </Label>
                <Controller
                  control={control}
                  name="from_uom_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="conversion-from"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.from_uom_id)}
                      >
                        <SelectValue placeholder="Select a unit" />
                      </SelectTrigger>
                      <SelectContent>
                        {units.map((unit) => (
                          <SelectItem key={unit.id} value={unit.id}>
                            {unit.code} - {unit.name} ({humanise(unit.uom_category)})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.from_uom_id?.message} />
              </div>

              <ArrowRight
                className="mb-2.5 hidden h-4 w-4 text-muted-foreground sm:block"
                aria-hidden
              />

              <div>
                <Label htmlFor="conversion-to" required>
                  To unit
                </Label>
                <Controller
                  control={control}
                  name="to_uom_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange} disabled={!fromUnit}>
                      <SelectTrigger
                        id="conversion-to"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.to_uom_id)}
                      >
                        <SelectValue
                          placeholder={fromUnit ? 'Select a unit' : 'Choose the from unit first'}
                        />
                      </SelectTrigger>
                      <SelectContent>
                        {targets.map((unit) => (
                          <SelectItem key={unit.id} value={unit.id}>
                            {unit.code} - {unit.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.to_uom_id?.message} />
                {fromUnit && targets.length === 0 && (
                  <p className="mt-1 text-xs text-warning">
                    {humanise(fromUnit.uom_category)} has no second unit yet. Add one before a
                    conversion can be defined.
                  </p>
                )}
              </div>
            </div>

            <div>
              <Label htmlFor="conversion-factor" required>
                Factor
              </Label>
              <Input
                id="conversion-factor"
                {...register('factor')}
                inputMode="decimal"
                autoComplete="off"
                placeholder="1000"
                aria-invalid={Boolean(errors.factor)}
                aria-describedby="conversion-factor-help"
                className="mt-1.5"
              />
              <FieldError message={errors.factor?.message} />
              <p id="conversion-factor-help" className="mt-1 text-xs text-muted-foreground">
                How many <strong>to</strong> units make one <strong>from</strong> unit. Kilograms to
                grams is 1000, not 0.001.
              </p>
            </div>

            {/* The equation, live. A factor read backwards is the single most
                expensive mistake available on this screen, and the only
                reliable guard is showing the operator the sentence they are
                about to save. */}
            <Alert variant={canPreview ? 'info' : 'default'}>
              <AlertTitle>This conversion will read</AlertTitle>
              <AlertDescription>
                {canPreview && fromUnit && toUnit && inverse ? (
                  <span className="block space-y-0.5">
                    <span className="block font-medium text-foreground">
                      {conversionStatement(fromUnit.code, toUnit.code, factor.trim())}
                    </span>
                    <span className="block">
                      and therefore 1 {toUnit.code} = {formatFactor(inverse)} {fromUnit.code}
                    </span>
                  </span>
                ) : (
                  'Choose both units and enter a factor to see the equation before you save it.'
                )}
              </AlertDescription>
            </Alert>

            <FieldError message={errors.root?.message} />
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
              Add conversion
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Remove a conversion.
 *
 * The consequence is stated in units rather than in the abstract: once the pair
 * is gone, a document quoting one of them cannot resolve into the other and the
 * resolver refuses rather than guessing.
 */
export function ConversionDeleteDialog({
  conversion,
  statement,
  onOpenChange,
}: {
  conversion: ConversionRow | null
  /** The equation as the grid shows it, so the confirmation names real units. */
  statement: string
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: (target: ConversionRow) => api.delete(`/products/uom-conversions/${target.id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.uoms.all })
      toast.success(success.deleted('Conversion'), statement)
      onOpenChange(false)
    },
  })

  return (
    <ConfirmDialog
      open={Boolean(conversion)}
      onOpenChange={onOpenChange}
      title="Remove this conversion?"
      description={
        conversion
          ? `${statement}. Once it is gone, a document quoting either unit cannot be resolved into the other - the resolver refuses rather than assuming a factor.`
          : undefined
      }
      confirmLabel="Remove conversion"
      destructive
      loading={mutation.isPending}
      onConfirm={() => conversion && mutation.mutate(conversion)}
    />
  )
}

// --------------------------------------------------------- the direction ----

/**
 * Convert a real quantity and show what the server said.
 *
 * `GET /products/uom-conversions/convert` resolves through the family's base
 * unit and inverts a stored pair when it has to, so its answer is the one every
 * document will get. Nothing here multiplies anything: an arithmetic check that
 * shares this screen's assumption about direction would confirm its own error.
 *
 * Reading it needs `uom.view`, the same right as the conversion list this
 * dialog is opened from.
 */
export function UomConversionCheckDialog({
  open,
  onOpenChange,
  units,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  units: readonly UomRow[]
}) {
  const [fromId, setFromId] = useState('')
  const [toId, setToId] = useState('')
  const [quantity, setQuantity] = useState('1')
  const [asked, setAsked] = useState<{
    from_uom_id: string
    to_uom_id: string
    quantity: string
  } | null>(null)

  useEffect(() => {
    if (!open) return
    setFromId('')
    setToId('')
    setQuantity('1')
    setAsked(null)
  }, [open])

  const answer = useQuery({
    queryKey: mastersKeys.conversionCheck(asked),
    queryFn: () =>
      api.get<ConversionResult>('/products/uom-conversions/convert', { params: asked ?? {} }),
    enabled: open && Boolean(asked),
    staleTime: staleTimes.reference,
    // A pair with no defined conversion is a business answer, not a transient
    // failure - retrying it three times only delays the message...
    retry: false,
    // ...and it is rendered inline below, so the global "Could not load data"
    // toast would mis-title a correct answer and duplicate the alert.
    meta: { suppressErrorToast: true },
  })

  // The answer is labelled from the ids the SERVER echoed back, not from the
  // two selects - those are live, and a result relabelled with a unit the user
  // has since picked is exactly the silent mislabelling this dialog exists to
  // rule out.
  const result = answer.data ?? null
  const answeredFrom = result
    ? (units.find((unit) => unit.id === result.from_uom_id) ?? null)
    : null
  const answeredTo = result ? (units.find((unit) => unit.id === result.to_uom_id) ?? null) : null

  const quantityIsValid =
    /^\d{1,14}(\.\d{1,6})?$/.test(quantity.trim()) && toDecimal(quantity.trim()).greaterThan(0)
  const canAsk = Boolean(fromId) && Boolean(toId) && fromId !== toId && quantityIsValid

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Check a conversion</DialogTitle>
          <DialogDescription>
            The server answers this exactly as it would for a document line, so it is the
            authoritative reading of the factor - not a second calculation done here.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="check-quantity">Quantity</Label>
              <Input
                id="check-quantity"
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
                inputMode="decimal"
                autoComplete="off"
                aria-invalid={!quantityIsValid}
                className="mt-1.5"
              />
              {!quantityIsValid && <FieldError message="Enter a positive decimal quantity" />}
            </div>

            <div>
              <Label htmlFor="check-from">From unit</Label>
              <Select value={fromId} onValueChange={setFromId}>
                <SelectTrigger id="check-from" className="mt-1.5">
                  <SelectValue placeholder="Select a unit" />
                </SelectTrigger>
                <SelectContent>
                  {units.map((unit) => (
                    <SelectItem key={unit.id} value={unit.id}>
                      {unit.code} - {unit.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="sm:col-start-2">
              <Label htmlFor="check-to">To unit</Label>
              <Select value={toId} onValueChange={setToId}>
                <SelectTrigger id="check-to" className="mt-1.5">
                  <SelectValue placeholder="Select a unit" />
                </SelectTrigger>
                <SelectContent>
                  {units.map((unit) => (
                    <SelectItem key={unit.id} value={unit.id}>
                      {unit.code} - {unit.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {asked && answer.error && (
            <Alert variant="destructive">
              <AlertTitle>No conversion for that pair</AlertTitle>
              <AlertDescription>{describeError(answer.error)}</AlertDescription>
            </Alert>
          )}

          {asked && !answer.error && result && answeredFrom && answeredTo && (
            <Alert variant="info">
              <AlertTitle>
                {result.input_quantity} {answeredFrom.code} = {result.converted_quantity}{' '}
                {answeredTo.code}
              </AlertTitle>
              <AlertDescription>
                Resolved at a factor of {result.factor} - one {answeredFrom.code} is {result.factor}{' '}
                {answeredTo.code}.
              </AlertDescription>
            </Alert>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button
            disabled={!canAsk}
            loading={answer.isFetching}
            onClick={() =>
              setAsked({
                from_uom_id: fromId,
                to_uom_id: toId,
                quantity: quantity.trim(),
              })
            }
          >
            Convert
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

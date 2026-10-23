/**
 * The three dialogs the transporter master needs.
 *
 * `TransporterFormDialog`   create and edit, one component - the field set is
 *                           identical apart from `code` and `is_active`, which
 *                           `TransporterUpdate` does not accept at all.
 * `TransporterCommandDialog` activate / deactivate / retire / restore, each with
 *                           the reason its endpoint wants, in the place that
 *                           endpoint wants it.
 * `TrackingLinkDialog`      resolve one consignment number against the carrier's
 *                           template, which is the only way to find out whether
 *                           a template that looks right actually produces the
 *                           carrier's page.
 *
 * All three read their vocabulary and their lifecycle rules from
 * `transporter-model.ts`; nothing about which command is legal in which state is
 * decided here.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
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
  Checkbox,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors, reportFieldErrors } from '@/lib/form-errors'
import { success, validation } from '@/lib/messages'
import { staleTimes } from '@/lib/query-client'
import { GSTIN_PATTERN, PAN_PATTERN, STATE_CODE_PATTERN } from '@/lib/statutory'
import { toast } from '@/lib/toast'

import {
  MAX_TRACKING_ID_LENGTH,
  TRACKING_PLACEHOLDER,
  TRACKING_TEMPLATE_MAX_LENGTH,
  TRANSPORTER_COMMANDS,
  TRANSPORTER_TYPES,
  TRANSPORTER_TYPE_LABELS,
  isHttpsUrl,
  runTransporterCommand,
  trackingTemplateProblem,
  transporterKeys,
  type PendingTransporterCommand,
  type TrackingLink,
  type Transporter,
  type TransporterCreateBody,
  type TransporterUpdateBody,
} from './transporter-model'

// ------------------------------------------------------------------ form ----

const schema = z
  .object({
    code: z.string().trim().min(1, validation.required('Code')).max(32, 'At most 32 characters'),
    name: z.string().trim().min(1, validation.required('Name')).max(255, 'At most 255 characters'),
    transporter_type: z.enum(TRANSPORTER_TYPES),

    gstin: z
      .string()
      .trim()
      .refine(
        (v) => v === '' || GSTIN_PATTERN.test(v.toUpperCase()),
        'Not a valid 15-character GSTIN',
      ),
    pan: z
      .string()
      .trim()
      .refine((v) => v === '' || PAN_PATTERN.test(v.toUpperCase()), 'Not a valid 10-character PAN'),

    contact_person: z.string().trim().max(160, 'At most 160 characters'),
    email: z
      .string()
      .trim()
      .refine((v) => v === '' || z.string().email().safeParse(v).success, validation.email),
    phone: z.string().trim().max(32, 'At most 32 characters'),
    mobile: z.string().trim().max(32, 'At most 32 characters'),
    website: z.string().trim().max(255, 'At most 255 characters'),

    address_line1: z.string().trim().max(255, 'At most 255 characters'),
    city: z.string().trim().max(120, 'At most 120 characters'),
    state: z.string().trim().max(120, 'At most 120 characters'),
    state_code: z
      .string()
      .trim()
      .refine((v) => v === '' || STATE_CODE_PATTERN.test(v), 'Two digits, as printed on the GSTIN'),
    postal_code: z.string().trim().max(16, 'At most 16 characters'),
    country: z
      .string()
      .trim()
      .min(1, validation.required('Country'))
      .max(80, 'At most 80 characters'),

    tracking_url_template: z
      .string()
      .trim()
      .max(TRACKING_TEMPLATE_MAX_LENGTH, `At most ${TRACKING_TEMPLATE_MAX_LENGTH} characters`),
    notes: z.string(),
    is_active: z.boolean(),
  })
  .superRefine((values, ctx) => {
    // The GSTIN embeds the PAN and the state code, and the API re-checks both
    // after merging an update into what is already on file. Catching a
    // transposed character here saves the round trip.
    const gstin = values.gstin.toUpperCase()
    if (gstin && values.pan && gstin.slice(2, 12) !== values.pan.toUpperCase()) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['pan'],
        message: 'The PAN does not match the one embedded in the GSTIN',
      })
    }
    if (gstin && values.state_code && gstin.slice(0, 2) !== values.state_code) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['state_code'],
        message: 'The state code does not match the first two digits of the GSTIN',
      })
    }

    const problem = trackingTemplateProblem(values.tracking_url_template)
    if (problem) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['tracking_url_template'],
        message: problem,
      })
    }
  })

type TransporterFormValues = z.infer<typeof schema>

function emptyValues(): TransporterFormValues {
  return {
    code: '',
    name: '',
    transporter_type: 'TRANSPORT',
    gstin: '',
    pan: '',
    contact_person: '',
    email: '',
    phone: '',
    mobile: '',
    website: '',
    address_line1: '',
    city: '',
    state: '',
    state_code: '',
    postal_code: '',
    country: 'India',
    tracking_url_template: '',
    notes: '',
    is_active: true,
  }
}

function valuesFrom(transporter: Transporter): TransporterFormValues {
  return {
    code: transporter.code,
    name: transporter.name,
    transporter_type: transporter.transporter_type,
    gstin: transporter.gstin ?? '',
    pan: transporter.pan ?? '',
    contact_person: transporter.contact_person ?? '',
    email: transporter.email ?? '',
    phone: transporter.phone ?? '',
    mobile: transporter.mobile ?? '',
    website: transporter.website ?? '',
    address_line1: transporter.address_line1 ?? '',
    city: transporter.city ?? '',
    state: transporter.state ?? '',
    state_code: transporter.state_code ?? '',
    postal_code: transporter.postal_code ?? '',
    country: transporter.country,
    tracking_url_template: transporter.tracking_url_template ?? '',
    notes: transporter.notes ?? '',
    // Never edited here - activation is its own audited endpoint - but the form
    // still shows the truth while a create is being typed.
    is_active: transporter.is_active,
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())
const upperOrNull = (value: string): string | null =>
  value.trim() === '' ? null : value.trim().toUpperCase()

/** Everything both payloads carry. Typed against the contract, not by hand. */
function sharedFields(values: TransporterFormValues) {
  return {
    name: values.name.trim(),
    transporter_type: values.transporter_type,
    gstin: upperOrNull(values.gstin),
    pan: upperOrNull(values.pan),
    contact_person: orNull(values.contact_person),
    email: orNull(values.email),
    phone: orNull(values.phone),
    mobile: orNull(values.mobile),
    website: orNull(values.website),
    address_line1: orNull(values.address_line1),
    city: orNull(values.city),
    state: orNull(values.state),
    state_code: upperOrNull(values.state_code),
    postal_code: orNull(values.postal_code),
    country: values.country.trim(),
    tracking_url_template: orNull(values.tracking_url_template),
    notes: orNull(values.notes),
  }
}

const buildCreate = (values: TransporterFormValues): TransporterCreateBody => ({
  ...sharedFields(values),
  // Normalised to upper case here as well as on the server, so what the user is
  // told was created matches what they typed into the duplicate they get next.
  code: values.code.trim().toUpperCase(),
  is_active: values.is_active,
})

/** `TransporterUpdate` forbids extras, so `code` and `is_active` never appear. */
const buildUpdate = (values: TransporterFormValues): TransporterUpdateBody => sharedFields(values)

export function TransporterFormDialog({
  open,
  onOpenChange,
  transporterId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Null for a create, an id for an edit. Never a retired transporter's id. */
  transporterId: string | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(transporterId)

  const detail = useQuery({
    queryKey: transporterKeys.detail(transporterId ?? ''),
    queryFn: () => api.get<Transporter>(`/transporters/${transporterId}`),
    enabled: open && Boolean(transporterId),
    staleTime: staleTimes.master,
  })

  const loaded = detail.data

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<TransporterFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  // Keyed on the id and on whether its payload has arrived, so a background
  // refetch cannot resolve mid-edit and wipe what is being typed.
  useEffect(() => {
    if (!open) return
    if (transporterId && !loaded) return
    reset(loaded && transporterId ? valuesFrom(loaded) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, transporterId, Boolean(loaded), reset])

  const save = useMutation({
    mutationFn: (values: TransporterFormValues) =>
      transporterId
        ? api.patch<Transporter>(`/transporters/${transporterId}`, buildUpdate(values))
        : api.post<Transporter>('/transporters', buildCreate(values)),
    onSuccess: (saved) => {
      // One prefix invalidation covers every list, the detail and the picker
      // options the dispatch screen reads.
      void queryClient.invalidateQueries({ queryKey: transporterKeys.all })
      toast.success(
        isEdit ? success.updated('Transporter') : success.created('Transporter'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const detailsBusy = isEdit && detail.isLoading

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? `Edit ${loaded?.code ?? 'transporter'}` : 'New transporter'}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - dispatches already quote it. Activation and retirement are separate, audited actions.'
              : 'A carrier or courier a dispatch can be handed to. Give it a tracking link template and every consignment number keyed against it resolves to the carrier page.'}
          </DialogDescription>
        </DialogHeader>

        {detailsBusy ? (
          <div className="space-y-3" aria-hidden>
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : (
          <form
            onSubmit={handleSubmit((values) => save.mutate(values))}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-6">
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="transporter-code" required>
                    Code
                  </Label>
                  <Input
                    id="transporter-code"
                    {...register('code')}
                    disabled={isEdit}
                    autoComplete="off"
                    className="mt-1.5 uppercase"
                    aria-invalid={Boolean(errors.code)}
                  />
                  <FieldError message={errors.code?.message} />
                </div>

                <div>
                  <Label htmlFor="transporter-name" required>
                    Name
                  </Label>
                  <Input
                    id="transporter-name"
                    {...register('name')}
                    autoComplete="organization"
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.name)}
                  />
                  <FieldError message={errors.name?.message} />
                </div>

                <div>
                  <Label htmlFor="transporter-type" required>
                    Type
                  </Label>
                  <Controller
                    control={control}
                    name="transporter_type"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger id="transporter-type" className="mt-1.5">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {TRANSPORTER_TYPES.map((type) => (
                            <SelectItem key={type} value={type}>
                              {TRANSPORTER_TYPE_LABELS[type]}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError message={errors.transporter_type?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    A freight transporter moves pallets under a lorry receipt; a courier moves
                    parcels under an airway bill.
                  </p>
                </div>
              </div>

              <div>
                <h3 className="text-sm font-medium">Statutory</h3>
                <div className="mt-3 grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="transporter-gstin">GSTIN</Label>
                    <Input
                      id="transporter-gstin"
                      {...register('gstin')}
                      autoComplete="off"
                      className="mt-1.5 uppercase"
                      aria-invalid={Boolean(errors.gstin)}
                    />
                    <FieldError message={errors.gstin?.message} />
                    <p className="mt-1 text-xs text-muted-foreground">
                      A transporter's GSTIN is what an e-way bill is raised against.
                    </p>
                  </div>
                  <div>
                    <Label htmlFor="transporter-pan">PAN</Label>
                    <Input
                      id="transporter-pan"
                      {...register('pan')}
                      autoComplete="off"
                      className="mt-1.5 uppercase"
                      aria-invalid={Boolean(errors.pan)}
                    />
                    <FieldError message={errors.pan?.message} />
                  </div>
                </div>
              </div>

              <div>
                <h3 className="text-sm font-medium">Contact</h3>
                <div className="mt-3 grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="transporter-contact-person">Contact person</Label>
                    <Input
                      id="transporter-contact-person"
                      {...register('contact_person')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.contact_person?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-email">Email</Label>
                    <Input
                      id="transporter-email"
                      type="email"
                      {...register('email')}
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.email)}
                    />
                    <FieldError message={errors.email?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-phone">Phone</Label>
                    <Input
                      id="transporter-phone"
                      {...register('phone')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.phone?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-mobile">Mobile</Label>
                    <Input
                      id="transporter-mobile"
                      {...register('mobile')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.mobile?.message} />
                  </div>
                  <div className="sm:col-span-2">
                    <Label htmlFor="transporter-website">Website</Label>
                    <Input
                      id="transporter-website"
                      {...register('website')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.website?.message} />
                  </div>
                </div>
              </div>

              <div>
                <h3 className="text-sm font-medium">Address</h3>
                <div className="mt-3 grid gap-4 sm:grid-cols-2">
                  <div className="sm:col-span-2">
                    <Label htmlFor="transporter-address">Address</Label>
                    <Input
                      id="transporter-address"
                      {...register('address_line1')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.address_line1?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-city">City</Label>
                    <Input
                      id="transporter-city"
                      {...register('city')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.city?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-state">State</Label>
                    <Input
                      id="transporter-state"
                      {...register('state')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.state?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-state-code">State code</Label>
                    <Input
                      id="transporter-state-code"
                      {...register('state_code')}
                      inputMode="numeric"
                      autoComplete="off"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.state_code)}
                    />
                    <FieldError message={errors.state_code?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-postal-code">Postal code</Label>
                    <Input
                      id="transporter-postal-code"
                      {...register('postal_code')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.postal_code?.message} />
                  </div>
                  <div>
                    <Label htmlFor="transporter-country" required>
                      Country
                    </Label>
                    <Input
                      id="transporter-country"
                      {...register('country')}
                      autoComplete="country-name"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.country)}
                    />
                    <FieldError message={errors.country?.message} />
                  </div>
                </div>
              </div>

              <div>
                <h3 className="text-sm font-medium">Tracking</h3>
                <div className="mt-3">
                  <Label htmlFor="transporter-tracking">Tracking link template</Label>
                  <Input
                    id="transporter-tracking"
                    {...register('tracking_url_template')}
                    autoComplete="off"
                    inputMode="url"
                    className="mt-1.5 font-mono text-xs"
                    placeholder={`https://carrier.example.com/track?awb=${TRACKING_PLACEHOLDER}`}
                    aria-invalid={Boolean(errors.tracking_url_template)}
                  />
                  <FieldError message={errors.tracking_url_template?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Must be HTTPS and must contain{' '}
                    <code className="font-mono">{TRACKING_PLACEHOLDER}</code>, which the consignment
                    number replaces. Without the placeholder every consignment resolves to the same
                    page, which looks like it works and never does. Leave empty for a carrier whose
                    tracking is by telephone.
                  </p>
                </div>
              </div>

              <div>
                <Label htmlFor="transporter-notes">Notes</Label>
                <Textarea id="transporter-notes" {...register('notes')} className="mt-1.5" />
                <FieldError message={errors.notes?.message} />
              </div>

              {!isEdit && (
                <div className="flex items-center gap-2.5">
                  <Controller
                    control={control}
                    name="is_active"
                    render={({ field }) => (
                      <Checkbox
                        id="transporter-active"
                        checked={field.value}
                        onCheckedChange={(checked) => field.onChange(checked === true)}
                      />
                    )}
                  />
                  <Label htmlFor="transporter-active" className="font-normal">
                    Active - selectable on new dispatches straight away
                  </Label>
                </div>
              )}
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={save.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" loading={save.isPending}>
                {isEdit ? 'Save changes' : 'Create transporter'}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

// --------------------------------------------------------------- command ----

export function TransporterCommandDialog({
  pending,
  onOpenChange,
}: {
  pending: PendingTransporterCommand | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  const copy = pending ? TRANSPORTER_COMMANDS[pending.command] : null
  // Restore takes no payload at all, so there is nothing to ask for; the other
  // three all insist on a reason, only in different parts of the request.
  const needsReason = copy !== null && copy.reason !== 'none'
  const reasonTooShort = reason.trim().length < REASON_MIN_LENGTH

  const close = () => {
    setReason('')
    setTouched(false)
    onOpenChange(false)
  }

  const run = useMutation({
    mutationFn: (variables: PendingTransporterCommand & { reason: string }) =>
      runTransporterCommand(variables, variables.reason),
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: transporterKeys.all })
      const done = TRANSPORTER_COMMANDS[variables.command]
      toast.success(
        done.successTitle,
        done.successDescription ?? `${variables.transporter.code} - ${variables.transporter.name}`,
      )
      close()
    },
    // Fired from a confirm box, not a form: a 422 on `reason` has no control
    // to land on, and the mutation cache stays quiet for it.
    onError: (error) => reportFieldErrors(error, 'Could not complete this transporter action'),
  })

  return (
    <ConfirmDialog
      open={Boolean(pending)}
      onOpenChange={(open) => !open && close()}
      title={copy?.title ?? ''}
      description={
        pending && copy
          ? `${copy.description} (${pending.transporter.code} - ${pending.transporter.name})`
          : undefined
      }
      confirmLabel={copy?.confirmLabel ?? 'Confirm'}
      destructive={copy?.destructive}
      loading={run.isPending}
      onConfirm={() => {
        if (!pending) return
        if (needsReason && reasonTooShort) {
          setTouched(true)
          return
        }
        run.mutate({ ...pending, reason: reason.trim() })
      }}
    >
      {needsReason && (
        <div>
          <Label htmlFor="transporter-command-reason" required>
            Reason
          </Label>
          <Input
            id="transporter-command-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={REASON_MAX_LENGTH}
            autoComplete="off"
            className="mt-1.5"
            aria-invalid={touched && reasonTooShort}
          />
          {touched && reasonTooShort && (
            <FieldError message={`Record at least ${REASON_MIN_LENGTH} characters of reason`} />
          )}
          <p className="mt-1 text-xs text-muted-foreground">
            Kept in the audit trail. Six months from now this is the only record of why the carrier
            was taken out of use.
          </p>
        </div>
      )}
    </ConfirmDialog>
  )
}

// -------------------------------------------------------------- tracking ----

/**
 * Resolve one consignment number against a carrier's template.
 *
 * Worth its own dialog because a template can be well-formed and still wrong -
 * the wrong query parameter name, the wrong host - and the only way to find out
 * is to put a real consignment number through it and follow the link. The
 * server does the substitution (and percent-encodes the number, which matters
 * because carrier consignment numbers arrive by phone and contain slashes and
 * spaces), so this never builds a URL itself.
 *
 * Gated by `transporter.view` like the rest of the page: the endpoint enforces
 * the view right, not the manage right, so a dispatcher who cannot edit the
 * master can still check a link.
 */
export function TrackingLinkDialog({
  transporter,
  onOpenChange,
}: {
  transporter: Transporter | null
  onOpenChange: (open: boolean) => void
}) {
  const [draft, setDraft] = useState('')
  const [submitted, setSubmitted] = useState('')

  // A different carrier is a different question; nothing typed for the last one
  // should survive into it.
  useEffect(() => {
    setDraft('')
    setSubmitted('')
  }, [transporter?.id])

  const link = useQuery({
    queryKey: transporterKeys.trackingLink(transporter?.id ?? '', submitted),
    queryFn: () =>
      api.get<TrackingLink>(`/transporters/${transporter?.id}/tracking-url`, {
        params: { tracking_id: submitted },
      }),
    enabled: Boolean(transporter) && submitted !== '',
    staleTime: staleTimes.master,
  })

  const resolved = link.data

  return (
    <Dialog open={Boolean(transporter)} onOpenChange={(open) => !open && onOpenChange(false)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {transporter ? `Tracking link - ${transporter.code}` : 'Tracking link'}
          </DialogTitle>
          <DialogDescription>
            Put a real consignment number through this carrier's template and follow the link it
            produces. Nothing is saved.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {transporter && (
            <DescriptionList
              columns={1}
              items={[
                { label: 'Carrier', value: transporter.name },
                {
                  label: 'Template on file',
                  value: transporter.tracking_url_template ? (
                    <span className="break-all font-mono text-xs">
                      {transporter.tracking_url_template}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">None</span>
                  ),
                },
              ]}
            />
          )}

          <form
            className="flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              setSubmitted(draft.trim())
            }}
          >
            <div className="flex-1">
              <Label htmlFor="tracking-id">Consignment number</Label>
              <Input
                id="tracking-id"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                maxLength={MAX_TRACKING_ID_LENGTH}
                autoComplete="off"
                className="mt-1.5"
                placeholder="LR / AWB number"
              />
            </div>
            <Button type="submit" loading={link.isFetching} disabled={draft.trim() === ''}>
              Resolve
            </Button>
          </form>

          {resolved && !link.isFetching && (
            <>
              {resolved.is_resolvable && isHttpsUrl(resolved.url) ? (
                <Alert variant="info">
                  <AlertTitle>Link for {resolved.tracking_id}</AlertTitle>
                  <AlertDescription>
                    <a
                      href={resolved.url ?? undefined}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="inline-flex items-start gap-1.5 break-all font-mono text-xs text-primary hover:underline"
                    >
                      {resolved.url}
                      <ExternalLink className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                    </a>
                  </AlertDescription>
                </Alert>
              ) : (
                <Alert variant="warning">
                  <AlertTitle>No link for this carrier</AlertTitle>
                  <AlertDescription>
                    There is no usable tracking template on file, so a dispatch with this carrier
                    shows the consignment number on its own. Add an HTTPS template containing{' '}
                    <code className="font-mono">{TRACKING_PLACEHOLDER}</code> to change that.
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

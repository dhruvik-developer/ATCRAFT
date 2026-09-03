/**
 * Create / edit dialog for a customer, in three tabs.
 *
 * One component serves both because the field set is identical apart from
 * `code`, which the API refuses to change once documents quote it.
 *
 * Standing is not part of the record's field set. Blocking, unblocking,
 * activating and deactivating are separate endpoints that each demand a recorded
 * reason, so they are separate actions here rather than checkboxes folded into
 * the save - a PATCH carrying `is_blocked` is rejected outright.
 *
 * The addresses and contacts tabs write through their own endpoints and save
 * independently of the details form, so each tab carries its own actions rather
 * than one Save button that would silently do nothing from the wrong tab.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MapPin, ShieldAlert, ShieldCheck, UserRound } from 'lucide-react'
import { useEffect, useState } from 'react'
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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
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
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { GSTIN_PATTERN, PAN_PATTERN } from '@/lib/statutory'
import { toast } from '@/lib/toast'

import { AddressesPanel, ContactsPanel, UnsavedCustomerNotice } from './customer-relations'
import { CUSTOMER_TYPES, PRICE_TYPES, type CustomerDetail } from './customer-types'

const MONEY = /^(\d{1,14}(\.\d{1,4})?)?$/
const WHOLE_DAYS = /^\d{0,4}$/

const schema = z
  .object({
    code: z.string().trim().min(1, 'Code is required').max(32, 'At most 32 characters'),
    name: z.string().trim().min(1, 'Name is required').max(255, 'At most 255 characters'),
    legal_name: z.string().trim().max(255, 'At most 255 characters'),
    customer_type: z.enum(CUSTOMER_TYPES),
    price_list_type: z.enum(PRICE_TYPES),
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
    /** Ignored while `unlimited_credit` is on. */
    credit_limit: z.string().trim().regex(MONEY, 'Enter an amount with up to 4 decimal places'),
    unlimited_credit: z.boolean(),
    credit_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
    notes: z.string().trim().max(1000, 'At most 1000 characters'),
    is_active: z.boolean(),
  })
  // A limited account with no figure is ambiguous: the API reads a missing limit
  // as unlimited, which is the opposite of what an empty box usually means.
  .superRefine((values, ctx) => {
    if (!values.unlimited_credit && values.credit_limit.trim() === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['credit_limit'],
        message: 'Enter a limit, or tick unlimited. Zero means no credit at all.',
      })
    }
  })

type CustomerFormValues = z.infer<typeof schema>

function emptyValues(): CustomerFormValues {
  return {
    code: '',
    name: '',
    legal_name: '',
    customer_type: 'CUSTOMER',
    price_list_type: 'SALE',
    gstin: '',
    pan: '',
    credit_limit: '0',
    unlimited_credit: false,
    credit_days: '0',
    notes: '',
    is_active: true,
  }
}

function valuesFrom(customer: CustomerDetail): CustomerFormValues {
  return {
    code: customer.code,
    name: customer.name,
    legal_name: customer.legal_name ?? '',
    customer_type: customer.customer_type,
    price_list_type: customer.price_list_type,
    gstin: customer.gstin ?? '',
    pan: customer.pan ?? '',
    credit_limit: customer.credit_limit ?? '',
    unlimited_credit: customer.credit_limit === null,
    credit_days: String(customer.credit_days),
    notes: customer.notes ?? '',
    is_active: customer.is_active,
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/**
 * `CustomerCreate` / `CustomerUpdate`, and nothing else.
 *
 * Both refuse unknown keys, so a field the screen shows but the schema does not
 * name cannot be smuggled along - it fails the whole request.
 */
function buildPayload(values: CustomerFormValues, isEdit: boolean): Record<string, unknown> {
  const common = {
    name: values.name.trim(),
    legal_name: orNull(values.legal_name),
    customer_type: values.customer_type,
    price_list_type: values.price_list_type,
    gstin: orNull(values.gstin)?.toUpperCase() ?? null,
    pan: orNull(values.pan)?.toUpperCase() ?? null,
    credit_days: Number.parseInt(values.credit_days || '0', 10),
    notes: orNull(values.notes),
  }

  if (!isEdit) {
    return {
      ...common,
      code: values.code.trim(),
      // Money stays a string all the way to the wire; null is "unlimited".
      credit_limit: values.unlimited_credit ? null : values.credit_limit.trim(),
      is_active: values.is_active,
    }
  }

  // An update clears a limit with its own flag: sending `credit_limit: null`
  // reads as "leave it alone" on a partial update.
  return values.unlimited_credit
    ? { ...common, clear_credit_limit: true }
    : { ...common, credit_limit: values.credit_limit.trim() }
}

// ------------------------------------------------------------- standing -----

type StandingAction = 'block' | 'unblock' | 'activate' | 'deactivate'

const STANDING_COPY: Record<
  StandingAction,
  { title: string; description: string; confirmLabel: string; destructive: boolean }
> = {
  block: {
    title: 'Block this account?',
    description:
      'Order entry will refuse it and the reason is shown to whoever tries. Orders already in flight are not withdrawn.',
    confirmLabel: 'Block account',
    destructive: true,
  },
  unblock: {
    title: 'Unblock this account?',
    description: 'New orders will be accepted for it again.',
    confirmLabel: 'Unblock account',
    destructive: false,
  },
  activate: {
    title: 'Activate this account?',
    description: 'It becomes selectable on new orders again.',
    confirmLabel: 'Activate',
    destructive: false,
  },
  deactivate: {
    title: 'Deactivate this account?',
    description: 'It stops being offered on new orders. Existing documents are unaffected.',
    confirmLabel: 'Deactivate',
    destructive: true,
  },
}

function StandingPanel({ customer, canEdit }: { customer: CustomerDetail; canEdit: boolean }) {
  const queryClient = useQueryClient()
  const [action, setAction] = useState<StandingAction | null>(null)
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  const run = useMutation({
    mutationFn: (next: StandingAction) =>
      api.post<CustomerDetail>(`/customers/${customer.id}/${next}`, { reason: reason.trim() }),
    onSuccess: (_saved, next) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(`Customer ${next}${next.endsWith('e') ? 'd' : 'ed'}`, customer.code)
      setAction(null)
      setReason('')
      setTouched(false)
    },
  })

  const copy = action ? STANDING_COPY[action] : null

  return (
    <fieldset className="rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">Standing</legend>

      {customer.is_blocked && (
        <Alert variant="warning" className="mb-3">
          <ShieldAlert aria-hidden />
          <AlertTitle>This account is blocked</AlertTitle>
          <AlertDescription>
            {customer.block_reason ?? 'No reason was recorded on the account.'}
          </AlertDescription>
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">
          {customer.is_active ? 'Active' : 'Inactive'} -{' '}
          {customer.is_blocked ? 'blocked for new orders' : 'clear for new orders'}
        </span>
      </div>

      {canEdit && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setAction(customer.is_blocked ? 'unblock' : 'block')}
          >
            {customer.is_blocked ? <ShieldCheck /> : <ShieldAlert />}
            {customer.is_blocked ? 'Unblock' : 'Block'}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setAction(customer.is_active ? 'deactivate' : 'activate')}
          >
            {customer.is_active ? 'Deactivate' : 'Activate'}
          </Button>
        </div>
      )}

      <ConfirmDialog
        open={action !== null}
        onOpenChange={(open) => {
          if (!open) {
            setAction(null)
            setTouched(false)
          }
        }}
        title={copy?.title ?? ''}
        description={copy?.description}
        confirmLabel={copy?.confirmLabel}
        destructive={copy?.destructive}
        loading={run.isPending}
        onConfirm={() => {
          if (reason.trim() === '') {
            setTouched(true)
            return
          }
          if (action) run.mutate(action)
        }}
      >
        <div>
          <Label htmlFor="standing-reason" required>
            Reason
          </Label>
          <Input
            id="standing-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            autoComplete="off"
            className="mt-1.5"
            aria-invalid={touched && reason.trim() === ''}
          />
          {touched && reason.trim() === '' && (
            <FieldError message="The API records this against the account - it needs a reason" />
          )}
        </div>
      </ConfirmDialog>
    </fieldset>
  )
}

// --------------------------------------------------------------- dialog -----

export function CustomerFormDialog({
  open,
  onOpenChange,
  customerId,
  canEdit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, null for a create. */
  customerId: string | null
  canEdit: boolean
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(customerId)

  // The list projection is narrower than the master record, so the dialog reads
  // the customer rather than editing a half-populated row.
  const detail = useQuery({
    queryKey: queryKeys.customers.detail(customerId ?? ''),
    queryFn: () => api.get<CustomerDetail>(`/customers/${customerId}`),
    enabled: open && Boolean(customerId),
    staleTime: staleTimes.master,
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<CustomerFormValues>({ resolver: zodResolver(schema), defaultValues: emptyValues() })

  // Seeded once per opening and keyed on the id, so a background refetch cannot
  // resolve mid-edit and wipe what is being typed.
  const loaded = detail.data
  useEffect(() => {
    if (!open) return
    if (customerId && !loaded) return
    reset(loaded && customerId ? valuesFrom(loaded) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, customerId, Boolean(loaded), reset])

  const unlimited = watch('unlimited_credit')

  const save = useMutation({
    mutationFn: (values: CustomerFormValues) =>
      customerId
        ? api.patch<CustomerDetail>(`/customers/${customerId}`, buildPayload(values, true))
        : api.post<CustomerDetail>('/customers', buildPayload(values, false)),
    onSuccess: (saved) => {
      // One prefix invalidation covers every customer list and detail at once.
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(
        isEdit ? 'Customer updated' : 'Customer created',
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
            {isEdit ? `Edit ${loaded?.code ?? 'customer'}` : 'New customer'}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - it is quoted on orders and invoices already issued.'
              : 'Addresses and contacts can be added as soon as the record is saved.'}
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="details" className="flex min-h-0 flex-col">
          <TabsList className="self-start">
            <TabsTrigger value="details">Details</TabsTrigger>
            <TabsTrigger value="addresses">
              <MapPin className="h-3.5 w-3.5" aria-hidden />
              Addresses
            </TabsTrigger>
            <TabsTrigger value="contacts">
              <UserRound className="h-3.5 w-3.5" aria-hidden />
              Contacts
            </TabsTrigger>
          </TabsList>

          <TabsContent value="details" className="flex min-h-0 flex-col">
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
                <DialogBody className="space-y-5">
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <Label htmlFor="customer-code" required>
                        Code
                      </Label>
                      <Input
                        id="customer-code"
                        {...register('code')}
                        disabled={isEdit}
                        autoComplete="off"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.code)}
                      />
                      <FieldError message={errors.code?.message} />
                    </div>

                    <div>
                      <Label htmlFor="customer-name" required>
                        Trading name
                      </Label>
                      <Input
                        id="customer-name"
                        {...register('name')}
                        autoComplete="off"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.name)}
                      />
                      <FieldError message={errors.name?.message} />
                    </div>

                    <div>
                      <Label htmlFor="customer-legal-name">Legal name</Label>
                      <Input
                        id="customer-legal-name"
                        {...register('legal_name')}
                        autoComplete="off"
                        className="mt-1.5"
                      />
                      <FieldError message={errors.legal_name?.message} />
                      <p className="mt-1 text-xs text-muted-foreground">
                        Printed on the tax invoice when it differs from the trading name.
                      </p>
                    </div>

                    <div>
                      <Label htmlFor="customer-type">Customer type</Label>
                      <Controller
                        control={control}
                        name="customer_type"
                        render={({ field }) => (
                          <Select value={field.value} onValueChange={field.onChange}>
                            <SelectTrigger id="customer-type" className="mt-1.5">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {CUSTOMER_TYPES.map((type) => (
                                <SelectItem key={type} value={type}>
                                  {humanise(type)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        )}
                      />
                    </div>

                    <div>
                      <Label htmlFor="customer-price-list">Price list</Label>
                      <Controller
                        control={control}
                        name="price_list_type"
                        render={({ field }) => (
                          <Select value={field.value} onValueChange={field.onChange}>
                            <SelectTrigger id="customer-price-list" className="mt-1.5">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {PRICE_TYPES.map((type) => (
                                <SelectItem key={type} value={type}>
                                  {humanise(type)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        )}
                      />
                      <p className="mt-1 text-xs text-muted-foreground">
                        Which item price order entry quotes for this account.
                      </p>
                    </div>

                    <div>
                      <Label htmlFor="customer-gstin">GSTIN</Label>
                      <Input
                        id="customer-gstin"
                        {...register('gstin')}
                        autoComplete="off"
                        className="mt-1.5 uppercase tabular"
                        aria-invalid={Boolean(errors.gstin)}
                      />
                      <FieldError message={errors.gstin?.message} />
                    </div>

                    <div>
                      <Label htmlFor="customer-pan">PAN</Label>
                      <Input
                        id="customer-pan"
                        {...register('pan')}
                        autoComplete="off"
                        className="mt-1.5 uppercase tabular"
                        aria-invalid={Boolean(errors.pan)}
                      />
                      <FieldError message={errors.pan?.message} />
                    </div>

                    <div>
                      <Label htmlFor="customer-credit-limit">Credit limit</Label>
                      <Input
                        id="customer-credit-limit"
                        {...register('credit_limit')}
                        inputMode="decimal"
                        disabled={unlimited}
                        className="mt-1.5 text-right tabular"
                        aria-invalid={Boolean(errors.credit_limit)}
                      />
                      <FieldError message={errors.credit_limit?.message} />
                      <div className="mt-2 flex items-center gap-2.5">
                        <Controller
                          control={control}
                          name="unlimited_credit"
                          render={({ field }) => (
                            <Checkbox
                              id="customer-unlimited-credit"
                              checked={field.value}
                              onCheckedChange={(checked) => field.onChange(checked === true)}
                            />
                          )}
                        />
                        <Label htmlFor="customer-unlimited-credit" className="font-normal">
                          Unlimited - no credit ceiling
                        </Label>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Zero means no credit: orders are expected against payment.
                      </p>
                    </div>

                    <div>
                      <Label htmlFor="customer-credit-days">Credit days</Label>
                      <Input
                        id="customer-credit-days"
                        {...register('credit_days')}
                        inputMode="numeric"
                        className="mt-1.5 text-right tabular"
                        aria-invalid={Boolean(errors.credit_days)}
                      />
                      <FieldError message={errors.credit_days?.message} />
                    </div>

                    <div className="sm:col-span-2">
                      <Label htmlFor="customer-notes">Notes</Label>
                      <Textarea id="customer-notes" {...register('notes')} className="mt-1.5" />
                      <FieldError message={errors.notes?.message} />
                    </div>
                  </div>

                  {isEdit && loaded ? (
                    <StandingPanel customer={loaded} canEdit={canEdit} />
                  ) : (
                    <fieldset className="rounded-lg border p-4">
                      <legend className="px-1 text-sm font-medium">Standing</legend>
                      <div className="flex items-center gap-2.5">
                        <Controller
                          control={control}
                          name="is_active"
                          render={({ field }) => (
                            <Checkbox
                              id="customer-active"
                              checked={field.value}
                              onCheckedChange={(checked) => field.onChange(checked === true)}
                            />
                          )}
                        />
                        <Label htmlFor="customer-active" className="font-normal">
                          Active - may be selected on new orders
                        </Label>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        Blocking is a separate, recorded action available once the account exists.
                      </p>
                    </fieldset>
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
                  <Button type="submit" loading={save.isPending} disabled={!canEdit}>
                    {isEdit ? 'Save changes' : 'Create customer'}
                  </Button>
                </DialogFooter>
              </form>
            )}
          </TabsContent>

          <TabsContent value="addresses" className="min-h-0 overflow-y-auto">
            {customerId ? (
              <AddressesPanel customerId={customerId} canEdit={canEdit} />
            ) : (
              <UnsavedCustomerNotice what="Addresses" />
            )}
          </TabsContent>

          <TabsContent value="contacts" className="min-h-0 overflow-y-auto">
            {customerId ? (
              <ContactsPanel customerId={customerId} canEdit={canEdit} />
            ) : (
              <UnsavedCustomerNotice what="Contacts" />
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Create / edit dialog for a vendor, in three tabs.
 *
 * One component serves both because the field set is identical apart from
 * `code`, which the API refuses to change once purchase orders quote it.
 *
 * Approval and rating are deliberately absent. Both have their own
 * permission-gated endpoints on the API, and a buyer who could type their own
 * supplier's score into an edit form would make the rating worthless.
 *
 * The contacts and categories tabs write through their own endpoints and save
 * independently, so each carries its own actions rather than one Save button
 * that would silently do nothing from the wrong tab.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layers, UserRound } from 'lucide-react'
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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { Checkbox, Input, Label, Skeleton, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { GSTIN_PATTERN, IFSC_PATTERN, PAN_PATTERN } from '@/lib/statutory'
import { toast } from '@/lib/toast'

import { UnsavedVendorNotice, VendorCategoriesPanel, VendorContactsPanel } from './vendor-relations'
import { VENDOR_TYPES, type VendorRow, type VendorType } from './vendor-types'

const MONEY = /^(\d{1,14}(\.\d{1,4})?)?$/
const WHOLE_DAYS = /^\d{0,3}$/

const schema = z
  .object({
    code: z.string().trim().min(1, 'Code is required').max(32, 'At most 32 characters'),
    name: z.string().trim().min(1, 'Name is required').max(255, 'At most 255 characters'),
    legal_name: z.string().trim().max(255, 'At most 255 characters'),
    vendor_types: z.array(z.enum(VENDOR_TYPES)).min(1, 'Choose at least one type'),

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
    msme_number: z.string().trim().max(32, 'At most 32 characters'),

    payment_terms_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
    credit_limit: z.string().trim().regex(MONEY, 'Enter an amount with up to 4 decimal places'),
    lead_time_days: z.string().trim().regex(WHOLE_DAYS, 'Whole days only'),
    currency: z
      .string()
      .trim()
      .length(3, 'Three-letter currency code')
      .regex(/^[A-Za-z]{3}$/, 'Three-letter currency code'),

    bank_name: z.string().trim().max(120, 'At most 120 characters'),
    bank_branch: z.string().trim().max(120, 'At most 120 characters'),
    bank_account_name: z.string().trim().max(255, 'At most 255 characters'),
    bank_account_number: z.string().trim().max(34, 'At most 34 characters'),
    bank_ifsc: z
      .string()
      .trim()
      .refine((v) => v === '' || IFSC_PATTERN.test(v.toUpperCase()), 'Not a valid IFSC'),

    address_line1: z.string().trim().max(255, 'At most 255 characters'),
    address_line2: z.string().trim().max(255, 'At most 255 characters'),
    city: z.string().trim().max(120, 'At most 120 characters'),
    state: z.string().trim().max(120, 'At most 120 characters'),
    state_code: z
      .string()
      .trim()
      .refine((v) => v === '' || /^\d{2}$/.test(v), 'Two digits, as printed on the GSTIN'),
    postal_code: z.string().trim().max(16, 'At most 16 characters'),
    country: z.string().trim().min(1, 'Country is required').max(80, 'At most 80 characters'),

    contact_person: z.string().trim().max(160, 'At most 160 characters'),
    email: z
      .string()
      .trim()
      .refine(
        (v) => v === '' || z.string().email().safeParse(v).success,
        'Not a valid email address',
      ),
    phone: z.string().trim().max(32, 'At most 32 characters'),
    mobile: z.string().trim().max(32, 'At most 32 characters'),
    website: z.string().trim().max(255, 'At most 255 characters'),

    notes: z.string().trim().max(2000, 'At most 2000 characters'),
    is_active: z.boolean(),
  })
  // The GSTIN embeds the PAN and the state code. Catching a mismatch here saves
  // a round trip and, more importantly, keeps a transposed character out of a
  // GST return, where it is expensive to fix.
  .superRefine((values, ctx) => {
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
  })

type VendorFormValues = z.infer<typeof schema>

function emptyValues(): VendorFormValues {
  return {
    code: '',
    name: '',
    legal_name: '',
    vendor_types: ['RAW_MATERIAL_SUPPLIER'],
    gstin: '',
    pan: '',
    msme_number: '',
    payment_terms_days: '0',
    credit_limit: '',
    lead_time_days: '0',
    currency: 'INR',
    bank_name: '',
    bank_branch: '',
    bank_account_name: '',
    bank_account_number: '',
    bank_ifsc: '',
    address_line1: '',
    address_line2: '',
    city: '',
    state: '',
    state_code: '',
    postal_code: '',
    country: 'India',
    contact_person: '',
    email: '',
    phone: '',
    mobile: '',
    website: '',
    notes: '',
    is_active: true,
  }
}

function valuesFrom(vendor: VendorRow): VendorFormValues {
  return {
    code: vendor.code,
    name: vendor.name,
    legal_name: vendor.legal_name ?? '',
    vendor_types: vendor.vendor_types.filter((type): type is VendorType =>
      (VENDOR_TYPES as readonly string[]).includes(type),
    ),
    gstin: vendor.gstin ?? '',
    pan: vendor.pan ?? '',
    msme_number: vendor.msme_number ?? '',
    payment_terms_days: String(vendor.payment_terms_days),
    credit_limit: vendor.credit_limit ?? '',
    lead_time_days: String(vendor.lead_time_days),
    currency: vendor.currency,
    bank_name: vendor.bank_name ?? '',
    bank_branch: vendor.bank_branch ?? '',
    bank_account_name: vendor.bank_account_name ?? '',
    // The API returns the account number masked, so an edit starts empty and
    // only overwrites the stored number when a full one is actually typed.
    bank_account_number: '',
    bank_ifsc: vendor.bank_ifsc ?? '',
    address_line1: vendor.address_line1 ?? '',
    address_line2: vendor.address_line2 ?? '',
    city: vendor.city ?? '',
    state: vendor.state ?? '',
    state_code: vendor.state_code ?? '',
    postal_code: vendor.postal_code ?? '',
    country: vendor.country,
    contact_person: vendor.contact_person ?? '',
    email: vendor.email ?? '',
    phone: vendor.phone ?? '',
    mobile: vendor.mobile ?? '',
    website: vendor.website ?? '',
    notes: vendor.notes ?? '',
    is_active: vendor.is_active,
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())
const upperOrNull = (value: string): string | null =>
  value.trim() === '' ? null : value.trim().toUpperCase()

function buildPayload(values: VendorFormValues, isCreate: boolean): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    name: values.name.trim(),
    legal_name: orNull(values.legal_name),
    vendor_types: values.vendor_types,
    gstin: upperOrNull(values.gstin),
    pan: upperOrNull(values.pan),
    msme_number: upperOrNull(values.msme_number),
    payment_terms_days: Number(values.payment_terms_days || 0),
    // Money stays a string on the wire; the API parses it as a Decimal.
    credit_limit: orNull(values.credit_limit),
    lead_time_days: Number(values.lead_time_days || 0),
    currency: values.currency.trim().toUpperCase(),
    bank_name: orNull(values.bank_name),
    bank_branch: orNull(values.bank_branch),
    bank_account_name: orNull(values.bank_account_name),
    bank_ifsc: upperOrNull(values.bank_ifsc),
    address_line1: orNull(values.address_line1),
    address_line2: orNull(values.address_line2),
    city: orNull(values.city),
    state: orNull(values.state),
    state_code: upperOrNull(values.state_code),
    postal_code: orNull(values.postal_code),
    country: values.country.trim(),
    contact_person: orNull(values.contact_person),
    email: orNull(values.email),
    phone: orNull(values.phone),
    mobile: orNull(values.mobile),
    website: orNull(values.website),
    notes: orNull(values.notes),
  }

  const accountNumber = orNull(values.bank_account_number)
  if (accountNumber) payload.bank_account_number = accountNumber

  if (isCreate) {
    payload.code = values.code.trim().toUpperCase()
    payload.is_active = values.is_active
  }

  return payload
}

export function VendorFormDialog({
  open,
  onOpenChange,
  vendorId,
  canEdit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Null for a create, an id for an edit. */
  vendorId: string | null
  canEdit: boolean
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(vendorId)

  const detail = useQuery({
    queryKey: queryKeys.vendors.detail(vendorId ?? ''),
    queryFn: () => api.get<VendorRow>(`/vendors/${vendorId}`),
    enabled: open && Boolean(vendorId),
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
  } = useForm<VendorFormValues>({ resolver: zodResolver(schema), defaultValues: emptyValues() })

  // Keyed on the vendor id and whether its payload has arrived, so a background
  // refetch cannot resolve mid-edit and wipe what is being typed.
  useEffect(() => {
    if (!open) return
    if (vendorId && !loaded) return
    reset(loaded && vendorId ? valuesFrom(loaded) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, vendorId, Boolean(loaded), reset])

  const save = useMutation({
    mutationFn: (values: VendorFormValues) =>
      vendorId
        ? api.patch<VendorRow>(`/vendors/${vendorId}`, buildPayload(values, false))
        : api.post<VendorRow>('/vendors', buildPayload(values, true)),
    onSuccess: (saved) => {
      // One prefix invalidation covers every vendor list, picker and detail.
      void queryClient.invalidateQueries({ queryKey: queryKeys.vendors.all })
      toast.success(
        isEdit ? 'Vendor updated' : 'Vendor created',
        isEdit
          ? `${saved.code} - ${saved.name}`
          : `${saved.code} is saved unapproved. Approve it before raising a purchase order.`,
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
          <DialogTitle>{isEdit ? `Edit ${loaded?.code ?? 'vendor'}` : 'New vendor'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - it is quoted on purchase orders already issued.'
              : 'The vendor is created unapproved. Approval is a separate, permissioned step, and a purchase order cannot be raised until it is done.'}
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="details" className="flex min-h-0 flex-col">
          <TabsList className="self-start">
            <TabsTrigger value="details">Details</TabsTrigger>
            <TabsTrigger value="contacts">
              <UserRound className="h-3.5 w-3.5" aria-hidden />
              Contacts
            </TabsTrigger>
            <TabsTrigger value="categories">
              <Layers className="h-3.5 w-3.5" aria-hidden />
              Categories
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
                <DialogBody className="space-y-6">
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <Label htmlFor="vendor-code" required>
                        Code
                      </Label>
                      <Input
                        id="vendor-code"
                        {...register('code')}
                        disabled={isEdit}
                        autoComplete="off"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.code)}
                      />
                      <FieldError message={errors.code?.message} />
                    </div>

                    <div>
                      <Label htmlFor="vendor-name" required>
                        Trading name
                      </Label>
                      <Input
                        id="vendor-name"
                        {...register('name')}
                        autoComplete="organization"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.name)}
                      />
                      <FieldError message={errors.name?.message} />
                    </div>

                    <div className="sm:col-span-2">
                      <Label htmlFor="vendor-legal-name">Legal name</Label>
                      <Input
                        id="vendor-legal-name"
                        {...register('legal_name')}
                        autoComplete="off"
                        className="mt-1.5"
                      />
                      <FieldError message={errors.legal_name?.message} />
                      <p className="mt-1 text-xs text-muted-foreground">
                        As printed on the GST certificate. Invoices are rejected without it often
                        enough to be worth capturing.
                      </p>
                    </div>

                    <fieldset className="sm:col-span-2">
                      <legend className="text-sm font-medium">
                        Types
                        <span className="ml-0.5 text-destructive" aria-hidden>
                          *
                        </span>
                      </legend>
                      <p className="mt-1 text-xs text-muted-foreground">
                        A vendor can hold several at once - the same firm may supply raw material
                        and also do job work.
                      </p>
                      <Controller
                        control={control}
                        name="vendor_types"
                        render={({ field }) => (
                          <div className="mt-2 grid gap-2 sm:grid-cols-2">
                            {VENDOR_TYPES.map((type) => {
                              const checked = field.value.includes(type)
                              return (
                                <div key={type} className="flex items-center gap-2.5">
                                  <Checkbox
                                    id={`vendor-type-${type}`}
                                    checked={checked}
                                    onCheckedChange={(next) =>
                                      field.onChange(
                                        next === true
                                          ? [...field.value, type]
                                          : field.value.filter((value) => value !== type),
                                      )
                                    }
                                  />
                                  <Label htmlFor={`vendor-type-${type}`} className="font-normal">
                                    {humanise(type)}
                                  </Label>
                                </div>
                              )
                            })}
                          </div>
                        )}
                      />
                      <FieldError message={errors.vendor_types?.message} />
                    </fieldset>
                  </div>

                  <div>
                    <h3 className="text-sm font-medium">Statutory</h3>
                    <div className="mt-3 grid gap-4 sm:grid-cols-3">
                      <div>
                        <Label htmlFor="vendor-gstin">GSTIN</Label>
                        <Input
                          id="vendor-gstin"
                          {...register('gstin')}
                          autoComplete="off"
                          className="mt-1.5 uppercase"
                          aria-invalid={Boolean(errors.gstin)}
                        />
                        <FieldError message={errors.gstin?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-pan">PAN</Label>
                        <Input
                          id="vendor-pan"
                          {...register('pan')}
                          autoComplete="off"
                          className="mt-1.5 uppercase"
                          aria-invalid={Boolean(errors.pan)}
                        />
                        <FieldError message={errors.pan?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-msme">MSME / Udyam number</Label>
                        <Input
                          id="vendor-msme"
                          {...register('msme_number')}
                          autoComplete="off"
                          className="mt-1.5 uppercase"
                        />
                        <FieldError message={errors.msme_number?.message} />
                        <p className="mt-1 text-xs text-muted-foreground">
                          The MSMED Act caps payment terms for a registered micro or small
                          enterprise.
                        </p>
                      </div>
                    </div>
                  </div>

                  <div>
                    <h3 className="text-sm font-medium">Commercial</h3>
                    <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                      <div>
                        <Label htmlFor="vendor-payment-terms">Payment terms (days)</Label>
                        <Input
                          id="vendor-payment-terms"
                          {...register('payment_terms_days')}
                          inputMode="numeric"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.payment_terms_days)}
                        />
                        <FieldError message={errors.payment_terms_days?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-credit-limit">Credit limit</Label>
                        <Input
                          id="vendor-credit-limit"
                          {...register('credit_limit')}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.credit_limit)}
                        />
                        <FieldError message={errors.credit_limit?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-lead-time">Lead time (days)</Label>
                        <Input
                          id="vendor-lead-time"
                          {...register('lead_time_days')}
                          inputMode="numeric"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lead_time_days)}
                        />
                        <FieldError message={errors.lead_time_days?.message} />
                        <p className="mt-1 text-xs text-muted-foreground">
                          MRP subtracts this from the required date to suggest an order date.
                        </p>
                      </div>
                      <div>
                        <Label htmlFor="vendor-currency" required>
                          Currency
                        </Label>
                        <Input
                          id="vendor-currency"
                          {...register('currency')}
                          autoComplete="off"
                          className="mt-1.5 uppercase"
                          aria-invalid={Boolean(errors.currency)}
                        />
                        <FieldError message={errors.currency?.message} />
                      </div>
                    </div>
                  </div>

                  <div>
                    <h3 className="text-sm font-medium">Address</h3>
                    <div className="mt-3 grid gap-4 sm:grid-cols-2">
                      <div className="sm:col-span-2">
                        <Label htmlFor="vendor-address1">Address line 1</Label>
                        <Input
                          id="vendor-address1"
                          {...register('address_line1')}
                          autoComplete="address-line1"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.address_line1?.message} />
                      </div>
                      <div className="sm:col-span-2">
                        <Label htmlFor="vendor-address2">Address line 2</Label>
                        <Input
                          id="vendor-address2"
                          {...register('address_line2')}
                          autoComplete="address-line2"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.address_line2?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-city">City</Label>
                        <Input
                          id="vendor-city"
                          {...register('city')}
                          autoComplete="address-level2"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.city?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-state">State</Label>
                        <Input
                          id="vendor-state"
                          {...register('state')}
                          autoComplete="address-level1"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.state?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-state-code">GST state code</Label>
                        <Input
                          id="vendor-state-code"
                          {...register('state_code')}
                          inputMode="numeric"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.state_code)}
                        />
                        <FieldError message={errors.state_code?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-postal">Postal code</Label>
                        <Input
                          id="vendor-postal"
                          {...register('postal_code')}
                          autoComplete="postal-code"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.postal_code?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-country" required>
                          Country
                        </Label>
                        <Input
                          id="vendor-country"
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
                    <h3 className="text-sm font-medium">Reaching them</h3>
                    <div className="mt-3 grid gap-4 sm:grid-cols-2">
                      <div>
                        <Label htmlFor="vendor-contact-person">Contact person</Label>
                        <Input
                          id="vendor-contact-person"
                          {...register('contact_person')}
                          autoComplete="name"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.contact_person?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-email">Email</Label>
                        <Input
                          id="vendor-email"
                          type="email"
                          {...register('email')}
                          autoComplete="email"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.email)}
                        />
                        <FieldError message={errors.email?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-mobile">Mobile</Label>
                        <Input
                          id="vendor-mobile"
                          {...register('mobile')}
                          inputMode="tel"
                          autoComplete="tel"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.mobile?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-phone">Landline</Label>
                        <Input
                          id="vendor-phone"
                          {...register('phone')}
                          inputMode="tel"
                          autoComplete="tel"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.phone?.message} />
                      </div>
                      <div className="sm:col-span-2">
                        <Label htmlFor="vendor-website">Website</Label>
                        <Input
                          id="vendor-website"
                          {...register('website')}
                          autoComplete="url"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.website?.message} />
                      </div>
                    </div>
                  </div>

                  <div>
                    <h3 className="text-sm font-medium">Bank</h3>
                    <div className="mt-3 grid gap-4 sm:grid-cols-2">
                      <div>
                        <Label htmlFor="vendor-bank-name">Bank</Label>
                        <Input
                          id="vendor-bank-name"
                          {...register('bank_name')}
                          autoComplete="off"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.bank_name?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-bank-branch">Branch</Label>
                        <Input
                          id="vendor-bank-branch"
                          {...register('bank_branch')}
                          autoComplete="off"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.bank_branch?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-bank-account-name">Account name</Label>
                        <Input
                          id="vendor-bank-account-name"
                          {...register('bank_account_name')}
                          autoComplete="off"
                          className="mt-1.5"
                        />
                        <FieldError message={errors.bank_account_name?.message} />
                      </div>
                      <div>
                        <Label htmlFor="vendor-bank-account-number">Account number</Label>
                        <Input
                          id="vendor-bank-account-number"
                          {...register('bank_account_number')}
                          autoComplete="off"
                          className="mt-1.5"
                          placeholder={
                            isEdit
                              ? (loaded?.bank_account_number_masked ?? 'Not on file')
                              : undefined
                          }
                        />
                        <FieldError message={errors.bank_account_number?.message} />
                        {isEdit && (
                          <p className="mt-1 text-xs text-muted-foreground">
                            Shown masked. Leave empty to keep the stored number.
                          </p>
                        )}
                      </div>
                      <div>
                        <Label htmlFor="vendor-bank-ifsc">IFSC</Label>
                        <Input
                          id="vendor-bank-ifsc"
                          {...register('bank_ifsc')}
                          autoComplete="off"
                          className="mt-1.5 uppercase"
                          aria-invalid={Boolean(errors.bank_ifsc)}
                        />
                        <FieldError message={errors.bank_ifsc?.message} />
                      </div>
                    </div>
                  </div>

                  <div>
                    <Label htmlFor="vendor-notes">Notes</Label>
                    <Textarea id="vendor-notes" {...register('notes')} className="mt-1.5" />
                    <FieldError message={errors.notes?.message} />
                  </div>

                  {!isEdit && (
                    <div className="flex items-center gap-2.5">
                      <Controller
                        control={control}
                        name="is_active"
                        render={({ field }) => (
                          <Checkbox
                            id="vendor-active"
                            checked={field.value}
                            onCheckedChange={(checked) => field.onChange(checked === true)}
                          />
                        )}
                      />
                      <Label htmlFor="vendor-active" className="font-normal">
                        Active
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
                  {/* Gating is UX only - the API authorises the write regardless. */}
                  <Button type="submit" loading={save.isPending} disabled={!canEdit}>
                    {isEdit ? 'Save changes' : 'Create vendor'}
                  </Button>
                </DialogFooter>
              </form>
            )}
          </TabsContent>

          <TabsContent value="contacts" className="min-h-0 overflow-y-auto">
            {vendorId ? (
              <VendorContactsPanel vendorId={vendorId} canEdit={canEdit} />
            ) : (
              <UnsavedVendorNotice what="Contacts" />
            )}
          </TabsContent>

          <TabsContent value="categories" className="min-h-0 overflow-y-auto">
            {vendorId ? (
              <VendorCategoriesPanel vendorId={vendorId} canEdit={canEdit} />
            ) : (
              <UnsavedVendorNotice what="Categories" />
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

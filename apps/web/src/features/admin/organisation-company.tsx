/**
 * The company record.
 *
 * There is exactly one per tenant and it is fetched by identity, not by id -
 * `GET /organization/company` returns the caller's own company, so no request
 * on this screen names a tenant.
 *
 * Without `company.manage` the same data is rendered as a description list
 * rather than a disabled form: a form nobody can submit is a worse way of
 * saying "read only" than simply not being a form.
 *
 * The bank details and invoice terms are a card of their own with their own
 * Save. They are printed on every invoice the company sends, and redirecting
 * where customers pay is how invoice-redirection fraud starts - so a change to
 * them is a deliberate act with its own audit row, never a side effect of
 * correcting a phone number in the card above (docs/assumptions.md §47).
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save } from 'lucide-react'
import { useEffect, type ReactNode } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { DescriptionList, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
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
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { DEFAULT_CURRENCY } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { success } from '@/lib/messages'
import { IFSC_PATTERN } from '@/lib/statutory'
import { toast } from '@/lib/toast'
import { asText } from '@/lib/utils'

import { orgKeys, type CompanyRecord } from './admin-types'

const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

const companySchema = z.object({
  name: z.string().trim().min(1, 'Name is required').max(255, 'At most 255 characters'),
  legal_name: z.string().trim().max(255, 'At most 255 characters'),
  gstin: z.string().trim().max(15, 'A GSTIN is 15 characters'),
  pan: z.string().trim().max(10, 'A PAN is 10 characters'),
  cin: z.string().trim().max(21, 'A CIN is 21 characters'),
  email: z
    .string()
    .trim()
    .refine(
      (value) => value === '' || z.string().email().safeParse(value).success,
      'Enter a valid email address',
    ),
  phone: z.string().trim().max(32, 'At most 32 characters'),
  website: z.string().trim().max(255, 'At most 255 characters'),
  address_line1: z.string().trim().max(255, 'At most 255 characters'),
  address_line2: z.string().trim().max(255, 'At most 255 characters'),
  city: z.string().trim().max(120, 'At most 120 characters'),
  state: z.string().trim().max(120, 'At most 120 characters'),
  state_code: z.string().trim().max(4, 'At most 4 characters'),
  postal_code: z.string().trim().max(16, 'At most 16 characters'),
  country: z.string().trim().min(1, 'Country is required').max(80, 'At most 80 characters'),
  currency: z.string().trim().length(3, 'Use the three-letter ISO code, for example INR'),
  financial_year_start_month: z.string(),
  timezone: z.string().trim().min(1, 'Time zone is required').max(64, 'At most 64 characters'),
})

type CompanyValues = z.infer<typeof companySchema>

function toValues(company: CompanyRecord): CompanyValues {
  return {
    name: company.name,
    legal_name: company.legal_name ?? '',
    gstin: company.gstin ?? '',
    pan: company.pan ?? '',
    cin: company.cin ?? '',
    email: company.email ?? '',
    phone: company.phone ?? '',
    website: company.website ?? '',
    address_line1: company.address_line1 ?? '',
    address_line2: company.address_line2 ?? '',
    city: company.city ?? '',
    state: company.state ?? '',
    state_code: company.state_code ?? '',
    postal_code: company.postal_code ?? '',
    country: company.country,
    currency: company.currency,
    financial_year_start_month: String(company.financial_year_start_month),
    timezone: company.timezone,
  }
}

export function CompanyTab({ canManage }: { canManage: boolean }) {
  const queryClient = useQueryClient()

  const company = useQuery({
    queryKey: orgKeys.company,
    queryFn: () => api.get<CompanyRecord>('/organization/company'),
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isDirty },
  } = useForm<CompanyValues>({
    resolver: zodResolver(companySchema),
    defaultValues: {
      name: '',
      legal_name: '',
      gstin: '',
      pan: '',
      cin: '',
      email: '',
      phone: '',
      website: '',
      address_line1: '',
      address_line2: '',
      city: '',
      state: '',
      state_code: '',
      postal_code: '',
      country: 'India',
      currency: DEFAULT_CURRENCY,
      financial_year_start_month: '4',
      timezone: 'Asia/Kolkata',
    },
  })

  const loadedId = company.data?.id
  useEffect(() => {
    if (company.data) reset(toValues(company.data))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedId, reset])

  const mutation = useMutation({
    mutationFn: (values: CompanyValues) =>
      api.patch<CompanyRecord>('/organization/company', {
        name: values.name.trim(),
        legal_name: asText(values.legal_name),
        gstin: asText(values.gstin),
        pan: asText(values.pan),
        cin: asText(values.cin),
        email: asText(values.email),
        phone: asText(values.phone),
        website: asText(values.website),
        address_line1: asText(values.address_line1),
        address_line2: asText(values.address_line2),
        city: asText(values.city),
        state: asText(values.state),
        state_code: asText(values.state_code),
        postal_code: asText(values.postal_code),
        country: values.country.trim(),
        currency: values.currency.trim().toUpperCase(),
        financial_year_start_month: Number(values.financial_year_start_month),
        timezone: values.timezone.trim(),
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      reset(toValues(saved))
      toast.success(success.updated('Company'), saved.name)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  if (company.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (company.error || !company.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the company record</AlertTitle>
        <AlertDescription>
          {(company.error as Error | null)?.message ?? 'No company was returned for this account.'}
        </AlertDescription>
      </Alert>
    )
  }

  const record = company.data

  // The bank card sits under the company card for both kinds of reader; it
  // saves on its own, for the reason given on `CompanyBankCard`.
  const withBankCard = (card: ReactNode) => (
    <div className="space-y-4">
      {card}
      <CompanyBankCard record={record} canManage={canManage} />
    </div>
  )

  if (!canManage) {
    return withBankCard(
      <Card>
        <CardHeader>
          <CardTitle>
            {record.code} - {record.name}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList
            columns={3}
            items={[
              { label: 'Legal name', value: record.legal_name ?? '-' },
              { label: 'GSTIN', value: record.gstin ?? '-' },
              { label: 'PAN', value: record.pan ?? '-' },
              { label: 'CIN', value: record.cin ?? '-' },
              { label: 'Email', value: record.email ?? '-' },
              { label: 'Phone', value: record.phone ?? '-' },
              { label: 'Website', value: record.website ?? '-' },
              {
                label: 'Address',
                value:
                  [
                    record.address_line1,
                    record.address_line2,
                    record.city,
                    record.state,
                    record.postal_code,
                  ]
                    .filter(Boolean)
                    .join(', ') || '-',
              },
              { label: 'Country', value: record.country },
              { label: 'Currency', value: record.currency },
              {
                label: 'Financial year starts',
                value: MONTHS[record.financial_year_start_month - 1] ?? '-',
              },
              { label: 'Time zone', value: record.timezone },
            ]}
          />
        </CardContent>
      </Card>,
    )
  }

  return withBankCard(
    <form onSubmit={handleSubmit((values) => mutation.mutate(values))} noValidate>
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 space-y-0">
          <CardTitle className="min-w-0 break-words">
            {record.code} - {record.name}
          </CardTitle>
          <Button
            type="submit"
            className="ml-auto"
            disabled={!isDirty}
            loading={mutation.isPending}
          >
            <Save />
            Save company
          </Button>
        </CardHeader>

        <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div>
            <Label htmlFor="company-name" required>
              Trading name
            </Label>
            <Input
              id="company-name"
              {...register('name')}
              aria-invalid={Boolean(errors.name)}
              className="mt-1.5"
            />
            <FieldError message={errors.name?.message} />
          </div>

          <div className="lg:col-span-2">
            <Label htmlFor="company-legal-name">Legal name</Label>
            <Input id="company-legal-name" {...register('legal_name')} className="mt-1.5" />
            <FieldError message={errors.legal_name?.message} />
          </div>

          <div>
            <Label htmlFor="company-gstin">GSTIN</Label>
            <Input id="company-gstin" {...register('gstin')} className="mt-1.5" />
            <FieldError message={errors.gstin?.message} />
          </div>

          <div>
            <Label htmlFor="company-pan">PAN</Label>
            <Input id="company-pan" {...register('pan')} className="mt-1.5" />
            <FieldError message={errors.pan?.message} />
          </div>

          <div>
            <Label htmlFor="company-cin">CIN</Label>
            <Input id="company-cin" {...register('cin')} className="mt-1.5" />
            <FieldError message={errors.cin?.message} />
          </div>

          <div>
            <Label htmlFor="company-email">Email</Label>
            <Input
              id="company-email"
              type="email"
              {...register('email')}
              aria-invalid={Boolean(errors.email)}
              className="mt-1.5"
            />
            <FieldError message={errors.email?.message} />
          </div>

          <div>
            <Label htmlFor="company-phone">Phone</Label>
            <Input id="company-phone" {...register('phone')} className="mt-1.5" />
            <FieldError message={errors.phone?.message} />
          </div>

          <div>
            <Label htmlFor="company-website">Website</Label>
            <Input id="company-website" {...register('website')} className="mt-1.5" />
            <FieldError message={errors.website?.message} />
          </div>

          <div className="sm:col-span-2 lg:col-span-3">
            <Label htmlFor="company-address1">Address</Label>
            <Input id="company-address1" {...register('address_line1')} className="mt-1.5" />
            <FieldError message={errors.address_line1?.message} />
            <Input
              id="company-address2"
              {...register('address_line2')}
              aria-label="Address line 2"
              className="mt-2"
            />
            <FieldError message={errors.address_line2?.message} />
          </div>

          <div>
            <Label htmlFor="company-city">City</Label>
            <Input id="company-city" {...register('city')} className="mt-1.5" />
            <FieldError message={errors.city?.message} />
          </div>

          <div>
            <Label htmlFor="company-state">State</Label>
            <Input id="company-state" {...register('state')} className="mt-1.5" />
            <FieldError message={errors.state?.message} />
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="company-state-code">GST state code</Label>
              <Input id="company-state-code" {...register('state_code')} className="mt-1.5" />
              <FieldError message={errors.state_code?.message} />
            </div>
            <div>
              <Label htmlFor="company-postal">Postal code</Label>
              <Input id="company-postal" {...register('postal_code')} className="mt-1.5" />
              <FieldError message={errors.postal_code?.message} />
            </div>
          </div>

          <div>
            <Label htmlFor="company-country" required>
              Country
            </Label>
            <Input id="company-country" {...register('country')} className="mt-1.5" />
            <FieldError message={errors.country?.message} />
          </div>

          <div>
            <Label htmlFor="company-currency" required>
              Currency
            </Label>
            <Input
              id="company-currency"
              {...register('currency')}
              maxLength={3}
              className="mt-1.5 uppercase"
            />
            <FieldError message={errors.currency?.message} />
          </div>

          <div>
            <Label htmlFor="company-fy-month" required>
              Financial year starts
            </Label>
            <Controller
              control={control}
              name="financial_year_start_month"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id="company-fy-month" className="mt-1.5">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {MONTHS.map((month, index) => (
                      <SelectItem key={month} value={String(index + 1)}>
                        {month}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              April for an Indian financial year. Changing it does not move a year that is already
              open.
            </p>
          </div>

          <div>
            <Label htmlFor="company-timezone" required>
              Time zone
            </Label>
            <Input id="company-timezone" {...register('timezone')} className="mt-1.5" />
            <FieldError message={errors.timezone?.message} />
            <p className="mt-1 text-xs text-muted-foreground">
              An IANA name such as Asia/Kolkata. Timestamps are stored in UTC regardless.
            </p>
          </div>
        </CardContent>
      </Card>
    </form>,
  )
}

// ------------------------------------------------------- bank and invoice ---

/** Spaces are how an identifier is read aloud, never part of it. */
const withoutSpaces = (value: string): string => value.replace(/\s+/g, '').toUpperCase()

const bankSchema = z.object({
  bank_name: z.string().trim().max(120, 'At most 120 characters'),
  bank_account_name: z.string().trim().max(255, 'At most 255 characters'),
  // Measured without its spaces, as the API measures it: a number keyed in the
  // groups it is printed in must not be refused for the gaps.
  bank_account_number: z
    .string()
    .trim()
    .refine(
      (value): boolean => withoutSpaces(value).length <= 34,
      'At most 34 characters, not counting spaces',
    ),
  bank_ifsc: z
    .string()
    .trim()
    .refine(
      (value): boolean => value === '' || IFSC_PATTERN.test(withoutSpaces(value)),
      'Not a valid IFSC - four letters, a zero, then six letters or digits',
    ),
  bank_branch: z.string().trim().max(120, 'At most 120 characters'),
  invoice_terms: z.string().trim().max(4000, 'At most 4000 characters'),
})

type BankValues = z.infer<typeof bankSchema>

function bankValues(company: CompanyRecord): BankValues {
  return {
    bank_name: company.bank_name ?? '',
    bank_account_name: company.bank_account_name ?? '',
    bank_account_number: company.bank_account_number ?? '',
    bank_ifsc: company.bank_ifsc ?? '',
    bank_branch: company.bank_branch ?? '',
    invoice_terms: company.invoice_terms ?? '',
  }
}

/**
 * Where customers pay, and the terms printed beneath every invoice.
 *
 * Every field is optional and a blank one is sent as null, which the API reads
 * as "clear it". The identifiers go out the way the bank writes them - no
 * spaces, upper case - which is also what the API stores, so the value read
 * back is the value that was sent.
 */
function CompanyBankCard({ record, canManage }: { record: CompanyRecord; canManage: boolean }) {
  const queryClient = useQueryClient()

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isDirty },
  } = useForm<BankValues>({
    resolver: zodResolver(bankSchema),
    defaultValues: bankValues(record),
  })

  const loadedId = record.id
  useEffect(() => {
    reset(bankValues(record))
    // Keyed on the record's identity, not its contents: a background refetch
    // must not wipe a half-typed account number.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedId, reset])

  const mutation = useMutation({
    mutationFn: (values: BankValues) =>
      api.patch<CompanyRecord>('/organization/company', {
        bank_name: asText(values.bank_name),
        bank_account_name: asText(values.bank_account_name),
        bank_account_number: asText(withoutSpaces(values.bank_account_number)),
        bank_ifsc: asText(withoutSpaces(values.bank_ifsc)),
        bank_branch: asText(values.bank_branch),
        invoice_terms: asText(values.invoice_terms),
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      reset(bankValues(saved))
      toast.success(success.updated('Bank details'), saved.name)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  if (!canManage) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Bank &amp; invoice</CardTitle>
          <CardDescription>Where customers pay, as printed on every invoice.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <DescriptionList
            columns={3}
            items={[
              { label: 'Bank', value: record.bank_name ?? '-' },
              { label: 'Branch', value: record.bank_branch ?? '-' },
              { label: 'Account name', value: record.bank_account_name ?? '-' },
              {
                label: 'Account number',
                value: <span className="font-mono">{record.bank_account_number ?? '-'}</span>,
              },
              {
                label: 'IFSC',
                value: <span className="font-mono">{record.bank_ifsc ?? '-'}</span>,
              },
            ]}
          />
          <DescriptionList
            columns={1}
            items={[
              {
                label: 'Invoice terms',
                value: <span className="whitespace-pre-line">{record.invoice_terms ?? '-'}</span>,
              },
            ]}
          />
        </CardContent>
      </Card>
    )
  }

  return (
    <form onSubmit={handleSubmit((values) => mutation.mutate(values))} noValidate>
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 space-y-0">
          <div className="min-w-0 space-y-1.5">
            <CardTitle>Bank &amp; invoice</CardTitle>
            <CardDescription>
              Where customers pay, and the terms printed beneath every invoice. Every change here is
              recorded in the audit trail.
            </CardDescription>
          </div>
          <Button
            type="submit"
            className="ml-auto"
            disabled={!isDirty}
            loading={mutation.isPending}
          >
            <Save />
            Save bank details
          </Button>
        </CardHeader>

        <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div>
            <Label htmlFor="company-bank-name">Bank</Label>
            <Input
              id="company-bank-name"
              {...register('bank_name')}
              autoComplete="off"
              aria-invalid={Boolean(errors.bank_name)}
              className="mt-1.5"
            />
            <FieldError message={errors.bank_name?.message} />
          </div>

          <div>
            <Label htmlFor="company-bank-branch">Branch</Label>
            <Input
              id="company-bank-branch"
              {...register('bank_branch')}
              autoComplete="off"
              aria-invalid={Boolean(errors.bank_branch)}
              className="mt-1.5"
            />
            <FieldError message={errors.bank_branch?.message} />
          </div>

          <div>
            <Label htmlFor="company-bank-ifsc">IFSC</Label>
            <Input
              id="company-bank-ifsc"
              {...register('bank_ifsc')}
              autoComplete="off"
              placeholder="HDFC0001234"
              aria-invalid={Boolean(errors.bank_ifsc)}
              className="mt-1.5 uppercase"
            />
            <FieldError message={errors.bank_ifsc?.message} />
          </div>

          <div className="sm:col-span-2 lg:col-span-1">
            <Label htmlFor="company-bank-account-name">Account name</Label>
            <Input
              id="company-bank-account-name"
              {...register('bank_account_name')}
              autoComplete="off"
              aria-invalid={Boolean(errors.bank_account_name)}
              className="mt-1.5"
            />
            <FieldError message={errors.bank_account_name?.message} />
          </div>

          <div className="sm:col-span-2">
            <Label htmlFor="company-bank-account-number">Account number</Label>
            <Input
              id="company-bank-account-number"
              {...register('bank_account_number')}
              autoComplete="off"
              aria-invalid={Boolean(errors.bank_account_number)}
              className="mt-1.5 font-mono"
            />
            <FieldError message={errors.bank_account_number?.message} />
            <p className="mt-1 text-xs text-muted-foreground">
              Spaces are dropped when it is saved, so it can be typed in the groups printed on the
              cheque leaf.
            </p>
          </div>

          <div className="sm:col-span-2 lg:col-span-3">
            <Label htmlFor="company-invoice-terms">Invoice terms</Label>
            <Textarea
              id="company-invoice-terms"
              {...register('invoice_terms')}
              rows={4}
              placeholder="Payment within 30 days of the invoice date. Interest at 18% a year on overdue amounts."
              aria-invalid={Boolean(errors.invoice_terms)}
              className="mt-1.5"
            />
            <FieldError message={errors.invoice_terms?.message} />
          </div>
        </CardContent>
      </Card>
    </form>
  )
}

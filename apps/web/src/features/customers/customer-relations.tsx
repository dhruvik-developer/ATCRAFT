/**
 * Address and contact books for one customer.
 *
 * They sit behind their own endpoints rather than inside the customer payload: a
 * distributor can carry a dozen ship-to addresses, and loading them with every
 * list row would be the N+1 read master rules 32 forbids.
 *
 * Both panels need a saved customer - an address hangs off a customer id - so
 * the create dialog says so plainly instead of collecting rows it cannot post.
 *
 * Two shapes of the API are visible in this file and are not accidents:
 *
 * * "Which one is default / primary" is promoted through its own endpoint, not
 *   through the update payload. The update schemas do not carry the flag at all,
 *   because promoting one row demotes another and that is a two-row operation.
 * * Removing an address or a contact needs a recorded reason, so the delete is a
 *   confirmation with a reason box rather than a bare "are you sure".
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MapPin, Pencil, Star, Trash2, Plus, UserRound } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { ConfirmDialog, EmptyState, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Input, Label, Skeleton } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { applyFieldErrors, reportFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { describeError, messages, success, validation } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { GSTIN_PATTERN, PINCODE_PATTERN, STATE_CODE_PATTERN } from '@/lib/statutory'
import { toast } from '@/lib/toast'

import { ADDRESS_TYPES, type CustomerAddress, type CustomerContact } from './customer-types'

const RELATION_LIMIT = 100

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

function PanelSkeleton() {
  return (
    <div className="space-y-2" aria-hidden>
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-16 w-full" />
    </div>
  )
}

export function UnsavedCustomerNotice({ what }: { what: string }) {
  return (
    <EmptyState
      title={`${what} open once the customer is saved`}
      description={`Create the customer first. ${what} are stored against its id, so they can be added the moment it exists.`}
    />
  )
}

/**
 * A removal that the API records against the account.
 *
 * The reason is a required query parameter, so it is collected rather than
 * invented - an audit row reading "deleted" tells nobody anything. It is held
 * to the API's own 3..255 bound before it is sent: a shorter one comes back as
 * a 422 against `reason`, which the mutation cache stays quiet about, and this
 * is a confirmation with no form to render it.
 */
function DeleteWithReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  loading,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel: string
  loading: boolean
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)
  const tooShort = reason.trim().length < REASON_MIN_LENGTH

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) {
          setReason('')
          setTouched(false)
        }
      }}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      destructive
      loading={loading}
      onConfirm={() => {
        if (tooShort) {
          setTouched(true)
          return
        }
        onConfirm(reason.trim())
      }}
    >
      <div>
        <Label htmlFor="relation-delete-reason" required>
          Reason
        </Label>
        <Input
          id="relation-delete-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={REASON_MAX_LENGTH}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={touched && tooShort}
        />
        {touched && tooShort && (
          <FieldError
            message={
              reason.trim() === ''
                ? 'The API records this removal - it needs a reason'
                : messages.errors.reasonTooShort
            }
          />
        )}
      </div>
    </ConfirmDialog>
  )
}

// ------------------------------------------------------------ addresses -----

const addressSchema = z.object({
  label: z.string().trim().max(64, 'At most 64 characters'),
  address_type: z.enum(ADDRESS_TYPES),
  address_line1: z
    .string()
    .trim()
    .min(1, validation.required('Address line 1'))
    .max(255, 'At most 255 characters'),
  address_line2: z.string().trim().max(255, 'At most 255 characters'),
  landmark: z.string().trim().max(255, 'At most 255 characters'),
  city: z.string().trim().min(1, validation.required('City')).max(64, 'At most 64 characters'),
  state: z.string().trim().max(64, 'At most 64 characters'),
  state_code: z
    .string()
    .trim()
    .refine((v) => v === '' || STATE_CODE_PATTERN.test(v), 'Two digits, as printed on the GSTIN'),
  postal_code: z
    .string()
    .trim()
    .refine((v) => v === '' || PINCODE_PATTERN.test(v), 'Six digits, not starting with 0'),
  country: z
    .string()
    .trim()
    .min(1, validation.required('Country'))
    .max(64, 'At most 64 characters'),
  gstin: z
    .string()
    .trim()
    .refine(
      (v) => v === '' || GSTIN_PATTERN.test(v.toUpperCase()),
      'Not a valid 15-character GSTIN',
    ),
  contact_person: z.string().trim().max(128, 'At most 128 characters'),
  phone: z.string().trim().max(32, 'At most 32 characters'),
  email: z
    .string()
    .trim()
    .refine((v) => v === '' || z.string().email().safeParse(v).success, validation.email),
  is_default: z.boolean(),
})

type AddressValues = z.infer<typeof addressSchema>

const emptyAddress = (): AddressValues => ({
  label: '',
  address_type: 'SHIPPING',
  address_line1: '',
  address_line2: '',
  landmark: '',
  city: '',
  state: '',
  state_code: '',
  postal_code: '',
  country: 'India',
  gstin: '',
  contact_person: '',
  phone: '',
  email: '',
  is_default: false,
})

const addressValuesFrom = (address: CustomerAddress): AddressValues => ({
  label: address.label ?? '',
  address_type: address.address_type,
  address_line1: address.address_line1,
  address_line2: address.address_line2 ?? '',
  landmark: address.landmark ?? '',
  city: address.city,
  state: address.state ?? '',
  state_code: address.state_code ?? '',
  postal_code: address.postal_code ?? '',
  country: address.country,
  gstin: address.gstin ?? '',
  contact_person: address.contact_person ?? '',
  phone: address.phone ?? '',
  email: address.email ?? '',
  is_default: address.is_default,
})

const addressSummary = (address: CustomerAddress): string =>
  [
    address.address_line1,
    address.address_line2,
    address.landmark,
    address.city,
    address.state,
    address.postal_code,
    address.country,
  ]
    .filter(Boolean)
    .join(', ')

export function AddressesPanel({ customerId, canEdit }: { customerId: string; canEdit: boolean }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<CustomerAddress | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<CustomerAddress | null>(null)

  const addresses = useQuery({
    queryKey: [...queryKeys.customers.detail(customerId), 'addresses'],
    queryFn: () =>
      api.getPage<CustomerAddress>(`/customers/${customerId}/addresses`, {
        params: { limit: RELATION_LIMIT },
      }),
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<AddressValues>({
    resolver: zodResolver(addressSchema),
    defaultValues: emptyAddress(),
  })

  // Seeded when the editor opens or switches row, never on every render of the
  // query result: a background refetch must not overwrite what is being typed.
  const editingId = editing && editing !== 'new' ? editing.id : null
  const editorOpen = editing !== null
  useEffect(() => {
    if (!editorOpen) return
    reset(editingId ? addressValuesFrom(editing as CustomerAddress) : emptyAddress())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorOpen, editingId, reset])

  const save = useMutation({
    mutationFn: (values: AddressValues) => {
      const common = {
        label: orNull(values.label),
        address_line1: values.address_line1.trim(),
        address_line2: orNull(values.address_line2),
        landmark: orNull(values.landmark),
        city: values.city.trim(),
        state: orNull(values.state),
        state_code: orNull(values.state_code),
        postal_code: orNull(values.postal_code),
        country: values.country.trim(),
        gstin: orNull(values.gstin)?.toUpperCase() ?? null,
        contact_person: orNull(values.contact_person),
        phone: orNull(values.phone),
        email: orNull(values.email),
      }
      // The update schema carries neither the type nor the default flag: the
      // type is fixed once documents quote the address, and the default is
      // promoted through its own endpoint so the old one is demoted with it.
      return editingId
        ? api.patch<CustomerAddress>(`/customers/${customerId}/addresses/${editingId}`, common)
        : api.post<CustomerAddress>(`/customers/${customerId}/addresses`, {
            ...common,
            address_type: values.address_type,
            is_default: values.is_default,
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(
        editingId ? success.updated('Address') : success.created('Address'),
        saved.label ?? saved.address_line1,
      )
      setEditing(null)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const setDefault = useMutation({
    mutationFn: (address: CustomerAddress) =>
      api.post<CustomerAddress>(`/customers/${customerId}/addresses/${address.id}/set-default`, {}),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(success.changed('Default address'), saved.label ?? saved.address_line1)
    },
    onError: (error) => reportFieldErrors(error, 'Could not change the default address'),
  })

  const remove = useMutation({
    mutationFn: ({ address, reason }: { address: CustomerAddress; reason: string }) =>
      api.delete(`/customers/${customerId}/addresses/${address.id}`, { params: { reason } }),
    onSuccess: (_result, { address }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(success.removed('Address'), address.label ?? address.address_line1)
      setPendingDelete(null)
    },
    // The reason travels as a query parameter, so its 422 has no form control.
    onError: (error) => reportFieldErrors(error, 'Could not remove this address'),
  })

  if (addresses.isLoading) return <PanelSkeleton />

  if (addresses.error) {
    return (
      <EmptyState
        title="Could not load addresses"
        description={describeError(addresses.error)}
        action={
          <Button variant="outline" size="sm" onClick={() => void addresses.refetch()}>
            Try again
          </Button>
        }
      />
    )
  }

  const rows = addresses.data?.data ?? []

  return (
    <div className="space-y-4">
      {rows.length === 0 && !editorOpen && (
        <EmptyState
          icon={MapPin}
          title="No addresses yet"
          description="Add a billing address so invoices carry the right place of supply, and a ship-to address for dispatch."
          action={
            canEdit ? (
              <Button size="sm" onClick={() => setEditing('new')}>
                <Plus />
                Add address
              </Button>
            ) : undefined
          }
        />
      )}

      {rows.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {rows.map((address) => (
            <li key={address.id} className="flex items-start gap-3 p-3">
              <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">
                    {address.label ?? humanise(address.address_type)}
                  </span>
                  <Badge variant="secondary">{humanise(address.address_type)}</Badge>
                  {address.is_default && <Badge variant="success">Default</Badge>}
                  {!address.is_active && <Badge variant="muted">Inactive</Badge>}
                </div>
                <p className="mt-0.5 text-sm text-muted-foreground">{addressSummary(address)}</p>
                {address.gstin && (
                  <p className="mt-0.5 text-xs tabular text-muted-foreground">
                    GSTIN {address.gstin}
                  </p>
                )}
                {(address.contact_person || address.phone || address.email) && (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {[address.contact_person, address.phone, address.email]
                      .filter(Boolean)
                      .join(' - ')}
                  </p>
                )}
              </div>
              {canEdit && (
                <div className="flex shrink-0 items-center gap-1">
                  {!address.is_default && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Make ${address.label ?? address.address_line1} the default address`}
                      disabled={setDefault.isPending}
                      onClick={() => setDefault.mutate(address)}
                    >
                      <Star />
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Edit address ${address.label ?? address.address_line1}`}
                    onClick={() => setEditing(address)}
                  >
                    <Pencil />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Remove address ${address.label ?? address.address_line1}`}
                    onClick={() => setPendingDelete(address)}
                  >
                    <Trash2 className="text-destructive" />
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {canEdit && !editorOpen && rows.length > 0 && (
        <Button type="button" variant="outline" size="sm" onClick={() => setEditing('new')}>
          <Plus />
          Add address
        </Button>
      )}

      {editorOpen && (
        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="space-y-4 rounded-lg border p-4"
          noValidate
        >
          <p className="text-sm font-medium">{editingId ? 'Edit address' : 'New address'}</p>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="address-label">Label</Label>
              <Input
                id="address-label"
                {...register('label')}
                className="mt-1.5"
                autoComplete="off"
              />
              <FieldError message={errors.label?.message} />
            </div>

            <div>
              <Label htmlFor="address-type">Used for</Label>
              <Controller
                control={control}
                name="address_type"
                render={({ field }) => (
                  <Select
                    value={field.value}
                    onValueChange={field.onChange}
                    disabled={Boolean(editingId)}
                  >
                    <SelectTrigger id="address-type" className="mt-1.5">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {ADDRESS_TYPES.map((type) => (
                        <SelectItem key={type} value={type}>
                          {humanise(type)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              {editingId && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Fixed once the address exists - documents already quote it.
                </p>
              )}
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="address-line1" required>
                Address line 1
              </Label>
              <Input
                id="address-line1"
                {...register('address_line1')}
                className="mt-1.5"
                autoComplete="address-line1"
                aria-invalid={Boolean(errors.address_line1)}
              />
              <FieldError message={errors.address_line1?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="address-line2">Address line 2</Label>
              <Input
                id="address-line2"
                {...register('address_line2')}
                className="mt-1.5"
                autoComplete="address-line2"
              />
              <FieldError message={errors.address_line2?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="address-landmark">Landmark</Label>
              <Input
                id="address-landmark"
                {...register('landmark')}
                className="mt-1.5"
                autoComplete="off"
              />
              <FieldError message={errors.landmark?.message} />
            </div>

            <div>
              <Label htmlFor="address-city" required>
                City
              </Label>
              <Input
                id="address-city"
                {...register('city')}
                className="mt-1.5"
                autoComplete="address-level2"
                aria-invalid={Boolean(errors.city)}
              />
              <FieldError message={errors.city?.message} />
            </div>

            <div>
              <Label htmlFor="address-state">State</Label>
              <Input
                id="address-state"
                {...register('state')}
                className="mt-1.5"
                autoComplete="address-level1"
                aria-invalid={Boolean(errors.state)}
              />
              <FieldError message={errors.state?.message} />
            </div>

            <div>
              <Label htmlFor="address-state-code">State code</Label>
              <Input
                id="address-state-code"
                {...register('state_code')}
                className="mt-1.5 tabular"
                inputMode="numeric"
                aria-invalid={Boolean(errors.state_code)}
              />
              <FieldError message={errors.state_code?.message} />
            </div>

            <div>
              <Label htmlFor="address-postal-code">PIN code</Label>
              <Input
                id="address-postal-code"
                {...register('postal_code')}
                className="mt-1.5 tabular"
                inputMode="numeric"
                autoComplete="postal-code"
                aria-invalid={Boolean(errors.postal_code)}
              />
              <FieldError message={errors.postal_code?.message} />
            </div>

            <div>
              <Label htmlFor="address-country" required>
                Country
              </Label>
              <Input
                id="address-country"
                {...register('country')}
                className="mt-1.5"
                autoComplete="country-name"
                aria-invalid={Boolean(errors.country)}
              />
              <FieldError message={errors.country?.message} />
            </div>

            <div>
              <Label htmlFor="address-gstin">GSTIN at this address</Label>
              <Input
                id="address-gstin"
                {...register('gstin')}
                className="mt-1.5 uppercase tabular"
                autoComplete="off"
                aria-invalid={Boolean(errors.gstin)}
              />
              <FieldError message={errors.gstin?.message} />
            </div>

            <div>
              <Label htmlFor="address-contact-person">Contact at this address</Label>
              <Input
                id="address-contact-person"
                {...register('contact_person')}
                className="mt-1.5"
                autoComplete="name"
              />
              <FieldError message={errors.contact_person?.message} />
            </div>

            <div>
              <Label htmlFor="address-phone">Phone</Label>
              <Input
                id="address-phone"
                {...register('phone')}
                className="mt-1.5"
                autoComplete="tel"
                inputMode="tel"
              />
              <FieldError message={errors.phone?.message} />
            </div>

            <div>
              <Label htmlFor="address-email">Email</Label>
              <Input
                id="address-email"
                type="email"
                {...register('email')}
                className="mt-1.5"
                autoComplete="email"
                aria-invalid={Boolean(errors.email)}
              />
              <FieldError message={errors.email?.message} />
            </div>

            {!editingId && (
              <div className="flex items-center gap-2.5 sm:col-span-2">
                <Controller
                  control={control}
                  name="is_default"
                  render={({ field }) => (
                    <Checkbox
                      id="address-default"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <Label htmlFor="address-default" className="font-normal">
                  Use as the default address for new orders
                </Label>
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setEditing(null)}
              disabled={save.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" size="sm" loading={save.isPending}>
              {editingId ? 'Save address' : 'Add address'}
            </Button>
          </div>
        </form>
      )}

      <DeleteWithReasonDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title="Remove this address?"
        description="Documents already issued keep the address printed on them. New orders will no longer offer it."
        confirmLabel="Remove address"
        loading={remove.isPending}
        onConfirm={(reason) => pendingDelete && remove.mutate({ address: pendingDelete, reason })}
      />
    </div>
  )
}

// ------------------------------------------------------------- contacts -----

const contactSchema = z.object({
  name: z.string().trim().min(1, validation.required('Name')).max(128, 'At most 128 characters'),
  designation: z.string().trim().max(64, 'At most 64 characters'),
  email: z
    .string()
    .trim()
    .refine((v) => v === '' || z.string().email().safeParse(v).success, validation.email),
  phone: z.string().trim().max(32, 'At most 32 characters'),
  is_primary: z.boolean(),
})

type ContactValues = z.infer<typeof contactSchema>

const emptyContact = (): ContactValues => ({
  name: '',
  designation: '',
  email: '',
  phone: '',
  is_primary: false,
})

const contactValuesFrom = (contact: CustomerContact): ContactValues => ({
  name: contact.name,
  designation: contact.designation ?? '',
  email: contact.email ?? '',
  phone: contact.phone ?? '',
  is_primary: contact.is_primary,
})

export function ContactsPanel({ customerId, canEdit }: { customerId: string; canEdit: boolean }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<CustomerContact | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<CustomerContact | null>(null)

  const contacts = useQuery({
    queryKey: [...queryKeys.customers.detail(customerId), 'contacts'],
    queryFn: () =>
      api.getPage<CustomerContact>(`/customers/${customerId}/contacts`, {
        params: { limit: RELATION_LIMIT },
      }),
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<ContactValues>({
    resolver: zodResolver(contactSchema),
    defaultValues: emptyContact(),
  })

  const editingId = editing && editing !== 'new' ? editing.id : null
  const editorOpen = editing !== null
  useEffect(() => {
    if (!editorOpen) return
    reset(editingId ? contactValuesFrom(editing as CustomerContact) : emptyContact())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorOpen, editingId, reset])

  const save = useMutation({
    mutationFn: (values: ContactValues) => {
      const common = {
        name: values.name.trim(),
        designation: orNull(values.designation),
        email: orNull(values.email),
        phone: orNull(values.phone),
      }
      // As with addresses, "primary" is promoted through its own endpoint.
      return editingId
        ? api.patch<CustomerContact>(`/customers/${customerId}/contacts/${editingId}`, common)
        : api.post<CustomerContact>(`/customers/${customerId}/contacts`, {
            ...common,
            is_primary: values.is_primary,
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(editingId ? success.updated('Contact') : success.created('Contact'), saved.name)
      setEditing(null)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const setPrimary = useMutation({
    mutationFn: (contact: CustomerContact) =>
      api.post<CustomerContact>(`/customers/${customerId}/contacts/${contact.id}/set-primary`, {}),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(success.changed('Primary contact'), saved.name)
    },
    onError: (error) => reportFieldErrors(error, 'Could not change the primary contact'),
  })

  const remove = useMutation({
    mutationFn: ({ contact, reason }: { contact: CustomerContact; reason: string }) =>
      api.delete(`/customers/${customerId}/contacts/${contact.id}`, { params: { reason } }),
    onSuccess: (_result, { contact }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      toast.success(success.removed('Contact'), contact.name)
      setPendingDelete(null)
    },
    onError: (error) => reportFieldErrors(error, 'Could not remove this contact'),
  })

  if (contacts.isLoading) return <PanelSkeleton />

  if (contacts.error) {
    return (
      <EmptyState
        title="Could not load contacts"
        description={describeError(contacts.error)}
        action={
          <Button variant="outline" size="sm" onClick={() => void contacts.refetch()}>
            Try again
          </Button>
        }
      />
    )
  }

  const rows = contacts.data?.data ?? []

  return (
    <div className="space-y-4">
      {rows.length === 0 && !editorOpen && (
        <EmptyState
          icon={UserRound}
          title="No contacts yet"
          description="Order acknowledgements and dispatch notifications go to the primary contact."
          action={
            canEdit ? (
              <Button size="sm" onClick={() => setEditing('new')}>
                <Plus />
                Add contact
              </Button>
            ) : undefined
          }
        />
      )}

      {rows.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {rows.map((contact) => (
            <li key={contact.id} className="flex items-start gap-3 p-3">
              <UserRound className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{contact.name}</span>
                  {contact.designation && (
                    <span className="text-sm text-muted-foreground">{contact.designation}</span>
                  )}
                  {contact.is_primary && <Badge variant="success">Primary</Badge>}
                  {!contact.is_active && <Badge variant="muted">Inactive</Badge>}
                </div>
                <p className="mt-0.5 text-sm text-muted-foreground">
                  {[contact.email, contact.phone].filter(Boolean).join(' - ') ||
                    'No contact details recorded'}
                </p>
              </div>
              {canEdit && (
                <div className="flex shrink-0 items-center gap-1">
                  {!contact.is_primary && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Make ${contact.name} the primary contact`}
                      disabled={setPrimary.isPending}
                      onClick={() => setPrimary.mutate(contact)}
                    >
                      <Star />
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Edit contact ${contact.name}`}
                    onClick={() => setEditing(contact)}
                  >
                    <Pencil />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Remove contact ${contact.name}`}
                    onClick={() => setPendingDelete(contact)}
                  >
                    <Trash2 className="text-destructive" />
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {canEdit && !editorOpen && rows.length > 0 && (
        <Button type="button" variant="outline" size="sm" onClick={() => setEditing('new')}>
          <Plus />
          Add contact
        </Button>
      )}

      {editorOpen && (
        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="space-y-4 rounded-lg border p-4"
          noValidate
        >
          <p className="text-sm font-medium">{editingId ? 'Edit contact' : 'New contact'}</p>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="contact-name" required>
                Name
              </Label>
              <Input
                id="contact-name"
                {...register('name')}
                className="mt-1.5"
                autoComplete="name"
                aria-invalid={Boolean(errors.name)}
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="contact-designation">Designation</Label>
              <Input
                id="contact-designation"
                {...register('designation')}
                className="mt-1.5"
                autoComplete="organization-title"
              />
              <FieldError message={errors.designation?.message} />
            </div>

            <div>
              <Label htmlFor="contact-email">Email</Label>
              <Input
                id="contact-email"
                type="email"
                {...register('email')}
                className="mt-1.5"
                autoComplete="email"
                aria-invalid={Boolean(errors.email)}
              />
              <FieldError message={errors.email?.message} />
            </div>

            <div>
              <Label htmlFor="contact-phone">Phone</Label>
              <Input
                id="contact-phone"
                {...register('phone')}
                className="mt-1.5"
                inputMode="tel"
                autoComplete="tel"
              />
              <FieldError message={errors.phone?.message} />
            </div>

            {!editingId && (
              <div className="flex items-center gap-2.5 sm:col-span-2">
                <Controller
                  control={control}
                  name="is_primary"
                  render={({ field }) => (
                    <Checkbox
                      id="contact-primary"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <Label htmlFor="contact-primary" className="font-normal">
                  Primary contact for order and dispatch notifications
                </Label>
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setEditing(null)}
              disabled={save.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" size="sm" loading={save.isPending}>
              {editingId ? 'Save contact' : 'Add contact'}
            </Button>
          </div>
        </form>
      )}

      <DeleteWithReasonDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title="Remove this contact?"
        description="They will stop receiving order and dispatch notifications for this customer."
        confirmLabel="Remove contact"
        loading={remove.isPending}
        onConfirm={(reason) => pendingDelete && remove.mutate({ contact: pendingDelete, reason })}
      />
    </div>
  )
}

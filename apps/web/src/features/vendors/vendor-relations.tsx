/**
 * Contact book and category qualifications for one vendor.
 *
 * Both sit behind their own endpoints rather than inside the vendor payload: a
 * supplier can carry a dozen named contacts and be qualified for twenty item
 * categories, and loading those with every list row would be the N+1 read
 * master rules 32 forbids.
 *
 * Both need a saved vendor - a contact hangs off a vendor id - so the create
 * dialog says so plainly instead of collecting rows it cannot post.
 *
 * Removal carries a reason because the API requires one: a qualification that
 * vanished with no recorded reason is what makes an audit trail useless six
 * months later.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layers, Pencil, Plus, Star, Trash2, UserRound } from 'lucide-react'
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
import { Badge, Checkbox, Input, Label, Skeleton, Textarea } from '@/components/ui/primitives'
import { useCategoryOptions } from '@/features/items/item-model'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { VENDOR_CONTACT_ROLES, type VendorCategoryLink, type VendorContact } from './vendor-types'

/** One page covers every contact or qualification a vendor realistically has. */
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

export function UnsavedVendorNotice({ what }: { what: string }) {
  return (
    <EmptyState
      title={`${what} open once the vendor is saved`}
      description={`Create the vendor first. ${what} are stored against its id, so they can be added the moment it exists.`}
    />
  )
}

/** Reason capture for a removal the API will not accept without one. */
function RemovalDialog({
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
  const tooShort = reason.trim().length < 3

  useEffect(() => {
    if (!open) {
      setReason('')
      setTouched(false)
    }
  }, [open])

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
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
        <Label htmlFor="vendor-removal-reason" required>
          Reason
        </Label>
        <Input
          id="vendor-removal-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          autoComplete="off"
          className="mt-1.5"
          aria-invalid={touched && tooShort}
        />
        {touched && tooShort && <FieldError message="Record at least three characters of reason" />}
      </div>
    </ConfirmDialog>
  )
}

// -------------------------------------------------------------- contacts ----

const contactSchema = z
  .object({
    name: z.string().trim().min(1, 'Name is required').max(160, 'At most 160 characters'),
    contact_role: z.enum(VENDOR_CONTACT_ROLES),
    designation: z.string().trim().max(120, 'At most 120 characters'),
    email: z
      .string()
      .trim()
      .refine(
        (v) => v === '' || z.string().email().safeParse(v).success,
        'Not a valid email address',
      ),
    phone: z.string().trim().max(32, 'At most 32 characters'),
    mobile: z.string().trim().max(32, 'At most 32 characters'),
    is_primary: z.boolean(),
  })
  // Mirrors the API check constraint: a contact nobody can reach is not a
  // contact, and catching it here gives a field-level message instead of a 422.
  .superRefine((values, ctx) => {
    if (!values.email && !values.phone && !values.mobile) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['email'],
        message: 'A contact needs an email, a phone or a mobile number',
      })
    }
  })

type ContactValues = z.infer<typeof contactSchema>

function emptyContact(): ContactValues {
  return {
    name: '',
    contact_role: 'MANAGER',
    designation: '',
    email: '',
    phone: '',
    mobile: '',
    is_primary: false,
  }
}

function contactValuesFrom(contact: VendorContact): ContactValues {
  return {
    name: contact.name,
    contact_role: contact.contact_role,
    designation: contact.designation ?? '',
    email: contact.email ?? '',
    phone: contact.phone ?? '',
    mobile: contact.mobile ?? '',
    is_primary: contact.is_primary,
  }
}

export function VendorContactsPanel({ vendorId, canEdit }: { vendorId: string; canEdit: boolean }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<VendorContact | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<VendorContact | null>(null)

  const contacts = useQuery({
    queryKey: [...queryKeys.vendors.detail(vendorId), 'contacts'],
    queryFn: () =>
      api.getPage<VendorContact>(`/vendors/${vendorId}/contacts`, {
        params: { limit: RELATION_LIMIT, sort_by: 'name', sort_dir: 'asc' },
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
    reset(editingId ? contactValuesFrom(editing as VendorContact) : emptyContact())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorOpen, editingId, reset])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.vendors.all })

  const save = useMutation({
    mutationFn: async (values: ContactValues) => {
      const payload = {
        name: values.name.trim(),
        contact_role: values.contact_role,
        designation: orNull(values.designation),
        email: orNull(values.email),
        phone: orNull(values.phone),
        mobile: orNull(values.mobile),
      }
      if (!editingId) {
        return api.post<VendorContact>(`/vendors/${vendorId}/contacts`, {
          ...payload,
          is_primary: values.is_primary,
        })
      }
      const updated = await api.patch<VendorContact>(`/vendors/contacts/${editingId}`, payload)
      // Promotion demotes whoever currently holds the flag, so the API models it
      // as an operation with its own endpoint rather than a field on the PATCH.
      if (values.is_primary && !updated.is_primary) {
        return api.post<VendorContact>(`/vendors/contacts/${editingId}/set-primary`)
      }
      return updated
    },
    onSuccess: () => {
      void invalidate()
      toast.success(editingId ? 'Contact updated' : 'Contact added')
      setEditing(null)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const remove = useMutation({
    mutationFn: ({ contact, reason }: { contact: VendorContact; reason: string }) =>
      api.delete(`/vendors/contacts/${contact.id}`, { params: { reason } }),
    onSuccess: () => {
      void invalidate()
      toast.success('Contact removed')
      setPendingDelete(null)
    },
  })

  if (contacts.isLoading) return <PanelSkeleton />

  if (contacts.error) {
    return (
      <EmptyState
        title="Could not load contacts"
        description={contacts.error.message}
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
          description="Purchase orders and delivery chasers go to the primary contact; QC queries go to the QC contact."
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
                  <Badge variant="secondary">{humanise(contact.contact_role)}</Badge>
                  {contact.is_primary && <Badge variant="success">Primary</Badge>}
                  {!contact.is_active && <Badge variant="muted">Inactive</Badge>}
                </div>
                <p className="mt-0.5 text-sm text-muted-foreground">
                  {[contact.designation, contact.email, contact.mobile, contact.phone]
                    .filter(Boolean)
                    .join(' - ') || 'No contact details recorded'}
                </p>
              </div>
              {canEdit && (
                <div className="flex shrink-0 items-center gap-1">
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
              <Label htmlFor="vendor-contact-name" required>
                Name
              </Label>
              <Input
                id="vendor-contact-name"
                {...register('name')}
                className="mt-1.5"
                autoComplete="name"
                aria-invalid={Boolean(errors.name)}
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="vendor-contact-role" required>
                Role
              </Label>
              <Controller
                control={control}
                name="contact_role"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="vendor-contact-role" className="mt-1.5">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {VENDOR_CONTACT_ROLES.map((role) => (
                        <SelectItem key={role} value={role}>
                          {humanise(role)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <p className="mt-1 text-xs text-muted-foreground">
                Decides which notifications reach this person, and the portal role if they are ever
                given a login.
              </p>
            </div>

            <div>
              <Label htmlFor="vendor-contact-designation">Designation</Label>
              <Input
                id="vendor-contact-designation"
                {...register('designation')}
                className="mt-1.5"
                autoComplete="organization-title"
              />
              <FieldError message={errors.designation?.message} />
            </div>

            <div>
              <Label htmlFor="vendor-contact-email">Email</Label>
              <Input
                id="vendor-contact-email"
                type="email"
                {...register('email')}
                className="mt-1.5"
                autoComplete="email"
                aria-invalid={Boolean(errors.email)}
              />
              <FieldError message={errors.email?.message} />
            </div>

            <div>
              <Label htmlFor="vendor-contact-mobile">Mobile</Label>
              <Input
                id="vendor-contact-mobile"
                {...register('mobile')}
                className="mt-1.5"
                inputMode="tel"
                autoComplete="tel"
              />
              <FieldError message={errors.mobile?.message} />
            </div>

            <div>
              <Label htmlFor="vendor-contact-phone">Landline</Label>
              <Input
                id="vendor-contact-phone"
                {...register('phone')}
                className="mt-1.5"
                inputMode="tel"
                autoComplete="tel"
              />
              <FieldError message={errors.phone?.message} />
            </div>

            <div className="flex items-center gap-2.5 sm:col-span-2">
              <Controller
                control={control}
                name="is_primary"
                render={({ field }) => (
                  <Checkbox
                    id="vendor-contact-primary"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <Label htmlFor="vendor-contact-primary" className="font-normal">
                Primary contact for purchase orders and delivery follow-up
              </Label>
            </div>
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

      <RemovalDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title="Remove this contact?"
        description="They stop receiving purchase order and delivery notifications for this vendor."
        confirmLabel="Remove contact"
        loading={remove.isPending}
        onConfirm={(reason) => pendingDelete && remove.mutate({ contact: pendingDelete, reason })}
      />
    </div>
  )
}

// ------------------------------------------------------------ categories ----

const categorySchema = z.object({
  item_category_id: z.string().min(1, 'Choose an item category'),
  is_preferred: z.boolean(),
  notes: z.string().trim().max(1000, 'At most 1000 characters'),
})

type CategoryValues = z.infer<typeof categorySchema>

export function VendorCategoriesPanel({
  vendorId,
  canEdit,
}: {
  vendorId: string
  canEdit: boolean
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<VendorCategoryLink | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<VendorCategoryLink | null>(null)

  const links = useQuery({
    queryKey: [...queryKeys.vendors.detail(vendorId), 'categories'],
    queryFn: () =>
      api.getPage<VendorCategoryLink>(`/vendors/${vendorId}/categories`, {
        params: { limit: RELATION_LIMIT },
      }),
  })

  // Only fetched while the editor is open - the picker is the one thing here
  // that needs the category master.
  const categories = useCategoryOptions(editing !== null)

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<CategoryValues>({
    resolver: zodResolver(categorySchema),
    defaultValues: { item_category_id: '', is_preferred: false, notes: '' },
  })

  const editingId = editing && editing !== 'new' ? editing.id : null
  const editorOpen = editing !== null
  useEffect(() => {
    if (!editorOpen) return
    const link = editing !== 'new' ? editing : null
    reset({
      item_category_id: link?.item_category_id ?? '',
      is_preferred: link?.is_preferred ?? false,
      notes: link?.notes ?? '',
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorOpen, editingId, reset])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.vendors.all })

  const save = useMutation({
    mutationFn: (values: CategoryValues) =>
      editingId
        ? api.patch<VendorCategoryLink>(`/vendors/categories/${editingId}`, {
            is_preferred: values.is_preferred,
            notes: orNull(values.notes),
          })
        : api.post<VendorCategoryLink>(`/vendors/${vendorId}/categories`, {
            item_category_id: values.item_category_id,
            is_preferred: values.is_preferred,
            notes: orNull(values.notes),
          }),
    onSuccess: () => {
      void invalidate()
      toast.success(editingId ? 'Qualification updated' : 'Vendor qualified for category')
      setEditing(null)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const remove = useMutation({
    mutationFn: ({ link, reason }: { link: VendorCategoryLink; reason: string }) =>
      api.delete(`/vendors/categories/${link.id}`, { params: { reason } }),
    onSuccess: () => {
      void invalidate()
      toast.success('Qualification revoked')
      setPendingDelete(null)
    },
  })

  if (links.isLoading) return <PanelSkeleton />

  if (links.error) {
    return (
      <EmptyState
        title="Could not load qualifications"
        description={links.error.message}
        action={
          <Button variant="outline" size="sm" onClick={() => void links.refetch()}>
            Try again
          </Button>
        }
      />
    )
  }

  const rows = links.data?.data ?? []

  return (
    <div className="space-y-4">
      {rows.length === 0 && !editorOpen && (
        <EmptyState
          icon={Layers}
          title="Not qualified for any category yet"
          description="Qualifications drive which vendors are proposed first when a requisition for a category needs sourcing."
          action={
            canEdit ? (
              <Button size="sm" onClick={() => setEditing('new')}>
                <Plus />
                Add qualification
              </Button>
            ) : undefined
          }
        />
      )}

      {rows.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {rows.map((link) => (
            <li key={link.id} className="flex items-start gap-3 p-3">
              <Layers className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">
                    {link.item_category_name ?? link.item_category_id}
                  </span>
                  {link.item_category_code && (
                    <span className="text-xs text-muted-foreground">{link.item_category_code}</span>
                  )}
                  {link.is_preferred && (
                    <Badge variant="success">
                      <Star className="h-3 w-3" aria-hidden />
                      Preferred
                    </Badge>
                  )}
                </div>
                {link.notes && <p className="mt-0.5 text-sm text-muted-foreground">{link.notes}</p>}
              </div>
              {canEdit && (
                <div className="flex shrink-0 items-center gap-1">
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Edit qualification for ${link.item_category_name ?? 'category'}`}
                    onClick={() => setEditing(link)}
                  >
                    <Pencil />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Revoke qualification for ${link.item_category_name ?? 'category'}`}
                    onClick={() => setPendingDelete(link)}
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
          Add qualification
        </Button>
      )}

      {editorOpen && (
        <form
          onSubmit={handleSubmit((values) => save.mutate(values))}
          className="space-y-4 rounded-lg border p-4"
          noValidate
        >
          <p className="text-sm font-medium">
            {editingId ? 'Edit qualification' : 'New qualification'}
          </p>

          <div className="space-y-3">
            <div>
              <Label htmlFor="vendor-category" required>
                Item category
              </Label>
              <Controller
                control={control}
                name="item_category_id"
                render={({ field }) => (
                  <Select
                    value={field.value}
                    onValueChange={field.onChange}
                    // The category a qualification points at is its identity, so
                    // it is changed by revoking and adding rather than silently
                    // repointing a row other documents already reference.
                    disabled={Boolean(editingId) || categories.isLoading}
                  >
                    <SelectTrigger
                      id="vendor-category"
                      className="mt-1.5"
                      aria-invalid={Boolean(errors.item_category_id)}
                    >
                      <SelectValue
                        placeholder={categories.isLoading ? 'Loading...' : 'Select a category'}
                      />
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
              <FieldError message={errors.item_category_id?.message} />
            </div>

            <div className="flex items-center gap-2.5">
              <Controller
                control={control}
                name="is_preferred"
                render={({ field }) => (
                  <Checkbox
                    id="vendor-category-preferred"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <Label htmlFor="vendor-category-preferred" className="font-normal">
                Preferred supplier for this category
              </Label>
            </div>

            <div>
              <Label htmlFor="vendor-category-notes">Notes</Label>
              <Textarea id="vendor-category-notes" {...register('notes')} className="mt-1.5" />
              <FieldError message={errors.notes?.message} />
            </div>
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
              {editingId ? 'Save qualification' : 'Add qualification'}
            </Button>
          </div>
        </form>
      )}

      <RemovalDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title="Revoke this qualification?"
        description="The vendor stops being proposed for this category when a requisition needs sourcing."
        confirmLabel="Revoke qualification"
        loading={remove.isPending}
        onConfirm={(reason) => pendingDelete && remove.mutate({ link: pendingDelete, reason })}
      />
    </div>
  )
}

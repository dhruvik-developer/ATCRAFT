/**
 * The write surfaces of the users tab: create/edit, role assignment and an
 * administrator-triggered password reset.
 *
 * Three API rules shape these forms rather than being discovered at save time:
 *
 * * The role set is replaced wholesale through its own endpoint, gated on
 *   `permission.assign`, so it is not a field on the edit form.
 * * Activation is a lifecycle change with a mandatory reason that also revokes
 *   sessions, so it is not a checkbox either - the page owns that dialog.
 * * A PATCH that omits a placement must not unassign it, so clearing one is an
 *   explicit `clear_*` flag rather than a null.
 *
 * A vendor-portal account may also be limited to some of the item categories
 * its vendor supplies - the managers the category-wise purchase order split
 * hands each order to. The API refuses a scope outside the vendor's own
 * qualifications, so the choices are exactly those; an empty scope is every
 * category the vendor supplies.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, ShieldCheck } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
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
  AlertTitle,
  Badge,
  Checkbox,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import {
  PASSWORD_MAX_LENGTH,
  PASSWORD_RULES,
  passwordField,
  unmetRules,
} from '@/features/auth/password-policy'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorCategoryOptions } from '@/features/vendors/vendor-types'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'

import {
  useBranchOptions,
  useDepartmentOptions,
  usePlantOptions,
  useRoleOptions,
  type PasswordResetResult,
  type UserDetail,
  type UserRow,
} from './admin-types'

// -------------------------------------------------------- create / edit -----

function userSchema(isEdit: boolean) {
  return z.object({
    email: z.string().trim().min(1, validation.required('Email')).email(validation.email),
    full_name: z
      .string()
      .trim()
      .min(1, validation.required('Name'))
      .max(255, 'At most 255 characters'),
    // On an edit the field means "set a new password"; blank leaves it alone.
    password: z
      .string()
      .max(PASSWORD_MAX_LENGTH, `At most ${PASSWORD_MAX_LENGTH} characters`)
      .superRefine((value, ctx) => {
        if (isEdit && value === '') return
        for (const rule of unmetRules(value)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: `Missing: ${rule.label.toLowerCase()}`,
          })
        }
      }),
    phone: z.string().trim().max(32, 'At most 32 characters'),
    employee_code: z.string().trim().max(32, 'At most 32 characters'),
    branch_id: z.string(),
    plant_id: z.string(),
    department_id: z.string(),
    vendor_id: z.string().nullable(),
    // Item categories a vendor account is limited to; empty is every one the
    // vendor supplies. Meaningless, and never sent, without a vendor.
    allowed_category_ids: z.array(z.string()),
    must_change_password: z.boolean(),
    role_ids: z.array(z.string()),
  })
}

type UserFormValues = z.infer<ReturnType<typeof userSchema>>

function emptyUser(): UserFormValues {
  return {
    email: '',
    full_name: '',
    password: '',
    phone: '',
    employee_code: '',
    branch_id: SELECT_NONE,
    plant_id: SELECT_NONE,
    department_id: SELECT_NONE,
    vendor_id: null,
    allowed_category_ids: [],
    must_change_password: true,
    role_ids: [],
  }
}

function userValues(user: UserDetail): UserFormValues {
  return {
    email: user.email,
    full_name: user.full_name,
    password: '',
    phone: user.phone ?? '',
    employee_code: user.employee_code ?? '',
    branch_id: user.branch_id ?? SELECT_NONE,
    plant_id: user.plant_id ?? SELECT_NONE,
    department_id: user.department_id ?? SELECT_NONE,
    vendor_id: user.vendor_id,
    allowed_category_ids: user.allowed_category_ids ?? [],
    must_change_password: user.must_change_password,
    role_ids: (user.roles ?? []).map((role) => role.id),
  }
}

/** The same set of ids, whatever the order. */
function sameIds(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  const set = new Set(left)
  return right.every((id) => set.has(id))
}

/**
 * Loads the full record itself rather than being handed a grid row.
 *
 * `GET /users` returns a deliberately narrow summary - no phone, no scope
 * arrays, no role objects - because a grid renders none of them. The edit form
 * needs all three, so it asks for the detail when it opens.
 */
export function UserFormDialog({
  open,
  onOpenChange,
  userId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, null for a create. */
  userId: string | null
}) {
  const queryClient = useQueryClient()
  const { can } = useAuth()
  const isEdit = Boolean(userId)

  const detail = useQuery({
    queryKey: queryKeys.users.detail(userId ?? ''),
    queryFn: () => api.get<UserDetail>(`/users/${userId}`),
    enabled: open && Boolean(userId),
    staleTime: staleTimes.master,
  })
  const user = detail.data ?? null

  const branches = useBranchOptions(open)
  const plants = usePlantOptions(open)
  const departments = useDepartmentOptions(open)
  const roles = useRoleOptions(open && !isEdit)

  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    setValue,
    setError,
    formState: { errors },
  } = useForm<UserFormValues>({
    resolver: zodResolver(userSchema(isEdit)),
    defaultValues: emptyUser(),
  })

  // Seeded once per opening, keyed on the id: a background refetch resolving
  // mid-edit must not wipe what is being typed. An edit whose detail has not
  // arrived yet is reset to blank rather than skipped, so the values of the
  // account edited last cannot survive into this opening - the fields stay
  // hidden until `user` resolves, and are seeded again when it does.
  const loadedId = user?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(user ? userValues(user) : emptyUser())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isEdit, loadedId, reset])

  const password = watch('password')
  const vendorId = watch('vendor_id')

  const mutation = useMutation({
    mutationFn: (values: UserFormValues) => {
      const shared = {
        email: values.email.trim().toLowerCase(),
        full_name: values.full_name.trim(),
        phone: asText(values.phone),
        employee_code: asText(values.employee_code),
      }

      // Create-or-edit follows the *intent* (`userId`), never the fetched
      // detail: on an edit whose detail failed to load, `user` is null, and
      // branching on it turned "Save changes" into a POST that created a second
      // account under an "Edit" heading.
      if (userId) {
        const storedScope = user?.allowed_category_ids ?? []
        // The scope is sent only when it, or the vendor behind it, changed:
        // the API re-checks it against the vendor's qualifications whenever it
        // arrives, and a qualification revoked since must not block an edit to
        // somebody's telephone number. Detaching the account from its vendor
        // empties it, because a scope is refused on an internal account.
        const scope = values.vendor_id
          ? values.vendor_id !== user?.vendor_id ||
            !sameIds(values.allowed_category_ids, storedScope)
            ? { allowed_category_ids: values.allowed_category_ids }
            : {}
          : storedScope.length > 0
            ? { allowed_category_ids: [] }
            : {}
        // "Absent" and "null" both arrive as None on the server, so an
        // unassignment is a flag rather than a null id.
        return api.patch<UserDetail>(`/users/${userId}`, {
          ...shared,
          ...(asId(values.branch_id)
            ? { branch_id: asId(values.branch_id) }
            : { clear_branch: true }),
          ...(asId(values.plant_id) ? { plant_id: asId(values.plant_id) } : { clear_plant: true }),
          ...(asId(values.department_id)
            ? { department_id: asId(values.department_id) }
            : { clear_department: true }),
          ...(values.vendor_id ? { vendor_id: values.vendor_id } : { clear_vendor: true }),
          ...scope,
          ...(values.password ? { password: values.password } : {}),
          must_change_password: values.must_change_password,
        })
      }

      return api.post<UserDetail>('/users', {
        ...shared,
        password: values.password,
        branch_id: asId(values.branch_id),
        plant_id: asId(values.plant_id),
        department_id: asId(values.department_id),
        vendor_id: values.vendor_id,
        allowed_category_ids: values.vendor_id ? values.allowed_category_ids : [],
        role_ids: values.role_ids,
        is_active: true,
        must_change_password: values.must_change_password,
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      toast.success(
        isEdit ? success.updated('User') : success.created('User'),
        `${saved.full_name} - ${saved.email}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const roleList = roles.data ?? []
  const selectableRoles = vendorId
    ? roleList.filter((role) => role.is_vendor_role)
    : roleList.filter((role) => !role.is_vendor_role)

  const loadingDetail = isEdit && detail.isLoading

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${user?.full_name ?? 'user'}` : 'New user'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Setting a password here signs the account out of every device it is currently using.'
              : 'The first password is hashed on arrival and never stored, logged or echoed back.'}
          </DialogDescription>
        </DialogHeader>

        {loadingDetail && (
          <div className="space-y-3" aria-busy="true">
            {Array.from({ length: 5 }).map((_, index) => (
              <Skeleton key={index} className="h-9 w-full" />
            ))}
          </div>
        )}

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className={loadingDetail ? 'hidden' : 'flex min-h-0 flex-col gap-4'}
          noValidate
        >
          <DialogBody className="space-y-5">
            {isEdit && detail.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load this user</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            )}

            {/* Never a form while `isEdit && !user`: it would hold the blank
                defaults under an "Edit" heading and its save could only mislead. */}
            {(!isEdit || user) && (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="user-name" required>
                      Full name
                    </Label>
                    <Input
                      id="user-name"
                      {...register('full_name')}
                      autoComplete="off"
                      aria-invalid={Boolean(errors.full_name)}
                      className="mt-1.5"
                    />
                    <FieldError message={errors.full_name?.message} />
                  </div>

                  <div>
                    <Label htmlFor="user-email" required>
                      Email
                    </Label>
                    <Input
                      id="user-email"
                      type="email"
                      {...register('email')}
                      autoComplete="off"
                      readOnly={isEdit && !can(P.USER_RESET_PASSWORD)}
                      aria-invalid={Boolean(errors.email)}
                      className="mt-1.5"
                    />
                    <FieldError message={errors.email?.message} />
                    {isEdit && !can(P.USER_RESET_PASSWORD) && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Changing the login email requires password-reset permission.
                      </p>
                    )}
                  </div>

                  <div>
                    <Label htmlFor="user-phone">Phone</Label>
                    <Input
                      id="user-phone"
                      {...register('phone')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.phone?.message} />
                  </div>

                  <div>
                    <Label htmlFor="user-employee-code">Employee code</Label>
                    <Input
                      id="user-employee-code"
                      {...register('employee_code')}
                      autoComplete="off"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.employee_code?.message} />
                  </div>

                  <div className="sm:col-span-2">
                    <Label htmlFor="user-password" required={!isEdit}>
                      {isEdit ? 'New password' : 'First password'}
                    </Label>
                    <Input
                      id="user-password"
                      type="password"
                      {...register('password')}
                      autoComplete="new-password"
                      aria-invalid={Boolean(errors.password)}
                      aria-describedby="user-password-policy"
                      className="mt-1.5"
                    />
                    <FieldError message={errors.password?.message} />
                    <ul
                      id="user-password-policy"
                      className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"
                    >
                      {PASSWORD_RULES.map((rule) => (
                        <li
                          key={rule.id}
                          className={
                            password && rule.satisfied(password) ? 'text-success' : undefined
                          }
                        >
                          {rule.label}
                        </li>
                      ))}
                    </ul>
                  </div>

                  <div>
                    <Label htmlFor="user-branch">Branch</Label>
                    <Controller
                      control={control}
                      name="branch_id"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="user-branch" className="mt-1.5">
                            <SelectValue placeholder="No branch" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>No branch</SelectItem>
                            {(branches.data ?? []).map((branch) => (
                              <SelectItem key={branch.id} value={branch.id}>
                                {branch.code} - {branch.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                  </div>

                  <div>
                    <Label htmlFor="user-plant">Plant</Label>
                    <Controller
                      control={control}
                      name="plant_id"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="user-plant" className="mt-1.5">
                            <SelectValue placeholder="No plant" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>No plant</SelectItem>
                            {(plants.data ?? []).map((plant) => (
                              <SelectItem key={plant.id} value={plant.id}>
                                {plant.code} - {plant.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                  </div>

                  <div>
                    <Label htmlFor="user-department">Department</Label>
                    <Controller
                      control={control}
                      name="department_id"
                      render={({ field }) => (
                        <Select value={field.value} onValueChange={field.onChange}>
                          <SelectTrigger id="user-department" className="mt-1.5">
                            <SelectValue placeholder="No department" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={SELECT_NONE}>No department</SelectItem>
                            {(departments.data ?? []).map((department) => (
                              <SelectItem key={department.id} value={department.id}>
                                {department.code} - {department.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                  </div>

                  <div>
                    <Controller
                      control={control}
                      name="vendor_id"
                      render={({ field }) => (
                        <VendorPicker
                          id="user-vendor"
                          label="Vendor portal account"
                          value={field.value}
                          onChange={(next) => {
                            field.onChange(next)
                            // Vendor and internal roles are mutually exclusive, so
                            // a selection made before the binding changed is no
                            // longer legal and the API would refuse the whole save.
                            setValue('role_ids', [])
                            // Categories are the old vendor's qualifications, and
                            // the API refuses any the new vendor does not hold.
                            setValue('allowed_category_ids', [])
                          }}
                          placeholder="Internal staff account"
                        />
                      )}
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Binding an account to a vendor limits it to the vendor portal, and to vendor
                      roles only.
                    </p>
                  </div>
                </div>

                {vendorId && (
                  <Controller
                    control={control}
                    name="allowed_category_ids"
                    render={({ field }) => (
                      <VendorCategoryScope
                        vendorId={vendorId}
                        // The same right the vendor picker above needs.
                        readable={can(P.VENDOR_VIEW)}
                        value={field.value}
                        onChange={field.onChange}
                      />
                    )}
                  />
                )}

                {!isEdit && (
                  <fieldset className="rounded-lg border p-4">
                    <legend className="px-1.5 text-sm font-medium">Roles</legend>
                    {selectableRoles.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        {roles.isLoading
                          ? 'Loading roles...'
                          : roles.error
                            ? 'The role list could not be loaded. Roles can be assigned once the account exists.'
                            : vendorId
                              ? 'No vendor roles exist yet. Create one on the Roles tab first.'
                              : 'No roles available.'}
                      </p>
                    ) : (
                      <Controller
                        control={control}
                        name="role_ids"
                        render={({ field }) => (
                          <div className="grid gap-2 sm:grid-cols-2">
                            {selectableRoles.map((role) => {
                              const checked = field.value.includes(role.id)
                              return (
                                <label
                                  key={role.id}
                                  className="flex cursor-pointer items-start gap-2.5 text-sm"
                                >
                                  <Checkbox
                                    checked={checked}
                                    onCheckedChange={(next) =>
                                      field.onChange(
                                        next
                                          ? [...field.value, role.id]
                                          : field.value.filter((id: string) => id !== role.id),
                                      )
                                    }
                                  />
                                  <span className="min-w-0">
                                    <span className="font-medium">{role.name}</span>
                                    <span className="block text-xs text-muted-foreground">
                                      {role.permission_count} permissions
                                    </span>
                                  </span>
                                </label>
                              )
                            })}
                          </div>
                        )}
                      />
                    )}
                  </fieldset>
                )}

                <label className="flex items-start gap-2.5 text-sm">
                  <Controller
                    control={control}
                    name="must_change_password"
                    render={({ field }) => (
                      <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                    )}
                  />
                  <span>
                    Require a password change at next sign-in
                    <span className="block text-xs text-muted-foreground">
                      A password someone else chose has been typed into a chat window or read out
                      over a telephone.
                    </span>
                  </span>
                </label>
              </>
            )}
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            {(!isEdit || user) && (
              <Button type="submit" loading={mutation.isPending}>
                {isEdit ? 'Save changes' : 'Create user'}
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------ vendor category scope -----

/**
 * Which of its vendor's item categories a vendor-portal account is limited to.
 *
 * Offers exactly the vendor's qualifications (`vendor_categories`), because the
 * API refuses anything else. A category covers the ones beneath it, so a
 * manager scoped to Handles sees the lever-handle orders too - the same subtree
 * rule the order assignment and the portal's visibility use. A scope saved
 * before a qualification was revoked keeps the stale id; it is shown, ticked,
 * so it can be unticked rather than silently resent and refused.
 */
function VendorCategoryScope({
  vendorId,
  readable,
  value,
  onChange,
}: {
  vendorId: string
  readable: boolean
  value: string[]
  onChange: (ids: string[]) => void
}) {
  const categories = useVendorCategoryOptions(vendorId, readable)
  const rows = useMemo(
    () =>
      [...(categories.data ?? [])].sort((a, b) =>
        (a.item_category_code ?? '').localeCompare(b.item_category_code ?? ''),
      ),
    [categories.data],
  )
  const offered = new Set(rows.map((row) => row.item_category_id))
  const stale = categories.data ? value.filter((id) => !offered.has(id)) : []

  const toggle = (id: string, on: boolean) =>
    onChange(on ? [...value, id] : value.filter((existing) => existing !== id))

  return (
    <fieldset className="rounded-lg border p-4">
      <legend className="px-1.5 text-sm font-medium">Item categories</legend>
      <p className="text-xs text-muted-foreground">
        Tick none and the account works with every category this vendor supplies. A category also
        covers the categories beneath it. Purchase orders are handed to the manager whose categories
        they fall in.
      </p>

      {!readable ? (
        <p className="mt-3 text-sm text-muted-foreground">
          Reading a vendor&apos;s categories needs the vendor view right, so the scope cannot be
          changed from this account.
        </p>
      ) : categories.isLoading ? (
        <p className="mt-3 text-sm text-muted-foreground">
          Loading the vendor&apos;s categories...
        </p>
      ) : categories.error ? (
        <p className="mt-3 text-sm text-destructive">{categories.error.message}</p>
      ) : rows.length === 0 && stale.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">
          This vendor is not qualified for any item category yet. Qualify it on the Vendors screen
          first, or leave the account unrestricted.
        </p>
      ) : (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {rows.map((row) => (
            <label
              key={row.item_category_id}
              className="flex cursor-pointer items-start gap-2.5 text-sm"
            >
              <Checkbox
                checked={value.includes(row.item_category_id)}
                onCheckedChange={(next) => toggle(row.item_category_id, next === true)}
              />
              <span className="min-w-0">
                <span className="font-medium">
                  {row.item_category_name ?? row.item_category_code ?? 'Category'}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {row.item_category_code}
                  {row.is_preferred ? ' - preferred vendor' : ''}
                </span>
              </span>
            </label>
          ))}
          {stale.map((id) => (
            <label key={id} className="flex cursor-pointer items-start gap-2.5 text-sm">
              <Checkbox checked onCheckedChange={() => toggle(id, false)} />
              <span className="min-w-0 text-warning">
                A category this vendor is no longer qualified for - untick it to save a new scope
              </span>
            </label>
          ))}
        </div>
      )}
    </fieldset>
  )
}

// ------------------------------------------------------ role assignment -----

const assignmentSchema = z.object({
  role_ids: z.array(z.string()),
  reason: z
    .string()
    .trim()
    .min(REASON_MIN_LENGTH, `Give a reason of at least ${REASON_MIN_LENGTH} characters`)
    .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
})

type AssignmentValues = z.infer<typeof assignmentSchema>

export function RoleAssignmentDialog({
  open,
  onOpenChange,
  userId,
  userName,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  userId: string
  userName: string
}) {
  const queryClient = useQueryClient()
  const roles = useRoleOptions(open)

  // The grid row carries role *codes*; assignment needs role ids and the
  // vendor binding, so the detail is fetched when the dialog opens.
  const detail = useQuery({
    queryKey: queryKeys.users.detail(userId),
    queryFn: () => api.get<UserDetail>(`/users/${userId}`),
    enabled: open,
    staleTime: staleTimes.master,
  })
  const user = detail.data ?? null

  const {
    control,
    handleSubmit,
    register,
    reset,
    setError,
    formState: { errors },
  } = useForm<AssignmentValues>({
    resolver: zodResolver(assignmentSchema),
    defaultValues: { role_ids: [], reason: '' },
  })

  useEffect(() => {
    if (open && user) reset({ role_ids: (user.roles ?? []).map((role) => role.id), reason: '' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user?.id, reset])

  const mutation = useMutation({
    mutationFn: (values: AssignmentValues) =>
      api.post<UserDetail>(`/users/${userId}/roles`, {
        role_ids: values.role_ids,
        reason: values.reason.trim(),
      }),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      const held = saved.roles?.length ?? 0
      toast.success(success.updated('Roles'), `${saved.full_name} now holds ${held} role(s)`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const isVendorUser = Boolean(user?.vendor_id)
  const roleList = roles.data ?? []
  const loadingDetail = detail.isLoading
  // The endpoint replaces the role set wholesale. With the detail unloaded the
  // checklist would be seeded from the empty defaults and a save would strip
  // every role the account holds; with the role list unloaded there is nothing
  // to choose from. Either way the form must not be submittable.
  const canSave = Boolean(user) && !roles.error

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Roles for {user?.full_name ?? userName}</DialogTitle>
          <DialogDescription>
            The submitted list replaces the account&apos;s roles entirely. Assigning a role grants
            every permission in it, and the API enforces that grant on every request from this
            account.
          </DialogDescription>
        </DialogHeader>

        {loadingDetail && (
          <div className="space-y-2" aria-busy="true">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-14 w-full" />
            ))}
          </div>
        )}

        <form
          onSubmit={handleSubmit((values) => {
            if (!canSave) return
            mutation.mutate(values)
          })}
          className={loadingDetail ? 'hidden' : 'flex min-h-0 flex-col gap-4'}
          noValidate
        >
          <DialogBody className="space-y-4">
            {detail.error && !detail.isLoading && (
              <Alert variant="destructive">
                <AlertTitle>Could not load this account&apos;s roles</AlertTitle>
                <AlertDescription>{detail.error.message}</AlertDescription>
              </Alert>
            )}

            {roles.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load the role list</AlertTitle>
                <AlertDescription>{roles.error.message}</AlertDescription>
              </Alert>
            )}

            {canSave && (
              <>
                {isVendorUser && (
                  <Alert variant="info">
                    <AlertTitle>Vendor portal account</AlertTitle>
                    <AlertDescription>
                      This account is bound to a vendor, so only vendor roles may be assigned to it.
                    </AlertDescription>
                  </Alert>
                )}

                <Controller
                  control={control}
                  name="role_ids"
                  render={({ field }) => (
                    <div className="space-y-2">
                      {roles.isLoading && (
                        <p className="text-sm text-muted-foreground">Loading roles...</p>
                      )}
                      {!roles.isLoading && roleList.length === 0 && (
                        <p className="text-sm text-muted-foreground">
                          No active roles exist yet. Create one on the Roles tab first.
                        </p>
                      )}
                      {roleList.map((role) => {
                        const blocked = isVendorUser !== role.is_vendor_role
                        const checked = field.value.includes(role.id)
                        return (
                          <label
                            key={role.id}
                            className={
                              blocked
                                ? 'flex items-start gap-2.5 rounded-md border p-3 text-sm opacity-60'
                                : 'flex cursor-pointer items-start gap-2.5 rounded-md border p-3 text-sm'
                            }
                          >
                            <Checkbox
                              checked={checked}
                              disabled={blocked}
                              onCheckedChange={(next) =>
                                field.onChange(
                                  next
                                    ? [...field.value, role.id]
                                    : field.value.filter((id: string) => id !== role.id),
                                )
                              }
                            />
                            <span className="min-w-0 flex-1">
                              <span className="flex flex-wrap items-center gap-2">
                                <span className="font-medium">{role.name}</span>
                                <span className="font-mono text-xs text-muted-foreground">
                                  {role.code}
                                </span>
                                {role.is_system && <Badge variant="secondary">System</Badge>}
                                {role.is_vendor_role && <Badge variant="muted">Vendor</Badge>}
                              </span>
                              <span className="mt-0.5 block text-xs text-muted-foreground">
                                {role.description ?? `${role.permission_count} permissions`}
                              </span>
                            </span>
                          </label>
                        )
                      })}
                    </div>
                  )}
                />

                <div>
                  <Label htmlFor="assign-reason" required>
                    Reason
                  </Label>
                  <Input
                    id="assign-reason"
                    {...register('reason')}
                    autoComplete="off"
                    aria-invalid={Boolean(errors.reason)}
                    className="mt-1.5"
                  />
                  <FieldError message={errors.reason?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Kept in the audit trail. An access review six months from now reads this line.
                  </p>
                </div>
              </>
            )}
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending} disabled={!canSave}>
              <ShieldCheck />
              Save roles
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------- password reset ----

const resetSchema = z.object({
  new_password: z.union([z.literal(''), passwordField]),
  reason: z
    .string()
    .trim()
    .min(REASON_MIN_LENGTH, `Give a reason of at least ${REASON_MIN_LENGTH} characters`)
    .max(REASON_MAX_LENGTH, `At most ${REASON_MAX_LENGTH} characters`),
})

type ResetValues = z.infer<typeof resetSchema>

export function ResetPasswordDialog({
  open,
  onOpenChange,
  user,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  user: UserRow
}) {
  const queryClient = useQueryClient()
  const [issued, setIssued] = useState<PasswordResetResult | null>(null)

  const {
    register,
    handleSubmit,
    reset,
    watch,
    setError,
    formState: { errors },
  } = useForm<ResetValues>({
    resolver: zodResolver(resetSchema),
    defaultValues: { new_password: '', reason: '' },
  })

  useEffect(() => {
    if (open) {
      reset({ new_password: '', reason: '' })
      setIssued(null)
    }
  }, [open, reset])

  const password = watch('new_password')

  const mutation = useMutation({
    mutationFn: (values: ResetValues) =>
      api.post<PasswordResetResult>(`/users/${user.id}/reset-password`, {
        ...(values.new_password ? { new_password: values.new_password } : {}),
        reason: values.reason.trim(),
      }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      setIssued(result)
      toast.success(
        success.reset('Password'),
        `${result.sessions_revoked} session(s) revoked for ${user.email}`,
      )
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Reset password for {user.full_name}</DialogTitle>
          <DialogDescription>
            The account is signed out everywhere and must choose a new password at its next sign-in.
          </DialogDescription>
        </DialogHeader>

        {issued ? (
          <>
            <Alert variant="warning">
              <AlertTitle>
                {issued.temporary_password ? 'Temporary password' : 'Password set'}
              </AlertTitle>
              <AlertDescription>
                {issued.temporary_password ? (
                  <>
                    <code className="mt-1.5 block break-all rounded-md bg-muted px-2 py-1.5 font-mono text-sm text-foreground">
                      {issued.temporary_password}
                    </code>
                    <span className="mt-2 block">
                      Shown once and never recoverable. Hand it over through a channel the account
                      holder controls.
                    </span>
                  </>
                ) : (
                  'The password you supplied is now in force.'
                )}
              </AlertDescription>
            </Alert>
            <DialogFooter>
              <Button onClick={() => onOpenChange(false)}>Done</Button>
            </DialogFooter>
          </>
        ) : (
          <form
            onSubmit={handleSubmit((values) => mutation.mutate(values))}
            className="flex flex-col gap-4"
            noValidate
          >
            <div>
              <Label htmlFor="reset-password">New password</Label>
              <Input
                id="reset-password"
                type="password"
                {...register('new_password')}
                autoComplete="new-password"
                aria-invalid={Boolean(errors.new_password)}
                className="mt-1.5"
              />
              <FieldError message={errors.new_password?.message} />
              <p className="mt-1 text-xs text-muted-foreground">
                Leave blank and the server generates one. That is the safer default: an
                administrator who invents passwords tends to reuse the same one.
              </p>
              {password && (
                <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  {PASSWORD_RULES.map((rule) => (
                    <li
                      key={rule.id}
                      className={rule.satisfied(password) ? 'text-success' : undefined}
                    >
                      {rule.label}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <Label htmlFor="reset-reason" required>
                Reason
              </Label>
              <Input
                id="reset-reason"
                {...register('reason')}
                autoComplete="off"
                aria-invalid={Boolean(errors.reason)}
                className="mt-1.5"
              />
              <FieldError message={errors.reason?.message} />
            </div>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="destructive" loading={mutation.isPending}>
                <KeyRound />
                Reset password
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

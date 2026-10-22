/**
 * Create and edit dialogs for the organisation structure.
 *
 * Five small forms in one file: each is a handful of fields, and splitting them
 * would mean five imports on the page for no benefit. `code` is absent from
 * every update payload because the API refuses to change one - a branch code
 * appears on issued documents and in historical audit rows, and a cost-centre
 * code on posted ledger history.
 *
 * A cost-centre field (a department's default, a centre's parent) is offered
 * only to an account holding `cost_centre.view`. Without it the field is not
 * shown, and the value the record already holds travels back unchanged on an
 * edit - a hidden control must never be the reason a saved value is cleared.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
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
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { useCostCentreOptions } from '@/hooks/use-organisation-options'
import { useUserOptions } from '@/hooks/use-user-options'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise, toDateInput } from '@/lib/format'
import { success, validation } from '@/lib/messages'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'

import {
  orgKeys,
  useBranchOptions,
  usePlantOptions,
  type BranchRow,
  type DepartmentRow,
  type FinancialYearRow,
  type PlantRow,
} from './admin-types'
import {
  COST_CENTRE_TYPES,
  subtreeIds,
  type CostCentreCreate,
  type CostCentreRow,
  type CostCentreType,
  type CostCentreUpdate,
} from './cost-centre-model'
import { CostCentreSelect } from './cost-centre-select'

const CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._\-/ ]*$/
const CODE_MESSAGE =
  'Use letters, digits, dot, dash, slash, underscore or space, starting with a letter or digit'

const codeField = z
  .string()
  .trim()
  .min(1, validation.required('Code'))
  .max(32, 'At most 32 characters')
  .regex(CODE_PATTERN, CODE_MESSAGE)

const nameField = z
  .string()
  .trim()
  .min(1, validation.required('Name'))
  .max(255, 'At most 255 characters')

const optionalEmail = z
  .string()
  .trim()
  .refine((value) => value === '' || z.string().email().safeParse(value).success, validation.email)

// ---------------------------------------------------------------- branch ----

const branchSchema = z.object({
  code: codeField,
  name: nameField,
  gstin: z.string().trim().max(15, 'A GSTIN is 15 characters'),
  address_line1: z.string().trim().max(255, 'At most 255 characters'),
  city: z.string().trim().max(120, 'At most 120 characters'),
  state: z.string().trim().max(120, 'At most 120 characters'),
  state_code: z.string().trim().max(4, 'At most 4 characters'),
  postal_code: z.string().trim().max(16, 'At most 16 characters'),
  phone: z.string().trim().max(32, 'At most 32 characters'),
  email: optionalEmail,
  is_active: z.boolean(),
  is_head_office: z.boolean(),
})

type BranchValues = z.infer<typeof branchSchema>

function emptyBranch(): BranchValues {
  return {
    code: '',
    name: '',
    gstin: '',
    address_line1: '',
    city: '',
    state: '',
    state_code: '',
    postal_code: '',
    phone: '',
    email: '',
    is_active: true,
    is_head_office: false,
  }
}

export function BranchFormDialog({
  open,
  onOpenChange,
  branch,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  branch?: BranchRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(branch)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<BranchValues>({
    resolver: zodResolver(branchSchema),
    defaultValues: emptyBranch(),
  })

  const editingId = branch?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(
      branch
        ? {
            code: branch.code,
            name: branch.name,
            gstin: branch.gstin ?? '',
            address_line1: branch.address_line1 ?? '',
            city: branch.city ?? '',
            state: branch.state ?? '',
            state_code: branch.state_code ?? '',
            postal_code: branch.postal_code ?? '',
            phone: branch.phone ?? '',
            email: branch.email ?? '',
            is_active: branch.is_active,
            is_head_office: branch.is_head_office,
          }
        : emptyBranch(),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: BranchValues) => {
      const shared = {
        name: values.name.trim(),
        gstin: asText(values.gstin),
        address_line1: asText(values.address_line1),
        city: asText(values.city),
        state: asText(values.state),
        state_code: asText(values.state_code),
        postal_code: asText(values.postal_code),
        phone: asText(values.phone),
        email: asText(values.email),
        is_active: values.is_active,
        is_head_office: values.is_head_office,
      }
      return branch
        ? api.patch<BranchRow>(`/organization/branches/${branch.id}`, shared)
        : api.post<BranchRow>('/organization/branches', { ...shared, code: values.code.trim() })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(
        isEdit ? success.updated('Branch') : success.created('Branch'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${branch?.code}` : 'New branch'}</DialogTitle>
          <DialogDescription>
            A branch is a GST registration and a billing address. Its code is printed on documents
            and cannot be changed later.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="branch-code" required>
                Code
              </Label>
              <Input
                id="branch-code"
                {...register('code')}
                disabled={isEdit}
                autoComplete="off"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5"
              />
              <FieldError message={errors.code?.message} />
            </div>

            <div>
              <Label htmlFor="branch-name" required>
                Name
              </Label>
              <Input
                id="branch-name"
                {...register('name')}
                autoComplete="off"
                aria-invalid={Boolean(errors.name)}
                className="mt-1.5"
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="branch-gstin">GSTIN</Label>
              <Input
                id="branch-gstin"
                {...register('gstin')}
                autoComplete="off"
                className="mt-1.5"
              />
              <FieldError message={errors.gstin?.message} />
            </div>

            <div>
              <Label htmlFor="branch-phone">Phone</Label>
              <Input
                id="branch-phone"
                {...register('phone')}
                autoComplete="off"
                className="mt-1.5"
              />
              <FieldError message={errors.phone?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="branch-address">Address</Label>
              <Input
                id="branch-address"
                {...register('address_line1')}
                autoComplete="off"
                className="mt-1.5"
              />
              <FieldError message={errors.address_line1?.message} />
            </div>

            <div>
              <Label htmlFor="branch-city">City</Label>
              <Input id="branch-city" {...register('city')} className="mt-1.5" />
              <FieldError message={errors.city?.message} />
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <Label htmlFor="branch-state">State</Label>
                <Input id="branch-state" {...register('state')} className="mt-1.5" />
                <FieldError message={errors.state?.message} />
              </div>
              <div>
                <Label htmlFor="branch-state-code">GST state code</Label>
                <Input id="branch-state-code" {...register('state_code')} className="mt-1.5" />
                <FieldError message={errors.state_code?.message} />
              </div>
            </div>

            <div>
              <Label htmlFor="branch-postal">Postal code</Label>
              <Input id="branch-postal" {...register('postal_code')} className="mt-1.5" />
              <FieldError message={errors.postal_code?.message} />
            </div>

            <div>
              <Label htmlFor="branch-email">Email</Label>
              <Input
                id="branch-email"
                type="email"
                {...register('email')}
                autoComplete="off"
                aria-invalid={Boolean(errors.email)}
                className="mt-1.5"
              />
              <FieldError message={errors.email?.message} />
            </div>

            <label className="flex items-center gap-2.5 text-sm">
              <Controller
                control={control}
                name="is_head_office"
                render={({ field }) => (
                  <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
              Head office
            </label>

            <label className="flex items-center gap-2.5 text-sm">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
              Active
            </label>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create branch'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------------- plant ----

const plantSchema = z.object({
  code: codeField,
  name: nameField,
  branch_id: z.string(),
  address_line1: z.string().trim().max(255, 'At most 255 characters'),
  city: z.string().trim().max(120, 'At most 120 characters'),
  state: z.string().trim().max(120, 'At most 120 characters'),
  is_active: z.boolean(),
})

type PlantValues = z.infer<typeof plantSchema>

function emptyPlant(): PlantValues {
  return {
    code: '',
    name: '',
    branch_id: SELECT_NONE,
    address_line1: '',
    city: '',
    state: '',
    is_active: true,
  }
}

export function PlantFormDialog({
  open,
  onOpenChange,
  plant,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  plant?: PlantRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(plant)
  const branches = useBranchOptions(open)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<PlantValues>({
    resolver: zodResolver(plantSchema),
    defaultValues: emptyPlant(),
  })

  const editingId = plant?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(
      plant
        ? {
            code: plant.code,
            name: plant.name,
            branch_id: plant.branch_id ?? SELECT_NONE,
            address_line1: plant.address_line1 ?? '',
            city: plant.city ?? '',
            state: plant.state ?? '',
            is_active: plant.is_active,
          }
        : emptyPlant(),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: PlantValues) => {
      const shared = {
        name: values.name.trim(),
        branch_id: asId(values.branch_id),
        address_line1: asText(values.address_line1),
        city: asText(values.city),
        state: asText(values.state),
        is_active: values.is_active,
      }
      return plant
        ? api.patch<PlantRow>(`/organization/plants/${plant.id}`, shared)
        : api.post<PlantRow>('/organization/plants', { ...shared, code: values.code.trim() })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(
        isEdit ? success.updated('Plant') : success.created('Plant'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${plant?.code}` : 'New plant'}</DialogTitle>
          <DialogDescription>
            A plant is a manufacturing site. Warehouses, production orders and users are placed
            against it.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="plant-code" required>
                Code
              </Label>
              <Input
                id="plant-code"
                {...register('code')}
                disabled={isEdit}
                autoComplete="off"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5"
              />
              <FieldError message={errors.code?.message} />
            </div>

            <div>
              <Label htmlFor="plant-name" required>
                Name
              </Label>
              <Input
                id="plant-name"
                {...register('name')}
                autoComplete="off"
                aria-invalid={Boolean(errors.name)}
                className="mt-1.5"
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="plant-branch">Branch</Label>
              <Controller
                control={control}
                name="branch_id"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="plant-branch" className="mt-1.5">
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

            <div className="sm:col-span-2">
              <Label htmlFor="plant-address">Address</Label>
              <Input
                id="plant-address"
                {...register('address_line1')}
                autoComplete="off"
                className="mt-1.5"
              />
              <FieldError message={errors.address_line1?.message} />
            </div>

            <div>
              <Label htmlFor="plant-city">City</Label>
              <Input id="plant-city" {...register('city')} className="mt-1.5" />
              <FieldError message={errors.city?.message} />
            </div>

            <div>
              <Label htmlFor="plant-state">State</Label>
              <Input id="plant-state" {...register('state')} className="mt-1.5" />
              <FieldError message={errors.state?.message} />
            </div>

            <label className="flex items-center gap-2.5 text-sm sm:col-span-2">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
              Active
            </label>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create plant'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------ department ----

const departmentSchema = z.object({
  code: codeField,
  name: nameField,
  plant_id: z.string(),
  parent_id: z.string(),
  head_user_id: z.string(),
  /** Empty means no default. */
  cost_centre_id: z.string(),
  is_active: z.boolean(),
})

type DepartmentValues = z.infer<typeof departmentSchema>

function emptyDepartment(): DepartmentValues {
  return {
    code: '',
    name: '',
    plant_id: SELECT_NONE,
    parent_id: SELECT_NONE,
    head_user_id: SELECT_NONE,
    cost_centre_id: '',
    is_active: true,
  }
}

export function DepartmentFormDialog({
  open,
  onOpenChange,
  department,
  siblings,
  canSeeUsers = true,
  canSeeCostCentres = false,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  department?: DepartmentRow | null
  /** Candidate parents - the current page of departments. */
  siblings: DepartmentRow[]
  /**
   * Whether the caller holds `user.view`, which `GET /users` enforces. Without
   * it the head-of-department picker is not offered: it could only ever list
   * "Unassigned", and the request behind it could only be refused.
   */
  canSeeUsers?: boolean
  /** Whether the caller holds `cost_centre.view`; the default-centre picker needs it. */
  canSeeCostCentres?: boolean
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(department)
  const plants = usePlantOptions(open)
  const users = useUserOptions({ enabled: open && canSeeUsers })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<DepartmentValues>({
    resolver: zodResolver(departmentSchema),
    defaultValues: emptyDepartment(),
  })

  const editingId = department?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(
      department
        ? {
            code: department.code,
            name: department.name,
            plant_id: department.plant_id ?? SELECT_NONE,
            parent_id: department.parent_id ?? SELECT_NONE,
            head_user_id: department.head_user_id ?? SELECT_NONE,
            cost_centre_id: department.cost_centre_id ?? '',
            is_active: department.is_active,
          }
        : emptyDepartment(),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: DepartmentValues) => {
      const shared = {
        name: values.name.trim(),
        plant_id: asId(values.plant_id),
        parent_id: asId(values.parent_id),
        head_user_id: asId(values.head_user_id),
        // Always sent. Without `cost_centre.view` the picker is not shown and
        // this is the value the department already had, so an edit by such an
        // account leaves the default exactly where it was.
        cost_centre_id: asId(values.cost_centre_id),
        is_active: values.is_active,
      }
      return department
        ? api.patch<DepartmentRow>(`/organization/departments/${department.id}`, shared)
        : api.post<DepartmentRow>('/organization/departments', {
            ...shared,
            code: values.code.trim(),
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(
        isEdit ? success.updated('Department') : success.created('Department'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${department?.code}` : 'New department'}</DialogTitle>
          <DialogDescription>
            Departments nest, so a shop-floor section can sit under Production without being a plant
            of its own.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="dept-code" required>
                Code
              </Label>
              <Input
                id="dept-code"
                {...register('code')}
                disabled={isEdit}
                autoComplete="off"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5"
              />
              <FieldError message={errors.code?.message} />
            </div>

            <div>
              <Label htmlFor="dept-name" required>
                Name
              </Label>
              <Input
                id="dept-name"
                {...register('name')}
                autoComplete="off"
                aria-invalid={Boolean(errors.name)}
                className="mt-1.5"
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="dept-plant">Plant</Label>
              <Controller
                control={control}
                name="plant_id"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="dept-plant" className="mt-1.5">
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
              <Label htmlFor="dept-parent">Parent department</Label>
              <Controller
                control={control}
                name="parent_id"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="dept-parent" className="mt-1.5">
                      <SelectValue placeholder="Top level" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_NONE}>Top level</SelectItem>
                      {siblings
                        .filter((candidate) => candidate.id !== department?.id)
                        .map((candidate) => (
                          <SelectItem key={candidate.id} value={candidate.id}>
                            {candidate.code} - {candidate.name}
                          </SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </div>

            {canSeeUsers && (
              <div className="sm:col-span-2">
                <Label htmlFor="dept-head">Head of department</Label>
                <Controller
                  control={control}
                  name="head_user_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="dept-head" className="mt-1.5">
                        <SelectValue placeholder="Unassigned" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>Unassigned</SelectItem>
                        {(users.data ?? []).map((option) => (
                          <SelectItem key={option.id} value={option.id}>
                            {option.full_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>
            )}

            {canSeeCostCentres && (
              <Controller
                control={control}
                name="cost_centre_id"
                render={({ field }) => (
                  <CostCentreSelect
                    id="dept-cost-centre"
                    label="Default cost centre"
                    value={field.value || null}
                    onChange={(next) => field.onChange(next ?? '')}
                    enabled={open}
                    noneLabel="No default"
                    hint="Where this department's spending is charged unless a document names another."
                    error={errors.cost_centre_id?.message}
                    className="sm:col-span-2"
                  />
                )}
              />
            )}

            <label className="flex items-center gap-2.5 text-sm sm:col-span-2">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
              Active
            </label>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create department'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------- cost centre ----

const costCentreSchema = z.object({
  code: codeField,
  name: nameField,
  // Required and deliberately not defaulted, as on the API: the type is how a
  // centre is grouped on every report, and a preselected one would misfile
  // each centre whose creator did not notice it.
  cost_centre_type: z
    .string()
    .refine(
      (value): boolean => (COST_CENTRE_TYPES as readonly string[]).includes(value),
      'Choose how this centre is grouped on reports',
    ),
  /** Empty means a top-level centre. */
  parent_id: z.string(),
  plant_id: z.string(),
  head_user_id: z.string(),
  description: z.string().trim().max(2000, 'At most 2000 characters'),
  is_active: z.boolean(),
})

type CostCentreValues = z.infer<typeof costCentreSchema>

function emptyCostCentre(): CostCentreValues {
  return {
    code: '',
    name: '',
    cost_centre_type: '',
    parent_id: '',
    plant_id: SELECT_NONE,
    head_user_id: SELECT_NONE,
    description: '',
    is_active: true,
  }
}

function costCentreValues(centre: CostCentreRow): CostCentreValues {
  return {
    code: centre.code,
    name: centre.name,
    cost_centre_type: centre.cost_centre_type,
    parent_id: centre.parent_id ?? '',
    plant_id: centre.plant_id ?? SELECT_NONE,
    head_user_id: centre.head_user_id ?? SELECT_NONE,
    description: centre.description ?? '',
    is_active: centre.is_active,
  }
}

/**
 * The fields both payloads share; the code is the create's alone.
 *
 * Every field is sent on an edit too. An explicit null is how the API is told
 * to clear one, and the form always holds the record's current value - so a
 * field nobody touched, or one whose picker this account is not shown, travels
 * back unchanged rather than being cleared.
 */
function costCentrePayload(values: CostCentreValues): Omit<CostCentreCreate, 'code'> {
  return {
    name: values.name.trim(),
    cost_centre_type: values.cost_centre_type as CostCentreType,
    parent_id: values.parent_id || null,
    plant_id: asId(values.plant_id),
    head_user_id: asId(values.head_user_id),
    description: asText(values.description),
    is_active: values.is_active,
  }
}

export function CostCentreFormDialog({
  open,
  onOpenChange,
  centre,
  canSeePlants = true,
  canSeeUsers = true,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  centre?: CostCentreRow | null
  /** Whether the caller holds `plant.view`; the plant picker needs it. */
  canSeePlants?: boolean
  /** Whether the caller holds `user.view`; the head picker needs it. */
  canSeeUsers?: boolean
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(centre)
  const plants = usePlantOptions(open && canSeePlants)
  const users = useUserOptions({ enabled: open && canSeeUsers })

  // The whole tree, not the page of the register on screen: a parent two pages
  // away is still a parent. The same request the parent picker makes.
  const reference = useCostCentreOptions(open)
  const ownSubtree = useMemo(
    () => (centre ? subtreeIds(reference.data ?? [], centre.id) : undefined),
    [centre, reference.data],
  )

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<CostCentreValues>({
    resolver: zodResolver(costCentreSchema),
    defaultValues: emptyCostCentre(),
  })

  const editingId = centre?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(centre ? costCentreValues(centre) : emptyCostCentre())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: CostCentreValues) => {
      if (centre) {
        const update: CostCentreUpdate = costCentrePayload(values)
        return api.patch<CostCentreRow>(`/organization/cost-centres/${centre.id}`, update)
      }
      const create: CostCentreCreate = { ...costCentrePayload(values), code: values.code.trim() }
      return api.post<CostCentreRow>('/organization/cost-centres', create)
    },
    onSuccess: (saved) => {
      // `orgKeys.all` is also the root of every cost-centre picker, so the new
      // centre is offered on the next document without a stale half hour.
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(
        isEdit ? success.updated('Cost centre') : success.created('Cost centre'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${centre?.code}` : 'New cost centre'}</DialogTitle>
          <DialogDescription>
            A cost centre is where money is charged - a shop, a process, a sales zone. Centres nest,
            and the code is printed on the cost centre statement, so it cannot be changed later.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="cc-code" required>
                Code
              </Label>
              <Input
                id="cc-code"
                {...register('code')}
                disabled={isEdit}
                autoComplete="off"
                placeholder="CC-MACH"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5"
              />
              <FieldError message={errors.code?.message} />
            </div>

            <div>
              <Label htmlFor="cc-name" required>
                Name
              </Label>
              <Input
                id="cc-name"
                {...register('name')}
                autoComplete="off"
                aria-invalid={Boolean(errors.name)}
                className="mt-1.5"
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="cc-type" required>
                Type
              </Label>
              <Controller
                control={control}
                name="cost_centre_type"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger
                      id="cc-type"
                      aria-label="Type"
                      aria-invalid={Boolean(errors.cost_centre_type)}
                      className="mt-1.5"
                    >
                      <SelectValue placeholder="How it is reported" />
                    </SelectTrigger>
                    <SelectContent>
                      {COST_CENTRE_TYPES.map((type) => (
                        <SelectItem key={type} value={type}>
                          {humanise(type)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError message={errors.cost_centre_type?.message} />
              <p className="mt-1 text-xs text-muted-foreground">
                Groups the centre on reports. Nothing else depends on it.
              </p>
            </div>

            <Controller
              control={control}
              name="parent_id"
              render={({ field }) => (
                <CostCentreSelect
                  id="cc-parent"
                  label="Parent cost centre"
                  value={field.value || null}
                  onChange={(next) => field.onChange(next ?? '')}
                  enabled={open}
                  noneLabel="Top level"
                  exclude={ownSubtree}
                  hint={
                    isEdit
                      ? 'A centre cannot move beneath one of its own sub-centres, so those are not offered.'
                      : undefined
                  }
                  error={errors.parent_id?.message}
                />
              )}
            />

            {canSeePlants && (
              <div>
                <Label htmlFor="cc-plant">Plant</Label>
                <Controller
                  control={control}
                  name="plant_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="cc-plant" className="mt-1.5">
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
                <FieldError message={errors.plant_id?.message} />
              </div>
            )}

            {canSeeUsers && (
              <div>
                <Label htmlFor="cc-head">Head</Label>
                <Controller
                  control={control}
                  name="head_user_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="cc-head" className="mt-1.5">
                        <SelectValue placeholder="Unassigned" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>Unassigned</SelectItem>
                        {(users.data ?? []).map((option) => (
                          <SelectItem key={option.id} value={option.id}>
                            {option.full_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.head_user_id?.message} />
              </div>
            )}

            <div className="sm:col-span-2">
              <Label htmlFor="cc-description">Description</Label>
              <Textarea id="cc-description" {...register('description')} className="mt-1.5" />
              <FieldError message={errors.description?.message} />
            </div>

            <div className="sm:col-span-2">
              <label className="flex items-center gap-2.5 text-sm">
                <Controller
                  control={control}
                  name="is_active"
                  render={({ field }) => (
                    <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                  )}
                />
                Active
              </label>
              <p className="mt-1 text-xs text-muted-foreground">
                An inactive centre is no longer offered on new documents; documents that already
                name it keep it. It cannot be switched off while an active sub-centre, a department
                default or a work centre still hands it out.
              </p>
              <FieldError message={errors.is_active?.message} />
            </div>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create cost centre'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------- financial year ----

const financialYearSchema = z
  .object({
    code: z.string().trim().min(1, validation.required('Code')).max(16, 'At most 16 characters'),
    start_date: z.string().min(1, validation.required('Start date')),
    end_date: z.string().min(1, validation.required('End date')),
    is_active: z.boolean(),
    notes: z.string().trim(),
  })
  .refine((values) => values.end_date > values.start_date, {
    message: 'The end date must fall after the start date',
    path: ['end_date'],
  })

type FinancialYearValues = z.infer<typeof financialYearSchema>

export function FinancialYearFormDialog({
  open,
  onOpenChange,
  year,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  year?: FinancialYearRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(year)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<FinancialYearValues>({
    resolver: zodResolver(financialYearSchema),
    defaultValues: { code: '', start_date: '', end_date: '', is_active: true, notes: '' },
  })

  const editingId = year?.id ?? null
  useEffect(() => {
    if (!open) return
    reset(
      year
        ? {
            code: year.code,
            start_date: toDateInput(year.start_date),
            end_date: toDateInput(year.end_date),
            is_active: year.is_active,
            notes: year.notes ?? '',
          }
        : { code: '', start_date: '', end_date: '', is_active: true, notes: '' },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: FinancialYearValues) => {
      const shared = {
        start_date: values.start_date,
        end_date: values.end_date,
        is_active: values.is_active,
        notes: asText(values.notes),
      }
      return year
        ? api.patch<FinancialYearRow>(`/organization/financial-years/${year.id}`, shared)
        : api.post<FinancialYearRow>('/organization/financial-years', {
            ...shared,
            code: values.code.trim(),
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: orgKeys.all })
      toast.success(
        isEdit ? success.updated('Financial year') : success.opened('Financial year'),
        saved.code,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${year?.code}` : 'Open a financial year'}</DialogTitle>
          <DialogDescription>
            The accounting calendar every posted document is dated against. Ranges may not overlap.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <Label htmlFor="fy-code" required>
                Code
              </Label>
              <Input
                id="fy-code"
                {...register('code')}
                disabled={isEdit}
                placeholder="2026-27"
                autoComplete="off"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5"
              />
              <FieldError message={errors.code?.message} />
            </div>

            <div>
              <Label htmlFor="fy-start" required>
                Start date
              </Label>
              <Input
                id="fy-start"
                type="date"
                {...register('start_date')}
                aria-invalid={Boolean(errors.start_date)}
                className="mt-1.5"
              />
              <FieldError message={errors.start_date?.message} />
            </div>

            <div>
              <Label htmlFor="fy-end" required>
                End date
              </Label>
              <Input
                id="fy-end"
                type="date"
                {...register('end_date')}
                aria-invalid={Boolean(errors.end_date)}
                className="mt-1.5"
              />
              <FieldError message={errors.end_date?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="fy-notes">Notes</Label>
              <Textarea id="fy-notes" {...register('notes')} className="mt-1.5" />
              <FieldError message={errors.notes?.message} />
            </div>

            <label className="flex items-center gap-2.5 text-sm sm:col-span-2">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
              Current year
            </label>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Open year'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

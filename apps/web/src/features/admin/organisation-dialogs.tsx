/**
 * Create and edit dialogs for the organisation structure.
 *
 * Four small forms in one file: each is a handful of fields, and splitting them
 * would mean four imports on the page for no benefit. `code` is absent from
 * every update payload because the API refuses to change one - a branch code
 * appears on issued documents and in historical audit rows.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
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
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { toDateInput } from '@/lib/format'
import { toast } from '@/lib/toast'

import {
  orgKeys,
  useBranchOptions,
  usePlantOptions,
  useUserOptions,
  type BranchRow,
  type DepartmentRow,
  type FinancialYearRow,
  type PlantRow,
} from './admin-types'

const NONE = '__none'
const CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._\-/ ]*$/
const CODE_MESSAGE =
  'Use letters, digits, dot, dash, slash, underscore or space, starting with a letter or digit'

const asId = (value: string): string | null => (value === NONE || value === '' ? null : value)
const asText = (value: string): string | null => (value.trim() === '' ? null : value.trim())

const codeField = z
  .string()
  .trim()
  .min(1, 'Code is required')
  .max(32, 'At most 32 characters')
  .regex(CODE_PATTERN, CODE_MESSAGE)

const nameField = z.string().trim().min(1, 'Name is required').max(255, 'At most 255 characters')

const optionalEmail = z
  .string()
  .trim()
  .refine(
    (value) => value === '' || z.string().email().safeParse(value).success,
    'Enter a valid email address',
  )

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
      toast.success(isEdit ? 'Branch updated' : 'Branch created', `${saved.code} - ${saved.name}`)
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

            <div className="grid grid-cols-2 gap-3">
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
    branch_id: NONE,
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
            branch_id: plant.branch_id ?? NONE,
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
      toast.success(isEdit ? 'Plant updated' : 'Plant created', `${saved.code} - ${saved.name}`)
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
                      <SelectItem value={NONE}>No branch</SelectItem>
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
  is_active: z.boolean(),
})

type DepartmentValues = z.infer<typeof departmentSchema>

function emptyDepartment(): DepartmentValues {
  return {
    code: '',
    name: '',
    plant_id: NONE,
    parent_id: NONE,
    head_user_id: NONE,
    is_active: true,
  }
}

export function DepartmentFormDialog({
  open,
  onOpenChange,
  department,
  siblings,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  department?: DepartmentRow | null
  /** Candidate parents - the current page of departments. */
  siblings: DepartmentRow[]
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(department)
  const plants = usePlantOptions(open)
  const users = useUserOptions(open)

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
            plant_id: department.plant_id ?? NONE,
            parent_id: department.parent_id ?? NONE,
            head_user_id: department.head_user_id ?? NONE,
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
        isEdit ? 'Department updated' : 'Department created',
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
                      <SelectItem value={NONE}>No plant</SelectItem>
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
                      <SelectItem value={NONE}>Top level</SelectItem>
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
                      <SelectItem value={NONE}>Unassigned</SelectItem>
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

// -------------------------------------------------------- financial year ----

const financialYearSchema = z
  .object({
    code: z.string().trim().min(1, 'Code is required').max(16, 'At most 16 characters'),
    start_date: z.string().min(1, 'Start date is required'),
    end_date: z.string().min(1, 'End date is required'),
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
      toast.success(isEdit ? 'Financial year updated' : 'Financial year opened', saved.code)
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

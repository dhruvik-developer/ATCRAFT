/**
 * The two write surfaces of the warehouse screen: the warehouse create/edit
 * dialog and the add-location dialog.
 *
 * Kept out of the page file because both are forms with their own schema, and a
 * single file holding the grid, the expansion panel and two forms would be read
 * by nobody.
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
import { useAuth } from '@/features/auth/auth-context'
import { useBranchOptions } from '@/hooks/use-organisation-options'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'

import {
  LOCATION_TYPES,
  useWarehouseOptions,
  WAREHOUSE_TYPES,
  type StorageLocation,
  type Warehouse,
} from './warehouse-model'

const CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._\-/ ]*$/
const CODE_MESSAGE =
  'Use letters, digits, dot, dash, slash, underscore or space, starting with a letter or digit'
const DECIMAL = /^(\d{1,14}(\.\d{1,4})?)?$/

// ------------------------------------------------------------ warehouse -----

const warehouseSchema = z.object({
  code: z
    .string()
    .trim()
    .min(1, 'Code is required')
    .max(32, 'Code is at most 32 characters')
    .regex(CODE_PATTERN, CODE_MESSAGE),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  warehouse_type: z.enum(WAREHOUSE_TYPES),
  branch_id: z.string(),
  parent_warehouse_id: z.string(),
  allow_negative_stock: z.boolean(),
  is_default: z.boolean(),
  address_line1: z.string().trim().max(255, 'At most 255 characters'),
  city: z.string().trim().max(120, 'At most 120 characters'),
  state: z.string().trim().max(120, 'At most 120 characters'),
  state_code: z.string().trim().max(4, 'GST state code is at most 4 characters'),
  postal_code: z.string().trim().max(16, 'At most 16 characters'),
  country: z.string().trim().max(80, 'At most 80 characters'),
  phone: z.string().trim().max(32, 'At most 32 characters'),
  email: z
    .string()
    .trim()
    .refine(
      (value) => value === '' || z.string().email().safeParse(value).success,
      'Enter a valid email address',
    ),
  notes: z.string().trim(),
})

type WarehouseFormValues = z.infer<typeof warehouseSchema>

function emptyWarehouse(): WarehouseFormValues {
  return {
    code: '',
    name: '',
    warehouse_type: 'RAW_MATERIAL',
    branch_id: SELECT_NONE,
    parent_warehouse_id: SELECT_NONE,
    allow_negative_stock: false,
    is_default: false,
    address_line1: '',
    city: '',
    state: '',
    state_code: '',
    postal_code: '',
    country: 'India',
    phone: '',
    email: '',
    notes: '',
  }
}

function warehouseValues(warehouse: Warehouse): WarehouseFormValues {
  return {
    code: warehouse.code,
    name: warehouse.name,
    warehouse_type: warehouse.warehouse_type,
    branch_id: warehouse.branch_id ?? SELECT_NONE,
    parent_warehouse_id: warehouse.parent_warehouse_id ?? SELECT_NONE,
    allow_negative_stock: warehouse.allow_negative_stock,
    is_default: warehouse.is_default,
    address_line1: warehouse.address_line1 ?? '',
    city: warehouse.city ?? '',
    state: warehouse.state ?? '',
    state_code: warehouse.state_code ?? '',
    postal_code: warehouse.postal_code ?? '',
    country: warehouse.country ?? 'India',
    phone: warehouse.phone ?? '',
    email: warehouse.email ?? '',
    notes: warehouse.notes ?? '',
  }
}

export function WarehouseFormDialog({
  open,
  onOpenChange,
  warehouse,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  warehouse?: Warehouse | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(warehouse)

  const branches = useBranchOptions(open && can(P.COMPANY_VIEW))
  const parents = useWarehouseOptions(open)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<WarehouseFormValues>({
    resolver: zodResolver(warehouseSchema),
    defaultValues: emptyWarehouse(),
  })

  // Seeded once per opening, keyed on the id rather than the object identity: a
  // background refetch would otherwise resolve mid-edit and wipe the form.
  const editingId = warehouse?.id ?? null
  useEffect(() => {
    if (open) reset(warehouse ? warehouseValues(warehouse) : emptyWarehouse())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: WarehouseFormValues) => {
      const shared = {
        name: values.name.trim(),
        warehouse_type: values.warehouse_type,
        branch_id: asId(values.branch_id),
        parent_warehouse_id: asId(values.parent_warehouse_id),
        allow_negative_stock: values.allow_negative_stock,
        notes: asText(values.notes),
        address_line1: asText(values.address_line1),
        city: asText(values.city),
        state: asText(values.state),
        state_code: asText(values.state_code),
        postal_code: asText(values.postal_code),
        country: values.country.trim() || 'India',
        phone: asText(values.phone),
        email: asText(values.email),
      }

      // The update schema forbids unknown keys, and code, default and active
      // state each have their own endpoint - so they are never sent here.
      return warehouse
        ? api.patch<Warehouse>(`/warehouses/${warehouse.id}`, shared)
        : api.post<Warehouse>('/warehouses', {
            ...shared,
            code: values.code.trim(),
            is_active: true,
            is_default: values.is_default,
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.warehouses.all })
      toast.success(
        isEdit ? success.updated('Warehouse') : success.created('Warehouse'),
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
          <DialogTitle>{isEdit ? `Edit ${warehouse?.code}` : 'New warehouse'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - it is printed on bin labels and quoted on issued documents.'
              : 'Codes are normalised by the server, so FG-01 and fg 01 cannot become two warehouses.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="wh-code" required>
                  Code
                </Label>
                <Input
                  id="wh-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="wh-name" required>
                  Name
                </Label>
                <Input
                  id="wh-name"
                  {...register('name')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>

              <div>
                <Label htmlFor="wh-type" required>
                  Type
                </Label>
                <Controller
                  control={control}
                  name="warehouse_type"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="wh-type" className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {WAREHOUSE_TYPES.map((type) => (
                          <SelectItem key={type} value={type}>
                            {humanise(type)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  A classification for defaults and reporting. It never decides whether a movement
                  is allowed.
                </p>
              </div>

              <div>
                <Label htmlFor="wh-branch">Branch</Label>
                <Controller
                  control={control}
                  name="branch_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="wh-branch" className="mt-1.5">
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
                <FieldError message={errors.branch_id?.message} />
              </div>

              <div>
                <Label htmlFor="wh-parent">Parent warehouse</Label>
                <Controller
                  control={control}
                  name="parent_warehouse_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="wh-parent" className="mt-1.5">
                        <SelectValue placeholder="Top level" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>Top level</SelectItem>
                        {(parents.data ?? [])
                          .filter((option) => option.id !== warehouse?.id)
                          .map((option) => (
                            <SelectItem key={option.id} value={option.id}>
                              {option.code} - {option.name}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.parent_warehouse_id?.message} />
              </div>

              <div>
                <Label htmlFor="wh-country">Country</Label>
                <Input id="wh-country" {...register('country')} className="mt-1.5" />
                <FieldError message={errors.country?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="wh-address">Address</Label>
                <Input
                  id="wh-address"
                  {...register('address_line1')}
                  autoComplete="off"
                  className="mt-1.5"
                />
                <FieldError message={errors.address_line1?.message} />
              </div>

              <div>
                <Label htmlFor="wh-city">City</Label>
                <Input id="wh-city" {...register('city')} className="mt-1.5" />
                <FieldError message={errors.city?.message} />
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <Label htmlFor="wh-state">State</Label>
                  <Input id="wh-state" {...register('state')} className="mt-1.5" />
                  <FieldError message={errors.state?.message} />
                </div>
                <div>
                  <Label htmlFor="wh-state-code">GST state code</Label>
                  <Input id="wh-state-code" {...register('state_code')} className="mt-1.5" />
                  <FieldError message={errors.state_code?.message} />
                </div>
              </div>

              <div>
                <Label htmlFor="wh-postal">Postal code</Label>
                <Input id="wh-postal" {...register('postal_code')} className="mt-1.5" />
                <FieldError message={errors.postal_code?.message} />
              </div>

              <div>
                <Label htmlFor="wh-phone">Phone</Label>
                <Input id="wh-phone" type="tel" {...register('phone')} className="mt-1.5" />
                <FieldError message={errors.phone?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="wh-email">Email</Label>
                <Input
                  id="wh-email"
                  type="email"
                  {...register('email')}
                  aria-invalid={Boolean(errors.email)}
                  className="mt-1.5"
                />
                <FieldError message={errors.email?.message} />
              </div>

              <div className="sm:col-span-2">
                <Label htmlFor="wh-notes">Notes</Label>
                <Textarea id="wh-notes" {...register('notes')} className="mt-1.5" />
              </div>
            </div>

            <div className="space-y-3 rounded-lg border p-4">
              <div className="flex items-start gap-2.5">
                <Controller
                  control={control}
                  name="allow_negative_stock"
                  render={({ field }) => (
                    <Checkbox
                      id="wh-negative"
                      className="mt-0.5"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <div>
                  <Label htmlFor="wh-negative" className="font-normal">
                    Allow issues before receipts
                  </Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Relaxes the availability pre-check for this warehouse only. The database still
                    refuses negative stock. Use it for line-side WIP, not as a workaround.
                  </p>
                </div>
              </div>

              {!isEdit && (
                <div className="flex items-start gap-2.5">
                  <Controller
                    control={control}
                    name="is_default"
                    render={({ field }) => (
                      <Checkbox
                        id="wh-default"
                        className="mt-0.5"
                        checked={field.value}
                        onCheckedChange={(checked) => field.onChange(checked === true)}
                      />
                    )}
                  />
                  <div>
                    <Label htmlFor="wh-default" className="font-normal">
                      Make this the company default
                    </Label>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      Receipts and issues propose this warehouse when a document does not name one.
                    </p>
                  </div>
                </div>
              )}
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
              {isEdit ? 'Save changes' : 'Create warehouse'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------- location -----

const locationSchema = z.object({
  code: z
    .string()
    .trim()
    .min(1, 'Code is required')
    .max(32, 'Code is at most 32 characters')
    .regex(CODE_PATTERN, CODE_MESSAGE),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  location_type: z.enum(LOCATION_TYPES),
  capacity_quantity: z.string().trim().regex(DECIMAL, 'Enter a number with up to 4 decimal places'),
  capacity_uom: z.string().trim().max(16, 'At most 16 characters'),
  barcode: z.string().trim().max(64, 'At most 64 characters'),
  notes: z.string().trim(),
})

type LocationFormValues = z.infer<typeof locationSchema>

const emptyLocation: LocationFormValues = {
  code: '',
  name: '',
  location_type: 'RACK',
  capacity_quantity: '',
  capacity_uom: '',
  barcode: '',
  notes: '',
}

export function LocationFormDialog({
  open,
  onOpenChange,
  warehouse,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  warehouse: Warehouse
}) {
  const queryClient = useQueryClient()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<LocationFormValues>({
    resolver: zodResolver(locationSchema),
    defaultValues: emptyLocation,
  })

  useEffect(() => {
    if (open) reset(emptyLocation)
  }, [open, reset])

  const mutation = useMutation({
    mutationFn: (values: LocationFormValues) =>
      api.post<StorageLocation>(`/warehouses/${warehouse.id}/locations`, {
        code: values.code.trim(),
        name: values.name.trim(),
        location_type: values.location_type,
        is_active: true,
        // Capacity stays a string on the wire: it is a Numeric(18,4) column.
        capacity_quantity: asText(values.capacity_quantity),
        capacity_uom: asText(values.capacity_uom),
        barcode: asText(values.barcode),
        notes: asText(values.notes),
      }),
    onSuccess: (saved) => {
      // The warehouse row carries a location_count, so the list is invalidated
      // alongside the panel that just gained a row.
      void queryClient.invalidateQueries({ queryKey: queryKeys.warehouses.all })
      toast.success(success.created('Storage location'), `${saved.code} - ${saved.name}`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add storage location</DialogTitle>
          <DialogDescription>
            Into {warehouse.code} - {warehouse.name}.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="loc-code" required>
                Code
              </Label>
              <Input
                id="loc-code"
                {...register('code')}
                autoComplete="off"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5"
              />
              <FieldError message={errors.code?.message} />
            </div>

            <div>
              <Label htmlFor="loc-name" required>
                Name
              </Label>
              <Input
                id="loc-name"
                {...register('name')}
                autoComplete="off"
                aria-invalid={Boolean(errors.name)}
                className="mt-1.5"
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="loc-type" required>
                Type
              </Label>
              <Controller
                control={control}
                name="location_type"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="loc-type" className="mt-1.5">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {LOCATION_TYPES.map((type) => (
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
              <Label htmlFor="loc-barcode">Barcode</Label>
              <Input
                id="loc-barcode"
                {...register('barcode')}
                autoComplete="off"
                className="mt-1.5"
              />
              <FieldError message={errors.barcode?.message} />
            </div>

            <div>
              <Label htmlFor="loc-capacity">Capacity</Label>
              <Input
                id="loc-capacity"
                {...register('capacity_quantity')}
                inputMode="decimal"
                aria-invalid={Boolean(errors.capacity_quantity)}
                className="mt-1.5 text-right tabular"
              />
              <FieldError message={errors.capacity_quantity?.message} />
            </div>

            <div>
              <Label htmlFor="loc-capacity-uom">Capacity unit</Label>
              <Input id="loc-capacity-uom" {...register('capacity_uom')} className="mt-1.5" />
              <FieldError message={errors.capacity_uom?.message} />
            </div>

            <div className="sm:col-span-2">
              <Label htmlFor="loc-notes">Notes</Label>
              <Textarea id="loc-notes" {...register('notes')} className="mt-1.5" />
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
              Add location
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

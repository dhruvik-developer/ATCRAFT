/**
 * Create / edit dialog for an item.
 *
 * One component serves both because the field set is identical apart from
 * `code`, which the API refuses to change after creation: the code is printed
 * on labels and quoted on documents that have already left the building.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Controller, useForm, type Control } from 'react-hook-form'
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
import { Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { LocationPicker } from '@/features/inventory/location-picker'
import { WarehouseSelect } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { humanise, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { useBrandOptions } from '@/hooks/use-brand-options'
import { useItemAttributeOptions } from '@/hooks/use-item-attribute-options'
import { useManufacturerOptions } from '@/hooks/use-manufacturer-options'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asId, asText } from '@/lib/utils'

import {
  ALL_CAPABILITIES,
  CAPABILITY_META,
  ITEM_GROUPS,
  PRODUCTION_TYPES,
  useCategoryOptions,
  useUomOptions,
  type ItemDetail,
} from './item-model'

const DECIMAL = /^(\d{1,14}(\.\d{1,4})?)?$/
const WHOLE_DAYS = /^\d{0,4}$/

const decimalField = (message = 'Enter a number with up to 4 decimal places') =>
  z.string().trim().regex(DECIMAL, message)

const schema = z.object({
  code: z.string().trim().min(1, 'Code is required').max(64, 'Code is at most 64 characters'),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  item_group: z.enum(ITEM_GROUPS),
  category_id: z.string(),
  finish_id: z.string(),
  size_id: z.string(),
  colour_id: z.string(),
  brand_id: z.string(),
  manufacturer_id: z.string(),
  manufacturer_part_number: z.string().trim().max(64, 'Part number is at most 64 characters'),
  base_uom_id: z.string().min(1, 'Base unit is required'),
  purchase_uom_id: z.string(),
  sale_uom_id: z.string(),
  hsn_code: z.string().trim().max(16, 'HSN code is at most 16 characters'),
  gst_rate: decimalField().refine(
    (value) => toDecimal(value).lte(100),
    'GST rate cannot exceed 100',
  ),
  production_type: z.enum(PRODUCTION_TYPES),
  standard_cost: decimalField(),
  reorder_level: decimalField(),
  default_warehouse_id: z.string(),
  default_location_id: z.string(),
  lead_time_days: z
    .string()
    .trim()
    .regex(WHOLE_DAYS, 'Whole days only')
    .refine((value) => value === '' || Number(value) <= 3650, 'Lead time cannot exceed 3650 days'),

  // The nine capabilities are independent booleans, never a single choice: one
  // item is legitimately purchasable, manufacturable and sellable at once
  // (master rules 1).
  can_purchase: z.boolean(),
  can_manufacture: z.boolean(),
  can_sell: z.boolean(),
  can_be_bom_component: z.boolean(),
  can_be_assembly_component: z.boolean(),
  can_be_spare: z.boolean(),
  can_job_work: z.boolean(),
  can_return: z.boolean(),
  can_scrap: z.boolean(),
  requires_production_qc: z.boolean(),
  requires_final_qc: z.boolean(),
})

type ItemFormValues = z.infer<typeof schema>

function emptyValues(): ItemFormValues {
  return {
    code: '',
    name: '',
    item_group: 'RAW_MATERIAL',
    category_id: SELECT_NONE,
    finish_id: SELECT_NONE,
    size_id: SELECT_NONE,
    colour_id: SELECT_NONE,
    brand_id: SELECT_NONE,
    manufacturer_id: SELECT_NONE,
    manufacturer_part_number: '',
    base_uom_id: '',
    purchase_uom_id: SELECT_NONE,
    sale_uom_id: SELECT_NONE,
    hsn_code: '',
    gst_rate: '0',
    production_type: 'BUY',
    standard_cost: '0',
    reorder_level: '0',
    default_warehouse_id: '',
    default_location_id: '',
    lead_time_days: '0',
    can_purchase: false,
    can_manufacture: false,
    can_sell: false,
    can_be_bom_component: false,
    can_be_assembly_component: false,
    can_be_spare: false,
    can_job_work: false,
    can_return: false,
    can_scrap: false,
    requires_production_qc: false,
    requires_final_qc: false,
  }
}

function valuesFrom(item: ItemDetail): ItemFormValues {
  return {
    code: item.code,
    name: item.name,
    item_group: item.item_group,
    category_id: item.category_id ?? SELECT_NONE,
    finish_id: item.finish_id ?? SELECT_NONE,
    size_id: item.size_id ?? SELECT_NONE,
    colour_id: item.colour_id ?? SELECT_NONE,
    brand_id: item.brand_id ?? SELECT_NONE,
    manufacturer_id: item.manufacturer_id ?? SELECT_NONE,
    manufacturer_part_number: item.manufacturer_part_number ?? '',
    base_uom_id: item.base_uom_id,
    purchase_uom_id: item.purchase_uom_id ?? SELECT_NONE,
    sale_uom_id: item.sale_uom_id ?? SELECT_NONE,
    hsn_code: item.hsn_code ?? '',
    gst_rate: item.gst_rate,
    production_type: item.production_type,
    standard_cost: item.standard_cost ?? '0',
    reorder_level: item.reorder_level,
    default_warehouse_id: item.default_warehouse_id ?? '',
    default_location_id: item.default_location_id ?? '',
    lead_time_days: String(item.lead_time_days),
    can_purchase: item.can_purchase,
    can_manufacture: item.can_manufacture,
    can_sell: item.can_sell,
    can_be_bom_component: item.can_be_bom_component,
    can_be_assembly_component: item.can_be_assembly_component,
    can_be_spare: item.can_be_spare,
    can_job_work: item.can_job_work,
    can_return: item.can_return,
    can_scrap: item.can_scrap,
    requires_production_qc: item.requires_production_qc,
    requires_final_qc: item.requires_final_qc,
  }
}

/** Decimals stay strings all the way to the wire - never parsed into a double. */
const asDecimal = (value: string): string => (value.trim() === '' ? '0' : value.trim())

function buildPayload(
  values: ItemFormValues,
  { includeCode, includeCost }: { includeCode: boolean; includeCost: boolean },
): Record<string, unknown> {
  return {
    ...(includeCode ? { code: values.code.trim() } : {}),
    name: values.name.trim(),
    item_group: values.item_group,
    category_id: asId(values.category_id),
    finish_id: asId(values.finish_id),
    size_id: asId(values.size_id),
    colour_id: asId(values.colour_id),
    // "No brand" / "No manufacturer" post null, which the API reads as "clear
    // it"; a blank part number posts null too, never an empty string.
    brand_id: asId(values.brand_id),
    manufacturer_id: asId(values.manufacturer_id),
    manufacturer_part_number: asText(values.manufacturer_part_number),
    base_uom_id: values.base_uom_id,
    purchase_uom_id: asId(values.purchase_uom_id),
    sale_uom_id: asId(values.sale_uom_id),
    hsn_code: asText(values.hsn_code),
    gst_rate: asDecimal(values.gst_rate),
    production_type: values.production_type,
    reorder_level: asDecimal(values.reorder_level),
    default_warehouse_id: values.default_warehouse_id || null,
    default_location_id: values.default_location_id || null,
    // Days are a whole count, not a monetary decimal, so an integer is correct.
    lead_time_days: Number.parseInt(values.lead_time_days || '0', 10),
    ...(includeCost ? { standard_cost: asDecimal(values.standard_cost) } : {}),
    can_purchase: values.can_purchase,
    can_manufacture: values.can_manufacture,
    can_sell: values.can_sell,
    can_be_bom_component: values.can_be_bom_component,
    can_be_assembly_component: values.can_be_assembly_component,
    can_be_spare: values.can_be_spare,
    can_job_work: values.can_job_work,
    can_return: values.can_return,
    can_scrap: values.can_scrap,
    requires_production_qc: values.requires_production_qc,
    requires_final_qc: values.requires_final_qc,
  }
}

/**
 * One optional reference chosen from a client-maintained list: a finish, a
 * size, a colour, a brand or a manufacturer.
 *
 * Written once rather than five times: they differ only in their label and
 * which list they read, and five copies of a Select is five places for a "No
 * finish" sentinel to be spelled differently.
 */
function ReferenceSelect({
  id,
  label,
  name,
  control,
  options,
}: {
  id: string
  label: string
  name: 'finish_id' | 'size_id' | 'colour_id' | 'brand_id' | 'manufacturer_id'
  control: Control<ItemFormValues>
  options: ReadonlyArray<{ id: string; name: string }> | undefined
}) {
  const none = `No ${label.toLowerCase()}`
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Controller
        control={control}
        name={name}
        render={({ field }) => (
          <Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger id={id} className="mt-1.5">
              <SelectValue placeholder={none} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_NONE}>{none}</SelectItem>
              {(options ?? []).map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      />
    </div>
  )
}

export function ItemFormDialog({
  open,
  onOpenChange,
  item,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  item?: ItemDetail | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(item)

  // Affordance only - the API decides whether cost may be written.
  const showCost = can(P.ITEM_COST_VIEW)

  const categories = useCategoryOptions(open && can(P.CATEGORY_VIEW))
  // The client's own vocabulary. Gated on the read right like the category
  // list above: an account without it gets empty selects rather than an
  // error, and the item's existing values still show on the detail screen.
  const mayReadAttributes = open && can(P.ITEM_ATTRIBUTE_VIEW)
  const finishes = useItemAttributeOptions('FINISH', mayReadAttributes)
  const sizes = useItemAttributeOptions('SIZE', mayReadAttributes)
  const colours = useItemAttributeOptions('COLOUR', mayReadAttributes)
  // Brand and maker are rights of their own, gated the same way: without one,
  // its select offers only "None", and an edit still posts the value the item
  // already carries - the form is seeded from the item, not from the list.
  const brands = useBrandOptions(open && can(P.BRAND_VIEW))
  const makers = useManufacturerOptions(open && can(P.MANUFACTURER_VIEW))
  const uoms = useUomOptions(open && can(P.UOM_VIEW))

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<ItemFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  // Seeded once per opening, keyed on the item's id rather than its identity: a
  // background refetch of the item would otherwise resolve mid-edit and wipe
  // what the user is typing.
  const editingId = item?.id ?? null
  useEffect(() => {
    if (open) reset(item ? valuesFrom(item) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: ItemFormValues) =>
      item
        ? api.patch<ItemDetail>(
            `/products/items/${item.id}`,
            buildPayload(values, { includeCode: false, includeCost: showCost }),
          )
        : api.post<ItemDetail>(
            '/products/items',
            buildPayload(values, { includeCode: true, includeCost: showCost }),
          ),
    onSuccess: (saved) => {
      // One prefix invalidation covers every item list and every item detail.
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.all })
      toast.success(
        isEdit ? success.updated('Item') : success.created('Item'),
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
          <DialogTitle>{isEdit ? `Edit ${item?.code}` : 'New item'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - it is quoted on documents that have already been issued.'
              : 'Codes are normalised by the server, so RM-001 and rm 001 cannot become two items.'}
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
                <Label htmlFor="item-code" required>
                  Code
                </Label>
                <Input
                  id="item-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="item-name" required>
                  Name
                </Label>
                <Input
                  id="item-name"
                  {...register('name')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>

              <div>
                <Label htmlFor="item-group" required>
                  Item group
                </Label>
                <Controller
                  control={control}
                  name="item_group"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="item-group" className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {ITEM_GROUPS.map((group) => (
                          <SelectItem key={group} value={group}>
                            {humanise(group)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Reporting classification only. What the item may do is set by the capabilities
                  below.
                </p>
              </div>

              <div>
                <Label htmlFor="item-category">Category</Label>
                <Controller
                  control={control}
                  name="category_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="item-category" className="mt-1.5">
                        <SelectValue placeholder="No category" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>No category</SelectItem>
                        {(categories.data ?? []).map((category) => (
                          <SelectItem key={category.id} value={category.id}>
                            {category.code} - {category.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.category_id?.message} />
              </div>

              {/* The physical attributes. Each is a value the client maintains
                  at Masters -> Attributes; before that they were free text, so
                  the same finish could be spelled three ways and no report
                  could group by it. */}
              <ReferenceSelect
                id="item-finish"
                label="Finish"
                name="finish_id"
                control={control}
                options={finishes.data}
              />

              <ReferenceSelect
                id="item-size"
                label="Size"
                name="size_id"
                control={control}
                options={sizes.data}
              />

              <ReferenceSelect
                id="item-colour"
                label="Colour"
                name="colour_id"
                control={control}
                options={colours.data}
              />

              {/* Who sells it and who makes it - classification maintained at
                  Masters -> Brands / Manufacturers. Nothing branches on either. */}
              <ReferenceSelect
                id="item-brand"
                label="Brand"
                name="brand_id"
                control={control}
                options={brands.data}
              />

              <ReferenceSelect
                id="item-manufacturer"
                label="Manufacturer"
                name="manufacturer_id"
                control={control}
                options={makers.data}
              />

              <div>
                <Label htmlFor="item-mpn">Manufacturer part number</Label>
                <Input
                  id="item-mpn"
                  {...register('manufacturer_part_number')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.manufacturer_part_number)}
                  className="mt-1.5"
                />
                <FieldError message={errors.manufacturer_part_number?.message} />
              </div>

              <div>
                <Label htmlFor="item-base-uom" required>
                  Base unit
                </Label>
                <Controller
                  control={control}
                  name="base_uom_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger
                        id="item-base-uom"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.base_uom_id)}
                      >
                        <SelectValue placeholder="Select a unit" />
                      </SelectTrigger>
                      <SelectContent>
                        {(uoms.data ?? []).map((uom) => (
                          <SelectItem key={uom.id} value={uom.id}>
                            {uom.code} - {uom.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.base_uom_id?.message} />
              </div>

              <div>
                <Label htmlFor="item-production-type">Production type</Label>
                <Controller
                  control={control}
                  name="production_type"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="item-production-type" className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {PRODUCTION_TYPES.map((type) => (
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
                <Label htmlFor="item-purchase-uom">Purchase unit</Label>
                <Controller
                  control={control}
                  name="purchase_uom_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="item-purchase-uom" className="mt-1.5">
                        <SelectValue placeholder="Same as base" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>Same as base</SelectItem>
                        {(uoms.data ?? []).map((uom) => (
                          <SelectItem key={uom.id} value={uom.id}>
                            {uom.code} - {uom.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="item-sale-uom">Sale unit</Label>
                <Controller
                  control={control}
                  name="sale_uom_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="item-sale-uom" className="mt-1.5">
                        <SelectValue placeholder="Same as base" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>Same as base</SelectItem>
                        {(uoms.data ?? []).map((uom) => (
                          <SelectItem key={uom.id} value={uom.id}>
                            {uom.code} - {uom.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="item-hsn">HSN code</Label>
                <Input
                  id="item-hsn"
                  {...register('hsn_code')}
                  autoComplete="off"
                  inputMode="numeric"
                  aria-invalid={Boolean(errors.hsn_code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.hsn_code?.message} />
              </div>

              <div>
                <Label htmlFor="item-gst">GST rate (%)</Label>
                <Input
                  id="item-gst"
                  {...register('gst_rate')}
                  inputMode="decimal"
                  aria-invalid={Boolean(errors.gst_rate)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.gst_rate?.message} />
              </div>

              <div>
                <Label htmlFor="item-reorder">Reorder level</Label>
                <Input
                  id="item-reorder"
                  {...register('reorder_level')}
                  inputMode="decimal"
                  aria-invalid={Boolean(errors.reorder_level)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.reorder_level?.message} />
              </div>

              <div>
                <Label htmlFor="item-lead-time">Lead time (days)</Label>
                <Input
                  id="item-lead-time"
                  {...register('lead_time_days')}
                  inputMode="numeric"
                  aria-invalid={Boolean(errors.lead_time_days)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.lead_time_days?.message} />
              </div>

              {showCost && (
                <div>
                  <Label htmlFor="item-standard-cost">Standard cost</Label>
                  <Input
                    id="item-standard-cost"
                    {...register('standard_cost')}
                    inputMode="decimal"
                    aria-invalid={Boolean(errors.standard_cost)}
                    className="mt-1.5 text-right tabular"
                  />
                  <FieldError message={errors.standard_cost?.message} />
                </div>
              )}
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              {can(P.WAREHOUSE_VIEW) && (
                <Controller
                  control={control}
                  name="default_warehouse_id"
                  render={({ field }) => (
                    <WarehouseSelect
                      id="item-default-warehouse"
                      label="Default warehouse"
                      value={field.value}
                      includeAll
                      allLabel="No default warehouse"
                      onChange={(value) => {
                        field.onChange(value)
                        setValue('default_location_id', '')
                      }}
                    />
                  )}
                />
              )}
              {can(P.LOCATION_VIEW) && (
                <Controller
                  control={control}
                  name="default_location_id"
                  render={({ field }) => (
                    <LocationPicker
                      id="item-default-bin"
                      label="Default bin"
                      warehouseId={watch('default_warehouse_id')}
                      value={field.value || null}
                      onChange={(value) => field.onChange(value ?? '')}
                    />
                  )}
                />
              )}
            </div>

            <fieldset className="rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">Capabilities</legend>
              <p className="mb-3 text-xs text-muted-foreground">
                Independent of each other. The same item can be purchased, manufactured and sold.
              </p>
              <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
                {ALL_CAPABILITIES.map((key) => {
                  const { label, icon: Icon } = CAPABILITY_META[key]
                  return (
                    <div key={key} className="flex items-center gap-2.5">
                      <Controller
                        control={control}
                        name={key}
                        render={({ field }) => (
                          <Checkbox
                            id={`cap-${key}`}
                            checked={field.value}
                            onCheckedChange={(checked) => field.onChange(checked === true)}
                          />
                        )}
                      />
                      <Label
                        htmlFor={`cap-${key}`}
                        className="flex items-center gap-1.5 font-normal"
                      >
                        <Icon className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                        {label}
                      </Label>
                    </div>
                  )
                })}
              </div>
            </fieldset>

            <fieldset className="rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">Quality control</legend>
              <p className="mb-3 text-xs text-muted-foreground">
                Production orders for this item hold their output for inspection, and packing lines
                for it raise a final inspection, when these are on.
              </p>
              <div className="grid gap-2.5 sm:grid-cols-2">
                <div className="flex items-center gap-2.5">
                  <Controller
                    control={control}
                    name="requires_production_qc"
                    render={({ field }) => (
                      <Checkbox
                        id="item-requires-production-qc"
                        checked={field.value}
                        onCheckedChange={(checked) => field.onChange(checked === true)}
                      />
                    )}
                  />
                  <Label htmlFor="item-requires-production-qc" className="font-normal">
                    Hold production output for QC
                  </Label>
                </div>
                <div className="flex items-center gap-2.5">
                  <Controller
                    control={control}
                    name="requires_final_qc"
                    render={({ field }) => (
                      <Checkbox
                        id="item-requires-final-qc"
                        checked={field.value}
                        onCheckedChange={(checked) => field.onChange(checked === true)}
                      />
                    )}
                  />
                  <Label htmlFor="item-requires-final-qc" className="font-normal">
                    Final inspection at packing
                  </Label>
                </div>
              </div>
            </fieldset>

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
              {isEdit ? 'Save changes' : 'Create item'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

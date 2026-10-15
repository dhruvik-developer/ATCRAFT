/**
 * Add / edit dialog for an item-vendor link.
 *
 * A link names one source for this item: the vendor's own code and name for
 * the part, whose manufacturer's part it delivers, and the commercial terms
 * (lead time, minimum order, order multiple). The last purchase price is
 * written by goods receipts, never here, and is shown read-only.
 *
 * All four writes - link, update, unlink and set-preferred - need
 * `item.update`, which is what the three endpoints enforce, so the parent
 * offers every action on exactly that right and this dialog assumes it was
 * opened by someone holding it. Setting a link preferred demotes the current
 * preferred source first; that ordering lives in the service, because the
 * partial unique index is checked per statement.
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
import { Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useManufacturerOptions } from '@/hooks/use-manufacturer-options'
import { SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, formatMoney } from '@/lib/format'
import { api } from '@/lib/api-client'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { asText } from '@/lib/utils'

import type { ItemVendorLink } from './item-model'

const DECIMAL = /^(\d{1,14}(\.\d{1,4})?)?$/
const WHOLE_DAYS = /^\d{0,4}$/

const schema = z.object({
  vendor_id: z.string().min(1, 'Choose a vendor'),
  vendor_item_code: z.string().trim().max(64, 'Vendor item code is at most 64 characters'),
  vendor_item_name: z.string().trim().max(255, 'Vendor item name is at most 255 characters'),
  manufacturer_id: z.string(),
  manufacturer_part_number: z.string().trim().max(64, 'Part number is at most 64 characters'),
  is_preferred: z.boolean(),
  lead_time_days: z
    .string()
    .trim()
    .regex(WHOLE_DAYS, 'Whole days only')
    .refine((value) => value === '' || Number(value) <= 3650, 'Lead time cannot exceed 3650 days'),
  min_order_quantity: z
    .string()
    .trim()
    .regex(DECIMAL, 'Enter a quantity with up to 4 decimal places'),
  order_multiple: z.string().trim().regex(DECIMAL, 'Enter a quantity with up to 4 decimal places'),
  is_active: z.boolean(),
})

type VendorLinkFormValues = z.infer<typeof schema>

function emptyValues(): VendorLinkFormValues {
  return {
    vendor_id: '',
    vendor_item_code: '',
    vendor_item_name: '',
    manufacturer_id: SELECT_NONE,
    manufacturer_part_number: '',
    is_preferred: false,
    lead_time_days: '0',
    min_order_quantity: '0',
    order_multiple: '0',
    is_active: true,
  }
}

function valuesFrom(link: ItemVendorLink): VendorLinkFormValues {
  return {
    vendor_id: link.vendor_id,
    vendor_item_code: link.vendor_item_code ?? '',
    vendor_item_name: link.vendor_item_name ?? '',
    manufacturer_id: link.manufacturer_id ?? SELECT_NONE,
    manufacturer_part_number: link.manufacturer_part_number ?? '',
    is_preferred: link.is_preferred,
    lead_time_days: String(link.lead_time_days),
    min_order_quantity: String(link.min_order_quantity),
    order_multiple: String(link.order_multiple),
    is_active: link.is_active,
  }
}

/** Decimals stay strings all the way to the wire - never parsed into a double. */
const asDecimal = (value: string): string => (value.trim() === '' ? '0' : value.trim())
const asIdOrNull = (value: string): string | null => (value === SELECT_NONE ? null : value)

function buildPayload(
  values: VendorLinkFormValues,
  { includeVendor }: { includeVendor: boolean },
): Record<string, unknown> {
  return {
    // The vendor a link points at is its identity, so it is changed by
    // removing and adding rather than by repointing a row other documents
    // already reference.
    ...(includeVendor ? { vendor_id: values.vendor_id } : {}),
    vendor_item_code: asText(values.vendor_item_code),
    vendor_item_name: asText(values.vendor_item_name),
    // An explicit null clears the maker and its part number rather than being
    // silently skipped: a maker recorded against the wrong source is a value a
    // person genuinely removes.
    manufacturer_id: asIdOrNull(values.manufacturer_id),
    manufacturer_part_number: asText(values.manufacturer_part_number),
    is_preferred: values.is_preferred,
    lead_time_days: Number.parseInt(values.lead_time_days || '0', 10),
    min_order_quantity: asDecimal(values.min_order_quantity),
    order_multiple: asDecimal(values.order_multiple),
    ...(!includeVendor ? { is_active: values.is_active } : {}),
  }
}

export function VendorLinkDialog({
  open,
  onOpenChange,
  itemId,
  itemCode,
  link,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  itemId: string
  itemCode: string
  /** Present for an edit, absent for an add. */
  link?: ItemVendorLink | null
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(link)

  // The vendor list is a separate right. Without it the add form cannot name a
  // vendor at all, so the parent does not offer the action - but the dialog
  // still renders the vendors the edit already carries.
  const vendors = useVendorOptions(open && can(P.VENDOR_VIEW))
  const makers = useManufacturerOptions(open && can(P.MANUFACTURER_VIEW))

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<VendorLinkFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  // Seeded once per opening, keyed on the link's id rather than its identity:
  // a background refetch of the list would otherwise resolve mid-edit and wipe
  // what the user is typing.
  const editingId = link?.id ?? null
  useEffect(() => {
    if (open) reset(link ? valuesFrom(link) : emptyValues())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: VendorLinkFormValues) =>
      link
        ? api.patch<ItemVendorLink>(
            `/products/item-vendors/${link.id}`,
            buildPayload(values, { includeVendor: false }),
          )
        : api.post<ItemVendorLink>(
            `/products/items/${itemId}/vendors`,
            buildPayload(values, { includeVendor: true }),
          ),
    onSuccess: (saved) => {
      // One prefix invalidation covers the item read and every panel on it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.detail(itemId) })
      toast.success(
        isEdit ? success.updated('Vendor link') : success.created('Vendor link'),
        saved.vendor_item_name ?? saved.vendor_item_code ?? 'Vendor linked',
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit vendor link' : `Link a vendor to ${itemCode}`}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The vendor itself cannot be changed - remove this link and add another to move a part between suppliers.'
              : 'Who supplies this item, under what code, and on what terms.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <Label htmlFor="vendor-link-vendor" required>
                  Vendor
                </Label>
                <Controller
                  control={control}
                  name="vendor_id"
                  render={({ field }) => (
                    <Select
                      value={field.value}
                      onValueChange={field.onChange}
                      disabled={isEdit || vendors.isLoading}
                    >
                      <SelectTrigger
                        id="vendor-link-vendor"
                        className="mt-1.5"
                        aria-invalid={Boolean(errors.vendor_id)}
                      >
                        <SelectValue
                          placeholder={vendors.isLoading ? 'Loading...' : 'Select a vendor'}
                        />
                      </SelectTrigger>
                      <SelectContent>
                        {(vendors.data ?? []).map((vendor) => (
                          <SelectItem key={vendor.id} value={vendor.id}>
                            {vendor.code} - {vendor.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <FieldError message={errors.vendor_id?.message} />
              </div>

              <div>
                <Label htmlFor="vendor-link-code">Vendor item code</Label>
                <Input
                  id="vendor-link-code"
                  {...register('vendor_item_code')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.vendor_item_code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.vendor_item_code?.message} />
              </div>

              <div>
                <Label htmlFor="vendor-link-name">Vendor item name</Label>
                <Input
                  id="vendor-link-name"
                  {...register('vendor_item_name')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.vendor_item_name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.vendor_item_name?.message} />
              </div>

              <div>
                <Label htmlFor="vendor-link-manufacturer">Manufacturer</Label>
                <Controller
                  control={control}
                  name="manufacturer_id"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="vendor-link-manufacturer" className="mt-1.5">
                        <SelectValue placeholder="No manufacturer" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={SELECT_NONE}>No manufacturer</SelectItem>
                        {(makers.data ?? []).map((maker) => (
                          <SelectItem key={maker.id} value={maker.id}>
                            {maker.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>

              <div>
                <Label htmlFor="vendor-link-mpn">Manufacturer part number</Label>
                <Input
                  id="vendor-link-mpn"
                  {...register('manufacturer_part_number')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.manufacturer_part_number)}
                  className="mt-1.5"
                />
                <FieldError message={errors.manufacturer_part_number?.message} />
              </div>

              <div>
                <Label htmlFor="vendor-link-lead-time">Lead time (days)</Label>
                <Input
                  id="vendor-link-lead-time"
                  {...register('lead_time_days')}
                  inputMode="numeric"
                  aria-invalid={Boolean(errors.lead_time_days)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.lead_time_days?.message} />
              </div>

              <div>
                <Label htmlFor="vendor-link-moq">Minimum order</Label>
                <Input
                  id="vendor-link-moq"
                  {...register('min_order_quantity')}
                  inputMode="decimal"
                  aria-invalid={Boolean(errors.min_order_quantity)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.min_order_quantity?.message} />
              </div>

              <div>
                <Label htmlFor="vendor-link-multiple">Order multiple</Label>
                <Input
                  id="vendor-link-multiple"
                  {...register('order_multiple')}
                  inputMode="decimal"
                  aria-invalid={Boolean(errors.order_multiple)}
                  className="mt-1.5 text-right tabular"
                />
                <FieldError message={errors.order_multiple?.message} />
              </div>

              <div className="flex items-center gap-2.5 pt-6">
                <Controller
                  control={control}
                  name="is_preferred"
                  render={({ field }) => (
                    <Checkbox
                      id="vendor-link-preferred"
                      checked={field.value}
                      onCheckedChange={(checked) => field.onChange(checked === true)}
                    />
                  )}
                />
                <Label htmlFor="vendor-link-preferred" className="font-normal">
                  Preferred source for this item
                </Label>
              </div>

              {isEdit && (
                <div className="flex items-center gap-2.5 pt-6">
                  <Controller
                    control={control}
                    name="is_active"
                    render={({ field }) => (
                      <Checkbox
                        id="vendor-link-active"
                        checked={field.value}
                        onCheckedChange={(checked) => field.onChange(checked === true)}
                      />
                    )}
                  />
                  <Label htmlFor="vendor-link-active" className="font-normal">
                    Active
                  </Label>
                </div>
              )}
            </div>

            {isEdit && link && (
              <p className="rounded-lg border bg-muted/40 p-3 text-sm text-muted-foreground">
                Last purchase price:{' '}
                {link.last_purchase_price === null ? (
                  'never purchased from this source'
                ) : (
                  <>
                    {formatMoney(link.last_purchase_price, { currency: link.currency })}
                    {link.last_purchase_date ? ` on ${formatDate(link.last_purchase_date)}` : null}
                  </>
                )}
                . Written by goods receipts, never here.
              </p>
            )}

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
              {isEdit ? 'Save changes' : 'Link vendor'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

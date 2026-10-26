import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
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
} from '@/components/ui/overlays'
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { describeError } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { useAuth } from '@/features/auth/auth-context'
import { P } from '@/lib/permissions'

/** datetime-local edits must preserve the local wall clock before converting back to UTC. */
function localDateTime(value: string): string {
  const date = new Date(value)
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 23)
}

const nullableText = (max: number) => z.string().trim().max(max)
const amount = z
  .string()
  .regex(/^\d{1,14}(\.\d{1,4})?$/, 'Enter a non-negative amount with up to 4 decimals')
const shipmentSchema = z
  .object({
    transporter_id: z.string(),
    courier_name: nullableText(120),
    tracking_reference: nullableText(120),
    lr_number: nullableText(64),
    vehicle_number: nullableText(32),
    driver_name: nullableText(120),
    driver_mobile: nullableText(32),
    shipped_at: z
      .string()
      .refine(
        (v): boolean => !Number.isNaN(new Date(v).valueOf()),
        'Choose a dispatch date and time',
      ),
    expected_delivery_date: z.string(),
    packages: z
      .string()
      .refine(
        (v): boolean =>
          v === '' || (/^\d+$/.test(v) && Number.isSafeInteger(Number(v)) && Number(v) >= 1),
        'Enter a whole number of at least 1',
      ),
    weight_kg: z
      .string()
      .refine(
        (v): boolean => v === '' || /^\d{1,14}(\.\d{1,4})?$/.test(v),
        'Up to 4 decimal places',
      ),
    freight_amount: amount,
    notes: nullableText(2000),
  })
  .superRefine((value, ctx) => {
    if (!value.transporter_id && !value.courier_name)
      ctx.addIssue({
        code: 'custom',
        path: ['courier_name'],
        message: 'Choose a transporter or enter the courier name',
      })
    if (new Date(value.shipped_at).valueOf() > Date.now())
      ctx.addIssue({
        code: 'custom',
        path: ['shipped_at'],
        message: 'Dispatch cannot be in the future',
      })
    const shippedDate = new Date(value.shipped_at)
    if (
      !Number.isNaN(shippedDate.valueOf()) &&
      value.expected_delivery_date &&
      value.expected_delivery_date < shippedDate.toISOString().slice(0, 10)
    )
      ctx.addIssue({
        code: 'custom',
        path: ['expected_delivery_date'],
        message: 'Expected delivery cannot precede dispatch',
      })
  })
type ShipmentValues = z.infer<typeof shipmentSchema>

export function InboundShipmentDialog({
  order,
  scope,
  shipment,
  onClose,
}: {
  order: { id: string; code: string }
  scope: 'staff' | 'vendor'
  shipment?: SchemaOf<'InboundShipmentRead'>
  onClose: () => void
}) {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const { can } = useAuth()
  const base = scope === 'vendor' ? '/vendor-portal' : '/procurement'
  const canReadTransporters = scope === 'vendor' || can(P.TRANSPORTER_VIEW)
  const options = useQuery({
    queryKey:
      scope === 'vendor'
        ? queryKeys.vendorPortal.transporters({ page })
        : queryKeys.procurement.inboundTransporters({ page }),
    enabled: canReadTransporters,
    queryFn: ({ signal }) =>
      api.getPage<SchemaOf<'TransporterOption'>>(
        scope === 'vendor' ? '/vendor-portal/transporters/options' : '/transporters',
        {
          params: { page, limit: PICKER_PAGE_SIZE },
          signal,
        },
      ),
  })
  const form = useForm<ShipmentValues>({
    resolver: zodResolver(shipmentSchema),
    defaultValues: {
      transporter_id: shipment?.transporter_id ?? '',
      courier_name: shipment?.courier_name ?? '',
      tracking_reference: shipment?.tracking_reference ?? '',
      lr_number: shipment?.lr_number ?? '',
      vehicle_number: shipment?.vehicle_number ?? '',
      driver_name: shipment?.driver_name ?? '',
      driver_mobile: shipment?.driver_mobile ?? '',
      shipped_at: shipment ? localDateTime(shipment.shipped_at) : '',
      expected_delivery_date: shipment?.expected_delivery_date ?? '',
      packages: shipment?.packages?.toString() ?? '',
      weight_kg: shipment?.weight_kg ?? '',
      freight_amount: shipment?.freight_amount ?? '0',
      notes: shipment?.notes ?? '',
    },
  })
  const mutation = useMutation({
    mutationFn: (values: ShipmentValues) => {
      const body: SchemaOf<'InboundShipmentCreate'> = {
        ...values,
        transporter_id: values.transporter_id || null,
        courier_name: values.courier_name || null,
        tracking_reference: values.tracking_reference || null,
        lr_number: values.lr_number || null,
        vehicle_number: values.vehicle_number || null,
        driver_name: values.driver_name || null,
        driver_mobile: values.driver_mobile || null,
        shipped_at: new Date(values.shipped_at).toISOString(),
        expected_delivery_date: values.expected_delivery_date || null,
        packages: values.packages ? Number(values.packages) : null,
        weight_kg: values.weight_kg || null,
        notes: values.notes || null,
      }
      const path = `${base}/purchase-orders/${order.id}/shipments`
      return shipment
        ? api.patch<SchemaOf<'InboundShipmentRead'>>(`${path}/${shipment.id}`, body)
        : api.post<SchemaOf<'InboundShipmentRead'>>(path, body)
    },
    onError: (error) => applyFieldErrors(error, form.setError),
    onSuccess: async (shipment) => {
      await client.invalidateQueries({
        queryKey: scope === 'vendor' ? queryKeys.vendorPortal.all : queryKeys.procurement.all,
      })
      toast.success(`Shipment ${shipment.shipment_number} saved for ${order.code}`)
      onClose()
    },
  })
  const fields = [
    ['courier_name', 'Courier name'],
    ['tracking_reference', 'Tracking reference'],
    ['lr_number', 'LR number'],
    ['vehicle_number', 'Vehicle number'],
    ['driver_name', 'Driver name'],
    ['driver_mobile', 'Driver mobile'],
    ['packages', 'Packages'],
    ['weight_kg', 'Weight (kg)'],
    ['freight_amount', 'Freight amount'],
  ] as const
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose()
      }}
    >
      <DialogContent className="max-h-[90svh] max-sm:bottom-0 max-sm:top-auto max-sm:translate-y-0 sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{shipment ? 'Edit shipment' : 'Add shipment'}</DialogTitle>
          <DialogDescription>
            {order.code}. Record one consignment, with its transport and delivery details.
          </DialogDescription>
        </DialogHeader>
        <form
          className="[&_input]:min-h-11 [&_select]:min-h-11"
          onSubmit={form.handleSubmit((values) => mutation.mutate(values))}
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label htmlFor="shipment-transporter">Transporter</Label>
              <select
                id="shipment-transporter"
                className="h-11 w-full rounded-md border bg-background px-3 text-sm"
                disabled={!canReadTransporters}
                {...form.register('transporter_id')}
              >
                <option value="">Use courier name</option>
                {shipment?.transporter_id &&
                  !options.data?.data.some((option) => option.id === shipment.transporter_id) && (
                    <option value={shipment.transporter_id}>Recorded transporter</option>
                  )}
                {options.data?.data.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.name}
                  </option>
                ))}
              </select>
              <FieldError message={form.formState.errors.transporter_id?.message} />
              {options.error && (
                <p role="alert" className="text-sm text-destructive">
                  {describeError(options.error)}
                </p>
              )}
              <div className="mt-1 flex gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={page === 1 || options.isFetching}
                  onClick={() => setPage(page - 1)}
                >
                  Previous transporters
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={!options.data?.pagination.has_next || options.isFetching}
                  onClick={() => setPage(page + 1)}
                >
                  More transporters
                </Button>
              </div>
            </div>
            {fields.map(([key, label]) => (
              <div key={key}>
                <Label htmlFor={`shipment-${key}`}>{label}</Label>
                <Input
                  id={`shipment-${key}`}
                  className="min-h-11"
                  inputMode={
                    ['weight_kg', 'freight_amount'].includes(key)
                      ? 'decimal'
                      : key === 'packages'
                        ? 'numeric'
                        : undefined
                  }
                  {...form.register(key)}
                />
                <FieldError message={form.formState.errors[key]?.message} />
              </div>
            ))}
            <div>
              <Label htmlFor="shipment-shipped">Dispatch date and time</Label>
              <Input
                id="shipment-shipped"
                type="datetime-local"
                step="0.001"
                className="min-h-11"
                {...form.register('shipped_at')}
              />
              <FieldError message={form.formState.errors.shipped_at?.message} />
            </div>
            <div>
              <Label htmlFor="shipment-expected">Expected delivery date</Label>
              <Input
                id="shipment-expected"
                type="date"
                className="min-h-11"
                {...form.register('expected_delivery_date')}
              />
              <FieldError message={form.formState.errors.expected_delivery_date?.message} />
            </div>
            <div className="sm:col-span-2">
              <Label htmlFor="shipment-notes">Notes</Label>
              <Textarea id="shipment-notes" {...form.register('notes')} />
              <FieldError message={form.formState.errors.notes?.message} />
              <FieldError message={form.formState.errors.root?.message} />
            </div>
          </DialogBody>
          <DialogFooter className="[&>button]:min-h-11">
            <Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Saving…' : shipment ? 'Save shipment' : 'Record shipment'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

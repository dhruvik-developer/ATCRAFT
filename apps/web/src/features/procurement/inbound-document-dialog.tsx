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
import { Input, Label } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { describeError } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { SchemaOf } from '@/types/api'
type VendorDocument = SchemaOf<'InboundDocumentRead'>
type VendorShipment = SchemaOf<'InboundShipmentRead'>

export function InboundDocumentDialog({
  order,
  scope,
  canShip,
  canInvoice,
  onClose,
}: {
  order: {
    id: string
    code: string
    shipments: VendorShipment[]
    shipments_pagination: { limit: number; has_next: boolean; has_previous: boolean }
  }
  scope: 'staff' | 'vendor'
  canShip: boolean
  canInvoice: boolean
  onClose: () => void
}) {
  const client = useQueryClient()
  const [shipmentPage, setShipmentPage] = useState(1)
  const base = scope === 'vendor' ? '/vendor-portal' : '/procurement'
  const shipments = useQuery({
    queryKey:
      scope === 'vendor'
        ? [...queryKeys.vendorPortal.order(order.id), 'upload-shipments', shipmentPage]
        : queryKeys.procurement.shipments(order.id, {
            page: shipmentPage,
            limit: order.shipments_pagination.limit,
          }),
    queryFn: ({ signal }) =>
      api.getPage<VendorShipment>(`${base}/purchase-orders/${order.id}/shipments`, {
        params: { page: shipmentPage, limit: order.shipments_pagination.limit },
        signal,
      }),
    enabled: scope === 'staff' || shipmentPage !== 1,
  })
  const shipmentRows =
    scope === 'vendor' && shipmentPage === 1 ? order.shipments : (shipments.data?.data ?? [])
  const shipmentPagination =
    scope === 'vendor' && shipmentPage === 1
      ? order.shipments_pagination
      : shipments.data?.pagination
  const kinds = [
    ...(canInvoice ? (['INVOICE'] as const) : []),
    ...(canShip ? (['DISPATCH_PHOTO', 'LR_COPY', 'DELIVERY_CHALLAN', 'OTHER'] as const) : []),
  ]
  const schema = z.object({
    kind: z
      .string()
      .refine((v): boolean => kinds.some((kind) => kind === v), 'Choose an allowed document kind'),
    shipment_id: z.string(),
    file: z.custom<File>(
      (v): boolean => v instanceof File && v.size > 0,
      'Choose a non-empty image or PDF',
    ),
  })
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    defaultValues: { kind: kinds[0], shipment_id: '' },
  })
  const mutation = useMutation({
    mutationFn: (values: z.infer<typeof schema>) => {
      const data = new FormData()
      data.append('kind', values.kind)
      data.append('file', values.file)
      if (values.shipment_id) data.append('shipment_id', values.shipment_id)
      return api.post<VendorDocument>(`${base}/purchase-orders/${order.id}/documents`, data)
    },
    onError: (error) => applyFieldErrors(error, form.setError),
    onSuccess: async (file) => {
      await client.invalidateQueries({
        queryKey: scope === 'vendor' ? queryKeys.vendorPortal.all : queryKeys.procurement.all,
      })
      toast.success(`${file.file_name} uploaded`)
      onClose()
    },
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose()
      }}
    >
      <DialogContent className="max-h-[90svh] max-sm:bottom-0 max-sm:top-auto max-sm:translate-y-0 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Upload document</DialogTitle>
          <DialogDescription>
            {order.code}. Files are shared only with this order’s buyer and your vendor account.
          </DialogDescription>
        </DialogHeader>
        <form
          className="[&_input]:min-h-11 [&_select]:min-h-11"
          onSubmit={form.handleSubmit((values) => mutation.mutate(values))}
        >
          <DialogBody className="space-y-4">
            <div>
              <Label htmlFor="upload-kind">Document kind</Label>
              <select
                id="upload-kind"
                className="h-11 w-full rounded-md border bg-background px-3 text-sm"
                {...form.register('kind')}
              >
                {kinds.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind.replaceAll('_', ' ')}
                  </option>
                ))}
              </select>
              <FieldError message={form.formState.errors.kind?.message} />
            </div>
            <div>
              <Label htmlFor="upload-shipment">Shipment (optional)</Label>
              <select
                id="upload-shipment"
                className="h-11 w-full rounded-md border bg-background px-3 text-sm"
                {...form.register('shipment_id')}
              >
                <option value="">Whole order</option>
                {shipmentRows.map((shipment) => (
                  <option key={shipment.id} value={shipment.id}>
                    Shipment {shipment.shipment_number}
                  </option>
                ))}
              </select>
              <FieldError message={form.formState.errors.shipment_id?.message} />
              {shipments.error && (
                <p role="alert" className="text-sm text-destructive">
                  {describeError(shipments.error)}
                </p>
              )}
              {(shipmentPagination?.has_next || shipmentPagination?.has_previous) && (
                <div className="mt-1 flex flex-wrap gap-2">
                  <Button
                    className="min-h-11"
                    type="button"
                    variant="ghost"
                    onClick={() => {
                      setShipmentPage(shipmentPage - 1)
                      form.setValue('shipment_id', '')
                    }}
                    disabled={!shipmentPagination.has_previous || shipments.isFetching}
                  >
                    Previous shipments
                  </Button>
                  <Button
                    className="min-h-11"
                    type="button"
                    variant="ghost"
                    onClick={() => {
                      setShipmentPage(shipmentPage + 1)
                      form.setValue('shipment_id', '')
                    }}
                    disabled={!shipmentPagination.has_next || shipments.isFetching}
                  >
                    More shipments
                  </Button>
                </div>
              )}
            </div>
            <div>
              <Label htmlFor="upload-file">Image or PDF</Label>
              <Input
                id="upload-file"
                type="file"
                accept="image/*,application/pdf"
                capture="environment"
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  if (file) form.setValue('file', file, { shouldValidate: true })
                }}
              />
              <FieldError message={form.formState.errors.file?.message} />
            </div>
            <FieldError message={form.formState.errors.root?.message} />
          </DialogBody>
          <DialogFooter className="[&>button]:min-h-11">
            <Button type="button" variant="outline" disabled={mutation.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Uploading…' : 'Upload document'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

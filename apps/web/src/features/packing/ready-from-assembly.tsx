/**
 * Ready from assembly: completed assemblies waiting to be packed.
 *
 * `GET /assembly/ready-for-packing` lists assemblies whose output is packed
 * nowhere yet. Packing one raises a draft packing order for its output - the
 * assembled item, from the warehouse it was received into - and marks the
 * assembly packed against it in the SAME transaction, so the row leaves this
 * list the moment its order exists and two packers cannot plan around the
 * same output.
 *
 * The customer and sales order come from the assembly's source when it was
 * raised for a sales order; otherwise the customer must be named here, and
 * naming a different customer than the source order's is refused.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { PackagePlus } from 'lucide-react'
import { useEffect, useState } from 'react'
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
import { Alert, AlertDescription, Input, Label, Textarea } from '@/components/ui/primitives'
import { assemblyKeys, type ReadyForPackingRow } from '@/features/assembly/assembly-model'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, formatQuantity, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { packingKeys, type PackingOrderDetail } from '@/features/packing/packing-types'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

/** `Quantity` is NUMERIC(18,4): at most 14 integer digits and 4 decimals. */
const OPTIONAL_QUANTITY = /^(\d{1,14}(\.\d{1,4})?)?$/

const schema = z.object({
  customer_id: z.string(),
  quantity: z
    .string()
    .trim()
    .regex(OPTIONAL_QUANTITY, 'Enter a quantity with up to 4 decimal places'),
  planned_date: z.string(),
  notes: z.string().trim().max(2000, 'At most 2000 characters'),
})

type FormValues = z.infer<typeof schema>

function PackFromAssemblyDialog({
  open,
  onOpenChange,
  row,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  row: ReadyForPackingRow
}) {
  const queryClient = useQueryClient()
  const fromSalesOrder = row.source_type === 'SALES_ORDER' && row.source_id !== null

  const {
    register,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      customer_id: '',
      quantity: String(row.assembled_quantity),
      planned_date: '',
      notes: '',
    },
  })

  useEffect(() => {
    if (open) {
      reset({
        customer_id: '',
        quantity: String(row.assembled_quantity),
        planned_date: '',
        notes: '',
      })
    }
    // Re-read on every open so the quantity is never a snapshot from an
    // earlier visit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reset])

  const pack = useMutation({
    mutationFn: (values: FormValues) =>
      api.post<PackingOrderDetail>('/packing/from-assembly', {
        assembly_order_id: row.assembly_order_id,
        ...(values.customer_id ? { customer_id: values.customer_id } : {}),
        ...(values.planned_date ? { planned_date: values.planned_date } : {}),
        ...(values.quantity.trim() ? { quantity: values.quantity.trim() } : {}),
        ...(values.notes.trim() ? { notes: values.notes.trim() } : {}),
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: packingKeys.all })
      // The assembly is marked packed in the same transaction, so it leaves
      // the ready list with its order.
      void queryClient.invalidateQueries({ queryKey: assemblyKeys.all })
      toast.success(
        success.created('Packing order'),
        `${created.code} raised for ${row.code}; the assembly is marked packed against it.`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Pack {row.code}</DialogTitle>
          <DialogDescription>
            Raises a draft packing order for {formatQuantity(row.assembled_quantity)} from the
            warehouse the output was received into, and marks the assembly packed against it in one
            transaction.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => pack.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div>
              <CustomerPicker
                id="pack-from-assembly-customer"
                label="Customer"
                value={watch('customer_id') || null}
                onChange={(value) => setValue('customer_id', value ?? '')}
                placeholder={fromSalesOrder ? 'From the sales order' : 'Choose the customer'}
              />
              <FieldError message={errors.customer_id?.message} />
              <p className="mt-1 text-xs text-muted-foreground">
                {fromSalesOrder
                  ? 'This assembly was raised for a sales order, so its customer is used unless another is named here.'
                  : 'This assembly was not raised for a sales order; name the customer the packing order is for.'}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label htmlFor="pack-from-assembly-quantity">Quantity</Label>
                <Input
                  id="pack-from-assembly-quantity"
                  {...register('quantity')}
                  inputMode="decimal"
                  autoComplete="off"
                  className="mt-1.5 text-right tabular"
                  aria-invalid={Boolean(errors.quantity)}
                />
                <FieldError message={errors.quantity?.message} />
                <p className="mt-1 text-xs text-muted-foreground">
                  Up to {formatQuantity(row.assembled_quantity)} produced.
                </p>
              </div>
              <div>
                <Label htmlFor="pack-from-assembly-date">Planned date</Label>
                <Input
                  id="pack-from-assembly-date"
                  type="date"
                  {...register('planned_date')}
                  className="mt-1.5"
                />
                <p className="mt-1 text-xs text-muted-foreground">Today when empty.</p>
              </div>
            </div>

            <div>
              <Label htmlFor="pack-from-assembly-notes">Notes</Label>
              <Textarea
                id="pack-from-assembly-notes"
                {...register('notes')}
                rows={2}
                maxLength={2000}
                className="mt-1.5"
                aria-invalid={Boolean(errors.notes)}
              />
              <FieldError message={errors.notes?.message} />
            </div>

            {pack.error && (
              <Alert variant="destructive">
                <AlertDescription>{pack.error.message}</AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={pack.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={pack.isPending}>
              Raise packing order
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function ReadyFromAssemblyPanel() {
  const { can } = useAuth()
  const [packing, setPacking] = useState<ReadyForPackingRow | null>(null)
  const canPack = can(P.PACKING_MANAGE)

  const ready = useListQuery<ReadyForPackingRow>({
    path: '/assembly/ready-for-packing',
    queryKey: assemblyKeys.readyForPacking,
    staleTime: staleTimes.transactional,
  })

  if (!ready.isLoading && !ready.error && ready.data.length === 0) {
    return null
  }

  return (
    <section aria-label="Ready from assembly" className="mb-5 rounded-lg border p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="font-semibold">Ready from assembly</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Completed assemblies whose output is packed nowhere yet. Packing one raises its packing
            order and claims it in one transaction.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void ready.refetch()}>
          Refresh
        </Button>
      </div>

      {ready.isLoading && (
        <p className="mt-3 text-sm text-muted-foreground">Reading completed assemblies...</p>
      )}
      {ready.error && <p className="mt-3 text-sm text-destructive">{ready.error.message}</p>}

      {!ready.isLoading && !ready.error && (
        <ul className="mt-3 divide-y rounded-md border">
          {ready.data.map((row) => (
            <li
              key={row.assembly_order_id}
              className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2.5 text-sm"
            >
              <span className="font-medium">{row.code}</span>
              <span className="text-muted-foreground">
                {formatQuantity(row.assembled_quantity)} produced
                {toDecimal(row.available_quantity).lessThan(toDecimal(row.assembled_quantity)) &&
                  ` · ${formatQuantity(row.available_quantity)} on hand`}
              </span>
              <span className="text-xs text-muted-foreground">
                Completed {formatDate(row.completed_at)}
                {row.source_type === 'SALES_ORDER' ? ' · for a sales order' : null}
              </span>
              {canPack && (
                <Button
                  variant="outline"
                  size="sm"
                  className="ml-auto"
                  onClick={() => setPacking(row)}
                >
                  <PackagePlus />
                  Pack
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}

      {packing && (
        <PackFromAssemblyDialog
          open
          onOpenChange={(open) => !open && setPacking(null)}
          row={packing}
        />
      )}
    </section>
  )
}

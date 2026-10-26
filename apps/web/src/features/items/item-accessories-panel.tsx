import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
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
import { Badge, Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker } from '@/features/inventory/pickers'
import { QUANTITY_PATTERN } from '@/features/masters/masters-model'
import { RetireWithReasonDialog } from '@/features/masters/masters-shared'
import { invalidateProductDescriptions } from '@/features/product-descriptions/specification-model'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { PRODUCT_DESCRIPTION_POSITION_MAX, PRODUCT_DESCRIPTION_TEXT_MAX } from '@/lib/constants'
import { formatQuantity, toDecimal } from '@/lib/format'
import { applyFieldErrors, fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success, validation } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

type Accessory = SchemaOf<'ItemAccessoryRead'>
const schema = z.object({
  accessory_item_id: z.string().min(1, validation.required('Accessory item')),
  quantity: z
    .string()
    .trim()
    .refine(
      (value) => QUANTITY_PATTERN.test(value) && toDecimal(value).greaterThan(0),
      'Enter a positive quantity with up to 4 decimal places.',
    ),
  relation: z.enum(['OPTIONAL', 'INCLUDED']),
  sort_order: z
    .string()
    .trim()
    .regex(/^\d{1,4}$/, `Enter a whole number from 0 to ${PRODUCT_DESCRIPTION_POSITION_MAX}.`),
  notes: z.string().max(PRODUCT_DESCRIPTION_TEXT_MAX),
})
type FormValues = z.infer<typeof schema>

export function AccessoryFormDialog({
  itemId,
  accessory,
  open,
  onOpenChange,
}: {
  itemId: string
  accessory?: Accessory | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const client = useQueryClient()
  const { can } = useAuth()
  const canEdit = can(P.ITEM_UPDATE)
  const {
    register,
    control,
    handleSubmit,
    reset,
    setValue,
    setError,
    watch,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      accessory_item_id: '',
      quantity: '1',
      relation: 'OPTIONAL',
      sort_order: '0',
      notes: '',
    },
  })
  const editingId = accessory?.id
  useEffect(() => {
    if (open)
      reset({
        accessory_item_id: accessory?.accessory_item_id ?? '',
        quantity: String(accessory?.quantity ?? '1'),
        relation: accessory?.relation ?? 'OPTIONAL',
        sort_order: String(accessory?.sort_order ?? 0),
        notes: accessory?.notes ?? '',
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])
  const relation = watch('relation')
  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = {
        quantity: values.quantity,
        relation: values.relation,
        sort_order: Number(values.sort_order),
        notes: values.notes.trim() || null,
      } satisfies SchemaOf<'ItemAccessoryUpdate'>
      return accessory
        ? api.patch<Accessory>(`/products/item-accessories/${accessory.id}`, body)
        : api.post<Accessory>(`/products/items/${itemId}/accessories`, {
            ...body,
            accessory_item_id: values.accessory_item_id,
          } satisfies SchemaOf<'ItemAccessoryCreate'>)
    },
    onSuccess: () => {
      invalidateProductDescriptions(client)
      toast.success(accessory ? success.updated('Accessory') : success.added('Accessory'))
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!mutation.isPending) onOpenChange(next)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{accessory ? 'Edit accessory' : 'Add accessory'}</DialogTitle>
          <DialogDescription>
            Optional accessories can be ordered separately. Included items describe what is in the
            box. These links never move stock or change a BOM.
          </DialogDescription>
        </DialogHeader>
        <form
          noValidate
          onSubmit={handleSubmit((values) => {
            if (canEdit && !mutation.isPending) mutation.mutate(values)
          })}
        >
          <DialogBody className="space-y-4">
            <fieldset disabled={mutation.isPending || !canEdit} className="space-y-4">
              <div>
                <Label htmlFor="accessory-relation">Relation</Label>
                <select
                  id="accessory-relation"
                  aria-invalid={Boolean(errors.relation)}
                  {...register('relation')}
                  onChange={(event) => {
                    setValue('relation', event.target.value as FormValues['relation'])
                    if (!accessory) setValue('accessory_item_id', '')
                  }}
                  className="h-11 w-full rounded-md border border-input bg-background px-3 text-sm"
                >
                  <option value="OPTIONAL">Optional add-on</option>
                  <option value="INCLUDED">In the box</option>
                </select>
                <FieldError message={errors.relation?.message} />
              </div>
              {accessory ? (
                <div>
                  <p className="text-sm font-medium">Accessory item</p>
                  <p>
                    {accessory.accessory_code} - {accessory.accessory_name}
                  </p>
                </div>
              ) : (
                <Controller
                  name="accessory_item_id"
                  control={control}
                  render={({ field }) => (
                    <ItemPicker
                      id="accessory-item"
                      label="Accessory item"
                      value={field.value || null}
                      onChange={(id) => field.onChange(id || '')}
                      filters={relation === 'OPTIONAL' ? { can_sell: true } : {}}
                      invalid={Boolean(errors.accessory_item_id)}
                      disabled={mutation.isPending || !canEdit}
                    />
                  )}
                />
              )}
              <FieldError message={errors.accessory_item_id?.message} />
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="accessory-quantity" required>
                    Quantity
                  </Label>
                  <Input
                    id="accessory-quantity"
                    inputMode="decimal"
                    {...register('quantity')}
                    aria-invalid={Boolean(errors.quantity)}
                  />
                  <FieldError message={errors.quantity?.message} />
                </div>
                <div>
                  <Label htmlFor="accessory-position">Position in the list</Label>
                  <Input
                    id="accessory-position"
                    inputMode="numeric"
                    {...register('sort_order')}
                    aria-invalid={Boolean(errors.sort_order)}
                  />
                  <FieldError message={errors.sort_order?.message} />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Lower positions appear first. Edit the position to reorder accessories.
              </p>
              <div>
                <Label htmlFor="accessory-notes">Notes</Label>
                <Textarea id="accessory-notes" {...register('notes')} />
                <FieldError message={errors.notes?.message} />
              </div>
            </fieldset>
            <FieldError message={errors.root?.message} />
            {mutation.error && (
              <FieldError
                message={fieldErrorSummary(mutation.error) || describeError(mutation.error)}
              />
            )}
          </DialogBody>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={mutation.isPending}
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            {canEdit && (
              <Button type="submit" loading={mutation.isPending}>
                {accessory ? 'Save accessory' : 'Add accessory'}
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function ItemAccessoriesPanel({ itemId }: { itemId: string }) {
  const { can } = useAuth()
  const canEdit = can(P.ITEM_UPDATE)
  const client = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Accessory | null>(null)
  const [removing, setRemoving] = useState<Accessory | null>(null)
  const list = useListQuery<Accessory>({
    path: `/products/items/${itemId}/accessories`,
    queryKey: (params) => queryKeys.items.accessories(itemId, params),
    namespace: 'accessories',
  })
  const remove = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      api.delete(`/products/item-accessories/${id}`, { params: { reason } }),
    onSuccess: () => {
      setRemoving(null)
      invalidateProductDescriptions(client)
      toast.success(success.removed('Accessory'))
    },
  })
  const columns = useMemo<ColumnDef<Accessory, unknown>[]>(
    () => [
      { accessorKey: 'accessory_code', header: 'Code', enableSorting: false },
      { accessorKey: 'accessory_name', header: 'Accessory', enableSorting: false },
      {
        accessorKey: 'relation',
        header: 'Relation',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant="secondary">
            {row.original.relation === 'INCLUDED' ? 'In the box' : 'Optional add-on'}
          </Badge>
        ),
      },
      {
        accessorKey: 'quantity',
        header: 'Quantity',
        enableSorting: false,
        cell: ({ row }) => formatQuantity(row.original.quantity),
      },
      { accessorKey: 'sort_order', header: 'Position', enableSorting: false },
      {
        accessorKey: 'notes',
        header: 'Notes',
        enableSorting: false,
        cell: ({ row }) => row.original.notes || '-',
      },
      ...(canEdit
        ? [
            {
              id: 'actions',
              header: '',
              enableSorting: false,
              cell: ({ row }) => (
                <div className="flex flex-wrap justify-end gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setEditing(row.original)
                      setFormOpen(true)
                    }}
                    aria-label={`Edit ${row.original.accessory_name}`}
                  >
                    Edit
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      remove.reset()
                      setRemoving(row.original)
                    }}
                    aria-label={`Remove ${row.original.accessory_name}`}
                  >
                    Remove
                  </Button>
                </div>
              ),
            } satisfies ColumnDef<Accessory, unknown>,
          ]
        : []),
    ],
    [canEdit, remove],
  )
  const add = canEdit ? (
    <Button
      onClick={() => {
        setEditing(null)
        setFormOpen(true)
      }}
    >
      Add accessory
    </Button>
  ) : undefined
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-2xl text-sm text-muted-foreground">
          Included items describe what is in the box; optional add-ons must be sellable. Accessories
          are informational links, separate from stock consumption, BOMs and MRP. A reciprocal
          accessory link is allowed.
        </p>
        {add}
      </div>
      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onRefresh={() => void list.refetch()}
        emptyTitle="No accessories"
        emptyDescription="Add included items or optional add-ons for this product."
        emptyAction={add}
      />
      {canEdit && (
        <>
          <AccessoryFormDialog
            itemId={itemId}
            open={formOpen}
            accessory={editing}
            onOpenChange={setFormOpen}
          />
          <RetireWithReasonDialog
            open={Boolean(removing)}
            onOpenChange={(open) => {
              if (!remove.isPending && !open) setRemoving(null)
            }}
            title={`Remove ${removing?.accessory_name ?? 'accessory'}?`}
            description="Remove this link from the product. The accessory item and its stock are kept."
            confirmLabel="Remove accessory"
            inputId="accessory-remove-reason"
            pending={remove.isPending}
            error={remove.error}
            errorHeadline="The accessory was not removed"
            onConfirm={(reason) => {
              if (removing && !remove.isPending) remove.mutate({ id: removing.id, reason })
            }}
          />
        </>
      )}
    </section>
  )
}

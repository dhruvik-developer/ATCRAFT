import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError } from '@/components/shared'
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
import { ItemPicker } from '@/features/inventory/pickers'
import { useAuth } from '@/features/auth/auth-context'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { formatQuantity } from '@/lib/format'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { api } from '@/lib/api-client'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

type PackingSpec = SchemaOf<'ItemPackingSpecRead'>
type PackingSpecWrite = SchemaOf<'ItemPackingSpecWrite'>
type RatioMode = 'quantity_per_unit' | 'units_per_carton'

function PackingSpecDialog({
  itemId,
  spec,
  open,
  onOpenChange,
  onSaved,
}: {
  itemId: string
  spec: PackingSpec | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void
}) {
  const [materialId, setMaterialId] = useState<string | null>(null)
  const [ratioMode, setRatioMode] = useState<RatioMode>('quantity_per_unit')
  const [ratio, setRatio] = useState('1')
  const [isDefault, setIsDefault] = useState(true)
  // A sentence, not an Error: describeError rightly says only "An unexpected
  // error occurred." for anything the API did not write, so the dialog's own
  // check used to reach the user as exactly that.
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setMaterialId(spec?.packing_material_item_id ?? null)
      setRatioMode(spec?.units_per_carton ? 'units_per_carton' : 'quantity_per_unit')
      setRatio(String(spec?.units_per_carton ?? spec?.quantity_per_unit ?? '1'))
      setIsDefault(spec?.is_default ?? true)
      setError(null)
    }
  }, [open, spec])

  const save = useMutation({
    // The material arrives checked: the submit handler refuses a spec without one.
    mutationFn: async (material: string) => {
      const body = {
        packing_material_item_id: material,
        quantity_per_unit: ratioMode === 'quantity_per_unit' ? ratio : null,
        units_per_carton: ratioMode === 'units_per_carton' ? Number(ratio) : null,
        is_default: isDefault,
      } satisfies PackingSpecWrite
      return spec
        ? api.put<PackingSpec>(`/packing/packing-specs/${spec.id}`, body)
        : api.post<PackingSpec>(`/packing/items/${itemId}/packing-specs`, body)
    },
    onSuccess: () => {
      onSaved()
      toast.success(
        spec ? success.updated('Packing specification') : success.added('Packing specification'),
      )
      onOpenChange(false)
    },
    onError: (reason) => setError(fieldErrorSummary(reason) || describeError(reason)),
    // Said at the top of the dialog; a toast would repeat it.
    meta: { suppressErrorToast: true },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!save.isPending) onOpenChange(next)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {spec ? 'Edit packing specification' : 'Add packing specification'}
          </DialogTitle>
          <DialogDescription>
            Default recipes generate packing-material demand in MRP. Carton history is not used as a
            recipe.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <FieldError className="mt-0 text-sm" message={error ?? undefined} />
          <ItemPicker
            id="packing-spec-material"
            label="Packing material"
            value={materialId}
            onChange={(value) => setMaterialId(value)}
            filters={{ can_purchase: true }}
            placeholder="Choose a purchasable item"
          />
          <div className="space-y-1.5">
            <Label htmlFor="packing-spec-ratio-type">Quantity rule</Label>
            <select
              id="packing-spec-ratio-type"
              className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={ratioMode}
              onChange={(event) => {
                const next = event.target.value as RatioMode
                setRatioMode(next)
                setRatio('1')
              }}
            >
              <option value="quantity_per_unit">Material quantity per finished unit</option>
              <option value="units_per_carton">Finished units per carton</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="packing-spec-ratio">
              {ratioMode === 'quantity_per_unit'
                ? 'Material quantity per unit'
                : 'Units per carton'}
            </Label>
            <Input
              id="packing-spec-ratio"
              type="number"
              min="0.0001"
              step={ratioMode === 'quantity_per_unit' ? '0.0001' : '1'}
              value={ratio}
              onChange={(event) => setRatio(event.target.value)}
              required
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isDefault}
              onChange={(event) => setIsDefault(event.target.checked)}
            />
            Use as the default MRP recipe for this material
          </label>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              const parsed = Number(ratio)
              if (
                !materialId ||
                !Number.isFinite(parsed) ||
                parsed <= 0 ||
                (ratioMode === 'units_per_carton' && !Number.isInteger(parsed))
              ) {
                setError(
                  'Choose a material and enter a positive value for the selected quantity rule.',
                )
                return
              }
              setError(null)
              save.mutate(materialId)
            }}
            disabled={save.isPending}
          >
            {save.isPending ? 'Saving…' : 'Save specification'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ItemPackingSpecsPanel({ itemId }: { itemId: string }) {
  const client = useQueryClient()
  const { can } = useAuth()
  const canEdit = can(P.PACKING_MANAGE)
  const [page, setPage] = useState(1)
  const [editor, setEditor] = useState<PackingSpec | null | undefined>(undefined)
  const [removing, setRemoving] = useState<PackingSpec | null>(null)
  const [deleteError, setDeleteError] = useState<Error | null>(null)

  const query = useQuery({
    queryKey: [...queryKeys.items.detail(itemId), 'packing-specs', page],
    queryFn: () =>
      api.getPage<PackingSpec>(`/packing/items/${itemId}/packing-specs`, {
        params: { page, limit: LOOKUP_LIST_LIMIT },
      }),
  })
  const invalidate = () =>
    void client.invalidateQueries({
      queryKey: [...queryKeys.items.detail(itemId), 'packing-specs'],
    })
  const remove = useMutation({
    mutationFn: ({ spec, reason }: { spec: PackingSpec; reason: string }) =>
      api.delete(`/packing/packing-specs/${spec.id}`, { params: { reason } }),
    onSuccess: () => {
      invalidate()
      toast.success(success.deleted('Packing specification'))
      setRemoving(null)
    },
    onError: (reason) => setDeleteError(reason as Error),
  })

  const columns = useMemo<ColumnDef<PackingSpec, unknown>[]>(() => {
    const definitions: ColumnDef<PackingSpec, unknown>[] = [
      {
        id: 'material',
        header: 'Packing material',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.packing_material_item_code && row.original.packing_material_item_name
            ? `${row.original.packing_material_item_code} - ${row.original.packing_material_item_name}`
            : row.original.packing_material_item_id,
      },
      {
        id: 'quantity_rule',
        header: 'Quantity rule',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.quantity_per_unit !== null
            ? `${formatQuantity(row.original.quantity_per_unit)} per finished unit`
            : `${row.original.units_per_carton} finished units per carton`,
      },
      {
        id: 'default',
        header: 'MRP recipe',
        enableSorting: false,
        cell: ({ row }) => (row.original.is_default ? 'Default' : 'Alternative'),
      },
    ]
    if (canEdit) {
      definitions.push({
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex gap-1">
            <Button size="sm" variant="ghost" onClick={() => setEditor(row.original)}>
              <Pencil aria-hidden />
              <span className="sr-only">Edit specification</span>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDeleteError(null)
                setRemoving(row.original)
              }}
            >
              <Trash2 aria-hidden />
              <span className="sr-only">Remove specification</span>
            </Button>
          </span>
        ),
      })
    }
    return definitions
  }, [canEdit])

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Packing specifications</h2>
          <p className="text-sm text-muted-foreground">
            Default recipes drive MRP demand for boxes and other purchasable packing materials.
          </p>
        </div>
        {canEdit && (
          <Button onClick={() => setEditor(null)}>
            <Plus aria-hidden />
            Add specification
          </Button>
        )}
      </div>
      <DataTable
        columns={columns}
        data={query.data?.data ?? []}
        pagination={query.data?.pagination}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error as Error | null}
        onPageChange={setPage}
        onRefresh={() => void query.refetch()}
        getRowId={(row) => row.id}
        emptyTitle="No packing specifications"
        emptyDescription="Add a default recipe to include packing materials in MRP."
      />
      {editor !== undefined && (
        <PackingSpecDialog
          itemId={itemId}
          spec={editor}
          open
          onOpenChange={(open) => {
            if (!open) setEditor(undefined)
          }}
          onSaved={invalidate}
        />
      )}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open && !remove.isPending) {
            setRemoving(null)
            setDeleteError(null)
          }
        }}
        title="Remove packing specification?"
        description={
          deleteError
            ? describeError(deleteError)
            : 'This recipe will no longer generate packing-material demand.'
        }
        confirmLabel="Remove specification"
        destructive
        loading={remove.isPending}
        onConfirm={() => {
          if (removing) remove.mutate({ spec: removing, reason: 'Item packing recipe removed' })
        }}
      />
    </section>
  )
}

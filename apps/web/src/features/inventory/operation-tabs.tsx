import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, StatusBadge } from '@/components/shared'
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
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { CostCentreName, CostCentreSelect } from '@/features/admin/cost-centre-select'
import { useAuth } from '@/features/auth/auth-context'
import BatchSelect from './movement-batch-picker'
import { useDepartmentOptions } from '@/hooks/use-organisation-options'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { REASON_MIN_LENGTH, SELECT_NONE } from '@/lib/constants'
import { formatDate, formatMoney, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { LocationPicker } from './location-picker'
import { ItemPicker, WarehouseSelect } from './pickers'

type Kind = 'bin-moves' | 'scrap-disposals' | 'internal-issues'
type Operation = (
  SchemaOf<'BinMoveRead'> | SchemaOf<'ScrapDisposalRead'> | SchemaOf<'InternalIssueRead'>
) &
  Partial<Pick<SchemaOf<'InternalIssueRead'>, 'cost_centre_id' | 'requires_approval'>> & {
    total_value?: string
  }
const DATE_FIELDS: Record<Kind, string> = {
  'bin-moves': 'move_date',
  'scrap-disposals': 'disposal_date',
  'internal-issues': 'issue_date',
}
const documentDate = (row: Operation) =>
  'move_date' in row ? row.move_date : 'disposal_date' in row ? row.disposal_date : row.issue_date

function OperationLines({ document }: { document: Operation }) {
  const { can } = useAuth()
  const canSeeCosts = can(P.ITEM_COST_VIEW) || can(P.INVENTORY_VALUATION_VIEW)
  return (
    <div className="space-y-3">
      <p className="text-sm">
        {document.warehouse_code ?? 'Warehouse'} · {formatDate(documentDate(document))}
      </p>
      <p className="text-sm">{document.reason}</p>
      {document.cost_centre_id && can(P.COST_CENTRE_VIEW) && (
        <CostCentreName id={document.cost_centre_id} />
      )}
      <div className="overflow-x-auto rounded border">
        <table className="w-full text-sm">
          <thead className="bg-muted text-left">
            <tr>
              <th className="p-2">Item</th>
              <th className="p-2">Batch</th>
              <th className="p-2">Bin</th>
              <th className="p-2 text-right">Quantity</th>
            </tr>
          </thead>
          <tbody>
            {(document.lines ?? []).map((line) => (
              <tr key={line.id} className="border-t">
                <td className="p-2">
                  {line.item_code ?? 'Retired item'}
                  <span className="block text-xs text-muted-foreground">{line.item_name}</span>
                </td>
                <td className="p-2">
                  {line.batch_code ?? (line.batch_id ? 'Batch unavailable' : '—')}
                </td>
                <td className="p-2">
                  {'from_location_id' in line
                    ? `${line.from_location_code ?? (line.from_location_id ? 'Retired bin' : 'Unbinned')} → ${line.to_location_code ?? (line.to_location_id ? 'Retired bin' : 'Unbinned')}`
                    : (line.location_code ?? (line.location_id ? 'Retired bin' : 'Unbinned'))}
                </td>
                <td className="p-2 text-right tabular">{formatQuantity(line.quantity)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {canSeeCosts && <p className="text-sm">FIFO value: {formatMoney(document.total_value)}</p>}
      {'disposal_kind' in document && (
        <p className="text-sm">
          {humanise(document.disposal_kind)} · Realised {formatMoney(document.realised_amount)}
          {document.buyer_reference ? ` · ${document.buyer_reference}` : ''}
        </p>
      )}
      {document.notes && (
        <p className="whitespace-pre-wrap text-sm text-muted-foreground">{document.notes}</p>
      )}
    </div>
  )
}
interface Line {
  item_id: string
  quantity: string
  batch_id: string
  location_id: string
  to_location_id: string
}
const blankLine = (): Line => ({
  item_id: '',
  quantity: '',
  batch_id: '',
  location_id: '',
  to_location_id: '',
})
const ITEM_FILTERS = { is_serial_tracked: false }
const TITLES: Record<Kind, string> = {
  'bin-moves': 'Bin move',
  'scrap-disposals': 'Scrap disposal',
  'internal-issues': 'Internal issue',
}

function OperationForm({ kind, onClose }: { kind: Kind; onClose: () => void }) {
  const { can } = useAuth()
  const client = useQueryClient()
  const [warehouse, setWarehouse] = useState('')
  const [reason, setReason] = useState('')
  const [lines, setLines] = useState<Line[]>([blankLine()])
  const [department, setDepartment] = useState('')
  const [centre, setCentre] = useState<string | null>(null)
  const [disposalKind, setDisposalKind] = useState('WRITE_OFF')
  const [buyer, setBuyer] = useState('')
  const [realised, setRealised] = useState('0')
  const [error, setError] = useState('')
  const departments = useDepartmentOptions(kind === 'internal-issues' && can(P.DEPARTMENT_VIEW))
  const patchLine = (index: number, patch: Partial<Line>) =>
    setLines((current) => current.map((line, at) => (at === index ? { ...line, ...patch } : line)))
  const mutation = useMutation({
    mutationFn: () =>
      api.post<Operation>('/inventory/' + kind, {
        warehouse_id: warehouse,
        reason: reason.trim(),
        ...(kind === 'internal-issues'
          ? { department_id: department || null, cost_centre_id: centre }
          : {}),
        ...(kind === 'scrap-disposals'
          ? {
              disposal_kind: disposalKind,
              buyer_reference: buyer.trim() || null,
              realised_amount: realised.trim() || '0',
            }
          : {}),
        lines: lines.map((line) => ({
          item_id: line.item_id,
          quantity: line.quantity.trim(),
          batch_id: line.batch_id || null,
          ...(kind === 'bin-moves'
            ? {
                from_location_id: line.location_id || null,
                to_location_id: line.to_location_id || null,
              }
            : { location_id: line.location_id || null }),
        })),
      }),
    onSuccess: (row) => {
      toast.success(
        kind === 'internal-issues' ? success.created(TITLES[kind]) : success.posted(TITLES[kind]),
        row.code,
      )
      void client.invalidateQueries({ queryKey: queryKeys.inventory.all })
      onClose()
    },
    onError: (failure) => setError(describeError(failure)),
  })
  const submit = () => {
    if (!warehouse || reason.trim().length < REASON_MIN_LENGTH) {
      setError('Choose a warehouse and record a reason for the stock movement.')
      return
    }
    if (
      lines.some(
        (line) =>
          !line.item_id ||
          !/^\d+(\.\d{1,4})?$/.test(line.quantity.trim()) ||
          !toDecimal(line.quantity).greaterThan(0),
      )
    ) {
      setError('Every line needs an item and a positive quantity, to 4 decimal places.')
      return
    }
    if (kind === 'bin-moves' && lines.some((line) => line.location_id === line.to_location_id)) {
      setError('Choose different source and destination bins on every line.')
      return
    }
    if (kind === 'internal-issues' && !department && !centre) {
      setError('Choose the department or cost centre receiving these materials.')
      return
    }
    setError('')
    mutation.mutate()
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>New {TITLES[kind].toLowerCase()}</DialogTitle>
          <DialogDescription>
            {kind === 'bin-moves'
              ? 'Move available stock between bins in one warehouse. Saving posts both sides of the move.'
              : kind === 'scrap-disposals'
                ? 'Remove stock already set aside as scrap. Saving records its disposal permanently.'
                : 'Raise a draft issue to a department or cost centre. Stock moves when it is posted or approved.'}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <WarehouseSelect
            id="operation-warehouse"
            label="Warehouse"
            value={warehouse}
            onChange={(value) => {
              setWarehouse(value)
              setLines((current) =>
                current.map((line) => ({
                  ...line,
                  location_id: '',
                  to_location_id: '',
                })),
              )
            }}
          />
          <div className="space-y-1.5">
            <Label htmlFor="operation-reason" required>
              Reason
            </Label>
            <Textarea
              id="operation-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>
          {kind === 'internal-issues' && (
            <div className="grid gap-3 sm:grid-cols-2">
              {can(P.DEPARTMENT_VIEW) && (
                <div className="space-y-1.5">
                  <Label htmlFor="operation-department">Department</Label>
                  <Select
                    value={department || SELECT_NONE}
                    onValueChange={(value) => setDepartment(value === SELECT_NONE ? '' : value)}
                  >
                    <SelectTrigger id="operation-department">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_NONE}>No department</SelectItem>
                      {departments.data?.map((row) => (
                        <SelectItem key={row.id} value={row.id}>
                          {row.code} - {row.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
              {can(P.COST_CENTRE_VIEW) && (
                <CostCentreSelect id="operation-centre" value={centre} onChange={setCentre} />
              )}
            </div>
          )}
          {kind === 'scrap-disposals' && (
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-1.5">
                <Label htmlFor="disposal-kind">Disposal kind</Label>
                <Select value={disposalKind} onValueChange={setDisposalKind}>
                  <SelectTrigger id="disposal-kind">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {['SALE', 'WRITE_OFF', 'RECYCLE'].map((value) => (
                      <SelectItem key={value} value={value}>
                        {humanise(value)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="disposal-buyer">Buyer or reference</Label>
                <Input
                  id="disposal-buyer"
                  value={buyer}
                  onChange={(event) => setBuyer(event.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="disposal-realised">Realised amount</Label>
                <Input
                  id="disposal-realised"
                  value={realised}
                  inputMode="decimal"
                  onChange={(event) => setRealised(event.target.value)}
                />
              </div>
            </div>
          )}
          <fieldset className="space-y-3">
            <legend className="mb-2 text-sm font-medium">Lines</legend>
            {lines.map((line, index) => (
              <div key={index} className="space-y-3 rounded border p-3">
                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem_auto]">
                  <ItemPicker
                    id={`operation-item-${index}`}
                    label={`Item, line ${index + 1}`}
                    value={line.item_id || null}
                    filters={ITEM_FILTERS}
                    onChange={(value) => patchLine(index, { item_id: value ?? '', batch_id: '' })}
                  />
                  <div className="space-y-1.5">
                    <Label htmlFor={`operation-quantity-${index}`}>Quantity</Label>
                    <Input
                      id={`operation-quantity-${index}`}
                      value={line.quantity}
                      inputMode="decimal"
                      onChange={(event) => patchLine(index, { quantity: event.target.value })}
                    />
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    disabled={lines.length === 1}
                    aria-label={`Remove line ${index + 1}`}
                    onClick={() => setLines((current) => current.filter((_, at) => at !== index))}
                  >
                    <Trash2 />
                  </Button>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  {line.item_id && (
                    <BatchSelect
                      id={`operation-batch-${index}`}
                      itemId={line.item_id}
                      value={line.batch_id}
                      onChange={(value) => patchLine(index, { batch_id: value })}
                    />
                  )}
                  <LocationPicker
                    id={`operation-bin-${index}`}
                    label={kind === 'bin-moves' ? 'From bin' : 'Bin'}
                    itemId={line.item_id}
                    warehouseId={warehouse}
                    value={line.location_id || null}
                    onChange={(value) => patchLine(index, { location_id: value ?? '' })}
                  />
                  {kind === 'bin-moves' && (
                    <LocationPicker
                      id={`operation-to-bin-${index}`}
                      label="To bin"
                      warehouseId={warehouse}
                      value={line.to_location_id || null}
                      onChange={(value) => patchLine(index, { to_location_id: value ?? '' })}
                    />
                  )}
                </div>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setLines((current) => [...current, blankLine()])}
            >
              <Plus />
              Add line
            </Button>
          </fieldset>
          <FieldError message={error} />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" disabled={mutation.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button loading={mutation.isPending} onClick={submit}>
            {kind === 'internal-issues'
              ? 'Create draft issue'
              : 'Post ' + TITLES[kind].toLowerCase()}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default function OperationTab({ kind }: { kind: Kind }) {
  const { can, user } = useAuth()
  const client = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [pending, setPending] = useState<{ row: Operation; verb: string } | null>(null)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState('')
  const [detailId, setDetailId] = useState<string | null>(null)
  const reviewRequired = pending?.verb === 'approve' || pending?.verb === 'post'
  const selectedId = detailId ?? (reviewRequired ? pending?.row.id : null)
  const detail = useQuery({
    queryKey: [...queryKeys.inventory.all, 'operation-detail', kind, selectedId],
    queryFn: () => api.get<Operation>(`/inventory/${kind}/${selectedId}`),
    enabled: Boolean(selectedId),
    staleTime: staleTimes.transactional,
  })
  const permission =
    kind === 'bin-moves'
      ? P.INVENTORY_TRANSFER
      : kind === 'scrap-disposals'
        ? P.INVENTORY_SCRAP_DISPOSE
        : P.INTERNAL_ISSUE_CREATE
  const canCreate = can(permission)
  const canSeeCosts = can(P.ITEM_COST_VIEW) || can(P.INVENTORY_VALUATION_VIEW)
  const list = useListQuery<Operation>({
    path: '/inventory/' + kind,
    queryKey: (params) => [...queryKeys.inventory.all, kind, params],
    defaultSortBy: DATE_FIELDS[kind],
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })
  const action = useMutation({
    mutationFn: () =>
      api.post<Operation>(
        `/inventory/${kind}/${pending?.row.id}/${pending?.verb}`,
        pending?.verb === 'cancel' || pending?.verb === 'reject' ? { reason: reason.trim() } : {},
      ),
    onSuccess: (row) => {
      toast.success(
        pending?.verb === 'reject'
          ? success.rejected(TITLES[kind])
          : pending?.verb === 'cancel'
            ? success.cancelled(TITLES[kind])
            : success.updated(TITLES[kind]),
        row.code,
      )
      void client.invalidateQueries({ queryKey: queryKeys.inventory.all })
      setPending(null)
      setReason('')
      setReasonError('')
    },
  })
  const columns = useMemo<ColumnDef<Operation, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Document',
        cell: ({ row }) => (
          <Button
            variant="link"
            className="h-auto p-0"
            onClick={() => setDetailId(row.original.id)}
          >
            {row.original.code}
          </Button>
        ),
      },
      {
        id: DATE_FIELDS[kind],
        accessorFn: documentDate,
        header: 'Date',
        cell: ({ row }) => formatDate(documentDate(row.original)),
      },
      { accessorKey: 'reason', header: 'Reason', enableSorting: false },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      ...(can(P.COST_CENTRE_VIEW) && kind === 'internal-issues'
        ? [
            {
              id: 'cost_centre',
              header: 'Cost centre',
              enableSorting: false,
              cell: ({ row }: { row: { original: Operation } }) => (
                <CostCentreName id={row.original.cost_centre_id ?? null} />
              ),
            },
          ]
        : []),
      ...(canSeeCosts
        ? [
            {
              id: 'value',
              header: 'FIFO value',
              enableSorting: false,
              cell: ({ row }: { row: { original: Operation } }) =>
                formatMoney(row.original.total_value),
            },
          ]
        : []),
      {
        id: 'actions',
        header: 'Actions',
        enableSorting: false,
        cell: ({ row }) => {
          if (
            kind !== 'internal-issues' ||
            row.original.status === 'POSTED' ||
            row.original.status === 'CANCELLED'
          )
            return null
          const issue = row.original
          return (
            <div className="flex flex-wrap justify-end gap-2">
              {canCreate && issue.status === 'DRAFT' && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    setPending({ row: issue, verb: issue.requires_approval ? 'submit' : 'post' })
                  }
                >
                  {issue.requires_approval ? 'Submit' : 'Post'}
                </Button>
              )}
              {can(P.INTERNAL_ISSUE_APPROVE) &&
                issue.status === 'PENDING_APPROVAL' &&
                (user?.is_superuser || issue.created_by_id !== user?.id) && (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setPending({ row: issue, verb: 'approve' })}
                    >
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setPending({ row: issue, verb: 'reject' })}
                    >
                      Reject
                    </Button>
                  </>
                )}
              {canCreate && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setPending({ row: issue, verb: 'cancel' })}
                >
                  Cancel
                </Button>
              )}
            </div>
          )
        },
      },
    ],
    [can, canCreate, canSeeCosts, kind, user],
  )
  return (
    <>
      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search document code"
        getRowId={(row) => row.id}
        emptyTitle={`No ${TITLES[kind].toLowerCase()} documents`}
        toolbar={
          canCreate && (
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus />
              New {TITLES[kind].toLowerCase()}
            </Button>
          )
        }
      />
      {createOpen && canCreate && (
        <OperationForm kind={kind} onClose={() => setCreateOpen(false)} />
      )}
      <Dialog
        open={Boolean(detailId)}
        onOpenChange={(open) => {
          if (!open) setDetailId(null)
        }}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>{detail.data?.code ?? TITLES[kind]}</DialogTitle>
            <DialogDescription>Recorded quantities and stock positions.</DialogDescription>
          </DialogHeader>
          <DialogBody>
            {detail.isPending && <p role="status">Loading document...</p>}
            <FieldError message={detail.error ? describeError(detail.error) : undefined} />
            {detail.data && <OperationLines document={detail.data} />}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDetailId(null)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={Boolean(pending)}
        onOpenChange={(open) => {
          if (!open) {
            setPending(null)
            setReason('')
            setReasonError('')
          }
        }}
        title={`${humanise(pending?.verb ?? '')} ${pending?.row.code ?? 'issue'}?`}
        description={
          pending?.verb === 'post' || pending?.verb === 'approve'
            ? 'Posting removes these materials from available stock.'
            : 'The status change is recorded with this document.'
        }
        confirmLabel={
          pending?.verb === 'cancel'
            ? 'Cancel issue'
            : pending?.verb === 'reject'
              ? 'Reject issue'
              : humanise(pending?.verb ?? 'Confirm')
        }
        loading={action.isPending || Boolean(reviewRequired && detail.isPending)}
        onConfirm={() => {
          if (
            (pending?.verb === 'cancel' || pending?.verb === 'reject') &&
            reason.trim().length < REASON_MIN_LENGTH
          ) {
            setReasonError(
              `Enter at least ${REASON_MIN_LENGTH} characters explaining this ${pending?.verb === 'reject' ? 'rejection' : 'cancellation'}.`,
            )
            return
          }
          if (reviewRequired && (!detail.data || detail.isError)) return
          action.mutate()
        }}
      >
        {reviewRequired && (
          <div className="space-y-2">
            {detail.isPending && <p role="status">Loading issue details...</p>}
            <FieldError message={detail.error ? describeError(detail.error) : undefined} />
            {detail.data && <OperationLines document={detail.data} />}
          </div>
        )}
        {(pending?.verb === 'cancel' || pending?.verb === 'reject') && (
          <div className="space-y-1.5">
            <Label htmlFor="issue-cancel-reason" required>
              Reason
            </Label>
            <Textarea
              id="issue-cancel-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <FieldError message={reasonError} />
          </div>
        )}
        <FieldError message={action.error ? describeError(action.error) : undefined} />
      </ConfirmDialog>
    </>
  )
}

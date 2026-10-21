/**
 * Batch and serial tracking - the two registers behind a recall.
 *
 * A balance says "40 of item X"; a lot says which 40, when they expire, and
 * which supplier's paperwork came with them. A serial says which individual
 * unit a customer is holding. They are deliberately separate tabs because they
 * answer different questions and because a lot and a unit are not the same
 * model: a lot has a lifecycle, a unit has a position and a movement history.
 *
 * The register is read-only for anyone with INVENTORY_VIEW. Changing a lot's
 * status or minting labels is a different right (INVENTORY_BATCH_MANAGE), and
 * the API authorises every mutation again.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { MoreHorizontal, Plus, QrCode, Tags } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatMoney, formatQuantity, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { ItemPicker, WarehouseSelect } from './pickers'
import {
  BatchActionDialog,
  RegisterBatchDialog,
  RegisterSerialsDialog,
  SerialDetailDialog,
} from './tracking-dialogs'
import {
  BATCH_STATUSES,
  batchLifecycleActions,
  type BatchStatus,
  type SerialStatus,
  type StockBatchRow,
  type StockSerialRow,
} from './types'

const ALL = '__all__'
const SERIAL_STATUSES: SerialStatus[] = [
  'IN_STOCK',
  'ISSUED',
  'IN_TRANSIT',
  'DISPATCHED',
  'RETURNED',
  'SCRAPPED',
]

function expiryHint(expiresOn: string | null): { label: string; tone: string } {
  if (!expiresOn) return { label: 'No expiry', tone: 'text-muted-foreground' }
  const days = Math.ceil(
    (new Date(`${expiresOn}T00:00:00`).getTime() - Date.now()) / (24 * 60 * 60 * 1000),
  )
  if (days < 0) return { label: `${formatDate(expiresOn)} - expired`, tone: 'text-destructive' }
  if (days <= 30) {
    return { label: `${formatDate(expiresOn)} - ${days} day(s) left`, tone: 'text-warning' }
  }
  return { label: formatDate(expiresOn), tone: 'text-muted-foreground' }
}

// ------------------------------------------------------------------- batches

function BatchesTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const [registerOpen, setRegisterOpen] = useState(false)
  const [action, setAction] = useState<{
    batch: StockBatchRow
    action: 'quarantine' | 'activate' | 'expire'
  } | null>(null)

  const itemId = searchParams.get('batches_item_id') ?? ''
  const status = (searchParams.get('batches_status') ?? '') as BatchStatus | ''
  const expiryTo = searchParams.get('batches_expiry_to') ?? ''

  const list = useListQuery<StockBatchRow>({
    path: '/inventory/batches',
    queryKey: queryKeys.inventory.batches,
    namespace: 'batches',
    filters: {
      ...(itemId ? { item_id: itemId } : {}),
      ...(status ? { status } : {}),
      ...(expiryTo ? { expiry_to: expiryTo } : {}),
    },
    defaultLimit: 50,
  })

  const canManage = can(P.INVENTORY_BATCH_MANAGE)

  const columns = useMemo<ColumnDef<StockBatchRow>[]>(
    () => [
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <p className="font-medium">{row.original.item_code ?? 'Unknown item'}</p>
            <p className="text-xs text-muted-foreground">{row.original.item_name ?? '-'}</p>
          </div>
        ),
      },
      {
        accessorKey: 'batch_number',
        header: 'Batch',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <p className="font-mono text-xs font-medium">{row.original.batch_number}</p>
            {row.original.supplier_batch_number && (
              <p className="text-xs text-muted-foreground">
                Supplier: {row.original.supplier_batch_number}
              </p>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'available',
        header: 'Available',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.available_quantity)}</span>
        ),
      },
      {
        id: 'on_hand',
        header: 'On hand',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <p className="tabular">{formatQuantity(row.original.on_hand_quantity)}</p>
            {toDecimal(row.original.in_transit_quantity).greaterThan(0) && (
              <p className="text-xs text-muted-foreground">
                {formatQuantity(row.original.in_transit_quantity)} in transit
              </p>
            )}
          </div>
        ),
      },
      {
        id: 'expiry',
        header: 'Expiry',
        enableSorting: false,
        cell: ({ row }) => {
          const hint = expiryHint(row.original.expires_on)
          return <span className={cn('text-sm', hint.tone)}>{hint.label}</span>
        },
      },
      {
        id: 'received',
        header: 'Received',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.received_on),
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        cell: ({ row }) => {
          const options = batchLifecycleActions(row.original.status)
          if (!canManage || options.length === 0) return null
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Actions for batch ${row.original.batch_number}`}
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {options.map((option) => (
                  <DropdownMenuItem
                    key={option.action}
                    className={option.action === 'activate' ? undefined : 'text-destructive'}
                    onSelect={() => setAction({ batch: row.original, action: option.action })}
                  >
                    {option.label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )
        },
      },
    ],
    [canManage],
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
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search batch or supplier batch number..."
        emptyTitle={
          itemId || status || expiryTo ? 'No batch matches these filters' : 'No batches yet'
        }
        emptyDescription={
          itemId || status || expiryTo
            ? 'Clear a filter or search a different number.'
            : 'Register the first lot before receiving its stock.'
        }
        emptyAction={
          canManage && !itemId && !status && !expiryTo ? (
            <Button size="sm" onClick={() => setRegisterOpen(true)}>
              <Plus /> Register batch
            </Button>
          ) : undefined
        }
        toolbar={
          <div className="flex flex-wrap items-end gap-2">
            <ItemPicker
              id="batch-filter-item"
              label="Item"
              value={itemId || null}
              onChange={(value) => list.setParam('item_id', value ?? undefined)}
              filters={{ is_batch_tracked: true }}
              placeholder="All batch-tracked items"
              hideLabel={false}
              className="min-w-[220px]"
            />
            <div className="space-y-1.5">
              <Label htmlFor="batch-filter-status">Status</Label>
              <Select
                value={status || ALL}
                onValueChange={(next) => list.setParam('status', next === ALL ? undefined : next)}
              >
                <SelectTrigger id="batch-filter-status" className="w-[160px]">
                  <SelectValue placeholder="All statuses" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All statuses</SelectItem>
                  {BATCH_STATUSES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {value.charAt(0) + value.slice(1).toLowerCase()}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="batch-filter-expiry">Expiring by</Label>
              <Input
                id="batch-filter-expiry"
                type="date"
                value={expiryTo}
                max="9999-12-31"
                onChange={(event) => list.setParam('expiry_to', event.target.value || undefined)}
                className="w-[160px]"
              />
            </div>
            <Button
              variant="outline"
              onClick={() =>
                list.setParams({
                  item_id: undefined,
                  status: undefined,
                  expiry_to: undefined,
                  q: undefined,
                })
              }
              disabled={!itemId && !status && !expiryTo && !list.search}
            >
              Clear filters
            </Button>
            {canManage && (
              <Button className="ml-auto" onClick={() => setRegisterOpen(true)}>
                <Plus /> Register batch
              </Button>
            )}
          </div>
        }
      />
      {registerOpen && <RegisterBatchDialog onClose={() => setRegisterOpen(false)} />}
      {action && (
        <BatchActionDialog
          batch={action.batch}
          action={action.action}
          onClose={() => setAction(null)}
        />
      )}
    </>
  )
}

// ------------------------------------------------------------------- serials

function SerialsTab() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const [registerOpen, setRegisterOpen] = useState(false)
  const [traceId, setTraceId] = useState<string | null>(null)

  const itemId = searchParams.get('serials_item_id') ?? ''
  const warehouseId = searchParams.get('serials_warehouse_id') ?? ''
  const status = (searchParams.get('serials_status') ?? '') as SerialStatus | ''

  const list = useListQuery<StockSerialRow>({
    path: '/inventory/serials',
    queryKey: queryKeys.inventory.serials,
    namespace: 'serials',
    filters: {
      ...(itemId ? { item_id: itemId } : {}),
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(status ? { status } : {}),
    },
    defaultLimit: 50,
  })

  const canManage = can(P.INVENTORY_BATCH_MANAGE)
  const canSeeValue = can(P.INVENTORY_VALUATION_VIEW)

  const columns = useMemo<ColumnDef<StockSerialRow>[]>(() => {
    const base: ColumnDef<StockSerialRow>[] = [
      {
        accessorKey: 'serial_number',
        header: 'Serial',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="font-mono text-xs font-medium">{row.original.serial_number}</span>
        ),
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <p className="font-medium">{row.original.item_code ?? 'Unknown item'}</p>
            <p className="text-xs text-muted-foreground">{row.original.item_name ?? '-'}</p>
          </div>
        ),
      },
      {
        id: 'batch',
        header: 'Batch',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="font-mono text-xs">{row.original.batch_number ?? '-'}</span>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'warehouse',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => row.original.warehouse_code ?? '-',
      },
      {
        id: 'received',
        header: 'Received',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.received_on),
      },
      {
        id: 'trace',
        header: '',
        enableSorting: false,
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Trace serial ${row.original.serial_number}`}
            onClick={() => setTraceId(row.original.id)}
          >
            <QrCode /> Trace
          </Button>
        ),
      },
    ]
    if (!canSeeValue) return base
    const valueColumn: ColumnDef<StockSerialRow> = {
      id: 'unit_cost',
      header: 'Unit cost',
      enableSorting: false,
      cell: ({ row }) => <span className="tabular">{formatMoney(row.original.unit_cost)}</span>,
    }
    base.splice(base.length - 1, 0, valueColumn)
    return base
  }, [canSeeValue])

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
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search serial number..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setTraceId(row.id)}
        emptyTitle={
          itemId || warehouseId || status ? 'No serial matches these filters' : 'No serials yet'
        }
        emptyDescription={
          itemId || warehouseId || status
            ? 'Clear a filter or search a different unit.'
            : 'Register a label run, or receive units against their GRN.'
        }
        emptyAction={
          canManage && !itemId && !warehouseId && !status ? (
            <Button size="sm" onClick={() => setRegisterOpen(true)}>
              <Plus /> Register serials
            </Button>
          ) : undefined
        }
        toolbar={
          <div className="flex flex-wrap items-end gap-2">
            <ItemPicker
              id="serial-filter-item"
              label="Item"
              value={itemId || null}
              onChange={(value) => list.setParam('item_id', value ?? undefined)}
              filters={{ is_serial_tracked: true }}
              placeholder="All serial-tracked items"
              className="min-w-[220px]"
            />
            <WarehouseSelect
              id="serial-filter-warehouse"
              label="Warehouse"
              value={warehouseId}
              onChange={(value) => list.setParam('warehouse_id', value || undefined)}
              includeAll
            />
            <div className="space-y-1.5">
              <Label htmlFor="serial-filter-status">Status</Label>
              <Select
                value={status || ALL}
                onValueChange={(next) => list.setParam('status', next === ALL ? undefined : next)}
              >
                <SelectTrigger id="serial-filter-status" className="w-[150px]">
                  <SelectValue placeholder="All statuses" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All statuses</SelectItem>
                  {SERIAL_STATUSES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {value.replace('_', ' ').toLowerCase()}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              variant="outline"
              onClick={() =>
                list.setParams({
                  item_id: undefined,
                  warehouse_id: undefined,
                  status: undefined,
                  q: undefined,
                })
              }
              disabled={!itemId && !warehouseId && !status && !list.search}
            >
              Clear filters
            </Button>
            {canManage && (
              <Button className="ml-auto" onClick={() => setRegisterOpen(true)}>
                <Plus /> Register serials
              </Button>
            )}
          </div>
        }
      />
      {registerOpen && <RegisterSerialsDialog onClose={() => setRegisterOpen(false)} />}
      {traceId && <SerialDetailDialog serialId={traceId} onClose={() => setTraceId(null)} />}
    </>
  )
}

// ---------------------------------------------------------------------- page

export default function TrackingPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') === 'serials' ? 'serials' : 'batches'
  const setTab = (next: string) => {
    setSearchParams(
      (current) => {
        const updated = new URLSearchParams(current)
        if (next === 'batches') updated.delete('tab')
        else updated.set('tab', next)
        return updated
      },
      { replace: true },
    )
  }

  return (
    <div>
      <PageHeader
        title="Batch and serial tracking"
        description="The lot behind a balance and the history of an individual unit. Quarantine, expiry and label runs are recorded here."
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="batches">
            <Tags /> Batches
          </TabsTrigger>
          <TabsTrigger value="serials">
            <QrCode /> Serials
          </TabsTrigger>
        </TabsList>
        <TabsContent value="batches" className="mt-4">
          <BatchesTab />
        </TabsContent>
        <TabsContent value="serials" className="mt-4">
          <SerialsTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

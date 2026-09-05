/**
 * Item detail.
 *
 * Six tabs, six independently-loaded panels: opening the master record must not
 * pull a stock query, a where-used explosion and two price lists before the
 * page can paint.
 *
 * Two of the actions here are retirements, and both are gated on
 * `item.delete` - see `item-retire-dialogs.tsx` for what those endpoints do
 * and, more to the point, what they do not refuse.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { Boxes, Layers, Pencil, RefreshCw, Shapes, Tags, Trash2, Truck } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DescriptionList, EmptyState, PageHeader, StatTile, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { ItemFormDialog } from './item-form-dialog'
import {
  ALL_CAPABILITIES,
  CAPABILITY_META,
  acceptsRetirement,
  acceptsVariantRetirement,
  useCategoryOptions,
  useUomOptions,
  type ItemDetail,
  type ItemPrice,
  type ItemStockSummary,
  type ItemVariant,
  type ItemVendorLink,
  type ItemWarehouseStockRow,
  type WhereUsedRow,
} from './item-model'
import { RetireItemDialog, RetireVariantDialog } from './item-retire-dialogs'

/**
 * Server-paged read for one panel inside a tab.
 *
 * `useListQuery` is deliberately not reused: it keeps its state in the URL, and
 * five panels on one screen would fight over the same `page` and `q`
 * parameters. Paging is still done by the server - only the page number is
 * local to the component.
 */
function usePanelQuery<T>({
  queryKey,
  path,
  params,
  enabled = true,
  staleTime,
}: {
  queryKey: readonly unknown[]
  path: string
  params?: Record<string, unknown>
  enabled?: boolean
  staleTime?: number
}) {
  const [page, setPage] = useState(1)

  const query = useQuery({
    queryKey: [...queryKey, page],
    queryFn: () => api.getPage<T>(path, { params: { page, limit: LOOKUP_LIST_LIMIT, ...params } }),
    placeholderData: keepPreviousData,
    enabled,
    staleTime,
  })

  // Endpoints that answer with a plain envelope rather than a paginated one
  // still put the collection in `data`; anything else is treated as empty
  // instead of crashing the table.
  const payload = query.data?.data
  return {
    rows: Array.isArray(payload) ? (payload as T[]) : [],
    pagination: query.data?.pagination,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    error: query.error as Error | null,
    refetch: () => void query.refetch(),
    page,
    setPage,
  }
}

function PanelDenied({ what }: { what: string }) {
  return (
    <EmptyState
      title={`${what} are not visible to your account`}
      description="Your role does not include the permission this panel needs. Ask an administrator if you believe this is wrong."
    />
  )
}

// ------------------------------------------------------------- Overview -----

function OverviewPanel({
  item,
  categoryLabel,
  uomLabel,
  canViewCost,
}: {
  item: ItemDetail
  categoryLabel: (id: string | null) => string
  uomLabel: (id: string | null) => string
  canViewCost: boolean
}) {
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>Master record</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList
            columns={3}
            items={[
              { label: 'Code', value: item.code },
              { label: 'Name', value: item.name },
              { label: 'Item group', value: humanise(item.item_group) },
              { label: 'Category', value: categoryLabel(item.category_id) },
              { label: 'Base unit', value: uomLabel(item.base_uom_id) },
              { label: 'Purchase unit', value: uomLabel(item.purchase_uom_id) },
              { label: 'Sale unit', value: uomLabel(item.sale_uom_id) },
              { label: 'Production type', value: humanise(item.production_type) },
              { label: 'Lead time', value: `${item.lead_time_days} day(s)` },
              { label: 'HSN code', value: item.hsn_code ?? '-' },
              { label: 'GST rate', value: formatPercent(item.gst_rate, 2) },
              { label: 'Valuation', value: humanise(item.valuation_method) },
              { label: 'Reorder level', value: formatQuantity(item.reorder_level) },
              { label: 'Reorder quantity', value: formatQuantity(item.reorder_quantity) },
              { label: 'Minimum order', value: formatQuantity(item.min_order_quantity) },
              { label: 'Safety stock', value: formatQuantity(item.safety_stock) },
              {
                label: 'Shelf life',
                value: item.shelf_life_days ? `${item.shelf_life_days} day(s)` : '-',
              },
              {
                label: 'Tracking',
                value:
                  [item.is_batch_tracked ? 'Batch' : null, item.is_serial_tracked ? 'Serial' : null]
                    .filter(Boolean)
                    .join(' + ') || 'None',
              },
              { label: 'Sale price', value: formatMoney(item.sale_price) },
              { label: 'Dealer price', value: formatMoney(item.dealer_price) },
              ...(canViewCost
                ? [
                    {
                      label: 'Standard cost',
                      value:
                        item.standard_cost === undefined ? '-' : formatMoney(item.standard_cost),
                    },
                  ]
                : []),
              { label: 'Finish', value: item.finish ?? '-' },
              { label: 'Size', value: item.size ?? '-' },
              { label: 'Colour', value: item.colour ?? '-' },
              { label: 'Created', value: formatDateTime(item.created_at) },
              { label: 'Last updated', value: formatDateTime(item.updated_at) },
            ]}
          />
          {item.description && (
            <p className="mt-5 border-t pt-4 text-sm text-muted-foreground">{item.description}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Capabilities</CardTitle>
        </CardHeader>
        <CardContent>
          {/* Rendered as nine separate states, never as one classification: an
              item can hold any combination of these roles at once. */}
          <ul className="space-y-2">
            {ALL_CAPABILITIES.map((key) => {
              const { label, icon: Icon } = CAPABILITY_META[key]
              const on = item[key]
              return (
                <li key={key} className="flex items-center justify-between gap-3 text-sm">
                  <span className="flex items-center gap-2">
                    <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
                    {label}
                  </span>
                  <Badge variant={on ? 'success' : 'muted'}>{on ? 'Yes' : 'No'}</Badge>
                </li>
              )
            })}
          </ul>
        </CardContent>
      </Card>
    </div>
  )
}

// ---------------------------------------------------------------- Stock -----

/**
 * Stock of this item, per warehouse.
 *
 * The endpoint answers with a *single-element* page whose pagination block
 * describes the `warehouses` list inside it, not the outer array - so the row
 * source is `data[0].warehouses` and the totals come from `data[0]`. Folding
 * the visible rows here instead would total only the page the user is looking
 * at, and the figure would move as they paged.
 */
function StockPanel({ itemId, uomCode }: { itemId: string; uomCode: string }) {
  const panel = usePanelQuery<ItemStockSummary>({
    queryKey: queryKeys.inventory.itemStock(itemId),
    path: `/inventory/items/${itemId}/summary`,
    // Availability is never served stale (master rules 34).
    staleTime: staleTimes.live,
  })

  const summary = panel.rows[0] ?? null
  const rows = useMemo(() => summary?.warehouses ?? [], [summary])

  const columns = useMemo<ColumnDef<ItemWarehouseStockRow, unknown>[]>(
    () => [
      {
        id: 'warehouse',
        header: 'Warehouse',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="font-medium">
            {row.original.warehouse_code ?? row.original.warehouse_id}
          </span>
        ),
      },
      {
        id: 'warehouse_name',
        header: 'Name',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.warehouse_name ?? '-'}</span>
        ),
      },
      {
        id: 'on_hand',
        header: 'On hand',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.on_hand_quantity),
      },
      {
        id: 'reserved',
        header: 'Reserved',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.reserved_quantity),
      },
      {
        id: 'qc_hold',
        header: 'QC hold',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.qc_hold_quantity),
      },
      {
        id: 'damaged',
        header: 'Damaged',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.damaged_quantity),
      },
      {
        id: 'in_transit',
        header: 'In transit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.in_transit_quantity),
      },
      {
        // Owned, but sitting at a job-work vendor. It is not on the shelf and it
        // is not promisable, and it is the quantity people forget when a part
        // reads short.
        id: 'job_work',
        header: 'At job work',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.job_work_quantity),
      },
      {
        id: 'available',
        header: 'Available',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.available_quantity)}</span>
        ),
      },
    ],
    [],
  )

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="On hand"
          value={formatQuantity(summary?.total_on_hand, uomCode)}
          hint="Across every warehouse, not just this page"
          icon={Boxes}
          isLoading={panel.isLoading}
        />
        <StatTile
          label="Reserved"
          value={formatQuantity(summary?.total_reserved, uomCode)}
          hint="Promised to a demand"
          tone="warning"
          isLoading={panel.isLoading}
        />
        <StatTile
          label="Available"
          value={formatQuantity(summary?.total_available, uomCode)}
          hint="On hand less reserved, QC hold and damaged"
          tone="success"
          isLoading={panel.isLoading}
        />
        <StatTile
          label="Owned elsewhere"
          value={formatQuantity(
            toDecimal(summary?.total_in_transit).plus(toDecimal(summary?.total_job_work)).toFixed(),
            uomCode,
          )}
          hint={
            formatQuantity(summary?.total_in_transit) +
            ' in transit, ' +
            formatQuantity(summary?.total_job_work) +
            ' at job work'
          }
          icon={Truck}
          isLoading={panel.isLoading}
        />
      </div>

      <DataTable
        columns={columns}
        data={rows}
        pagination={panel.pagination}
        isLoading={panel.isLoading}
        isFetching={panel.isFetching}
        error={panel.error}
        onPageChange={panel.setPage}
        onRefresh={panel.refetch}
        getRowId={(row) => row.warehouse_id}
        emptyTitle="No stock recorded"
        emptyDescription="This item has no balance row in any warehouse yet."
      />
    </div>
  )
}

// ------------------------------------------------------------- Variants -----

/**
 * A price or cost delta, with the sign that makes it readable as a delta.
 *
 * `Intl` supplies the minus; the plus has to be added. A zero delta prints as a
 * dash so the eye skips the variants that change nothing, and the test is made
 * on the decimal rather than on the string - "0.00" and "0" are the same number
 * and have to read the same way.
 */
function DeltaCell({ value }: { value: ItemVariant['price_delta'] }) {
  const amount = toDecimal(value)
  if (amount.isZero()) return <span className="text-muted-foreground">-</span>
  return (
    <span className={amount.isNegative() ? 'text-destructive' : undefined}>
      {amount.isPositive() ? '+' : ''}
      {formatMoney(value)}
    </span>
  )
}

/**
 * The finish/size/colour combinations of this item.
 *
 * A variant is not a separate item - it shares this item's BOM, planning
 * parameters and all nine capability flags - which is why the list belongs on
 * the item rather than on a screen of its own.
 *
 * The request deliberately omits `is_active`, so an inactive variant still
 * appears and is greyed by its status badge instead. Filtering it out would
 * hide the row most likely to need retiring.
 *
 * Creating and editing a variant are not offered here: `POST
 * /products/items/{id}/variants` and `PATCH /products/variants/{id}` have no
 * screen anywhere in the application yet. Retirement is, because a variant
 * entered wrongly otherwise sits in the price list's picker forever.
 */
function VariantsPanel({
  itemId,
  canViewCost,
  canRetire,
  onRetire,
}: {
  itemId: string
  canViewCost: boolean
  canRetire: boolean
  onRetire: (variant: ItemVariant) => void
}) {
  const panel = usePanelQuery<ItemVariant>({
    queryKey: [...queryKeys.items.detail(itemId), 'variants'],
    path: `/products/items/${itemId}/variants`,
    staleTime: staleTimes.master,
  })

  const columns = useMemo<ColumnDef<ItemVariant, unknown>[]>(() => {
    const defs: ColumnDef<ItemVariant, unknown>[] = [
      {
        id: 'code',
        header: 'Code',
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'name',
        header: 'Name',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[20rem] truncate">{row.original.name}</span>
        ),
      },
      {
        // The three attributes share one column: individually they are mostly
        // blank, and it is the combination that identifies the variant - the
        // unique index is on all three at once.
        id: 'attributes',
        header: 'Finish / size / colour',
        enableSorting: false,
        cell: ({ row }) => {
          const attributes = [row.original.finish, row.original.size, row.original.colour]
            .filter(Boolean)
            .join(' / ')
          return attributes ? (
            attributes
          ) : (
            <span className="text-muted-foreground">No attributes</span>
          )
        },
      },
      {
        id: 'price_delta',
        header: 'Price delta',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <DeltaCell value={row.original.price_delta} />,
      },
    ]

    // `VariantOut` carries `cost_delta` unconditionally - unlike the item's
    // `standard_cost`, which the API itself withholds - so hiding this column is
    // presentation, not access control. It follows the same permission anyway,
    // because a cost is a cost wherever it is shown.
    if (canViewCost) {
      defs.push({
        id: 'cost_delta',
        header: 'Cost delta',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <DeltaCell value={row.original.cost_delta} />,
      })
    }

    defs.push(
      {
        id: 'barcode',
        header: 'Barcode',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.barcode ?? '-'}</span>
        ),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
    )

    if (canRetire) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        size: 56,
        meta: { align: 'right' },
        // Every variant a list returns accepts retirement - the predicate says
        // why - but it is asked rather than assumed, so the day the API grows a
        // rule the row stops offering the action on its own.
        cell: ({ row }) =>
          acceptsVariantRetirement(row.original) ? (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Retire variant ${row.original.code}`}
              onClick={() => onRetire(row.original)}
            >
              <Trash2 className="text-destructive" />
            </Button>
          ) : null,
      })
    }

    return defs
  }, [canRetire, canViewCost, onRetire])

  return (
    <DataTable
      columns={columns}
      data={panel.rows}
      pagination={panel.pagination}
      isLoading={panel.isLoading}
      isFetching={panel.isFetching}
      error={panel.error}
      onPageChange={panel.setPage}
      onRefresh={panel.refetch}
      getRowId={(row) => row.id}
      emptyTitle="No variants"
      emptyDescription="This item is stocked and sold in one form only. A finish, size or colour needing its own code is a variant; anything needing its own BOM is a new item."
    />
  )
}

// ----------------------------------------------------------- Where used -----

function WhereUsedPanel({ itemId }: { itemId: string }) {
  const panel = usePanelQuery<WhereUsedRow>({
    queryKey: queryKeys.bom.whereUsed(itemId),
    path: `/bom/where-used/${itemId}`,
    staleTime: staleTimes.master,
  })

  const columns = useMemo<ColumnDef<WhereUsedRow, unknown>[]>(
    () => [
      {
        id: 'parent',
        header: 'Parent item',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.parent_item_id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.parent_item_code}
          </Link>
        ),
      },
      {
        id: 'parent_name',
        header: 'Description',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.parent_item_name}</span>
        ),
      },
      {
        id: 'bom',
        header: 'BOM',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            {row.original.bom_code}
            <span className="text-muted-foreground">v{row.original.bom_version}</span>
            {row.original.is_default && <Badge variant="secondary">Default</Badge>}
          </span>
        ),
      },
      {
        id: 'bom_status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.bom_status} />,
      },
      {
        id: 'quantity_per',
        header: 'Qty per',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.quantity_per),
      },
      {
        // How far above this item the parent sits. `levels` defaults to 1, so
        // it is normally all ones - it stops being noise the moment a caller
        // asks for a deeper explosion.
        id: 'level',
        header: 'Level',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatNumber(row.original.level),
      },
      // No scrap or optional column: `WhereUsedRead` carries neither, so both
      // were permanently empty. They live on the BOM line, which the BOM screen
      // shows.
    ],
    [],
  )

  return (
    <DataTable
      columns={columns}
      data={panel.rows}
      pagination={panel.pagination}
      isLoading={panel.isLoading}
      isFetching={panel.isFetching}
      error={panel.error}
      onPageChange={panel.setPage}
      onRefresh={panel.refetch}
      getRowId={(row) => `${row.bom_id}-${row.line_number}`}
      emptyTitle="Not used in any BOM"
      emptyDescription="No bill of materials lists this item as a component."
    />
  )
}

// --------------------------------------------------------------- Prices -----

function PricesPanel({ itemId }: { itemId: string }) {
  const panel = usePanelQuery<ItemPrice>({
    queryKey: [...queryKeys.items.detail(itemId), 'prices'],
    path: `/products/items/${itemId}/prices`,
    staleTime: staleTimes.master,
  })

  const columns = useMemo<ColumnDef<ItemPrice, unknown>[]>(
    () => [
      {
        id: 'price_type',
        header: 'Channel',
        enableSorting: false,
        cell: ({ row }) => <Badge variant="secondary">{humanise(row.original.price_type)}</Badge>,
      },
      {
        id: 'price',
        header: 'Price',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.price, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'min_quantity',
        header: 'From quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.min_quantity),
      },
      {
        id: 'valid_from',
        header: 'Valid from',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.valid_from),
      },
      {
        id: 'valid_to',
        header: 'Valid to',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.valid_to ? (
            formatDate(row.original.valid_to)
          ) : (
            <span className="text-muted-foreground">Open ended</span>
          ),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
      {
        id: 'notes',
        header: 'Notes',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate text-muted-foreground">
            {row.original.notes ?? '-'}
          </span>
        ),
      },
    ],
    [],
  )

  return (
    <DataTable
      columns={columns}
      data={panel.rows}
      pagination={panel.pagination}
      isLoading={panel.isLoading}
      isFetching={panel.isFetching}
      error={panel.error}
      onPageChange={panel.setPage}
      onRefresh={panel.refetch}
      getRowId={(row) => row.id}
      emptyTitle="No dated prices"
      emptyDescription="Order entry falls back to the price on the item master until a price list row exists."
    />
  )
}

// -------------------------------------------------------------- Vendors -----

interface VendorOption {
  id: string
  code: string
  name: string
}

function VendorsPanel({ itemId, canViewVendors }: { itemId: string; canViewVendors: boolean }) {
  const panel = usePanelQuery<ItemVendorLink>({
    queryKey: [...queryKeys.items.detail(itemId), 'vendors'],
    path: `/products/items/${itemId}/vendors`,
    staleTime: staleTimes.master,
  })

  const vendors = useQuery({
    // Its own shape: this stores an array, where the list page stores a page
    // envelope under `list` and `useVendorOptions` stores `VendorOption[]`
    // under `options`. An invalidation of `vendors.all` still reaches it.
    queryKey: [...queryKeys.vendors.all, 'label-lookup', LOOKUP_LIST_LIMIT] as const,
    // A vendor name is a label on someone else's screen. If this account may
    // not read vendors the cell falls back to the vendor's own item name rather
    // than replacing a working panel with an error; any other failure is one.
    queryFn: async () => {
      try {
        return (
          await api.getPage<VendorOption>('/vendors', { params: { limit: LOOKUP_LIST_LIMIT } })
        ).data
      } catch (error) {
        return emptyOnPermissionError<VendorOption>(error)
      }
    },
    staleTime: staleTimes.master,
    enabled: canViewVendors,
  })
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  const columns = useMemo<ColumnDef<ItemVendorLink, unknown>[]>(
    () => [
      {
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const vendor = vendorById.get(row.original.vendor_id)
          return (
            <span className="flex items-center gap-2">
              <span className="font-medium">
                {vendor
                  ? `${vendor.code} - ${vendor.name}`
                  : (row.original.vendor_item_name ?? '-')}
              </span>
              {row.original.is_preferred && <Badge variant="success">Preferred</Badge>}
            </span>
          )
        },
      },
      {
        id: 'vendor_item_code',
        header: 'Vendor item code',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.vendor_item_code ?? '-'}</span>
        ),
      },
      {
        id: 'lead_time',
        header: 'Lead time',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => `${row.original.lead_time_days} d`,
      },
      {
        id: 'min_order_quantity',
        header: 'Min order',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.min_order_quantity),
      },
      {
        id: 'order_multiple',
        header: 'Multiple',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.order_multiple),
      },
      {
        id: 'last_purchase_price',
        header: 'Last price',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.last_purchase_price === null ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            formatMoney(row.original.last_purchase_price, { currency: row.original.currency })
          ),
      },
      {
        id: 'last_purchase_date',
        header: 'Last purchased',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.last_purchase_date),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
    ],
    [vendorById],
  )

  return (
    <DataTable
      columns={columns}
      data={panel.rows}
      pagination={panel.pagination}
      isLoading={panel.isLoading}
      isFetching={panel.isFetching}
      error={panel.error}
      onPageChange={panel.setPage}
      onRefresh={panel.refetch}
      getRowId={(row) => row.id}
      emptyTitle="No vendors linked"
      emptyDescription="Link a supplier so requisitions and purchase orders can propose one."
    />
  )
}

// ----------------------------------------------------------------- page -----

export default function ItemDetailPage() {
  const { itemId = '' } = useParams<{ itemId: string }>()
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [editOpen, setEditOpen] = useState(false)
  const [retireOpen, setRetireOpen] = useState(false)
  // The variant being retired is held here rather than inside the panel: Radix
  // unmounts an inactive tab, and a confirmation that disappears when the user
  // glances at another tab is worse than no confirmation.
  const [retiringVariant, setRetiringVariant] = useState<ItemVariant | null>(null)

  const canViewCost = can(P.ITEM_COST_VIEW)
  // Both retirements are gated on `item.delete`, which is what
  // `DELETE /products/items/{id}` and `DELETE /products/variants/{id}` enforce.
  // Not `item.update`: that right governs deactivating an item, which is
  // reversible, and this is not.
  const canRetire = can(P.ITEM_DELETE)

  const itemQuery = useQuery({
    queryKey: queryKeys.items.detail(itemId),
    queryFn: () => api.get<ItemDetail>(`/products/items/${itemId}`),
    staleTime: staleTimes.master,
    enabled: Boolean(itemId),
  })

  const categories = useCategoryOptions(can(P.CATEGORY_VIEW))
  const uoms = useUomOptions(can(P.UOM_VIEW))
  const categoryById = useMemo(() => indexById(categories.data), [categories.data])
  const uomById = useMemo(() => indexById(uoms.data), [uoms.data])

  const item = itemQuery.data

  if (itemQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-full max-w-64" />
        <Skeleton className="h-4 w-full max-w-96" />
        <Skeleton className="h-9 w-full max-w-80" />
        <div className="grid gap-4 lg:grid-cols-3">
          <Skeleton className="h-80 lg:col-span-2" />
          <Skeleton className="h-80" />
        </div>
      </div>
    )
  }

  if (itemQuery.error) {
    return (
      <>
        <PageHeader title="Item" breadcrumbs={[{ label: 'Items', to: '/items' }]} />
        <Alert variant="destructive">
          <AlertTitle>Could not load this item</AlertTitle>
          <AlertDescription>{itemQuery.error.message}</AlertDescription>
          <Button
            variant="outline"
            size="sm"
            className="mt-4"
            onClick={() => void itemQuery.refetch()}
          >
            <RefreshCw />
            Try again
          </Button>
        </Alert>
      </>
    )
  }

  if (!item) {
    return (
      <>
        <PageHeader title="Item" breadcrumbs={[{ label: 'Items', to: '/items' }]} />
        <EmptyState
          title="Item not found"
          description="It may have been retired, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/items">Back to items</Link>
            </Button>
          }
        />
      </>
    )
  }

  const categoryLabel = (id: string | null) => {
    if (!id) return 'Uncategorised'
    return categoryById.get(id)?.name ?? '-'
  }
  const uomLabel = (id: string | null) => (id ? (uomById.get(id)?.code ?? '-') : '-')
  const baseUomCode = uomById.get(item.base_uom_id)?.code ?? ''

  const canEdit = can(P.ITEM_UPDATE)
  // Offered only from the two states the lifecycle actually retires from. An
  // ARCHIVED item is refused by the machine, so putting the user through a
  // confirmation for it would spend the decision on nothing.
  const canRetireThis = canRetire && acceptsRetirement(item)

  const tab = searchParams.get('tab') ?? 'overview'

  return (
    <>
      <PageHeader
        title={`${item.code} - ${item.name}`}
        description={humanise(item.item_group)}
        breadcrumbs={[{ label: 'Items', to: '/items' }, { label: item.code }]}
        status={<StatusBadge status={item.lifecycle_state} />}
        actions={
          // Null rather than an empty fragment when the principal may do
          // neither: `PageHeader` renders the action rail on truthiness alone.
          canEdit || canRetireThis ? (
            <>
              {canEdit && (
                <Button variant="outline" onClick={() => setEditOpen(true)}>
                  <Pencil />
                  Edit
                </Button>
              )}
              {canRetireThis && (
                <Button variant="outline" onClick={() => setRetireOpen(true)}>
                  <Trash2 className="text-destructive" />
                  Retire
                </Button>
              )}
            </>
          ) : null
        }
      />

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setSearchParams(
            (current) => {
              const next = new URLSearchParams(current)
              next.set('tab', value)
              return next
            },
            { replace: true },
          )
        }
      >
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="variants">
            <Shapes className="h-3.5 w-3.5" aria-hidden />
            Variants
          </TabsTrigger>
          <TabsTrigger value="stock">
            <Boxes className="h-3.5 w-3.5" aria-hidden />
            Stock
          </TabsTrigger>
          <TabsTrigger value="where-used">
            <Layers className="h-3.5 w-3.5" aria-hidden />
            Where used
          </TabsTrigger>
          <TabsTrigger value="prices">
            <Tags className="h-3.5 w-3.5" aria-hidden />
            Prices
          </TabsTrigger>
          <TabsTrigger value="vendors">
            <Truck className="h-3.5 w-3.5" aria-hidden />
            Vendors
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <OverviewPanel
            item={item}
            categoryLabel={categoryLabel}
            uomLabel={uomLabel}
            canViewCost={canViewCost}
          />
        </TabsContent>

        <TabsContent value="variants">
          {/* No permission branch: listing variants needs `item.view`, which the
              route itself already required to reach this page. */}
          <VariantsPanel
            itemId={item.id}
            canViewCost={canViewCost}
            canRetire={canRetire}
            onRetire={setRetiringVariant}
          />
        </TabsContent>

        <TabsContent value="stock">
          {can(P.INVENTORY_VIEW) ? (
            <StockPanel itemId={item.id} uomCode={baseUomCode} />
          ) : (
            <PanelDenied what="Stock balances" />
          )}
        </TabsContent>

        <TabsContent value="where-used">
          {can(P.BOM_VIEW) ? (
            <WhereUsedPanel itemId={item.id} />
          ) : (
            <PanelDenied what="Bills of materials" />
          )}
        </TabsContent>

        <TabsContent value="prices">
          {can(P.PRICE_VIEW) ? <PricesPanel itemId={item.id} /> : <PanelDenied what="Prices" />}
        </TabsContent>

        <TabsContent value="vendors">
          <VendorsPanel itemId={item.id} canViewVendors={can(P.VENDOR_VIEW)} />
        </TabsContent>
      </Tabs>

      {canEdit && <ItemFormDialog open={editOpen} onOpenChange={setEditOpen} item={item} />}

      {canRetire && (
        <>
          <RetireItemDialog
            item={item}
            uomCode={baseUomCode}
            open={retireOpen}
            onOpenChange={setRetireOpen}
          />
          <RetireVariantDialog
            variant={retiringVariant}
            itemId={item.id}
            open={retiringVariant !== null}
            onOpenChange={(next) => {
              if (!next) setRetiringVariant(null)
            }}
          />
        </>
      )}
    </>
  )
}

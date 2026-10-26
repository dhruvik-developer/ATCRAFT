/**
 * Quality control.
 *
 * The pending tab is the working queue and is deliberately the landing tab: an
 * inspection that has not been done is material sitting in QC hold, which is
 * stock the plant has paid for and cannot issue. Ageing is therefore a first
 * class column rather than something to be worked out from a date - a lot that
 * has been waiting nine days is a production stoppage waiting to happen, and it
 * should be impossible to scan this list without seeing that.
 *
 * Every action is gated on a permission. That gating is UX only: the API
 * authorises each request independently (master rules 38).
 */

import { type ColumnDef } from '@tanstack/react-table'
import {
  BarChart3,
  ClipboardCheck,
  ClipboardList,
  ExternalLink,
  Microscope,
  Plus,
  Wrench,
  X,
} from 'lucide-react'
import { Suspense, lazy, useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, PanelLoader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
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
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { InspectionDialog } from './inspection-dialog'

const RaiseInspectionDialog = lazy(() =>
  import('./raise-inspection-dialog').then((m) => ({ default: m.RaiseInspectionDialog })),
)
const InspectionDetailDialog = lazy(() =>
  import('./inspection-detail-dialog').then((m) => ({ default: m.InspectionDetailDialog })),
)
const QualityReports = lazy(() =>
  import('./quality-reports').then((m) => ({ default: m.QualityReports })),
)
import {
  QC_DECIDED_RESULTS,
  QC_INSPECTION_STATUSES,
  QC_STAGES,
  ageInDays,
  isInspectable,
  inspectionBadgeStatus,
  referenceLink,
  type QcInspectionRow,
} from './quality-types'

/**
 * The pending queue has no key in the shared registry. Nesting it under
 * `quality` means an invalidation of `queryKeys.quality.all` still sweeps it.
 */
const pendingKey = (params: unknown) => ['quality', 'pending', params] as const

/** Days in QC hold before the wait itself becomes the problem. */
const AGEING_WARNING_DAYS = 3
const AGEING_CRITICAL_DAYS = 7

function AgeingCell({ raisedAt }: { raisedAt: string | null }) {
  const days = ageInDays(raisedAt)
  if (days === null) return <span className="text-muted-foreground">-</span>
  if (days >= AGEING_CRITICAL_DAYS) {
    return <Badge variant="destructive">{days} days waiting</Badge>
  }
  if (days >= AGEING_WARNING_DAYS) {
    return <Badge variant="warning">{days} days</Badge>
  }
  return <span className="tabular text-muted-foreground">{days === 0 ? 'Today' : `${days}d`}</span>
}

function ReferenceCell({ inspection }: { inspection: QcInspectionRow }) {
  const to = referenceLink(
    inspection.reference_type,
    inspection.reference_id,
    inspection.reference_code,
  )
  const label = inspection.reference_code ?? humanise(inspection.reference_type)

  if (!to) return <span className="text-muted-foreground">{label}</span>

  return (
    <Link to={to} className="inline-flex items-center gap-1 hover:underline">
      <span className="truncate">{label}</span>
      <ExternalLink className="h-3 w-3 shrink-0 opacity-60" aria-hidden />
    </Link>
  )
}

/**
 * The item an inspection is against.
 *
 * The row carries `item_id` and nothing else - no code, no name, no UoM - so
 * this links to the item master rather than rendering a blank cell. Resolving
 * names here would mean a request per row against an unbounded table.
 */
function ItemCell({ inspection }: { inspection: QcInspectionRow }) {
  return (
    <div className="max-w-[16rem]">
      <Link
        to={`/items/${inspection.item_id}`}
        className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
        title="Open this item"
      >
        {inspection.item_id.slice(0, 8)}
        <ExternalLink className="h-3 w-3 shrink-0 opacity-60" aria-hidden />
      </Link>
      {inspection.batch_id && (
        <span className="block truncate text-xs text-muted-foreground">
          Batch {inspection.batch_id.slice(0, 8)}
        </span>
      )}
    </div>
  )
}

// ------------------------------------------------------------- pending ------

function PendingTab({
  onInspect,
  onOpen,
}: {
  onInspect: (inspection: QcInspectionRow) => void
  onOpen: (inspection: QcInspectionRow) => void
}) {
  const { canAny } = useAuth()
  const [searchParams] = useSearchParams()

  // Gating is UX only - the API authorises the results and decision posts.
  const canInspect = canAny(P.QC_INSPECT, P.QC_DECIDE)

  const stage = searchParams.get('stage') ?? ''

  /**
   * `GET /quality/pending` takes `qc_stage`, `warehouse_id` and the page
   * parameters - nothing else.
   *
   * It has no `search`, no `sort_by` and no `sort_dir`: the queue is ordered
   * oldest-first by the route itself, because the oldest hold is the one
   * blocking a delivery. FastAPI drops query parameters a route does not
   * declare rather than rejecting them, so a search box and sortable headers
   * here would take input, change the URL, refetch, and return exactly the same
   * page - an inspector would conclude the lot was not in the queue. They are
   * therefore not offered. The full history on the next tab does support both.
   */
  const list = useListQuery<QcInspectionRow>({
    path: '/quality/pending',
    queryKey: pendingKey,
    // The queue changes as fast as the gate receives; a cached view here means
    // two inspectors picking up the same lot.
    staleTime: staleTimes.transactional,
    refetchOnWindowFocus: true,
    filters: stage ? { qc_stage: stage } : {},
  })

  const { setParam } = list

  const columns = useMemo<ColumnDef<QcInspectionRow, unknown>[]>(() => {
    const defs: ColumnDef<QcInspectionRow, unknown>[] = [
      {
        id: 'code',
        header: 'Inspection',
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'qc_stage',
        header: 'Stage',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.qc_stage} />,
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => <ItemCell inspection={row.original} />,
      },
      {
        id: 'reference',
        header: 'Against',
        enableSorting: false,
        cell: ({ row }) => <ReferenceCell inspection={row.original} />,
      },
      {
        id: 'inspected_quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.inspected_quantity),
      },
      {
        // How much of the lot is actually being looked at. A sample of 8 from a
        // batch of 4000 is a materially different assurance from a 100% check,
        // and the row now carries it.
        id: 'sample_size',
        header: 'Sample',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-muted-foreground">{formatQuantity(row.original.sample_size)}</span>
        ),
      },
      {
        id: 'created_at',
        header: 'Raised',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.created_at),
      },
      {
        id: 'ageing',
        header: 'Waiting',
        enableSorting: false,
        cell: ({ row }) => <AgeingCell raisedAt={row.original.created_at} />,
      },
    ]

    if (canInspect) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <Button
            variant="outline"
            size="sm"
            onClick={(event) => {
              // The row itself opens the read-only detail; the button must
              // not open both dialogs.
              event.stopPropagation()
              onInspect(row.original)
            }}
          >
            <Microscope />
            Inspect
          </Button>
        ),
      })
    }

    return defs
  }, [canInspect, onInspect])

  return (
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
      onRowClick={onOpen}
      getRowId={(row) => row.id}
      emptyTitle="Nothing waiting for inspection"
      emptyDescription="Every lot that needs a decision has had one. New receipts, production entries and packing starts raise inspections automatically - or raise one by hand."
      toolbar={
        <>
          <Select
            value={stage || SELECT_ALL}
            onValueChange={(value) => setParam('stage', value === SELECT_ALL ? undefined : value)}
          >
            <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by stage">
              <SelectValue placeholder="All stages" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All stages</SelectItem>
              {QC_STAGES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {stage && (
            <Button variant="ghost" size="sm" onClick={() => setParam('stage', undefined)}>
              <X />
              Clear
            </Button>
          )}

          {/* The queue endpoint offers no search, so say where searching lives
              rather than leaving a gap where a search box used to sit. */}
          <span className="text-xs text-muted-foreground">
            Oldest first. Search the full history on the next tab.
          </span>
        </>
      }
    />
  )
}

// --------------------------------------------------------- all inspections --

function InspectionsTab({
  onInspect,
  onOpen,
  reworkOnly,
}: {
  onInspect: (inspection: QcInspectionRow) => void
  onOpen: (inspection: QcInspectionRow) => void
  reworkOnly?: boolean
}) {
  const { canAny } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canInspect = canAny(P.QC_INSPECT, P.QC_DECIDE)

  const stage = searchParams.get('stage') ?? ''
  const result = searchParams.get('result') ?? ''
  const status = searchParams.get('status') ?? ''

  const list = useListQuery<QcInspectionRow>({
    path: '/quality/inspections',
    queryKey: queryKeys.quality.inspections,
    defaultSortBy: 'inspected_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(stage ? { qc_stage: stage } : {}),
      ...(reworkOnly ? { rework_only: true } : result ? { result } : {}),
      ...(reworkOnly && status ? { status } : {}),
    },
  })

  const { setParam } = list

  /**
   * Both filter keys, written once.
   *
   * react-router resolves even the functional form of `setSearchParams`
   * against the `searchParams` captured for *this* render rather than against
   * an accumulator, so `setParam('stage', ...)` followed by
   * `setParam('result', ...)` both start from the same snapshot and the second
   * navigation discards the first. Clearing the two filters cleared only the
   * result. Compute the next params once, write them once.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        params.delete('stage')
        params.delete('result')
        params.delete('status')
        params.set('page', '1')
        return params
      },
      { replace: true },
    )
  }, [setSearchParams])

  const filterCount = (stage ? 1 : 0) + (result ? 1 : 0) + (status ? 1 : 0)

  const columns = useMemo<ColumnDef<QcInspectionRow, unknown>[]>(() => {
    const defs: ColumnDef<QcInspectionRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Inspection',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'qc_stage',
        header: 'Stage',
        cell: ({ row }) => <StatusBadge status={row.original.qc_stage} />,
      },
      {
        id: 'status',
        header: 'Outcome',
        enableSorting: false,
        // `result` is never null - an undecided inspection carries PENDING - so
        // the status is shown only while the result says nothing yet.
        cell: ({ row }) => <StatusBadge status={inspectionBadgeStatus(row.original)} />,
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => <ItemCell inspection={row.original} />,
      },
      {
        id: 'reference',
        header: 'Against',
        enableSorting: false,
        cell: ({ row }) => <ReferenceCell inspection={row.original} />,
      },
      {
        id: 'inspected_quantity',
        header: 'Inspected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.inspected_quantity),
      },
      {
        id: 'accepted_quantity',
        header: 'Accepted',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-success">{formatQuantity(row.original.accepted_quantity)}</span>
        ),
      },
      {
        id: 'rejected_quantity',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-destructive">{formatQuantity(row.original.rejected_quantity)}</span>
        ),
      },
      {
        id: 'rework_quantity',
        header: 'Rework',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-warning">{formatQuantity(row.original.rework_quantity)}</span>
        ),
      },
      {
        // Rejected material that has to go back to the vendor is a debit note
        // and a return challan waiting to be raised, not just a number.
        id: 'return_required',
        header: 'Return',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.return_required ? (
            <Badge variant="warning">To vendor</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        accessorKey: 'inspected_at',
        header: 'Decided',
        cell: ({ row }) =>
          row.original.inspected_at ? (
            <span className="text-xs text-muted-foreground">
              {formatDate(row.original.inspected_at)}
            </span>
          ) : (
            <span className="text-xs text-warning">Undecided</span>
          ),
      },
    ]

    if (canInspect) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          isInspectable(row.original.status) ? (
            <Button
              variant="outline"
              size="sm"
              onClick={(event) => {
                event.stopPropagation()
                onInspect(row.original)
              }}
            >
              <Microscope />
              Inspect
            </Button>
          ) : null,
      })
    }

    return defs
  }, [canInspect, onInspect])

  return (
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
      searchPlaceholder="Search inspection, item or reference..."
      onRowClick={onOpen}
      getRowId={(row) => row.id}
      emptyTitle={reworkOnly ? 'No rework on the bench' : 'No inspections found'}
      emptyDescription={
        reworkOnly
          ? 'Nothing is holding quantity for rework. Lots the verdict sends for rework land here.'
          : 'Nothing matches the current filters. Clear them to see the full history.'
      }
      toolbar={
        <>
          <Select
            value={stage || SELECT_ALL}
            onValueChange={(value) => setParam('stage', value === SELECT_ALL ? undefined : value)}
          >
            <SelectTrigger className="w-full sm:w-[180px]" aria-label="Filter by stage">
              <SelectValue placeholder="All stages" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={SELECT_ALL}>All stages</SelectItem>
              {QC_STAGES.map((value) => (
                <SelectItem key={value} value={value}>
                  {humanise(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {reworkOnly ? (
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {QC_INSPECTION_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <Select
              value={result || SELECT_ALL}
              onValueChange={(value) =>
                setParam('result', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by result">
                <SelectValue placeholder="All results" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All results</SelectItem>
                {QC_DECIDED_RESULTS.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}

          {filterCount > 0 && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              <X />
              Clear
            </Button>
          )}
        </>
      }
    />
  )
}

// ------------------------------------------------------------------ page ----

const TABS = [
  { value: 'pending', label: 'Pending inspections', icon: ClipboardCheck },
  { value: 'inspections', label: 'All inspections', icon: ClipboardList },
  { value: 'rework', label: 'Rework', icon: Wrench },
  { value: 'reports', label: 'Reports', icon: BarChart3 },
] as const

export default function QualityPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [inspecting, setInspecting] = useState<QcInspectionRow | null>(null)
  const [viewing, setViewing] = useState<QcInspectionRow | null>(null)
  const [raising, setRaising] = useState(false)

  const tab = searchParams.get('tab') ?? 'pending'

  const changeTab = (next: string) => {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        params.set('tab', next)
        // Page 4 of the pending queue is meaningless once the table below is
        // the full history, and so is a filter the next list does not offer.
        for (const key of ['page', 'q', 'sort', 'dir', 'stage', 'result', 'status'])
          params.delete(key)
        return params
      },
      { replace: true },
    )
  }

  return (
    <div>
      <PageHeader
        title="Quality"
        description="Material waiting on a decision, the inspections already made, what they sent back for rework, and what they say about the items and vendors behind them."
        actions={
          can(P.QC_INSPECT) ? (
            <Button onClick={() => setRaising(true)}>
              <Plus />
              Raise inspection
            </Button>
          ) : undefined
        }
      />

      <Tabs value={tab} onValueChange={changeTab}>
        <TabsList>
          {TABS.map(({ value, label, icon: Icon }) => (
            <TabsTrigger key={value} value={value}>
              <Icon className="h-4 w-4" aria-hidden />
              {label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="pending">
          <PendingTab onInspect={setInspecting} onOpen={setViewing} />
        </TabsContent>
        <TabsContent value="inspections">
          <InspectionsTab onInspect={setInspecting} onOpen={setViewing} />
        </TabsContent>
        <TabsContent value="rework">
          <InspectionsTab onInspect={setInspecting} onOpen={setViewing} reworkOnly />
        </TabsContent>
        <TabsContent value="reports">
          {/* The only charting tab here, and not the default one - lazy so
              recharts stays out of the quality page's chunk. */}
          <Suspense fallback={<PanelLoader />}>
            <QualityReports />
          </Suspense>
        </TabsContent>
      </Tabs>

      <InspectionDialog
        inspection={inspecting}
        open={Boolean(inspecting)}
        onOpenChange={(open) => {
          if (!open) setInspecting(null)
        }}
      />

      <Suspense fallback={null}>
        <InspectionDetailDialog
          inspection={viewing}
          open={Boolean(viewing)}
          onOpenChange={(open) => {
            if (!open) setViewing(null)
          }}
          onInspect={setInspecting}
        />
      </Suspense>

      <Suspense fallback={null}>
        <RaiseInspectionDialog
          open={raising}
          onOpenChange={setRaising}
          onRaised={(inspection) => setViewing(inspection)}
        />
      </Suspense>
    </div>
  )
}

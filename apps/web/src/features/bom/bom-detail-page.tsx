/**
 * One bill of materials, in four views.
 *
 * The tab that matters is "Exploded requirements". The engine consolidates a
 * component that appears in several branches into ONE row carrying the merged
 * quantity (docs/database.md section 5), and a screen that quietly showed a
 * merged row as if it came from a single place would hide the most useful thing
 * the engine does. So every merged row says how many sub-assemblies it came
 * from, and the contributing paths are one keystroke away.
 *
 * Each tab loads its own data: opening a bill must not fetch a tree, an
 * explosion, a cost roll-up and a where-used list before the page can paint.
 *
 * A bill leaves service in one of two ways, and the header offers them as two
 * separate buttons because they are two separate things. "Mark obsolete" retires
 * an APPROVED bill: the record stays, planning stops choosing it, and every
 * order raised against it still resolves. "Delete draft" removes a DRAFT that
 * was never released - the row, its lines and its operations go, and only the
 * audit entry remains. The statuses that permit them do not overlap, so at most
 * one of the two is ever on screen.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  BadgeIndianRupee,
  CalendarClock,
  Copy,
  GitBranch,
  Layers,
  Merge,
  Pencil,
  RefreshCw,
  Send,
  ShieldCheck,
  Split,
  Trash2,
  XCircle,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  PageHeader,
  StatTile,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useItemFacts } from '@/features/dispatch/dispatch-types'
import { useAuth } from '@/features/auth/auth-context'
import { optionLabel, useWorkCentreOptions } from '@/features/production/work-order-model'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatQuantity,
  humanise,
} from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { CopyBomDialog, BomEditDialog } from './bom-dialogs'
import {
  isApprovable,
  isDeletable,
  isEditable,
  isObsoletable,
  isRejectable,
  isSubmittable,
  type BomCost,
  type BomCostComponent,
  type BomDetail,
  type BomExplosion,
  type BomOperation,
  type BomRow,
  type BomWhereUsedRow,
  type ExplodedComponent,
} from './bom-model'
import { BomStructure } from './bom-tree'

const QUANTITY_PATTERN = /^\d{1,12}(\.\d{1,4})?$/

/** The four status moves, all of which take a recorded reason. */
type StatusAction = 'submit' | 'approve' | 'reject' | 'obsolete'

/**
 * Everything the confirmation can be collecting a reason for.
 *
 * Deletion joins the four status moves here because it asks the user for the
 * same thing, and nowhere else. It is a DELETE rather than a POST, and its
 * reason travels in the QUERY STRING rather than a body - `ReasonQuery` in the
 * BOM router says so, and says why: a DELETE body is legal and proxies drop it.
 * Sending it as a body instead is a 422 at the moment of use that no build
 * catches, so the two carriers are separated once, in `runCommand`.
 */
type BomCommand = StatusAction | 'delete'

/** The toast headline for each status move, from the shared catalogue. */
const COMMAND_TOAST: Record<StatusAction, (noun: string) => string> = {
  submit: success.submitted,
  approve: success.approved,
  reject: success.rejected,
  obsolete: (noun) => success.statusChanged(noun, 'OBSOLETE'),
}

const COMMAND_COPY: Record<BomCommand, { title: string; description: string; confirm: string }> = {
  submit: {
    title: 'Submit',
    description:
      'The bill goes forward for approval and stops accepting line edits. An approver decides from here.',
    confirm: 'Submit',
  },
  approve: {
    title: 'Approve',
    description:
      'An approved bill becomes available to MRP, production orders and costing. Approval is recorded against your account.',
    confirm: 'Approve',
  },
  reject: {
    title: 'Reject',
    description:
      'The bill goes back to draft so its lines can be corrected. The reason is what the author reads.',
    confirm: 'Reject',
  },
  obsolete: {
    title: 'Mark obsolete',
    description:
      'The bill stops being selected for new planning and production. Documents that already reference this version keep it, so history stays intact. If it was the default for its item, it stops being the default too, which frees the slot for a replacement.',
    confirm: 'Mark obsolete',
  },
  // Said plainly, and said differently from "Mark obsolete" above: this one
  // does not retire a record, it destroys one. It is offered on a draft alone
  // because a draft has never been released to anything that could point at it.
  delete: {
    title: 'Delete draft',
    description:
      'The draft is removed permanently, together with its lines and operations. Nothing is retired and nothing is kept: only the audit entry recording the deletion survives. A draft that alternate structures point at is refused by the API even so.',
    confirm: 'Delete draft',
  },
}

function PanelDenied({ what }: { what: string }) {
  return (
    <EmptyState
      title={`${what} are not visible to your account`}
      description="Your role does not include the permission this panel needs. Ask an administrator if you believe this is wrong."
    />
  )
}

function PanelError({ error, onRetry }: { error: Error; onRetry: () => void }) {
  return (
    <Alert variant="destructive">
      <AlertTitle>Could not load this panel</AlertTitle>
      <AlertDescription>{describeError(error)}</AlertDescription>
      <Button variant="outline" size="sm" className="mt-4" onClick={onRetry}>
        <RefreshCw />
        Try again
      </Button>
    </Alert>
  )
}

function RowSkeletons({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2 rounded-lg border p-3">
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className="h-8 w-full" />
      ))}
    </div>
  )
}

// ------------------------------------------------------------- structure ----

/**
 * The bill's own single level of lines.
 *
 * There is no nested-structure endpoint: `GET /bom/{id}` returns this bill's
 * lines and nothing below them, and the explosion is consolidated by item, so a
 * faithful multi-level tree cannot be rebuilt from either without inventing
 * parentage. What this panel does instead is name the components - the lines
 * carry `component_item_id` and no part number, and the explosion is the only
 * endpoint in the module that returns codes and names, so its level-1 rows are
 * used as a lookup. That lookup is an enhancement, never a dependency: if it
 * fails the lines still render.
 */
function StructurePanel({ bom }: { bom: BomDetail }) {
  const lines = useMemo(
    () => [...(bom.lines ?? [])].sort((a, b) => a.line_number - b.line_number),
    [bom.lines],
  )
  const operations = useMemo(
    () => [...(bom.operations ?? [])].sort((a, b) => a.operation_sequence - b.operation_sequence),
    [bom.operations],
  )
  const workCentres = useWorkCentreOptions(operations.length > 0)
  const centreById = useMemo(() => {
    const map = new Map<string, string>()
    for (const centre of workCentres.data ?? []) map.set(centre.id, optionLabel(centre))
    return map
  }, [workCentres.data])

  const names = useQuery({
    queryKey: queryKeys.bom.explosion(bom.id, bom.output_quantity),
    queryFn: () =>
      api.get<BomExplosion>(`/bom/${bom.id}/explode`, {
        params: { quantity: bom.output_quantity },
      }),
    enabled: lines.length > 0,
    staleTime: staleTimes.master,
  })

  const nameByItemId = useMemo(() => {
    const map = new Map<string, ExplodedComponent>()
    for (const component of names.data?.components ?? []) map.set(component.item_id, component)
    return map
  }, [names.data])

  if (lines.length === 0) {
    return (
      <EmptyState
        icon={GitBranch}
        title="This bill has no components yet"
        description="A draft bill starts empty. Add component lines before submitting it for approval."
      />
    )
  }

  return (
    <div className="space-y-3">
      <Alert variant="info">
        <AlertTitle>This bill&apos;s own components, one level down</AlertTitle>
        <AlertDescription>
          Quantities here are per {formatQuantity(bom.output_quantity)} of the finished item, as the
          bill was written. Sub-assemblies carry their own bills; for the components of the whole
          structure at a real order size, use Exploded requirements.
        </AlertDescription>
      </Alert>
      <BomStructure lines={lines} names={nameByItemId} label={bom.code} />
      <OperationsPanel operations={operations} centreById={centreById} />
    </div>
  )
}

/**
 * The labour half of the bill, as stored - not as the routing currently reads.
 *
 * `bom_operations` is a snapshot: approving a bill without operations copies
 * the item's default routing in, and later routing edits do not rewrite it.
 * An empty bill costs material only, which is exactly what the Cost tab will
 * show until steps are added here or snapshotted at approval.
 */
function OperationsPanel({
  operations,
  centreById,
}: {
  operations: BomOperation[]
  centreById: Map<string, string>
}) {
  const columns = useMemo<ColumnDef<BomOperation, unknown>[]>(
    () => [
      {
        id: 'operation_sequence',
        header: 'Step',
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.operation_sequence}</span>,
      },
      {
        id: 'name',
        header: 'Operation',
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        id: 'work_center',
        header: 'Work centre',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.work_center_id ? (
            (centreById.get(row.original.work_center_id) ?? 'Assigned')
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'setup_time_minutes',
        header: 'Setup (min)',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.setup_time_minutes),
      },
      {
        id: 'run_time_minutes_per_unit',
        header: 'Run / unit (min)',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.run_time_minutes_per_unit),
      },
      {
        id: 'cost_per_hour',
        header: 'Rate / hour',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.cost_per_hour),
      },
      {
        id: 'fixed_cost',
        header: 'Fixed charge',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.fixed_cost),
      },
      {
        id: 'is_outsourced',
        header: '',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_outsourced ? (
            <Badge variant="warning">Job work</Badge>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ],
    [centreById],
  )

  if (operations.length === 0) {
    return (
      <Alert variant="info">
        <AlertTitle>No operations on this bill</AlertTitle>
        <AlertDescription>
          Material only, until steps are added on a draft or snapshotted from the item&apos;s
          default routing at approval.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Operations</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={operations}
          getRowId={(row) => row.id}
          emptyTitle="No operations"
        />
      </CardContent>
    </Card>
  )
}

// -------------------------------------------------------------- explosion ---

/** Immediate parents of a component, taken from the paths that reached it. */
function parentsOf(component: ExplodedComponent): string[] {
  const paths = component.source_paths ?? []
  const parents = new Set<string>()
  for (const path of paths) {
    const parent = path.length >= 2 ? path[path.length - 2] : path[0]
    if (parent) parents.add(parent)
  }
  return [...parents]
}

function SourcePathsCell({ component }: { component: ExplodedComponent }) {
  const paths = component.source_paths ?? []
  const parents = parentsOf(component)
  const merged = parents.length > 1

  if (paths.length === 0) return <span className="text-muted-foreground">-</span>

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-auto max-w-[15rem] justify-start gap-1.5 whitespace-normal px-1.5 py-1 text-left"
          aria-label={`Show the ${paths.length} path(s) that contribute to ${component.item_code}`}
        >
          {merged ? (
            <>
              <Merge className="text-primary" aria-hidden />
              <span className="text-xs font-medium text-primary">
                appears in {parents.length} sub-assemblies
              </span>
            </>
          ) : (
            <>
              <Split className="text-muted-foreground" aria-hidden />
              <span className="text-xs text-muted-foreground">{parents[0]}</span>
            </>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" collisionPadding={16} className="w-96 max-w-[calc(100vw-2rem)]">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Contributing paths
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {merged
            ? 'These branches were merged into the single quantity shown on the row.'
            : 'This component reaches the bill through one branch.'}
        </p>
        <ol className="mt-3 space-y-2">
          {paths.map((path, index) => (
            <li key={index} className="text-xs leading-relaxed">
              <span className="mr-1.5 text-muted-foreground">{index + 1}.</span>
              {path.map((step, stepIndex) => (
                <span key={stepIndex}>
                  {stepIndex > 0 && <span className="mx-1 text-muted-foreground">&rsaquo;</span>}
                  <span
                    className={
                      stepIndex === path.length - 1 ? 'font-medium' : 'text-muted-foreground'
                    }
                  >
                    {step}
                  </span>
                </span>
              ))}
            </li>
          ))}
        </ol>
      </PopoverContent>
    </Popover>
  )
}

function ExplosionPanel({ bom }: { bom: BomDetail }) {
  const [input, setInput] = useState(bom.output_quantity)
  const [quantity, setQuantity] = useState(bom.output_quantity)

  // One request per keystroke would put a full multi-level traversal behind
  // every digit typed.
  useEffect(() => {
    const timer = setTimeout(() => setQuantity(input.trim()), 400)
    return () => clearTimeout(timer)
  }, [input])

  const isValid = QUANTITY_PATTERN.test(quantity) && quantity !== '0'

  const explosion = useQuery({
    queryKey: queryKeys.bom.explosion(bom.id, quantity),
    queryFn: () => api.get<BomExplosion>(`/bom/${bom.id}/explode`, { params: { quantity } }),
    enabled: isValid,
    placeholderData: keepPreviousData,
    staleTime: staleTimes.master,
  })

  const components = useMemo(() => explosion.data?.components ?? [], [explosion.data])
  const mergedCount = useMemo(
    // `source_path_count` is exact even when the listing of paths was capped, so
    // it - not the length of the list - decides whether a row is a merge.
    () => components.filter((component) => component.source_path_count > 1).length,
    [components],
  )
  const deepestLevel = useMemo(
    () => components.reduce((deepest, component) => Math.max(deepest, component.level), 0),
    [components],
  )

  const columns = useMemo<ColumnDef<ExplodedComponent, unknown>[]>(
    () => [
      {
        id: 'level',
        header: 'Lvl',
        enableSorting: false,
        meta: { align: 'center' },
        cell: ({ row }) => (
          <span className="tabular text-muted-foreground">{row.original.level}</span>
        ),
      },
      {
        id: 'item_code',
        header: 'Component',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.item_id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.item_code}
          </Link>
        ),
      },
      {
        id: 'item_name',
        header: 'Description',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate text-muted-foreground">
            {row.original.item_name}
          </span>
        ),
      },
      {
        id: 'total_quantity',
        header: 'Total required',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatQuantity(row.original.total_quantity)}</span>
        ),
      },
      {
        id: 'scrap_adjusted_quantity',
        header: 'With scrap',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.scrap_adjusted_quantity),
      },
      {
        id: 'sourcing',
        header: 'Sourcing',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex flex-wrap items-center gap-1">
            {row.original.is_manufactured && <Badge variant="default">Made</Badge>}
            {row.original.is_purchased && <Badge variant="secondary">Bought</Badge>}
            {!row.original.is_manufactured && !row.original.is_purchased && (
              <span className="text-muted-foreground">-</span>
            )}
          </span>
        ),
      },
      {
        id: 'source_paths',
        header: 'Where it comes from',
        enableSorting: false,
        cell: ({ row }) => <SourcePathsCell component={row.original} />,
      },
    ],
    [],
  )

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Explode for an order quantity</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <Label htmlFor="explode-quantity">Quantity to explode</Label>
              <Input
                id="explode-quantity"
                inputMode="decimal"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                aria-invalid={!isValid}
                aria-describedby="explode-quantity-hint"
                className="mt-1.5 w-40"
              />
            </div>
            <p id="explode-quantity-hint" className="pb-2 text-xs text-muted-foreground">
              {isValid
                ? `Requirements below are for ${formatQuantity(quantity)}.`
                : 'Enter a quantity above zero with up to 4 decimal places.'}
            </p>
          </div>

          <Alert variant="info">
            <AlertTitle>One row per item, merged across every branch</AlertTitle>
            <AlertDescription>
              A component used in more than one sub-assembly is shown once, with the quantities from
              all of its branches added together - so a screw used twice in one handle and four
              times in a lock body appears as a single line for six, not as two lines. Open the last
              column on any merged row to see exactly which branches were combined.
            </AlertDescription>
          </Alert>
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Distinct components"
          value={components.length}
          hint="After consolidation"
          icon={Layers}
          isLoading={explosion.isLoading}
        />
        <StatTile
          label="Merged rows"
          value={mergedCount}
          hint="Reached through more than one branch"
          icon={Merge}
          tone={mergedCount > 0 ? 'success' : 'default'}
          isLoading={explosion.isLoading}
        />
        <StatTile
          label="Deepest level"
          value={explosion.data?.max_level_reached ?? deepestLevel}
          hint="Levels traversed below the finished item"
          icon={GitBranch}
          isLoading={explosion.isLoading}
        />
      </div>

      {/* A phantom is dissolved on the way through: it never appears as a
          component of its own, its children do. Saying which ones vanished is
          the difference between a correct explosion and one that looks like it
          lost a sub-assembly (docs/assumptions.md 12). */}
      {(explosion.data?.phantom_item_codes.length ?? 0) > 0 && (
        <Alert variant="info">
          <AlertTitle>
            {explosion.data?.phantom_item_codes.length} phantom sub-assembl
            {explosion.data?.phantom_item_codes.length === 1 ? 'y was' : 'ies were'} passed through
          </AlertTitle>
          <AlertDescription>
            {explosion.data?.phantom_item_codes.join(', ')} carry no stock of their own, so their
            components appear directly in the list below rather than as a line of their own.
          </AlertDescription>
        </Alert>
      )}

      <DataTable
        columns={columns}
        data={components}
        isLoading={explosion.isLoading}
        isFetching={explosion.isFetching}
        error={explosion.error as Error | null}
        onRefresh={() => void explosion.refetch()}
        getRowId={(row) => row.item_id}
        emptyTitle={isValid ? 'Nothing to explode' : 'Enter a quantity'}
        emptyDescription={
          isValid
            ? 'This bill resolves to no components at the requested quantity.'
            : 'The explosion runs once the quantity above is a number greater than zero.'
        }
      />
    </div>
  )
}

// -------------------------------------------------------------------- cost --

function CostPanel({ bomId }: { bomId: string }) {
  const cost = useQuery({
    queryKey: [...queryKeys.bom.detail(bomId), 'cost'],
    queryFn: () => api.get<BomCost>(`/bom/${bomId}/cost`),
    staleTime: staleTimes.master,
  })

  const componentColumns = useMemo<ColumnDef<BomCostComponent, unknown>[]>(
    () => [
      {
        id: 'item_code',
        header: 'Component',
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium">{row.original.item_code}</span>,
      },
      {
        id: 'quantity_per_unit',
        header: 'Qty per unit',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.quantity_per_unit),
      },
      {
        id: 'unit_cost',
        header: 'Unit cost',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatMoney(row.original.unit_cost),
      },
      {
        id: 'cost_source',
        header: 'Rate from',
        enableSorting: false,
        // Which valuation the rate came from decides how much a roll-up is worth
        // arguing with, so it is shown rather than left in the payload.
        cell: ({ row }) => (
          <span className="text-muted-foreground">{humanise(row.original.cost_source)}</span>
        ),
      },
      {
        id: 'extended_cost',
        header: 'Extended',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">{formatMoney(row.original.extended_cost)}</span>
        ),
      },
    ],
    [],
  )

  if (cost.isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-24" />
          ))}
        </div>
        <RowSkeletons />
      </div>
    )
  }

  if (cost.error) {
    return <PanelError error={cost.error as Error} onRetry={() => void cost.refetch()} />
  }

  const data = cost.data
  if (!data) {
    return (
      <EmptyState
        icon={BadgeIndianRupee}
        title="No cost roll-up available"
        description="This bill has not been costed yet. Costs appear once its components carry a standard cost."
      />
    )
  }

  const components = data.components ?? []
  const uncosted = data.items_without_cost ?? []

  return (
    <div className="space-y-5">
      {/* A roll-up over items that carry no standard cost is an understatement,
          not an answer. Naming them is what stops it being quoted as one. */}
      {uncosted.length > 0 && (
        <Alert variant="warning">
          <AlertTitle>
            {uncosted.length} item{uncosted.length === 1 ? '' : 's'} in this structure carr
            {uncosted.length === 1 ? 'ies' : 'y'} no cost
          </AlertTitle>
          <AlertDescription>
            {uncosted.join(', ')} contributed nothing to the figures below, so the total is a floor
            rather than the full cost of the bill.
          </AlertDescription>
        </Alert>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Material cost"
          value={formatMoney(data.material_cost)}
          hint="Components, rolled up through every level"
        />
        <StatTile
          label="Operation cost"
          value={formatMoney(data.operation_cost)}
          hint="Setup and run time at work-centre rates"
        />
        <StatTile
          label="Total cost"
          value={formatMoney(data.total_cost)}
          hint={`For an output of ${formatQuantity(data.output_quantity)}`}
        />
        <StatTile
          label="Cost per unit"
          value={formatMoney(data.unit_cost)}
          hint="Total divided by the output quantity"
          tone="success"
        />
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Material breakdown</h2>
        <DataTable
          columns={componentColumns}
          data={components}
          getRowId={(row) => row.item_id}
          emptyTitle="No component costs"
          emptyDescription="The roll-up returned no costed components for this bill."
        />
      </section>
    </div>
  )
}

// -------------------------------------------------------------- where used --

function WhereUsedPanel({ itemId }: { itemId: string }) {
  const [page, setPage] = useState(1)

  // `useListQuery` is deliberately not reused here: it keeps its state in the
  // URL, and this panel shares the query string with the tab strip and the
  // explosion quantity.
  const query = useQuery({
    queryKey: [...queryKeys.bom.whereUsed(itemId), page],
    queryFn: () =>
      api.getPage<BomWhereUsedRow>(`/bom/where-used/${itemId}`, {
        params: { page, limit: LOOKUP_LIST_LIMIT },
      }),
    placeholderData: keepPreviousData,
    staleTime: staleTimes.master,
  })

  const columns = useMemo<ColumnDef<BomWhereUsedRow, unknown>[]>(
    () => [
      {
        id: 'parent_item_code',
        header: 'Used in',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/bom/${row.original.bom_id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.parent_item_code}
          </Link>
        ),
      },
      {
        id: 'parent_item_name',
        header: 'Description',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[20rem] truncate text-muted-foreground">
            {row.original.parent_item_name}
          </span>
        ),
      },
      {
        id: 'bom_code',
        header: 'BOM',
        enableSorting: false,
        cell: ({ row }) => (
          <span>
            {row.original.bom_code}
            <span className="ml-1 text-muted-foreground">v{row.original.bom_version}</span>
          </span>
        ),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <StatusBadge status={row.original.bom_status} />
            {row.original.is_default && <Badge variant="success">Default</Badge>}
          </span>
        ),
      },
      {
        id: 'line_number',
        header: 'Line',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{row.original.line_number}</span>,
      },
      {
        id: 'level',
        header: 'Level',
        enableSorting: false,
        meta: { align: 'right' },
        // The where-used walk can climb more than one level when asked to, so
        // the level says how far above this item the parent sits.
        cell: ({ row }) => <span className="tabular">{row.original.level}</span>,
      },
      {
        id: 'quantity_per',
        header: 'Qty per',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.quantity_per),
      },
    ],
    [],
  )

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Every bill that lists this item as a component. Change the item and each of these parents
        inherits the change at its next explosion.
      </p>
      <DataTable
        columns={columns}
        data={query.data?.data ?? []}
        pagination={query.data?.pagination}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error as Error | null}
        onPageChange={setPage}
        onRefresh={() => void query.refetch()}
        getRowId={(row) => row.bom_id}
        emptyTitle="Not used in any bill"
        emptyDescription="No other bill of materials lists this item as a component."
      />
    </div>
  )
}

// -------------------------------------------------------------------- page --

export default function BomDetailPage() {
  const { bomId = '' } = useParams<{ bomId: string }>()
  const { can } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [action, setAction] = useState<BomCommand | null>(null)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  const [copyOpen, setCopyOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)

  const reasonTooShort = reason.trim().length < REASON_MIN_LENGTH

  const bomQuery = useQuery({
    queryKey: queryKeys.bom.detail(bomId),
    queryFn: () => api.get<BomDetail>(`/bom/${bomId}`),
    staleTime: staleTimes.master,
    enabled: Boolean(bomId),
  })

  // The bill carries `item_id` and nothing else about the item, so this page had
  // no way to say which product it is for - it offered "Open item master" and
  // left the planner to click through. `useItemFacts` reads the item master by
  // id: the same endpoint the despatch and packing screens already use for the
  // same reason, so no field is invented and nothing changes on the server.
  //
  // Declared here, above the loading and error returns, because hooks must run
  // in the same order on every render - and read off the query rather than the
  // narrowed `bom`, which does not exist until after those returns. The hook is
  // disabled on an empty id, so it simply does not fire while the bill loads.
  const item = useItemFacts(bomQuery.data?.item_id ?? '', can(P.ITEM_VIEW))

  // Read out of the query before the mutation, because a deletion answers with
  // nothing: without this the confirmation toast could not name what it removed.
  const bomCode = bomQuery.data?.code ?? ''

  /**
   * All four status moves take the same body - a recorded reason - and answer
   * with the summary rather than the detail. Posting no body at all is a 422:
   * the request body is required on every one of them.
   *
   * Deletion is the exception on both counts and is branched here rather than
   * at the button: its reason is a query parameter, and `ApiResponse[None]`
   * hands back no bill, because there is no longer a bill to hand back.
   */
  const runCommand = useMutation({
    mutationFn: async ({ key, why }: { key: BomCommand; why: string }): Promise<BomRow | null> => {
      if (key === 'delete') {
        await api.delete(`/bom/${bomId}`, { params: { reason: why } })
        return null
      }
      return api.post<BomRow>(`/bom/${bomId}/${key}`, { reason: why })
    },
    onSuccess: (updated, { key }) => {
      // One prefix invalidation covers the list, this detail, the where-used
      // panels on the item master and the BOM picker on a production order -
      // they all hang off `queryKeys.bom`.
      void queryClient.invalidateQueries({ queryKey: queryKeys.bom.all })
      setAction(null)
      setReason('')
      setReasonTouched(false)

      if (key === 'delete' || !updated) {
        toast.success(
          success.deleted('Draft'),
          `${bomCode} was deleted, along with its lines and operations.`,
        )
        // `GET /bom/{id}` now answers 404. Replacing the history entry keeps
        // Back from returning to a page that can only fail.
        navigate('/bom', { replace: true })
        return
      }

      toast.success(
        COMMAND_TOAST[key]('BOM'),
        `${updated.code} is now ${humanise(updated.status).toLowerCase()}.`,
      )
    },
  })

  if (bomQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-72" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <Skeleton className="h-9 w-full max-w-sm" />
        <RowSkeletons rows={8} />
      </div>
    )
  }

  if (bomQuery.error) {
    return (
      <>
        <PageHeader
          title="Bill of materials"
          breadcrumbs={[{ label: 'Bills of Material', to: '/bom' }]}
        />
        <Alert variant="destructive">
          <AlertTitle>Could not load this bill</AlertTitle>
          <AlertDescription>{describeError(bomQuery.error)}</AlertDescription>
          <Button
            variant="outline"
            size="sm"
            className="mt-4"
            onClick={() => void bomQuery.refetch()}
          >
            <RefreshCw />
            Try again
          </Button>
        </Alert>
      </>
    )
  }

  const bom = bomQuery.data
  if (!bom) {
    return (
      <>
        <PageHeader
          title="Bill of materials"
          breadcrumbs={[{ label: 'Bills of Material', to: '/bom' }]}
        />
        <EmptyState
          title="Bill not found"
          description="It may have been removed, or the link may be wrong."
          action={
            <Button variant="outline" asChild>
              <Link to="/bom">Back to bills of material</Link>
            </Button>
          }
        />
      </>
    )
  }

  const tab = searchParams.get('tab') ?? 'structure'
  const setTab = (value: string) =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        next.set('tab', value)
        return next
      },
      { replace: true },
    )

  // A draft is submitted, a submitted bill is approved or rejected, an approved
  // one is retired. Offering a move the state machine forbids only produces a
  // 409 the user cannot act on.
  const canSubmit = can(P.BOM_UPDATE) && isSubmittable(bom.status)
  const canApprove = can(P.BOM_APPROVE) && isApprovable(bom.status)
  const canReject = can(P.BOM_APPROVE) && isRejectable(bom.status)
  // Only a draft still accepts line edits, and the API enforces the same rule:
  // PATCH on any other status is refused by name.
  const canEdit = can(P.BOM_UPDATE) && isEditable(bom.status)
  // Retiring and deleting are both `bom.obsolete`, not `bom.update`: that is
  // what `POST /bom/{id}/obsolete` and `DELETE /bom/{id}` declare in the BOM
  // router. Deletion has no right of its own in the registry, and the API
  // reasons that destroying a structure is closer to retiring one than to
  // editing it - an engineer entitled to change a draft is not thereby
  // entitled to erase it.
  const canObsolete = can(P.BOM_OBSOLETE) && isObsoletable(bom.status)
  const canDeleteDraft = can(P.BOM_OBSOLETE) && isDeletable(bom.status)

  return (
    <>
      <PageHeader
        title={bom.code}
        description={
          (item.data ? `${item.data.code} - ${item.data.name} - ` : '') +
          `Version ${bom.version}${bom.revision ? ` rev ${bom.revision}` : ''} - ` +
          `${humanise(bom.bom_type)} - output ${formatQuantity(bom.output_quantity)}`
        }
        breadcrumbs={[{ label: 'Bills of Material', to: '/bom' }, { label: bom.code }]}
        status={
          <span className="flex items-center gap-2">
            <StatusBadge status={bom.status} />
            {bom.is_default && <Badge variant="success">Default</Badge>}
          </span>
        }
        actions={
          <>
            {/* Affordances only - the API authorises each of these independently. */}
            {canSubmit && (
              <Button onClick={() => setAction('submit')}>
                <Send />
                Submit for approval
              </Button>
            )}
            {canApprove && (
              <Button onClick={() => setAction('approve')}>
                <ShieldCheck />
                Approve
              </Button>
            )}
            {canReject && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={() => setAction('reject')}
              >
                <XCircle />
                Reject
              </Button>
            )}
            {can(P.BOM_CREATE) && (
              <Button variant="outline" onClick={() => setCopyOpen(true)}>
                <Copy />
                Copy as new version
              </Button>
            )}
            {canEdit && (
              <Button variant="outline" onClick={() => setEditOpen(true)}>
                <Pencil />
                Edit draft
              </Button>
            )}
            {/* Two separate buttons on purpose, and never both at once: the
                statuses that permit them do not overlap. Retiring an approved
                bill keeps the record other documents point at; deleting a draft
                destroys one that never took effect. */}
            {canObsolete && (
              <Button variant="outline" onClick={() => setAction('obsolete')}>
                <CalendarClock />
                Mark obsolete
              </Button>
            )}
            {canDeleteDraft && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={() => setAction('delete')}
              >
                <Trash2 />
                Delete draft
              </Button>
            )}
          </>
        }
      />

      <Card className="mb-5">
        <CardContent className="pt-5">
          <DescriptionList
            columns={4}
            items={[
              {
                label: 'Item',
                // The bill names `item_id` and nothing else about the item.
                value: (
                  <Link to={`/items/${bom.item_id}`} className="hover:underline">
                    {item.data ? `${item.data.code} - ${item.data.name}` : 'Open item master'}
                  </Link>
                ),
              },
              { label: 'Output quantity', value: formatQuantity(bom.output_quantity) },
              { label: 'Effective from', value: formatDate(bom.effective_from) },
              {
                label: 'Effective to',
                value: bom.effective_to ? formatDate(bom.effective_to) : 'Open ended',
              },
              {
                label: 'Approved',
                value: bom.approved_at ? formatDate(bom.approved_at) : 'Not approved',
              },
              { label: 'Lines', value: formatNumber(bom.lines?.length ?? 0) },
              { label: 'Operations', value: formatNumber(bom.operations?.length ?? 0) },
              { label: 'Created', value: formatDateTime(bom.created_at) },
              { label: 'Last updated', value: formatDateTime(bom.updated_at) },
              { label: 'Notes', value: bom.notes ?? '-' },
            ]}
          />
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="structure">
            <GitBranch className="h-3.5 w-3.5" aria-hidden />
            Structure
          </TabsTrigger>
          <TabsTrigger value="explode">
            <Merge className="h-3.5 w-3.5" aria-hidden />
            Exploded requirements
          </TabsTrigger>
          <TabsTrigger value="cost">
            <BadgeIndianRupee className="h-3.5 w-3.5" aria-hidden />
            Cost
          </TabsTrigger>
          <TabsTrigger value="where-used">
            <Layers className="h-3.5 w-3.5" aria-hidden />
            Where used
          </TabsTrigger>
        </TabsList>

        <TabsContent value="structure">
          <StructurePanel bom={bom} />
        </TabsContent>

        <TabsContent value="explode">
          <ExplosionPanel bom={bom} />
        </TabsContent>

        <TabsContent value="cost">
          {can(P.BOM_COST_VIEW) ? <CostPanel bomId={bom.id} /> : <PanelDenied what="BOM costs" />}
        </TabsContent>

        <TabsContent value="where-used">
          <WhereUsedPanel itemId={bom.item_id} />
        </TabsContent>
      </Tabs>

      <ConfirmDialog
        open={action !== null}
        onOpenChange={(open) => {
          if (!open) {
            setAction(null)
            // Cleared on close, not only on success: one dialog now serves five
            // commands, and a reason typed for a submission must not end up
            // recorded against the deletion opened next.
            setReason('')
            setReasonTouched(false)
          }
        }}
        title={action ? `${COMMAND_COPY[action].title} ${bom.code}?` : ''}
        description={action ? COMMAND_COPY[action].description : undefined}
        confirmLabel={action ? COMMAND_COPY[action].confirm : 'Confirm'}
        destructive={action === 'reject' || action === 'obsolete' || action === 'delete'}
        loading={runCommand.isPending}
        onConfirm={() => {
          if (!action) return
          if (reasonTooShort) {
            setReasonTouched(true)
            return
          }
          runCommand.mutate({ key: action, why: reason.trim() })
        }}
      >
        <div>
          <Label htmlFor="bom-status-reason" required>
            Reason
          </Label>
          {/* 3 to 255 characters on both carriers: `BomStatusChange.reason` for
              the four POSTs, and the `ReasonQuery` the DELETE reads out of the
              query string. Checked here so a too-short reason is a message on
              the field rather than a 422 after the record is already gone. */}
          <Input
            id="bom-status-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            autoComplete="off"
            className="mt-1.5"
            maxLength={REASON_MAX_LENGTH}
            aria-invalid={reasonTouched && reasonTooShort}
          />
          {reasonTouched && reasonTooShort && (
            <FieldError
              message={`The API records a reason against every status change - at least ${REASON_MIN_LENGTH} characters`}
            />
          )}
        </div>
      </ConfirmDialog>

      {can(P.BOM_CREATE) && (
        <CopyBomDialog
          open={copyOpen}
          onOpenChange={setCopyOpen}
          bom={bom}
          onCopied={(created) => navigate(`/bom/${created.id}`)}
        />
      )}

      {canEdit && <BomEditDialog bom={bom} open={editOpen} onOpenChange={setEditOpen} />}
    </>
  )
}

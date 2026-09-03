/**
 * Transporter master - the carriers and couriers a dispatch can be handed to.
 *
 * Every narrowing is a query parameter the API answers; the browser never holds
 * more than one page (master rules 31/36/78).
 *
 * The screen's one real subtlety is that it shows two independent lifecycle
 * axes in one column. `is_active` decides whether a carrier may be named on a
 * new dispatch. `deleted_at` decides whether the master row still exists at
 * all. They are separate filters on the API and the list keeps them separate:
 * "Active / Inactive" narrows the first, and "Include retired" *widens* the
 * result to cover the second rather than replacing it - there is no "retired
 * only" filter in the contract to offer, and pretending otherwise would be a
 * filter that lies. The Status column then states the derived answer, so a row
 * is never ambiguous about which of the two put it where it is.
 *
 * A retired row offers exactly one action, because every by-id endpoint except
 * restore is scoped to undeleted rows - see `transporter-model.ts`.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Link2, MoreHorizontal, Pencil, Plus, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader } from '@/components/shared'
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
} from '@/components/ui/overlays'
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import {
  TransporterCommandDialog,
  TransporterFormDialog,
  TrackingLinkDialog,
} from './transporter-dialogs'
import {
  TRANSPORTER_COMMANDS,
  TRANSPORTER_STATE_LABELS,
  TRANSPORTER_TYPES,
  TRANSPORTER_TYPE_LABELS,
  commandsFor,
  isEditable,
  transporterKeys,
  transporterState,
  type PendingTransporterCommand,
  type Transporter,
  type TransporterState,
} from './transporter-model'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = ['transporter_type', 'is_active', 'include_deleted'] as const

const STATE_VARIANT: Record<TransporterState, 'secondary' | 'muted' | 'destructive'> = {
  ACTIVE: 'secondary',
  INACTIVE: 'muted',
  // Withdrawn rather than merely switched off, and only visible when the list
  // has been widened to include it.
  RETIRED: 'destructive',
}

export default function TransportersPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  // Create, edit and all four lifecycle commands are one right on this router:
  // every write is `require_permission(Permission.TRANSPORTER_MANAGE)`.
  const canManage = can(P.TRANSPORTER_MANAGE)

  const [formOpen, setFormOpen] = useState(false)
  const [formTransporterId, setFormTransporterId] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingTransporterCommand | null>(null)
  const [trackingTransporter, setTrackingTransporter] = useState<Transporter | null>(null)

  const transporterType = searchParams.get('transporter_type') ?? ''
  const activeState = searchParams.get('is_active') ?? ''
  const includeRetired = searchParams.get('include_deleted') === 'true'

  const list = useListQuery<Transporter>({
    path: '/transporters',
    queryKey: transporterKeys.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(transporterType ? { transporter_type: transporterType } : {}),
      // Deliberately unset by default. A transporter master is a handful of
      // rows, and defaulting to active-only is how someone concludes a carrier
      // has vanished when it was only switched off.
      ...(activeState === 'true' ? { is_active: true } : {}),
      ...(activeState === 'false' ? { is_active: false } : {}),
      ...(includeRetired ? { include_deleted: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` is not a React state setter: three `setParam` calls in a
   * row each build the next URL from the same captured `searchParams`, so only
   * the last navigation survives and the other two filters stay exactly where
   * they were.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        // The page belonged to the narrowed list, not to this one.
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  const openForm = (transporterId: string | null) => {
    setFormTransporterId(transporterId)
    setFormOpen(true)
  }

  const columns = useMemo<ColumnDef<Transporter, unknown>[]>(() => {
    const defs: ColumnDef<Transporter, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <span className="block truncate">{row.original.name}</span>
            {(row.original.city || row.original.state) && (
              <span className="block truncate text-xs text-muted-foreground">
                {[row.original.city, row.original.state].filter(Boolean).join(', ')}
              </span>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'transporter_type',
        header: 'Type',
        cell: ({ row }) => (
          <Badge variant="secondary">
            {TRANSPORTER_TYPE_LABELS[row.original.transporter_type]}
          </Badge>
        ),
      },
      {
        id: 'gstin',
        header: 'GSTIN',
        enableSorting: false,
        // An e-way bill is raised against the transporter's GSTIN, so a carrier
        // without one is a fact worth stating rather than a blank cell.
        cell: ({ row }) =>
          row.original.gstin ? (
            <span className="tabular text-xs">{row.original.gstin}</span>
          ) : (
            <span className="text-muted-foreground">Unregistered</span>
          ),
      },
      {
        id: 'contact',
        header: 'Contact',
        enableSorting: false,
        cell: ({ row }) => {
          const number = row.original.mobile ?? row.original.phone
          if (!row.original.contact_person && !number) {
            return <span className="text-muted-foreground">-</span>
          }
          return (
            <div className="max-w-[14rem]">
              {row.original.contact_person && (
                <span className="block truncate text-xs">{row.original.contact_person}</span>
              )}
              {number && <span className="block truncate text-xs tabular">{number}</span>}
            </div>
          )
        },
      },
      {
        id: 'tracking',
        header: 'Tracking',
        enableSorting: false,
        cell: ({ row }) => {
          if (!row.original.tracking_url_template) {
            // Not a defect: plenty of local carriers track by telephone. The
            // dispatch then shows the bare consignment number.
            return <span className="text-xs text-muted-foreground">By phone</span>
          }
          // Resolving needs the row alive - the tracking-url endpoint is scoped
          // to undeleted transporters and answers 404 for a retired one.
          if (transporterState(row.original) === 'RETIRED') {
            return <Badge variant="muted">Link on file</Badge>
          }
          return (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setTrackingTransporter(row.original)}
              aria-label={`Resolve a consignment number for ${row.original.code}`}
            >
              <Link2 />
              Resolve
            </Button>
          )
        },
      },
      {
        id: 'state',
        header: 'Status',
        // Not sortable on purpose: this is derived from `is_active` and
        // `deleted_at`, and the API sorts on `is_active` alone - which would
        // scatter retired rows through the inactive ones and read as a bug.
        enableSorting: false,
        cell: ({ row }) => {
          const state = transporterState(row.original)
          return <Badge variant={STATE_VARIANT[state]}>{TRANSPORTER_STATE_LABELS[state]}</Badge>
        },
      },
      {
        accessorKey: 'updated_at',
        header: 'Updated',
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {formatDate(row.original.updated_at)}
          </span>
        ),
      },
    ]

    // Gating is UX only - the API authorises each of these independently.
    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const state = transporterState(row.original)
          return (
            <div className="flex items-center justify-end gap-1">
              {isEditable(state) && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Edit ${row.original.code}`}
                  onClick={() => openForm(row.original.id)}
                >
                  <Pencil />
                </Button>
              )}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`More actions for ${row.original.code}`}
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {commandsFor(state).map((command) => (
                    <DropdownMenuItem
                      key={command}
                      destructive={TRANSPORTER_COMMANDS[command].destructive}
                      onSelect={() => setPending({ transporter: row.original, command })}
                    >
                      {TRANSPORTER_COMMANDS[command].label}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          )
        },
      })
    }

    return defs
  }, [canManage])

  const newTransporterButton = canManage ? (
    <Button onClick={() => openForm(null)}>
      <Plus />
      New transporter
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Transporters"
        description="The carriers and couriers a dispatch is handed to. A transporter with a tracking link template turns every consignment number into a link the customer can follow."
        actions={newTransporterButton}
      />

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
        searchPlaceholder="Search code, name, GSTIN or city..."
        getRowId={(row) => row.id}
        emptyTitle="No transporters found"
        emptyDescription={
          filterCount > 0
            ? 'Nothing matches the current filters. Clear one, or tick "Include retired" if the carrier was withdrawn.'
            : 'Add the carriers and couriers you dispatch with, so a consignment can name one and its tracking link resolves.'
        }
        emptyAction={newTransporterButton}
        toolbar={
          <>
            <Select
              value={transporterType || ALL}
              onValueChange={(value) =>
                setParam('transporter_type', value === ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-[170px]" aria-label="Filter by carrier type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All types</SelectItem>
                {TRANSPORTER_TYPES.map((type) => (
                  <SelectItem key={type} value={type}>
                    {TRANSPORTER_TYPE_LABELS[type]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={activeState || ALL}
              onValueChange={(value) => setParam('is_active', value === ALL ? undefined : value)}
            >
              <SelectTrigger className="w-[170px]" aria-label="Filter by availability">
                <SelectValue placeholder="Any availability" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any availability</SelectItem>
                <SelectItem value="true">Active</SelectItem>
                <SelectItem value="false">Inactive</SelectItem>
              </SelectContent>
            </Select>

            {/*
              Widens the result rather than narrowing it: `include_deleted=true`
              adds retired rows alongside the live ones. A retired carrier is
              always inactive too, so this and the availability filter compose
              exactly as they read.
            */}
            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="transporters-include-retired"
                checked={includeRetired}
                onCheckedChange={(checked) =>
                  setParam('include_deleted', checked === true ? 'true' : undefined)
                }
              />
              <Label
                htmlFor="transporters-include-retired"
                className="cursor-pointer text-xs font-normal"
              >
                Include retired
              </Label>
            </div>

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />

      {canManage && (
        <TransporterFormDialog
          open={formOpen}
          onOpenChange={setFormOpen}
          transporterId={formTransporterId}
        />
      )}

      <TransporterCommandDialog
        pending={pending}
        onOpenChange={(open) => !open && setPending(null)}
      />

      <TrackingLinkDialog
        transporter={trackingTransporter}
        onOpenChange={(open) => !open && setTrackingTransporter(null)}
      />
    </>
  )
}

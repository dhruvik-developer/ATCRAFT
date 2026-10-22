/**
 * Vendor master list.
 *
 * Every filter is a query parameter the API answers: a company with a long
 * supply base carries thousands of vendors and the browser never receives more
 * than one page (master rules 31/36/78).
 *
 * Two things this screen is careful about. A vendor holds several types at
 * once, so "type" is a set of badges rather than one column value. And an
 * unrated vendor is rendered as unrated - never as zero - because a zero would
 * read as "bad" when the truth is "no evidence yet".
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { BadgeCheck, MoreHorizontal, Pencil, Plus, Star, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, DescriptionList, FieldError, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_ALL } from '@/lib/constants'
import { reportFieldErrors } from '@/lib/form-errors'
import { formatDate, formatNumber, formatPercent, humanise, toDecimal } from '@/lib/format'
import { messages, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { VendorFormDialog } from './vendor-form-dialog'
import {
  acceptsRating,
  acceptsRetirement,
  DEFAULT_RATING_WINDOW_DAYS,
  MAX_VENDOR_RATING,
  RATING_WEIGHTS,
  RATING_WINDOW_CHOICES,
  VENDOR_TYPES,
  vendorState,
  type VendorRating,
  type VendorRatingRequest,
  type VendorRow,
} from './vendor-types'

/** Which lifecycle command a reason dialog is currently collecting a reason for. */
type PendingCommand = {
  vendor: VendorRow
  action: 'approve' | 'withdraw-approval' | 'activate' | 'deactivate' | 'retire'
}

const COMMAND_COPY: Record<
  PendingCommand['action'],
  { title: string; description: string; confirmLabel: string; destructive: boolean }
> = {
  approve: {
    title: 'Approve this vendor?',
    description:
      'Purchase orders may be raised on an approved vendor. The approval is recorded against your account.',
    confirmLabel: 'Approve vendor',
    destructive: false,
  },
  'withdraw-approval': {
    title: 'Withdraw approval?',
    description:
      'New purchase orders are refused on this vendor. Orders already raised are unaffected.',
    confirmLabel: 'Withdraw approval',
    destructive: true,
  },
  activate: {
    title: 'Reactivate this vendor?',
    description: 'The vendor becomes selectable again on new documents.',
    confirmLabel: 'Reactivate',
    destructive: false,
  },
  deactivate: {
    title: 'Suspend this vendor?',
    description:
      'The vendor disappears from the pickers on new documents. Existing orders and receipts are untouched.',
    confirmLabel: 'Suspend vendor',
    destructive: true,
  },
  // Retirement is the only one of the five that takes the record out of the
  // master rather than moving it between live states, so the copy names both
  // what goes with it and what the API refuses to do it over.
  retire: {
    title: 'Retire this vendor?',
    description:
      'The vendor leaves the master. Its contacts are retired with it and its item-category qualifications are removed, so it stops surfacing in category-based sourcing, and it will not appear in this list again. Refused while it still has an open purchase order, is named as a source on an item, or has an active portal user - close, unlink or deactivate those first. A retired vendor can be restored, but it comes back suspended and unapproved, and its contacts and qualifications do not come back with it.',
    confirmLabel: 'Retire vendor',
    destructive: true,
  },
}

/**
 * Five stars filled to the fraction the rating holds.
 *
 * The overlay approach keeps 3.8 looking like 3.8 rather than rounding it to
 * four, and the accessible name carries the number and the sample size, because
 * "3.8 from 12 receipts" is a very different claim from "3.8 from 2".
 */
function RatingStars({ rating, sampleSize }: { rating: string | null; sampleSize: number }) {
  if (rating === null) {
    return <span className="text-xs text-muted-foreground">Unrated</span>
  }

  const value = toDecimal(rating)
  // Percentage of the star row to paint. Computed with decimal.js and only then
  // turned into a number, which is a layout width and not a business figure.
  const percent = value.dividedBy(MAX_VENDOR_RATING).times(100).toDecimalPlaces(2).toNumber()
  const label = `Rated ${value.toDecimalPlaces(2).toFixed(2)} out of ${MAX_VENDOR_RATING} from ${sampleSize} receipt${sampleSize === 1 ? '' : 's'}`

  return (
    <span className="flex items-center gap-1.5">
      <span className="relative inline-flex" role="img" aria-label={label}>
        <span className="flex" aria-hidden>
          {Array.from({ length: MAX_VENDOR_RATING }).map((_, index) => (
            <Star key={index} className="h-3.5 w-3.5 text-muted-foreground/40" />
          ))}
        </span>
        <span
          className="absolute inset-y-0 left-0 flex overflow-hidden"
          style={{ width: `${percent}%` }}
          aria-hidden
        >
          {Array.from({ length: MAX_VENDOR_RATING }).map((_, index) => (
            <Star key={index} className="h-3.5 w-3.5 fill-warning text-warning" />
          ))}
        </span>
      </span>
      <span className="tabular text-xs text-muted-foreground">
        {value.toDecimalPlaces(1).toFixed(1)}
      </span>
    </span>
  )
}

/**
 * Where one vendor's score came from, and the one place it can be recorded.
 *
 * `VendorRead` carries the stored rating and nothing behind it - no on-time
 * percentage, no rejection rate, no sample. Those live on
 * `GET /vendors/{id}/rating`, which recomputes them over a window without
 * storing anything, and which says in `reason` why a rating is or is not being
 * applied. Fetched for one vendor on demand rather than per row, which would be
 * a request per line of the grid.
 *
 * `POST` on the same path is the same computation, kept. That is the whole of
 * "record a rating": the request body carries the measurement window and no
 * figures at all, because the API will not take an on-time or a rejection
 * number from a client. So this dialog is a preview with a Save, not a score
 * form - every dimension is read back, and the only thing the buyer decides is
 * the window it was measured over.
 */
function VendorRatingDialog({
  vendor,
  onOpenChange,
}: {
  vendor: VendorRow | null
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [windowDays, setWindowDays] = useState(DEFAULT_RATING_WINDOW_DAYS)

  // Recording is gated on vendor.rate, which is what the POST declares, and not
  // on vendor.update: the two are separate rights on purpose, so a buyer who
  // may score their suppliers need not also be able to edit the master row.
  const canRate = can(P.VENDOR_RATE)

  useEffect(() => {
    // Back to the default window when the dialog closes. Carrying the last
    // window over to the next vendor would judge it over a period nobody chose
    // for it, and the number that comes back looks identical either way.
    if (!vendor) setWindowDays(DEFAULT_RATING_WINDOW_DAYS)
  }, [vendor])

  const rating = useQuery({
    // The window is part of the key: two windows are two different answers, and
    // sharing one key would put the last window's evidence under this window's
    // heading for as long as the query stays fresh.
    queryKey: [...queryKeys.vendors.detail(vendor?.id ?? ''), 'rating', windowDays] as const,
    queryFn: () =>
      api.get<VendorRating>(`/vendors/${vendor?.id}/rating`, {
        params: { window_days: windowDays },
      }),
    enabled: Boolean(vendor),
    staleTime: staleTimes.master,
  })

  const record = useMutation({
    mutationFn: (body: VendorRatingRequest) =>
      api.post<VendorRating>(`/vendors/${vendor?.id}/rating`, body),
    onSuccess: (result) => {
      // The stored score, its sample size and the date it was worked out all
      // live on the vendor row, so the list behind this dialog is stale the
      // moment the POST lands - and so is the preview, which afterwards reads
      // back the figure that was kept rather than a hypothetical one.
      void queryClient.invalidateQueries({ queryKey: queryKeys.vendors.all })

      if (result.applied) {
        toast.success(
          success.recorded('Vendor rating'),
          `${toDecimal(result.rating).toDecimalPlaces(2).toFixed(2)} out of ${MAX_VENDOR_RATING} from ${result.receipt_count} receipt${result.receipt_count === 1 ? '' : 's'}`,
        )
        return
      }
      // A 200 that stored nothing: an insufficient window leaves the previous
      // rating untouched rather than clearing it. Reporting that as saved would
      // be a claim somebody sources on.
      toast.warning('Nothing recorded', result.reason)
    },
  })

  const data = rating.data
  // A retired vendor is resolved with `get_or_404` before anything is measured,
  // so the preview and the POST both 404 on one. The list does not ask for
  // retired rows today, which makes this a guard rather than a filter.
  const ratable = vendor !== null && acceptsRating(vendorState(vendor))
  // Offered only when the preview says the window holds enough evidence: the
  // endpoint answers 200 for an insufficient one and keeps nothing, which is
  // the one failure mode that looks exactly like success.
  const canApply = ratable && Boolean(data?.is_sufficient) && !rating.isFetching

  return (
    <Dialog open={Boolean(vendor)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{vendor ? `${vendor.code} - ${vendor.name}` : 'Vendor rating'}</DialogTitle>
          <DialogDescription>
            Measured from goods receipts and QC inspections over the window you choose. No figure
            here can be typed in - recording keeps exactly what the API computed.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div>
            <Label htmlFor="vendor-rating-window">Measurement window</Label>
            <Select
              value={String(windowDays)}
              onValueChange={(value) => setWindowDays(Number(value))}
            >
              <SelectTrigger id="vendor-rating-window" className="mt-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RATING_WINDOW_CHOICES.map((choice) => (
                  <SelectItem key={choice.days} value={String(choice.days)}>
                    {choice.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {rating.isLoading && (
            <div className="space-y-3" aria-hidden>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-3/4" />
            </div>
          )}

          {!rating.isLoading && rating.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not work out this rating</AlertTitle>
              <AlertDescription>{rating.error.message}</AlertDescription>
            </Alert>
          )}

          {!rating.isLoading && !rating.error && data && (
            <>
              <DescriptionList
                columns={2}
                items={[
                  {
                    label: 'Rating',
                    value: <RatingStars rating={data.rating} sampleSize={data.receipt_count} />,
                  },
                  {
                    label: 'Measured over',
                    value: `${formatNumber(data.window_days)} days`,
                  },
                  {
                    label: 'Delivered on time',
                    value:
                      data.on_time_percentage === null
                        ? '-'
                        : formatPercent(data.on_time_percentage, 1),
                  },
                  {
                    label: 'Rejected on inspection',
                    value:
                      data.rejection_percentage === null
                        ? '-'
                        : formatPercent(data.rejection_percentage, 1),
                  },
                  { label: 'Receipts in the window', value: formatNumber(data.receipt_count) },
                  {
                    label: 'Quality evidence',
                    value: data.has_quality_evidence ? 'Inspected' : 'None recorded',
                  },
                ]}
              />

              <Alert variant={data.is_sufficient ? 'info' : 'warning'}>
                <AlertTitle>
                  {data.is_sufficient
                    ? 'Enough evidence to rate this vendor'
                    : 'Not enough evidence to rate this vendor'}
                </AlertTitle>
                <AlertDescription>{data.reason}</AlertDescription>
              </Alert>

              <p className="text-xs text-muted-foreground">
                On-time delivery is weighted {RATING_WEIGHTS.delivery}% and inspected quality{' '}
                {RATING_WEIGHTS.quality}%, on a scale of 0 to {MAX_VENDOR_RATING}. A window with no
                inspections is scored on delivery alone rather than credited with a quality result
                it has not earned.
              </p>
            </>
          )}
        </DialogBody>

        {canRate && (
          <DialogFooter className="sm:items-center sm:justify-between">
            {/* Keyed off the evidence rather than off canApply, so the line
                does not accuse the window of being thin while it is loading. */}
            <p className="text-xs text-muted-foreground">
              {data && !data.is_sufficient
                ? 'An insufficient window records nothing, and the score already on the vendor is left as it is.'
                : 'Recording stores this score against the vendor and dates it, replacing the one it holds.'}
            </p>
            <Button
              onClick={() => record.mutate({ window_days: windowDays })}
              loading={record.isPending}
              disabled={!canApply}
            >
              Record this rating
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default function VendorsPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()

  const [dialogVendorId, setDialogVendorId] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [pending, setPending] = useState<PendingCommand | null>(null)
  const [ratingVendor, setRatingVendor] = useState<VendorRow | null>(null)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)

  const canCreate = can(P.VENDOR_CREATE)
  const canUpdate = can(P.VENDOR_UPDATE)
  // Each of the two new actions is gated on the right its own endpoint
  // enforces, not on vendor.update: POST /vendors/{id}/rating declares
  // Permission.VENDOR_RATE and DELETE /vendors/{id} declares
  // Permission.VENDOR_DELETE, and neither is implied by being able to edit a
  // vendor.
  const canRate = can(P.VENDOR_RATE)
  const canDelete = can(P.VENDOR_DELETE)

  const vendorType = searchParams.get('vendor_type') ?? ''
  const approval = searchParams.get('approval') ?? ''
  const activeState = searchParams.get('is_active') === 'false' ? 'false' : 'true'

  const list = useListQuery<VendorRow>({
    path: '/vendors',
    queryKey: queryKeys.vendors.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(vendorType ? { vendor_type: vendorType } : {}),
      ...(approval === 'approved' ? { is_approved: true } : {}),
      ...(approval === 'pending' ? { is_approved: false } : {}),
      is_active: activeState !== 'false',
    },
  })

  const { setParam, setParams } = list
  const filterCount = (vendorType ? 1 : 0) + (approval ? 1 : 0) + (activeState === 'false' ? 1 : 0)

  const openDialog = (vendorId: string | null) => {
    setDialogVendorId(vendorId)
    setDialogOpen(true)
  }

  // The four lifecycle commands share a shape: a POST to a named sub-resource
  // carrying the reason the audit trail needs (docs/database.md section 8).
  // Retirement joins them in the same dialog because it asks the user for the
  // same thing, but it is neither a POST nor a body: it is a DELETE whose
  // reason travels in the query string, which is what `ReasonQuery` in the
  // router says and why - a DELETE body is legal, and proxies drop it. Sending
  // it as a body instead is a 422 at the moment of use, which no build catches.
  const command = useMutation({
    mutationFn: async ({ vendor, action, reason: text }: PendingCommand & { reason: string }) => {
      if (action === 'retire') {
        await api.delete(`/vendors/${vendor.id}`, { params: { reason: text } })
        // `ApiResponse[None]`: there is no vendor to hand back, and the row is
        // about to leave the list anyway.
        return null
      }
      return api.post<VendorRow>(`/vendors/${vendor.id}/${action}`, { reason: text })
    },
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.vendors.all })
      toast.success(
        {
          approve: success.approved('Vendor'),
          'withdraw-approval': 'Approval withdrawn',
          activate: 'Vendor reactivated',
          deactivate: 'Vendor suspended',
          retire: success.retired('Vendor'),
        }[variables.action],
        `${variables.vendor.code} - ${variables.vendor.name}`,
      )
      setPending(null)
      setReason('')
      setReasonTouched(false)
    },
    // Fired from a confirm box, not a form: a 422 on `reason` has no control
    // to land on, and the mutation cache stays quiet for it.
    onError: (error) => reportFieldErrors(error, 'Could not complete this vendor action'),
  })

  const columns = useMemo<ColumnDef<VendorRow, unknown>[]>(() => {
    const defs: ColumnDef<VendorRow, unknown>[] = [
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
        id: 'vendor_types',
        header: 'Types',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex max-w-[16rem] flex-wrap gap-1">
            {row.original.vendor_types.length === 0 ? (
              <span className="text-muted-foreground">-</span>
            ) : (
              row.original.vendor_types.map((type) => (
                <Badge key={type} variant="secondary">
                  {humanise(type)}
                </Badge>
              ))
            )}
          </div>
        ),
      },
      {
        id: 'gstin',
        header: 'GSTIN',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.gstin ? (
            <span className="tabular text-xs">{row.original.gstin}</span>
          ) : (
            <span className="text-muted-foreground">Unregistered</span>
          ),
      },
      {
        accessorKey: 'rating',
        header: 'Rating',
        // Clickable, because the stored score says nothing about why it is what
        // it is. The figures behind it are a separate endpoint, fetched for the
        // one vendor asked about rather than for every row of the grid.
        cell: ({ row }) => (
          <button
            type="button"
            onClick={() => setRatingVendor(row.original)}
            aria-label={`Show how ${row.original.code} was rated`}
            className="rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <RatingStars
              rating={row.original.rating}
              sampleSize={row.original.rating_sample_size}
            />
          </button>
        ),
      },
      {
        accessorKey: 'lead_time_days',
        header: 'Lead time',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">
            {row.original.lead_time_days > 0
              ? `${formatNumber(row.original.lead_time_days)} d`
              : '-'}
          </span>
        ),
      },
      {
        id: 'rated_on',
        header: 'Rated',
        enableSorting: false,
        // When the score was last recomputed. A four-star rating last worked
        // out eight months ago is a different claim from one from last week,
        // and the list is the only place that difference is visible.
        cell: ({ row }) =>
          row.original.rating_updated_at ? (
            <span className="text-xs text-muted-foreground">
              {formatDate(row.original.rating_updated_at)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'is_approved',
        header: 'Approved',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_approved ? (
            <Badge variant="success">
              <BadgeCheck className="h-3 w-3" aria-hidden />
              Approved
            </Badge>
          ) : (
            <Badge variant="warning">Not approved</Badge>
          ),
      },
      {
        id: 'is_active',
        header: 'Active',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_active ? (
            <Badge variant="secondary">Active</Badge>
          ) : (
            <Badge variant="muted">Suspended</Badge>
          ),
      },
    ]

    // Gating is UX only - the API authorises each of these independently. Three
    // separate rights reach this column: the lifecycle commands and the edit
    // pencil are vendor.update, recording a rating is vendor.rate, and retiring
    // is vendor.delete. Someone holding only one of them still gets a menu.
    if (canUpdate || canRate || canDelete) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const vendor = row.original
          const state = vendorState(vendor)
          const showRate = canRate && acceptsRating(state)
          const showRetire = canDelete && acceptsRetirement(state)

          return (
            <div className="flex items-center justify-end gap-1">
              {canUpdate && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Edit ${vendor.code}`}
                  onClick={() => openDialog(vendor.id)}
                >
                  <Pencil />
                </Button>
              )}
              {(canUpdate || showRate || showRetire) && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`More actions for ${vendor.code}`}
                    >
                      <MoreHorizontal />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    {canUpdate && (
                      <>
                        {vendor.is_approved ? (
                          <DropdownMenuItem
                            destructive
                            onSelect={() => setPending({ vendor, action: 'withdraw-approval' })}
                          >
                            Withdraw approval
                          </DropdownMenuItem>
                        ) : (
                          <DropdownMenuItem
                            onSelect={() => setPending({ vendor, action: 'approve' })}
                          >
                            Approve vendor
                          </DropdownMenuItem>
                        )}
                        {vendor.is_active ? (
                          <DropdownMenuItem
                            destructive
                            onSelect={() => setPending({ vendor, action: 'deactivate' })}
                          >
                            Suspend vendor
                          </DropdownMenuItem>
                        ) : (
                          <DropdownMenuItem
                            onSelect={() => setPending({ vendor, action: 'activate' })}
                          >
                            Reactivate vendor
                          </DropdownMenuItem>
                        )}
                      </>
                    )}
                    {showRate && (
                      <>
                        {canUpdate && <DropdownMenuSeparator />}
                        <DropdownMenuItem onSelect={() => setRatingVendor(vendor)}>
                          Record a rating
                        </DropdownMenuItem>
                      </>
                    )}
                    {showRetire && (
                      <>
                        {(canUpdate || showRate) && <DropdownMenuSeparator />}
                        <DropdownMenuItem
                          destructive
                          onSelect={() => setPending({ vendor, action: 'retire' })}
                        >
                          Retire vendor
                        </DropdownMenuItem>
                      </>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          )
        },
      })
    }

    return defs
  }, [canDelete, canRate, canUpdate])

  const newVendorButton = canCreate ? (
    <Button onClick={() => openDialog(null)}>
      <Plus />
      New vendor
    </Button>
  ) : null

  const copy = pending ? COMMAND_COPY[pending.action] : null
  const reasonTooShort = reason.trim().length < REASON_MIN_LENGTH

  return (
    <>
      <PageHeader
        title="Vendors"
        description="The supply base. A vendor must be approved before a purchase order can be raised on it, and its rating is computed from receipts rather than typed in."
        actions={newVendorButton}
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
        emptyTitle="No vendors found"
        emptyDescription="Nothing matches the current filters. Clear a filter, or add the supplier."
        emptyAction={newVendorButton}
        toolbar={
          <>
            <Select
              value={vendorType || SELECT_ALL}
              onValueChange={(value) =>
                setParam('vendor_type', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[210px]" aria-label="Filter by vendor type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All types</SelectItem>
                {VENDOR_TYPES.map((type) => (
                  <SelectItem key={type} value={type}>
                    {humanise(type)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={approval || SELECT_ALL}
              onValueChange={(value) =>
                setParam('approval', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by approval">
                <SelectValue placeholder="Any approval" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any approval</SelectItem>
                <SelectItem value="approved">Approved</SelectItem>
                <SelectItem value="pending">Not approved</SelectItem>
              </SelectContent>
            </Select>

            <Select
              value={activeState}
              onValueChange={(value) => setParam('is_active', value === 'true' ? undefined : value)}
            >
              <SelectTrigger className="w-full sm:w-[150px]" aria-label="Filter by status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="true">Active</SelectItem>
                <SelectItem value="false">Suspended</SelectItem>
              </SelectContent>
            </Select>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  // One navigation, not one per filter: `setSearchParams` computes every
                  // functional update in a tick from the same captured value, so a loop of
                  // `setParam` calls clears exactly the last one and looks like it did nothing.
                  setParams({
                    vendor_type: undefined,
                    approval: undefined,
                    is_active: undefined,
                  })
                }}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      <VendorRatingDialog
        vendor={ratingVendor}
        onOpenChange={(open) => !open && setRatingVendor(null)}
      />

      {(canCreate || canUpdate) && (
        <VendorFormDialog
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          vendorId={dialogVendorId}
          canEdit={dialogVendorId ? canUpdate : canCreate}
        />
      )}

      <ConfirmDialog
        open={Boolean(pending)}
        onOpenChange={(open) => {
          if (!open) {
            setPending(null)
            setReason('')
            setReasonTouched(false)
          }
        }}
        title={copy?.title ?? ''}
        description={
          pending
            ? `${copy?.description} (${pending.vendor.code} - ${pending.vendor.name})`
            : undefined
        }
        confirmLabel={copy?.confirmLabel ?? 'Confirm'}
        destructive={copy?.destructive}
        loading={command.isPending}
        onConfirm={() => {
          if (!pending) return
          if (reasonTooShort) {
            setReasonTouched(true)
            return
          }
          command.mutate({ ...pending, reason: reason.trim() })
        }}
      >
        <div>
          <Label htmlFor="vendor-command-reason" required>
            Reason
          </Label>
          {/* The same 3..255 bound on both carriers the five commands use:
              StatusChangeRequest.reason for the four POSTs, and the
              ReasonQuery the DELETE reads out of the query string. */}
          <Input
            id="vendor-command-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            autoComplete="off"
            className="mt-1.5"
            maxLength={REASON_MAX_LENGTH}
            aria-invalid={reasonTouched && reasonTooShort}
          />
          {reasonTouched && reasonTooShort && (
            <FieldError message={messages.errors.reasonTooShort} />
          )}
        </div>
      </ConfirmDialog>
    </>
  )
}

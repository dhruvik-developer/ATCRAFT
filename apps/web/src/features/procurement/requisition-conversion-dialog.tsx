/**
 * Raising purchase orders from approved requisitions: the category-wise split.
 *
 * The API decides the split, not this screen. For every line still to order it
 * resolves a vendor (the buyer's override, the requester's suggestion, the
 * item's preferred vendor, then the category's preferred vendor), a price (the
 * override, the requisition's estimate, then the last price paid to that vendor
 * - never the item's standard cost) and the procurement category the vendor is
 * engaged at, and proposes one draft order per vendor, category and warehouse,
 * each handed to the vendor user who manages that category. This dialog shows
 * the proposal, lets the buyer correct it line by line and asks again. Every
 * correction is a new preview; nothing is re-priced or re-grouped here.
 *
 * What the API would refuse is shown before Raise is pressed rather than after:
 * a line with no vendor or no price (the whole conversion is refused, so
 * nothing is ever half-raised), more orders than one conversion may raise, and
 * open orders the proposal would duplicate. A duplicate is still raised only
 * through the same 409 conversation every purchase order goes through, so the
 * acknowledgement and its reason are recorded the same way.
 *
 * The orders are drafts. Submit, approve and send still happen on each one.
 *
 * The batch picker shares this module so both arrive in one lazy chunk: the
 * requisitions queue pays for neither until a buyer asks to raise orders.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { ExternalLink, PackageX, RefreshCw, ShieldAlert, Undo2, UserRound } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { FieldError, StatusBadge } from '@/components/shared'
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
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Checkbox,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import {
  daysUntil,
  formatDate,
  formatMoney,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { describeError, raisedDocuments } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn, indexById } from '@/lib/utils'

import {
  ASSIGNEE_REASON_COPY,
  DUPLICATE_CONFIDENCE_COPY,
  MAX_REQUISITIONS_PER_CONVERSION,
  PRICE_SOURCE_COPY,
  VENDOR_SOURCE_COPY,
  duplicateConfidence,
  isUrgentPriority,
  parseDuplicateConflict,
  requisitionKeys,
  type DuplicateConflict,
  type RequisitionBatchConversionPayload,
  type RequisitionConversionGroup,
  type RequisitionConversionLine,
  type RequisitionConversionOverride,
  type RequisitionConversionPayload,
  type RequisitionConversionResult,
  type RequisitionConversionTarget,
  type RequisitionConversionUnresolvedLine,
  type RequisitionRef,
  type RequisitionRow,
} from './procurement-types'
import {
  PurchaseOrderDuplicateDialog,
  type DuplicateDialogCopy,
} from './purchase-order-duplicate-dialog'

// ------------------------------------------------------------ overrides -----

/**
 * The bounds `RequisitionConversionLineOverride` declares: a quantity of up to
 * four decimal places and a price of up to six, both within NUMERIC(18, n). A
 * value outside them is held back with a message rather than sent for a 422.
 */
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const PRICE = /^\d{1,12}(\.\d{1,6})?$/

const isQuantity = (text: string): boolean =>
  QUANTITY.test(text.trim()) && toDecimal(text.trim()).greaterThan(0)
const isUnitPrice = (text: string): boolean => PRICE.test(text.trim())

/** What the buyer has changed on one line, as typed. Empty text means "as proposed". */
interface LineEdit {
  vendorId: string | null
  quantity: string
  unitPrice: string
  exclude: boolean
}

const NO_EDIT: LineEdit = { vendorId: null, quantity: '', unitPrice: '', exclude: false }

const isUntouched = (edit: LineEdit): boolean =>
  edit.vendorId === null && edit.quantity === '' && edit.unitPrice === '' && !edit.exclude

const hasInvalidText = (edit: LineEdit): boolean =>
  (edit.quantity.trim() !== '' && !isQuantity(edit.quantity)) ||
  (edit.unitPrice.trim() !== '' && !isUnitPrice(edit.unitPrice))

/**
 * The overrides as the API takes them, one per edited line, in a stable order.
 *
 * Only what is valid is sent: a half-typed price must not turn the preview into
 * a 422. The Raise button is held back while anything typed is invalid, so a
 * value the buyer can see but the API never received cannot be converted.
 */
function overridesOf(edits: Record<string, LineEdit>): RequisitionConversionOverride[] {
  return Object.keys(edits)
    .sort()
    .flatMap((lineId) => {
      const edit = edits[lineId] ?? NO_EDIT
      const quantity = isQuantity(edit.quantity) ? edit.quantity.trim() : null
      const unitPrice = isUnitPrice(edit.unitPrice) ? edit.unitPrice.trim() : null
      if (!edit.exclude && edit.vendorId === null && quantity === null && unitPrice === null) {
        return []
      }
      return [
        {
          requisition_line_id: lineId,
          vendor_id: edit.vendorId,
          quantity,
          unit_price: unitPrice,
          exclude: edit.exclude,
        },
      ]
    })
}

/** A decimal string without the API's trailing zeros, for a placeholder: "100", "182.5". */
function plain(value: string): string {
  return toDecimal(value).toFixed()
}

// ------------------------------------------------------------- requests -----

/**
 * The read-only preview.
 *
 * Each path is written out rather than assembled from a base: the permission
 * gate guard recovers the route from the literal it can see, and a path built
 * out of variables would quietly drop out of its coverage.
 */
function previewConversion(
  target: RequisitionConversionTarget,
  lines: RequisitionConversionOverride[],
): Promise<RequisitionConversionResult> {
  const body: RequisitionConversionPayload = { lines, acknowledge_duplicates: false }
  if (target.kind === 'single') {
    return api.post<RequisitionConversionResult>(
      `/procurement/purchase-requisitions/${target.requisition.id}/conversion-preview`,
      body,
    )
  }
  const batch: RequisitionBatchConversionPayload = {
    ...body,
    requisition_ids: target.requisitions.map((requisition) => requisition.id),
  }
  return api.post<RequisitionConversionResult>(
    '/procurement/purchase-requisitions/conversion-preview',
    batch,
  )
}

/**
 * The conversion itself.
 *
 * `acknowledgement` is the one supported answer to a duplicate refusal: the
 * same request resubmitted with `acknowledge_duplicates` and the buyer's reason,
 * which the API refuses without `purchase_order.approve`.
 */
function convertRequisitions(
  target: RequisitionConversionTarget,
  lines: RequisitionConversionOverride[],
  acknowledgement: string | null,
): Promise<RequisitionConversionResult> {
  const body: RequisitionConversionPayload = acknowledgement
    ? {
        lines,
        acknowledge_duplicates: true,
        duplicate_acknowledgement_reason: acknowledgement,
      }
    : { lines, acknowledge_duplicates: false }
  if (target.kind === 'single') {
    return api.post<RequisitionConversionResult>(
      `/procurement/purchase-requisitions/${target.requisition.id}/convert-to-orders`,
      body,
    )
  }
  const batch: RequisitionBatchConversionPayload = {
    ...body,
    requisition_ids: target.requisitions.map((requisition) => requisition.id),
  }
  return api.post<RequisitionConversionResult>(
    '/procurement/purchase-requisitions/convert-to-orders',
    batch,
  )
}

/**
 * A duplicate is a business answer, not a transport failure: returned as an
 * outcome it stays off the global error toast, which would otherwise sit on top
 * of the dialog that actually explains it.
 */
type ConvertOutcome =
  | { kind: 'converted'; result: RequisitionConversionResult }
  | { kind: 'duplicate'; conflict: DuplicateConflict }

const CONVERSION_DUPLICATE_COPY: DuplicateDialogCopy = {
  notCreated: 'No purchase order was raised - the conversion raises every order or none.',
  dismissLabel: 'Back to the proposal',
  confirmLabel: 'Raise them anyway',
}

/** Whether `key` begins with `prefix`, the way TanStack matches an invalidation. */
const startsWith = (key: readonly unknown[], prefix: readonly unknown[]): boolean =>
  prefix.every((part, index) => key[index] === part)

function lineLabel(line: { requisition_code: string; line_number: number }): string {
  return `${line.requisition_code} line ${line.line_number}`
}

// --------------------------------------------------------------- dialog -----

export default function RequisitionConversionDialog({
  target,
  open,
  onOpenChange,
}: {
  target: RequisitionConversionTarget
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const [edits, setEdits] = useState<Record<string, LineEdit>>({})
  // An excluded line leaves the preview altogether, so what to call it while it
  // is out - and how to bring it back - is remembered here.
  const [excludedNames, setExcludedNames] = useState<Record<string, string>>({})
  const [conflict, setConflict] = useState<DuplicateConflict | null>(null)

  const requisitionIds = useMemo(
    () =>
      target.kind === 'single'
        ? [target.requisition.id]
        : target.requisitions.map((requisition) => requisition.id),
    [target],
  )

  // Every correction is a new proposal, asked for once the typing stops.
  const overrides = useMemo(() => overridesOf(edits), [edits])
  const typed = JSON.stringify(overrides)
  const settled = useDebouncedValue(typed)
  const settledOverrides = useMemo(
    () => JSON.parse(settled) as RequisitionConversionOverride[],
    [settled],
  )
  const settling = settled !== typed

  const preview = useQuery({
    queryKey: requisitionKeys.conversion(requisitionIds, settledOverrides),
    queryFn: () => previewConversion(target, settledOverrides),
    enabled: open,
    // The proposal being corrected stays on screen while the next one is
    // computed, instead of collapsing to a skeleton on every keystroke.
    placeholderData: keepPreviousData,
    staleTime: staleTimes.transactional,
    // Rendered inline with a retry; a toast on top would say it twice.
    meta: { suppressErrorToast: true },
  })

  const warehouses = useWarehouseOptions(open && can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const convert = useMutation({
    mutationFn: async (acknowledgement: string | null): Promise<ConvertOutcome> => {
      try {
        const result = await convertRequisitions(target, settledOverrides, acknowledgement)
        return { kind: 'converted', result }
      } catch (error) {
        const duplicate = parseDuplicateConflict(error)
        if (duplicate) return { kind: 'duplicate', conflict: duplicate }
        throw error
      }
    },
    onSuccess: (outcome) => {
      if (outcome.kind === 'duplicate') {
        setConflict(outcome.conflict)
        return
      }
      // The requisitions, the order book and planning all changed. The preview
      // being closed is left alone: asked again, it would only describe orders
      // that now exist.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.procurement.all,
        predicate: (query) => !startsWith(query.queryKey, requisitionKeys.conversions),
      })
      void queryClient.invalidateQueries({ queryKey: queryKeys.mrp.all })
      const codes = outcome.result.groups.flatMap((group) =>
        group.purchase_order_code ? [group.purchase_order_code] : [],
      )
      const copy = raisedDocuments(codes, { one: 'Purchase order', many: 'purchase orders' })
      toast.success(copy.title, copy.description)
      setConflict(null)
      onOpenChange(false)
    },
    // A refusal the preview did not foresee - a line ordered by somebody else a
    // moment ago, say - is reported by the global handler; asking again shows
    // the buyer what changed.
    onError: () => void preview.refetch(),
  })

  function update(lineId: string, change: Partial<LineEdit>) {
    setEdits((current) => {
      const next = { ...(current[lineId] ?? NO_EDIT), ...change }
      const copy = { ...current }
      if (isUntouched(next)) delete copy[lineId]
      else copy[lineId] = next
      return copy
    })
  }

  function leaveOut(lineId: string, name: string) {
    setExcludedNames((current) => ({ ...current, [lineId]: name }))
    update(lineId, { exclude: true })
  }

  const result = preview.data
  const groups = result?.groups ?? []
  const unresolved = result?.unresolved_lines ?? []
  const excluded = Object.entries(edits).filter(([, edit]) => edit.exclude)
  // A line left out is not converted, so what was typed on it cannot hold the
  // rest back - and its fields are no longer on screen to be corrected.
  const invalid = Object.values(edits).some((edit) => !edit.exclude && hasInvalidText(edit))
  const overLimit = result ? groups.length > result.max_orders : false
  const lineCount = groups.reduce((sum, group) => sum + group.lines.length, 0)

  const ready =
    Boolean(result) &&
    !settling &&
    !preview.isFetching &&
    !preview.error &&
    !invalid &&
    groups.length > 0 &&
    unresolved.length === 0 &&
    !overLimit

  const title =
    target.kind === 'single'
      ? `Raise purchase orders from ${target.requisition.code}`
      : `Raise purchase orders from ${target.requisitions.length} requisitions`

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent size="xl">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>
              One draft purchase order per vendor and item category, each handed to the vendor user
              who manages that category. Correct a line and the proposal is worked out again. Every
              order still goes through approval before the vendor sees it.
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="space-y-4">
            {target.kind === 'batch' && (
              <ul className="flex flex-wrap gap-1.5" aria-label="Requisitions being converted">
                {target.requisitions.map((requisition) => (
                  <li key={requisition.id}>
                    <Badge variant="secondary">{requisition.code}</Badge>
                  </li>
                ))}
              </ul>
            )}

            {preview.isLoading && (
              <div className="space-y-3" aria-busy="true">
                <Skeleton className="h-5 w-1/3" />
                <Skeleton className="h-32 w-full" />
                <Skeleton className="h-32 w-full" />
              </div>
            )}

            {!preview.isLoading && preview.error && (
              <Alert variant="destructive">
                <AlertTitle>The proposal could not be worked out</AlertTitle>
                <AlertDescription>
                  {describeError(preview.error)}
                  <span className="mt-3 flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" onClick={() => void preview.refetch()}>
                      <RefreshCw />
                      Try again
                    </Button>
                    {overrides.length > 0 && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setEdits({})
                          setExcludedNames({})
                        }}
                      >
                        <Undo2 />
                        Discard my changes
                      </Button>
                    )}
                  </span>
                </AlertDescription>
              </Alert>
            )}

            {result && !preview.error && (
              <>
                <ProposalSummary
                  orderCount={groups.length}
                  lineCount={lineCount}
                  skipped={result.skipped_line_count}
                  updating={settling || preview.isFetching}
                />

                {overLimit && (
                  <Alert variant="warning">
                    <AlertTitle>Too many orders for one conversion</AlertTitle>
                    <AlertDescription>
                      This would raise {groups.length} purchase orders; at most {result.max_orders}{' '}
                      can be raised at once. Leave some lines out, or convert fewer requisitions,
                      and raise the rest afterwards.
                    </AlertDescription>
                  </Alert>
                )}

                {result.requires_acknowledgement && (
                  <Alert variant="warning">
                    <ShieldAlert aria-hidden />
                    <AlertTitle>Some of these orders may already be on order</AlertTitle>
                    <AlertDescription>
                      The orders flagged below overlap open purchase orders. Raising them anyway
                      needs purchase order approval rights and a recorded reason - you are asked for
                      it when you raise them.
                    </AlertDescription>
                  </Alert>
                )}

                {unresolved.length > 0 && (
                  <UnresolvedLines
                    lines={unresolved}
                    edits={edits}
                    canPickVendor={can(P.VENDOR_VIEW)}
                    onChange={update}
                    onLeaveOut={leaveOut}
                  />
                )}

                {groups.map((group) => (
                  <ProposedOrder
                    key={`${group.vendor_id}:${group.item_category_id ?? ''}:${group.warehouse_id}`}
                    group={group}
                    warehouseCode={warehouseById.get(group.warehouse_id)?.code ?? null}
                    edits={edits}
                    canPickVendor={can(P.VENDOR_VIEW)}
                    onChange={update}
                    onLeaveOut={leaveOut}
                  />
                ))}

                {groups.length === 0 && unresolved.length === 0 && (
                  <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                    Nothing is left to order: every line is already on an order, cancelled or left
                    out.
                  </p>
                )}
              </>
            )}

            {excluded.length > 0 && (
              <section aria-labelledby="conversion-excluded" className="rounded-lg border p-3">
                <h3 id="conversion-excluded" className="text-sm font-medium">
                  Left out of this conversion
                </h3>
                <p className="text-xs text-muted-foreground">
                  They stay open on the requisition, ready to be ordered later.
                </p>
                <ul className="mt-2 divide-y">
                  {excluded.map(([lineId]) => (
                    <li
                      key={lineId}
                      className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
                    >
                      <span className="min-w-0 break-words">
                        {excludedNames[lineId] ?? 'Requisition line'}
                      </span>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => update(lineId, { exclude: false })}
                      >
                        <Undo2 />
                        Include again
                      </Button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </DialogBody>

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              disabled={!ready}
              loading={convert.isPending}
              onClick={() => convert.mutate(null)}
            >
              {groups.length === 0
                ? 'Raise purchase orders'
                : groups.length === 1
                  ? 'Raise 1 purchase order'
                  : `Raise ${groups.length} purchase orders`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {conflict && (
        <PurchaseOrderDuplicateDialog
          conflict={conflict}
          open={Boolean(conflict)}
          onOpenChange={(next) => {
            if (!next) setConflict(null)
          }}
          onAcknowledge={(reason) => convert.mutate(reason)}
          isResolving={convert.isPending}
          canAcknowledge={can(P.PURCHASE_ORDER_APPROVE)}
          copy={CONVERSION_DUPLICATE_COPY}
        />
      )}
    </>
  )
}

// -------------------------------------------------------------- summary -----

function ProposalSummary({
  orderCount,
  lineCount,
  skipped,
  updating,
}: {
  orderCount: number
  lineCount: number
  skipped: number
  updating: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" role="status">
      <span className="font-medium">
        {orderCount === 1 ? '1 purchase order' : `${orderCount} purchase orders`}
      </span>
      <span className="text-muted-foreground">
        {lineCount === 1 ? '1 line' : `${lineCount} lines`}
      </span>
      {skipped > 0 && (
        <span className="text-muted-foreground">
          {skipped === 1 ? '1 line' : `${skipped} lines`} not included - already ordered, cancelled
          or left out
        </span>
      )}
      {updating && <span className="text-xs text-muted-foreground">Updating the proposal...</span>}
    </div>
  )
}

// ------------------------------------------------------- proposed order -----

function ProposedOrder({
  group,
  warehouseCode,
  edits,
  canPickVendor,
  onChange,
  onLeaveOut,
}: {
  group: RequisitionConversionGroup
  warehouseCode: string | null
  edits: Record<string, LineEdit>
  canPickVendor: boolean
  onChange: (lineId: string, change: Partial<LineEdit>) => void
  onLeaveOut: (lineId: string, name: string) => void
}) {
  const headingId = `conversion-${group.vendor_id}-${group.item_category_id ?? 'none'}-${group.warehouse_id}`
  const category = group.item_category_name
    ? `${group.item_category_name}${group.item_category_code ? ` (${group.item_category_code})` : ''}`
    : 'No item category'
  const assigned = group.assigned_vendor_user_name

  return (
    <section aria-labelledby={headingId} className="rounded-lg border">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b p-3">
        <div className="min-w-0">
          <h3 id={headingId} className="break-words font-medium">
            {group.vendor_name}{' '}
            <span className="text-xs font-normal text-muted-foreground">{group.vendor_code}</span>
          </h3>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <Badge variant="default">{category}</Badge>
            {warehouseCode && <span>Deliver to {warehouseCode}</span>}
            <span>Expected {formatDate(group.expected_delivery_date)}</span>
            <span className={isUrgentPriority(group.priority) ? 'font-medium text-warning' : ''}>
              Priority {group.priority}
            </span>
          </div>
        </div>
        <div className="text-right">
          <p className="font-semibold tabular">
            {formatMoney(group.total_amount, { currency: group.currency })}
          </p>
          <p className="text-xs text-muted-foreground">
            incl. {formatMoney(group.tax_amount, { currency: group.currency })} tax
          </p>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2 text-sm">
        <UserRound className="h-4 w-4 text-muted-foreground" aria-hidden />
        <span className="font-medium">{assigned ?? 'Unassigned'}</span>
        <span className="text-xs text-muted-foreground">
          {ASSIGNEE_REASON_COPY[group.assignee_reason]}
        </span>
      </div>

      {group.duplicate_candidates.length > 0 && (
        <div className="border-b p-3">
          <Alert variant="warning">
            <AlertTitle>May duplicate an open order</AlertTitle>
            <AlertDescription>
              <ul className="mt-1 space-y-1">
                {group.duplicate_candidates.map((candidate) => {
                  const confidence =
                    DUPLICATE_CONFIDENCE_COPY[duplicateConfidence(candidate.confidence)]
                  return (
                    <li
                      key={candidate.purchase_order_line_id}
                      className="flex flex-wrap items-center gap-2"
                    >
                      <Link
                        to={`/procurement/purchase-orders/${candidate.purchase_order_id}`}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="inline-flex items-center gap-1 font-medium text-foreground hover:underline"
                      >
                        {candidate.purchase_order_code}
                        <ExternalLink className="h-3 w-3" aria-hidden />
                        <span className="sr-only">(opens in a new tab)</span>
                      </Link>
                      <StatusBadge status={candidate.status} />
                      <Badge variant={confidence.variant}>{confidence.label}</Badge>
                      <span className="text-xs">
                        {formatQuantity(candidate.open_quantity)} still expected
                      </span>
                    </li>
                  )
                })}
              </ul>
            </AlertDescription>
          </Alert>
        </div>
      )}

      <ul className="divide-y">
        {group.lines.map((line) => (
          <ProposedLine
            key={line.requisition_line_id}
            line={line}
            currency={group.currency}
            edit={edits[line.requisition_line_id] ?? NO_EDIT}
            canPickVendor={canPickVendor}
            onChange={(change) => onChange(line.requisition_line_id, change)}
            onLeaveOut={() =>
              onLeaveOut(
                line.requisition_line_id,
                `${lineLabel(line)} - ${line.item_code} ${line.item_name}`,
              )
            }
          />
        ))}
      </ul>
    </section>
  )
}

function ProposedLine({
  line,
  currency,
  edit,
  canPickVendor,
  onChange,
  onLeaveOut,
}: {
  line: RequisitionConversionLine
  currency: string
  edit: LineEdit
  canPickVendor: boolean
  onChange: (change: Partial<LineEdit>) => void
  onLeaveOut: () => void
}) {
  const id = line.requisition_line_id
  const name = lineLabel(line)
  const quantityInvalid = edit.quantity.trim() !== '' && !isQuantity(edit.quantity)
  const priceInvalid = edit.unitPrice.trim() !== '' && !isUnitPrice(edit.unitPrice)
  const days = daysUntil(line.required_date)

  return (
    <li className="space-y-3 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="break-words text-sm">
            <span className="font-medium">{line.item_code}</span>{' '}
            <span className="text-muted-foreground">{line.item_name}</span>
          </p>
          <p className="text-xs text-muted-foreground">
            {name}
            {line.required_date && (
              <>
                {' '}
                - needed by{' '}
                <span className={days !== null && days < 0 ? 'text-destructive' : undefined}>
                  {formatDate(line.required_date)}
                </span>
              </>
            )}
          </p>
        </div>
        <p className="text-sm font-medium tabular">{formatMoney(line.line_total, { currency })}</p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[9rem_10rem_minmax(0,1fr)_auto] lg:items-start">
        <div>
          <Label htmlFor={`conversion-quantity-${id}`}>
            Quantity<span className="sr-only"> for {name}</span>
          </Label>
          <Input
            id={`conversion-quantity-${id}`}
            inputMode="decimal"
            value={edit.quantity}
            placeholder={plain(line.quantity)}
            onChange={(event) => onChange({ quantity: event.target.value })}
            aria-invalid={quantityInvalid}
            className="mt-1.5"
          />
          {quantityInvalid ? (
            <FieldError message="A quantity above zero, with up to 4 decimal places" />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              {edit.quantity.trim() === '' ? 'Still to order' : 'Your quantity'}
            </p>
          )}
        </div>

        <div>
          <Label htmlFor={`conversion-price-${id}`}>
            Unit price<span className="sr-only"> for {name}</span>
          </Label>
          <Input
            id={`conversion-price-${id}`}
            inputMode="decimal"
            value={edit.unitPrice}
            placeholder={plain(line.unit_price)}
            onChange={(event) => onChange({ unitPrice: event.target.value })}
            aria-invalid={priceInvalid}
            className="mt-1.5"
          />
          {priceInvalid ? (
            <FieldError message="A price with up to 6 decimal places" />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              {PRICE_SOURCE_COPY[line.price_source]}
            </p>
          )}
        </div>

        <div className="min-w-0">
          {canPickVendor ? (
            <VendorPicker
              id={`conversion-vendor-${id}`}
              label={`Vendor for ${name}`}
              hideLabel
              value={edit.vendorId}
              onChange={(vendorId) => onChange({ vendorId })}
              placeholder="Change vendor"
              approvedOnly
              className="mt-0 lg:mt-6"
            />
          ) : null}
          <p className="mt-1 text-xs text-muted-foreground">
            {VENDOR_SOURCE_COPY[line.vendor_source]}
          </p>
        </div>

        <Button
          variant="ghost"
          size="sm"
          className="justify-self-start lg:mt-6"
          onClick={onLeaveOut}
        >
          <PackageX />
          Leave out<span className="sr-only"> {name}</span>
        </Button>
      </div>
    </li>
  )
}

// ----------------------------------------------------- unresolved lines -----

/**
 * Lines the API cannot convert, each with every reason and a way to fix it here.
 *
 * A missing vendor or price is something the buyer can supply on the spot. An
 * item retired, deactivated or no longer purchasable since the requisition was
 * approved is not, and that line can only be left out - it stays open on the
 * requisition. Until each one is dealt with the API refuses the whole
 * conversion, so Raise stays disabled.
 */
function UnresolvedLines({
  lines,
  edits,
  canPickVendor,
  onChange,
  onLeaveOut,
}: {
  lines: RequisitionConversionUnresolvedLine[]
  edits: Record<string, LineEdit>
  canPickVendor: boolean
  onChange: (lineId: string, change: Partial<LineEdit>) => void
  onLeaveOut: (lineId: string, name: string) => void
}) {
  return (
    <section
      aria-labelledby="conversion-unresolved"
      className="rounded-lg border border-destructive/40 bg-destructive/5"
    >
      <header className="border-b border-destructive/20 p-3">
        <h3 id="conversion-unresolved" className="font-medium">
          {lines.length === 1
            ? '1 line cannot be converted yet'
            : `${lines.length} lines cannot be converted yet`}
        </h3>
        <p className="text-xs text-muted-foreground">
          Name a vendor or a price where that is what is missing, or leave the line out - it stays
          open on the requisition. Nothing is raised while any line is unresolved.
        </p>
      </header>
      <ul className="divide-y divide-destructive/20">
        {lines.map((line) => {
          const id = line.requisition_line_id
          const edit = edits[id] ?? NO_EDIT
          const name = lineLabel(line)
          const priceInvalid = edit.unitPrice.trim() !== '' && !isUnitPrice(edit.unitPrice)
          return (
            <li key={id} className="space-y-3 p-3">
              <div>
                <p className="text-sm font-medium">
                  {name} - {line.item_code}
                </p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
                  {line.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_10rem_auto] lg:items-start">
                {canPickVendor && (
                  <VendorPicker
                    id={`conversion-unresolved-vendor-${id}`}
                    label={`Vendor for ${name}`}
                    value={edit.vendorId}
                    onChange={(vendorId) => onChange(id, { vendorId })}
                    placeholder="Choose a vendor"
                    approvedOnly
                  />
                )}
                <div>
                  <Label htmlFor={`conversion-unresolved-price-${id}`}>
                    Unit price<span className="sr-only"> for {name}</span>
                  </Label>
                  <Input
                    id={`conversion-unresolved-price-${id}`}
                    inputMode="decimal"
                    value={edit.unitPrice}
                    onChange={(event) => onChange(id, { unitPrice: event.target.value })}
                    aria-invalid={priceInvalid}
                    className="mt-1.5"
                  />
                  {priceInvalid && <FieldError message="A price with up to 6 decimal places" />}
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="justify-self-start lg:mt-6"
                  onClick={() => onLeaveOut(id, `${name} - ${line.item_code}`)}
                >
                  <PackageX />
                  Leave out<span className="sr-only"> {name}</span>
                </Button>
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

// --------------------------------------------------------- batch picker -----

/**
 * Choose several approved requisitions to convert together.
 *
 * MRP raises one single-line requisition per suggestion, so a planning run's
 * purchases arrive as dozens of small documents; converting them one at a time
 * would give each vendor a purchase order per line. The list is the API's own
 * `convertible_only` narrowing - approved or converted with a line still to
 * order - paged and searched on the server like every other list, with its
 * state held here rather than in the queue's URL. A selection survives paging,
 * and stops at the API's bound.
 */
export function RequisitionBatchPicker({
  open,
  onOpenChange,
  onContinue,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onContinue: (requisitions: RequisitionRef[]) => void
}) {
  const { can } = useAuth()
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Map<string, RequisitionRef>>(() => new Map())

  const params = useMemo(
    () => ({
      page,
      limit: PICKER_PAGE_SIZE,
      with_total: true,
      convertible_only: true,
      // The most overdue need first: that is the order a buyer works them in.
      sort_by: 'required_date',
      sort_dir: 'asc' as const,
      ...(search ? { search } : {}),
    }),
    [page, search],
  )

  const list = useQuery({
    queryKey: requisitionKeys.convertible(params),
    queryFn: () => api.getPage<RequisitionRow>('/procurement/purchase-requisitions', { params }),
    enabled: open,
    placeholderData: keepPreviousData,
    staleTime: staleTimes.transactional,
  })

  const warehouses = useWarehouseOptions(open && can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const rows = useMemo(() => list.data?.data ?? [], [list.data])
  const full = selected.size >= MAX_REQUISITIONS_PER_CONVERSION

  const toggle = useCallback(
    (row: RequisitionRow, on: boolean) =>
      setSelected((current) => {
        const next = new Map(current)
        if (on && next.size < MAX_REQUISITIONS_PER_CONVERSION) {
          next.set(row.id, { id: row.id, code: row.code })
        } else if (!on) {
          next.delete(row.id)
        }
        return next
      }),
    [],
  )

  const pageIds = rows.map((row) => row.id)
  const pageSelected = pageIds.filter((rowId) => selected.has(rowId)).length
  const allOnPage = pageIds.length > 0 && pageSelected === pageIds.length

  const columns = useMemo<ColumnDef<RequisitionRow, unknown>[]>(
    () => [
      {
        id: 'select',
        header: () => (
          <Checkbox
            aria-label="Select every requisition on this page"
            checked={allOnPage ? true : pageSelected > 0 ? 'indeterminate' : false}
            onCheckedChange={(checked) =>
              setSelected((current) => {
                const next = new Map(current)
                for (const row of rows) {
                  if (checked === true) {
                    if (next.size >= MAX_REQUISITIONS_PER_CONVERSION) break
                    next.set(row.id, { id: row.id, code: row.code })
                  } else {
                    next.delete(row.id)
                  }
                }
                return next
              })
            }
          />
        ),
        enableSorting: false,
        enableHiding: false,
        cell: ({ row }) => {
          const checked = selected.has(row.original.id)
          return (
            <Checkbox
              aria-label={`Select ${row.original.code}`}
              checked={checked}
              disabled={!checked && full}
              onCheckedChange={(next) => toggle(row.original, next === true)}
            />
          )
        },
      },
      {
        id: 'code',
        header: 'Requisition',
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'required',
        header: 'Required',
        enableSorting: false,
        cell: ({ row }) => {
          const days = daysUntil(row.original.required_date)
          return (
            <span className={days !== null && days < 0 ? 'font-medium text-destructive' : ''}>
              {formatDate(row.original.required_date)}
            </span>
          )
        },
      },
      {
        id: 'to_order',
        header: 'To order',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.open_line_count === 1 ? '1 line' : `${row.original.open_line_count} lines`,
      },
      {
        id: 'warehouse',
        header: 'Deliver to',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {warehouseById.get(row.original.warehouse_id)?.code ?? '-'}
          </span>
        ),
      },
      {
        id: 'source',
        header: 'Raised from',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.source_type === 'MANUAL' ? (
            <span className="text-xs text-muted-foreground">Manual</span>
          ) : (
            <Badge variant="default">{humanise(row.original.source_type)}</Badge>
          ),
      },
      {
        id: 'purpose',
        header: 'Purpose',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block max-w-[16rem] truncate text-muted-foreground">
            {row.original.purpose ?? '-'}
          </span>
        ),
      },
    ],
    [allOnPage, full, pageSelected, rows, selected, toggle, warehouseById],
  )

  const chosen = [...selected.values()]

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Raise purchase orders from approved requisitions</DialogTitle>
          <DialogDescription>
            Tick the requisitions to convert together - up to {MAX_REQUISITIONS_PER_CONVERSION}.
            Their lines are pooled, so each vendor gets one order per item category and delivery
            warehouse rather than one per requisition.
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          <DataTable
            columns={columns}
            data={rows}
            pagination={list.data?.pagination}
            isLoading={list.isLoading}
            isFetching={list.isFetching}
            error={list.error}
            onPageChange={setPage}
            onSearchChange={(term) => {
              setSearch(term)
              setPage(1)
            }}
            onRefresh={() => void list.refetch()}
            searchValue={search}
            searchPlaceholder="Search requisition code or purpose..."
            getRowId={(row) => row.id}
            emptyTitle="Nothing approved is waiting to be ordered"
            emptyDescription="Every approved requisition is already on purchase orders."
          />
        </DialogBody>

        <DialogFooter className="sm:items-center">
          <p className={cn('text-sm text-muted-foreground sm:mr-auto', full && 'text-warning')}>
            {full
              ? `${chosen.length} selected - the most one conversion can take`
              : `${chosen.length} selected`}
          </p>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={chosen.length === 0} onClick={() => onContinue(chosen)}>
            Review purchase orders
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

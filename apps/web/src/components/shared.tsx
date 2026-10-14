/**
 * Cross-feature building blocks: page header, status badge, stat tile,
 * confirmation dialog, description list, field error, date-range filter,
 * panel loader.
 *
 * These exist so twenty screens present a status, a heading and a destructive
 * confirmation the same way. Divergence there is what makes an ERP feel
 * assembled by committee.
 */

import { AlertTriangle, ChevronRight, Loader2, type LucideIcon } from 'lucide-react'
import { type ComponentPropsWithoutRef, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { prefetchRoute } from '@/app/route-loaders'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Badge, Card, CardContent, Input, Label, Skeleton } from '@/components/ui/primitives'
import { humanise } from '@/lib/format'
import { cn } from '@/lib/utils'

// ------------------------------------------------------------ PanelLoader ---

/**
 * Suspense fallback for a lazily-loaded panel inside an already-rendered page.
 *
 * Sized for a tab body rather than a whole route, so switching to a chart tab
 * does not collapse the page around the spinner.
 */
export function PanelLoader() {
  return (
    <div className="grid h-64 place-items-center" role="status" aria-label="Loading">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
    </div>
  )
}

// ------------------------------------------------------------ PageHeader ----

export interface Breadcrumb {
  label: string
  to?: string
}

export function PageHeader({
  title,
  description,
  breadcrumbs,
  actions,
  status,
}: {
  title: string
  description?: string
  breadcrumbs?: Breadcrumb[]
  actions?: ReactNode
  status?: ReactNode
}) {
  return (
    <div className="mb-5">
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className="mb-1.5">
          <ol className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
            {breadcrumbs.map((crumb, index) => (
              <li key={`${crumb.label}-${index}`} className="flex items-center gap-1">
                {index > 0 && <ChevronRight className="h-3 w-3 opacity-50" aria-hidden />}
                {crumb.to ? (
                  <Link to={crumb.to} className="hover:text-foreground hover:underline">
                    {crumb.label}
                  </Link>
                ) : (
                  <span>{crumb.label}</span>
                )}
              </li>
            ))}
          </ol>
        </nav>
      )}

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="min-w-0 break-words text-xl font-semibold tracking-tight">{title}</h1>
            {status}
          </div>
          {description && (
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</p>
          )}
        </div>
        {/* No shrink-0 here: a flex item that cannot shrink is sized to all of
            its buttons on one line, so the inner flex-wrap would never fire. */}
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  )
}

// ----------------------------------------------------------- StatusBadge ----

/**
 * Maps every document status in the system to a visual weight.
 *
 * Central rather than per-feature so "APPROVED" is the same green on a purchase
 * order and on a BOM, and an operator learns the colour language once.
 */
const STATUS_VARIANTS: Record<
  string,
  'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'muted' | 'outline'
> = {
  // neutral / early lifecycle
  DRAFT: 'muted',
  OPEN: 'default',
  PLANNED: 'secondary',
  PENDING: 'warning',
  PENDING_APPROVAL: 'warning',
  QUEUED: 'secondary',
  NOT_DISPATCHED: 'muted',
  // A deactivated master, a retired machine and a deleted file are settled
  // facts, not drafts. Left unmapped they wore the same grey as DRAFT.
  INACTIVE: 'muted',
  ARCHIVED: 'muted',
  RETIRED: 'muted',
  DELETED: 'muted',
  ROTATED: 'muted',
  READ: 'muted',
  NOT_REQUIRED: 'muted',
  NOT_MATCHED: 'muted',

  // in flight
  RUNNING: 'default',
  IN_PROGRESS: 'default',
  IN_PROCESS: 'default',
  RELEASED: 'default',
  SENT: 'default',
  ACKNOWLEDGED: 'default',
  IN_TRANSIT: 'default',
  MATERIAL_ISSUED: 'default',
  COMPONENTS_ISSUED: 'default',
  ISSUED: 'default',
  ALLOCATED: 'default',
  READY_FOR_DISPATCH: 'default',
  UNREAD: 'default',
  COUNTED: 'default',
  ORDERED: 'default',
  JOB_WORK: 'default',
  PARTIALLY_RECEIVED: 'warning',
  PARTIALLY_DISPATCHED: 'warning',
  PARTIALLY_PAID: 'warning',
  PARTIALLY_ALLOCATED: 'warning',
  PARTIALLY_CONSUMED: 'warning',
  PARTIALLY_ORDERED: 'warning',
  QC_PENDING: 'warning',
  QC_HOLD: 'warning',
  HOLD: 'warning',
  REWORK: 'warning',
  DISPUTED: 'warning',
  UNDER_REVIEW: 'warning',
  QUARANTINED: 'warning',
  MAINTENANCE: 'warning',
  SUSPENDED: 'warning',
  VARIANCE: 'warning',
  OVERRIDDEN: 'warning',
  LATE: 'destructive',

  // settled, good
  APPROVED: 'success',
  CONFIRMED: 'success',
  COMPLETED: 'success',
  RECEIVED: 'success',
  DELIVERED: 'success',
  DISPATCHED: 'success',
  ACCEPTED: 'success',
  PAID: 'success',
  GRN_COMPLETED: 'success',
  QC_COMPLETED: 'success',
  INVOICED: 'success',
  CONSUMED: 'secondary',
  ACTIVE: 'success',
  AVAILABLE: 'success',
  IN_STOCK: 'success',
  RETURN: 'success',
  RETURNED: 'success',
  PACKED: 'success',
  HANDED_OVER: 'success',
  PASSED: 'success',
  MATCHED: 'success',
  SELECTED: 'success',
  EXECUTED: 'success',

  // settled, closed
  CLOSED: 'secondary',
  // Every goods receipt and every stock adjustment in a live tenant is POSTED,
  // and without an entry here all of them fell through to the neutral fallback
  // and read the same grey as a DRAFT. A posted document is a settled fact that
  // moved stock, which is the opposite of a draft.
  POSTED: 'success',
  OBSOLETE: 'muted',
  RELEASED_RESERVATION: 'muted',
  IGNORED: 'muted',
  CONVERTED: 'secondary',
  EXPIRED: 'muted',

  // bad outcomes
  CANCELLED: 'destructive',
  REJECTED: 'destructive',
  // Not terminal - the buyer cancels or re-sends - but it is a refusal to act on.
  VENDOR_DECLINED: 'destructive',
  FAILED: 'destructive',
  SCRAP: 'destructive',
  SCRAPPED: 'destructive',
  DAMAGED: 'destructive',
  BREAKDOWN: 'destructive',
  REVOKED: 'destructive',
  BLOCKED: 'destructive',
}

export function StatusBadge({ status, className }: { status?: string | null; className?: string }) {
  if (!status) return <span className="text-muted-foreground">-</span>
  const variant = STATUS_VARIANTS[status.toUpperCase()] ?? 'secondary'
  return (
    <Badge variant={variant} className={className}>
      {humanise(status)}
    </Badge>
  )
}

// -------------------------------------------------------------- StatTile ----

export function StatTile({
  label,
  value,
  hint,
  icon: Icon,
  tone = 'default',
  to,
  isLoading,
}: {
  label: string
  value: ReactNode
  hint?: string
  icon?: LucideIcon
  tone?: 'default' | 'success' | 'warning' | 'destructive'
  to?: string
  isLoading?: boolean
}) {
  const toneClass = {
    default: 'text-foreground',
    success: 'text-success',
    warning: 'text-warning',
    destructive: 'text-destructive',
  }[tone]
  // The value truncates on a narrow tile, so the full figure has to be
  // reachable somewhere - the title is where a truncated string belongs.
  const valueTitle =
    typeof value === 'string' || typeof value === 'number' ? String(value) : undefined

  const body = (
    <Card className={cn('h-full transition-colors', to && 'hover:border-primary/40')}>
      <CardContent className="flex items-start gap-3 p-4">
        {Icon && (
          <div className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-md bg-muted">
            <Icon className={cn('h-4 w-4', toneClass)} aria-hidden />
          </div>
        )}
        <div className="min-w-0">
          <p className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {label}
          </p>
          {isLoading ? (
            <Skeleton className="mt-1.5 h-7 w-20" />
          ) : (
            <p
              className={cn('mt-0.5 truncate text-2xl font-semibold tabular', toneClass)}
              title={valueTitle}
            >
              {value}
            </p>
          )}
          {hint && (
            <p className="mt-0.5 truncate text-xs text-muted-foreground" title={hint}>
              {hint}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )

  // A KPI tile is one of the most-clicked ways into a screen, so warm its chunk
  // on hover and on focus the way the sidebar already does. `prefetchRoute` is
  // idempotent, ignores a path it has no loader for, and swallows failures, so
  // this costs nothing when the tile points at a detail route or an anchor.
  const warm = to ? () => prefetchRoute(to) : undefined

  return to ? (
    <Link to={to} className="block focus-visible:rounded-lg" onMouseEnter={warm} onFocus={warm}>
      {body}
    </Link>
  ) : (
    body
  )
}

// -------------------------------------------------------- ConfirmDialog -----

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  loading = false,
  onConfirm,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
  loading?: boolean
  onConfirm: () => void
  children?: ReactNode
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader>
          <div className="flex items-start gap-3">
            {destructive && (
              <div className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-destructive/10">
                <AlertTriangle className="h-4 w-4 text-destructive" aria-hidden />
              </div>
            )}
            <div>
              <DialogTitle>{title}</DialogTitle>
              {description && (
                <DialogDescription className="mt-1.5">{description}</DialogDescription>
              )}
            </div>
          </div>
        </DialogHeader>

        {children}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button
            variant={destructive ? 'destructive' : 'default'}
            onClick={onConfirm}
            loading={loading}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------- DescriptionList ----

export function DescriptionList({
  items,
  columns = 2,
  className,
}: {
  items: { label: string; value: ReactNode }[]
  columns?: 1 | 2 | 3 | 4
  className?: string
}) {
  return (
    <dl
      className={cn(
        'grid gap-x-6 gap-y-3.5',
        {
          1: 'grid-cols-1',
          2: 'sm:grid-cols-2',
          3: 'sm:grid-cols-3',
          4: 'sm:grid-cols-2 lg:grid-cols-4',
        }[columns],
        className,
      )}
    >
      {items.map((item) => (
        <div key={item.label} className="min-w-0">
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {item.label}
          </dt>
          <dd className="mt-0.5 break-words text-sm">{item.value ?? '-'}</dd>
        </div>
      ))}
    </dl>
  )
}

// ------------------------------------------------------- DateRangeFilter ----

type DateInputProps = Omit<
  ComponentPropsWithoutRef<typeof Input>,
  'id' | 'type' | 'value' | 'onChange'
>

/**
 * The "From / To" pair every dated list filters on.
 *
 * One component so the toolbars agree on the label style, the input width and
 * - the part that matters on a phone - how the group breaks: a label stays
 * beside its own input, and the two pairs wrap independently, so a strip that
 * no longer fits stacks "To" under "From" instead of pushing the toolbar past
 * the viewport.
 *
 *     <DateRangeFilter
 *       id="orders-required"
 *       fromLabel="Required from"
 *       from={requiredFrom}
 *       to={requiredTo}
 *       onFromChange={(value) => setParam('required_from', value || undefined)}
 *       onToChange={(value) => setParam('required_to', value || undefined)}
 *     />
 *
 * The inputs are `${id}-from` and `${id}-to` - the names the hand-rolled
 * toolbars already used. Bounds and any other input attribute go through
 * `fromInputProps` / `toInputProps` (`{ max: to }`, `{ min: from, required: true }`).
 */
export function DateRangeFilter({
  id,
  from,
  to,
  onFromChange,
  onToChange,
  fromLabel = 'From',
  toLabel = 'To',
  fromInputProps,
  toInputProps,
  className,
}: {
  /** Prefix for the two input ids: `${id}-from` and `${id}-to`. */
  id: string
  /** ISO date (`YYYY-MM-DD`), or empty for no bound. */
  from: string
  to: string
  /** Receives the input's value - empty when the field is cleared. */
  onFromChange: (value: string) => void
  onToChange: (value: string) => void
  fromLabel?: ReactNode
  toLabel?: ReactNode
  fromInputProps?: DateInputProps
  toInputProps?: DateInputProps
  className?: string
}) {
  return (
    <div className={cn('flex flex-wrap items-center gap-1.5', className)}>
      <span className="flex items-center gap-1.5">
        <Label htmlFor={`${id}-from`} className="text-xs text-muted-foreground">
          {fromLabel}
        </Label>
        <Input
          id={`${id}-from`}
          type="date"
          value={from}
          onChange={(event) => onFromChange(event.target.value)}
          {...fromInputProps}
          className={cn('w-[9.5rem]', fromInputProps?.className)}
        />
      </span>
      <span className="flex items-center gap-1.5">
        <Label htmlFor={`${id}-to`} className="text-xs text-muted-foreground">
          {toLabel}
        </Label>
        <Input
          id={`${id}-to`}
          type="date"
          value={to}
          onChange={(event) => onToChange(event.target.value)}
          {...toInputProps}
          className={cn('w-[9.5rem]', toInputProps?.className)}
        />
      </span>
    </div>
  )
}

// ------------------------------------------------------------ FieldError ----

export function FieldError({ message }: { message?: string | string[] }) {
  if (!message) return null
  const text = Array.isArray(message) ? message.join('. ') : message
  return (
    <p role="alert" className="mt-1 text-xs text-destructive">
      {text}
    </p>
  )
}

// ------------------------------------------------------------ EmptyState ----

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon?: LucideIcon
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="rounded-lg border border-dashed py-14 text-center">
      {Icon && <Icon className="mx-auto mb-3 h-8 w-8 text-muted-foreground/40" aria-hidden />}
      <p className="font-medium">{title}</p>
      {description && (
        <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">{description}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  )
}

/**
 * Cross-feature building blocks: page header, status badge, stat tile,
 * confirmation dialog, description list, field error.
 *
 * These exist so twenty screens present a status, a heading and a destructive
 * confirmation the same way. Divergence there is what makes an ERP feel
 * assembled by committee.
 */

import { AlertTriangle, ChevronRight, type LucideIcon } from 'lucide-react'
import { type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Badge, Card, CardContent, Skeleton } from '@/components/ui/primitives'
import { humanise } from '@/lib/format'
import { cn } from '@/lib/utils'

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
            <h1 className="truncate text-xl font-semibold tracking-tight">{title}</h1>
            {status}
          </div>
          {description && (
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
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
  PENDING: 'warning',
  PENDING_APPROVAL: 'warning',
  QUEUED: 'secondary',
  NOT_DISPATCHED: 'muted',

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
  PARTIALLY_RECEIVED: 'warning',
  PARTIALLY_DISPATCHED: 'warning',
  PARTIALLY_PAID: 'warning',
  PARTIALLY_ALLOCATED: 'warning',
  PARTIALLY_CONSUMED: 'warning',
  QC_PENDING: 'warning',
  HOLD: 'warning',
  REWORK: 'warning',
  DISPUTED: 'warning',
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

  // settled, closed
  CLOSED: 'secondary',
  OBSOLETE: 'muted',
  RELEASED_RESERVATION: 'muted',
  IGNORED: 'muted',
  CONVERTED: 'secondary',
  EXPIRED: 'muted',

  // bad outcomes
  CANCELLED: 'destructive',
  REJECTED: 'destructive',
  FAILED: 'destructive',
  SCRAP: 'destructive',
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
            <p className={cn('mt-0.5 truncate text-2xl font-semibold tabular', toneClass)}>
              {value}
            </p>
          )}
          {hint && <p className="mt-0.5 truncate text-xs text-muted-foreground">{hint}</p>}
        </div>
      </CardContent>
    </Card>
  )

  return to ? (
    <Link to={to} className="block focus-visible:rounded-lg">
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

/**
 * A table drawn the way DataTable draws one, for a screen that lays out its own
 * rows: a document's lines, a statement, a summary.
 *
 * Thirty raw tables re-spelled their header and cell classes a dozen ways, and
 * six of them left money and quantities left-aligned in proportional figures, so
 * a dealer's statement or a vendor's job-work quantities could not be compared
 * digit by digit - which `align="right"` (`text-right tabular`) exists to
 * guarantee. The parts here carry DataTable's own classes; DataTable renders its
 * header and body cells with them, so the two cannot drift apart.
 *
 * `TableScroller` is the overflow wrapper every one of them needs: a named,
 * focusable region, so a keyboard user can scroll to the right-hand columns -
 * the amounts, totals and statuses - where the browser does not make a scroller
 * focusable by itself (Safari, Chrome before 130).
 */

import {
  forwardRef,
  type HTMLAttributes,
  type ReactNode,
  type TdHTMLAttributes,
  type ThHTMLAttributes,
} from 'react'

import { cn } from '@/lib/utils'

export type TableAlign = 'left' | 'right' | 'center'

const HEAD_ALIGN: Record<TableAlign, string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
}

// Quantities and money are read in columns, digit under digit.
const CELL_ALIGN: Record<TableAlign, string> = {
  left: '',
  right: 'text-right tabular',
  center: 'text-center',
}

/** The classes DataTable's header cells wear. */
export const tableHeadClassName =
  'h-10 px-3 align-middle text-xs font-semibold uppercase tracking-wide text-muted-foreground'

/** The classes DataTable's body cells wear. */
export const tableCellClassName = 'px-3 py-2 align-middle'

/** A table's horizontal scroller: a named region a keyboard can reach and scroll. */
export function TableScroller({
  label,
  className,
  children,
}: {
  /** The region's name: what the table holds, as its caption or heading says it. */
  label: string
  className?: string
  children: ReactNode
}) {
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      className={cn(
        'overflow-x-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        className,
      )}
    >
      {children}
    </div>
  )
}

export const Table = forwardRef<HTMLTableElement, HTMLAttributes<HTMLTableElement>>(
  ({ className, ...props }, ref) => (
    <table ref={ref} className={cn('w-full caption-bottom text-sm', className)} {...props} />
  ),
)
Table.displayName = 'Table'

export const TableHeader = forwardRef<
  HTMLTableSectionElement,
  HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead ref={ref} className={cn('bg-muted/50 [&_tr]:border-b', className)} {...props} />
))
TableHeader.displayName = 'TableHeader'

export const TableBody = forwardRef<
  HTMLTableSectionElement,
  HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn('[&_tr:last-child]:border-0', className)} {...props} />
))
TableBody.displayName = 'TableBody'

export const TableFooter = forwardRef<
  HTMLTableSectionElement,
  HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tfoot
    ref={ref}
    className={cn('border-t bg-muted/50 font-medium [&_tr]:border-0', className)}
    {...props}
  />
))
TableFooter.displayName = 'TableFooter'

export const TableRow = forwardRef<HTMLTableRowElement, HTMLAttributes<HTMLTableRowElement>>(
  ({ className, ...props }, ref) => (
    <tr ref={ref} className={cn('border-b transition-colors', className)} {...props} />
  ),
)
TableRow.displayName = 'TableRow'

export const TableHead = forwardRef<
  HTMLTableCellElement,
  ThHTMLAttributes<HTMLTableCellElement> & { align?: TableAlign }
>(({ className, align = 'left', scope = 'col', ...props }, ref) => (
  <th
    ref={ref}
    scope={scope}
    className={cn(tableHeadClassName, HEAD_ALIGN[align], className)}
    {...props}
  />
))
TableHead.displayName = 'TableHead'

export const TableCell = forwardRef<
  HTMLTableCellElement,
  TdHTMLAttributes<HTMLTableCellElement> & { align?: TableAlign }
>(({ className, align = 'left', ...props }, ref) => (
  <td ref={ref} className={cn(tableCellClassName, CELL_ALIGN[align], className)} {...props} />
))
TableCell.displayName = 'TableCell'

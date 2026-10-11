/**
 * The cost-centre picker every carrier form uses, and the label every detail
 * view prints.
 *
 * Six documents carry a cost centre - departments, work centres, production
 * and job-work orders, stock adjustments and vendor invoices - and each one
 * needs the same three behaviours, which is why they live here once:
 *
 * 1. **Optional, and null means unallocated** (docs/assumptions.md §B1-1). The
 *    first option is always the empty one, and it is labelled for what it
 *    means on that form - "Unallocated" on a document, "As the invoice" on an
 *    invoice line, "Top level" for a parent.
 * 2. **Active centres only, plus the one already chosen.** A new allocation to
 *    an inactive centre is refused, but a document keeps the centre it was
 *    raised with after somebody deactivates it - so its own edit form must
 *    still be able to name it (§B1-3).
 * 3. **The tree reads as a tree.** Children are indented under their parent,
 *    built from the full reference list rather than from whichever page of the
 *    register happens to be loaded.
 *
 * Callers render these only for an account holding `cost_centre.view`, the
 * right `GET /organization/cost-centres` enforces, and pass `enabled` so the
 * list is fetched only while a dialog is actually open.
 */

import { useMemo } from 'react'

import { FieldError } from '@/components/shared'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Label } from '@/components/ui/primitives'
import { useCostCentreOptions } from '@/hooks/use-organisation-options'
import { SELECT_NONE } from '@/lib/constants'
import { cn } from '@/lib/utils'

import { costCentreLabel, pickableCostCentres } from './cost-centre-model'

// ASSUMPTION: a picker is rendered only for `cost_centre.view`. Without it a new
// document goes out unallocated and an edit carries the saved centre back
// unchanged - every caller seeds its form from the record for exactly that
// reason. docs/assumptions.md §B1-9.

/** The item's own left padding (`pl-8`), which the check mark sits inside. */
const ITEM_INSET_REM = 2
/** How far each level of the tree steps in. */
const LEVEL_INDENT_REM = 0.75

export function CostCentreSelect({
  id,
  label = 'Cost centre',
  value,
  onChange,
  enabled = true,
  noneLabel = 'Unallocated',
  hint,
  error,
  exclude,
  disabled = false,
  className,
}: {
  id: string
  label?: string
  /** The chosen centre, or null for none. */
  value: string | null
  onChange: (costCentreId: string | null) => void
  /** Whether the list may be fetched - false while the dialog is closed. */
  enabled?: boolean
  /** What "no centre" means on this form. */
  noneLabel?: string
  hint?: string
  error?: string
  /** Centres that may not be chosen here - a centre's own subtree, as its parent. */
  exclude?: ReadonlySet<string>
  disabled?: boolean
  className?: string
}) {
  const options = useCostCentreOptions(enabled)
  const entries = useMemo(
    () => pickableCostCentres(options.data ?? [], value, exclude),
    [options.data, value, exclude],
  )

  // The record's value is kept selectable even when the list cannot name it -
  // a list cut off at the lookup limit - rather than shown as unallocated,
  // which the next save would then make true.
  const unlisted =
    value !== null && options.isSuccess && !entries.some(({ row }) => row.id === value)

  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id}>{label}</Label>
      <Select
        value={value ?? SELECT_NONE}
        onValueChange={(next) => onChange(next === SELECT_NONE ? null : next)}
        disabled={disabled}
      >
        <SelectTrigger
          id={id}
          aria-label={label}
          aria-invalid={Boolean(error)}
          className={cn(error && 'border-destructive')}
        >
          <SelectValue placeholder={options.isLoading ? 'Loading...' : noneLabel} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SELECT_NONE}>{noneLabel}</SelectItem>
          {entries.map(({ row, depth }) => (
            <SelectItem
              key={row.id}
              value={row.id}
              // On the item rather than inside its text: the text is what the
              // closed trigger repeats, and it should not arrive indented.
              style={
                depth > 0
                  ? { paddingLeft: `${ITEM_INSET_REM + depth * LEVEL_INDENT_REM}rem` }
                  : undefined
              }
            >
              {costCentreLabel(row)}
            </SelectItem>
          ))}
          {unlisted && value !== null && (
            <SelectItem value={value}>The cost centre already recorded</SelectItem>
          )}
        </SelectContent>
      </Select>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      <FieldError message={error} />
    </div>
  )
}

/**
 * One cost centre as a person reads it - "CC-MACH - Machining" - for a detail
 * view.
 *
 * Read from the same bounded list the pickers use, so it costs no request a
 * picker on the same screen has not already made, and it names an inactive
 * centre as faithfully as an active one.
 */
export function CostCentreName({
  id,
  enabled = true,
  emptyLabel = 'Unallocated',
}: {
  id: string | null | undefined
  enabled?: boolean
  emptyLabel?: string
}) {
  const options = useCostCentreOptions(enabled && Boolean(id))

  if (!id) return <span className="text-muted-foreground">{emptyLabel}</span>

  const row = options.data?.find((entry) => entry.id === id)
  if (!row) return <span className="text-muted-foreground">-</span>
  return <span>{costCentreLabel(row)}</span>
}

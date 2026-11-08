/**
 * The small components every tab on the item-masters screen shares.
 *
 * Moved out of `masters-page.tsx` when the brand and manufacturer tabs arrived
 * as their own lazily loaded modules: a tab that imported these from the page
 * would tie its chunk back to the page's, and a second copy in each tab is how
 * two tabs come to filter the same URL keys two different ways. The URL-writing
 * hook beside them lives in `use-filter-writer.ts`.
 */

import { type ReactNode } from 'react'

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge } from '@/components/ui/primitives'
import { SELECT_ALL } from '@/lib/constants'

/** Active / inactive, the same three-way control on most of the tabs. */
export function StatusFilter({
  value,
  onChange,
  label,
}: {
  value: string
  onChange: (next: string | undefined) => void
  label: string
}) {
  return (
    <Select
      value={value || SELECT_ALL}
      onValueChange={(next) => onChange(next === SELECT_ALL ? undefined : next)}
    >
      <SelectTrigger className="w-full sm:w-[150px]" aria-label={label}>
        <SelectValue placeholder="Any status" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={SELECT_ALL}>Any status</SelectItem>
        <SelectItem value="active">Active</SelectItem>
        <SelectItem value="inactive">Inactive</SelectItem>
      </SelectContent>
    </Select>
  )
}

export function ActiveBadge({ isActive }: { isActive: boolean }) {
  return isActive ? (
    <Badge variant="secondary">Active</Badge>
  ) : (
    <Badge variant="muted">Inactive</Badge>
  )
}

/** The sentence that introduces a tab, above its table. */
export function TabIntro({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 mt-4 flex flex-wrap items-start justify-between gap-3">
      <p className="max-w-3xl text-sm text-muted-foreground">{children}</p>
      {actions && (
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
  )
}

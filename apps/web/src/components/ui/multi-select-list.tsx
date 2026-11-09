/**
 * A list to choose several rows from: one Tab stop, the arrow keys, Space to tick.
 *
 * The serial picker shared by dispatch, packing, job work, transfers and QC drew
 * each unit as its own toggle button - up to fifty Tab stops between its search
 * box and the form's next field, each 34px tall - and the MRP warehouse choice
 * drew a column of `role="checkbox"` buttons the arrow keys could not reach,
 * saying "Loading warehouses..." and "No warehouses are configured yet." to no
 * one. `SearchPicker` had already solved both for a single choice; this is its
 * keyboard model for several:
 *
 * - A `listbox` with `aria-multiselectable`, whose rows are `option`s that say
 *   whether each is ticked through `aria-selected`. The list is one Tab stop (a
 *   roving tabindex): ArrowDown, ArrowUp, Home and End move, and Space or Enter
 *   ticks or unticks the row. DOM focus moves, as in `SearchPicker`, because
 *   real focus is what every screen reader follows.
 * - ArrowUp on the first row calls `onLeaveTop`, where a caller hands focus back
 *   to its search box, and a character typed in the list goes to `onType`, so it
 *   refines the search instead of being lost.
 * - A row that cannot be ticked is listed with its reason and can be reached, so
 *   it is read rather than skipped, but does not tick (`aria-disabled`).
 * - Rows are at least 44px tall for a thumb.
 * - `status` - "Searching...", the empty sentence, a count for a screen reader -
 *   is said in one `role="status"` region beside the list, never inside it, and
 *   the region is always mounted so a change to it is announced.
 *
 * No count is imposed. A caller that needs exactly N says so beside the list, as
 * the serial picker's "2 of 3 identified" does, and the API stays the judge.
 */

import { Check } from 'lucide-react'
import {
  useId,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
} from 'react'

import { cn } from '@/lib/utils'

export interface MultiSelectListHandle {
  /** Moves focus into the list, at its Tab stop. */
  focus: () => void
}

export interface MultiSelectListProps<T> {
  /** The listbox's id, for a search box's `aria-controls`; generated when omitted. */
  id?: string
  /** The listbox's accessible name. */
  label: string
  rows: readonly T[]
  getKey: (row: T) => string
  isSelected: (row: T) => boolean
  /** A row listed with its reason that cannot be ticked. */
  isDisabled?: (row: T) => boolean
  onToggle: (row: T) => void
  renderOption: (row: T) => ReactNode
  /** Said in the live region beside the list. */
  status?: ReactNode
  /** The status region's classes: `sr-only` for a count only a screen reader needs. */
  statusClassName?: string
  /** The list's own classes, typically its maximum height. */
  className?: string
  /** ArrowUp on the first row, where a caller focuses its search box. */
  onLeaveTop?: () => void
  /** A character typed in the list, for the caller's search box. */
  onType?: (character: string) => void
  /** Lets a search box move focus into the list on ArrowDown. */
  handleRef?: Ref<MultiSelectListHandle>
}

export function MultiSelectList<T>({
  id,
  label,
  rows,
  getKey,
  isSelected,
  isDisabled,
  onToggle,
  renderOption,
  status,
  statusClassName,
  className,
  onLeaveTop,
  onType,
  handleRef,
}: MultiSelectListProps<T>) {
  const generatedId = useId()
  const listboxId = id ?? generatedId
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [activeKey, setActiveKey] = useState<string | null>(null)

  const keys = rows.map(getKey)
  optionRefs.current.length = rows.length
  const activeIndex = activeKey === null ? -1 : keys.indexOf(activeKey)
  // The list's one Tab stop: the row last reached, else the first ticked row,
  // else the first row (WAI-ARIA listbox).
  const stop = activeIndex >= 0 ? activeIndex : Math.max(rows.findIndex(isSelected), 0)

  const reach = (index: number) => {
    const option = optionRefs.current[index]
    if (!option) return
    setActiveKey(keys[index] ?? null)
    option.focus()
  }

  useImperativeHandle(handleRef, () => ({ focus: () => reach(stop) }))

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = optionRefs.current.indexOf(event.target as HTMLButtonElement)
    if (index < 0) return
    const last = rows.length - 1
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        reach(Math.min(index + 1, last))
        return
      case 'ArrowUp':
        event.preventDefault()
        if (index === 0) onLeaveTop?.()
        else reach(index - 1)
        return
      case 'Home':
        event.preventDefault()
        reach(0)
        return
      case 'End':
        event.preventDefault()
        reach(last)
        return
    }
    // Space is not a character here: on a row it ticks.
    const typed =
      event.key.length === 1 &&
      event.key !== ' ' &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    if (typed && onType) {
      event.preventDefault()
      onType(event.key)
    }
  }

  return (
    <>
      {rows.length > 0 && (
        <div
          id={listboxId}
          role="listbox"
          aria-label={label}
          aria-multiselectable="true"
          onKeyDown={onKeyDown}
          className={cn('space-y-1 overflow-y-auto overscroll-contain', className)}
        >
          {rows.map((row, index) => {
            const key = keys[index]!
            const selected = isSelected(row)
            const unavailable = isDisabled?.(row) ?? false
            return (
              <button
                key={key}
                ref={(node) => {
                  optionRefs.current[index] = node
                }}
                type="button"
                role="option"
                aria-selected={selected}
                aria-disabled={unavailable || undefined}
                tabIndex={index === stop ? 0 : -1}
                onFocus={() => setActiveKey(key)}
                onClick={() => {
                  if (!unavailable) onToggle(row)
                }}
                className={cn(
                  'flex min-h-11 w-full items-center gap-2 rounded-sm border px-2 py-1.5 text-left text-sm',
                  selected ? 'border-primary bg-primary/5' : 'border-transparent',
                  'hover:bg-accent focus-visible:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
                  'aria-disabled:cursor-not-allowed aria-disabled:opacity-60 aria-disabled:hover:bg-transparent',
                )}
              >
                {/* A tick box rather than a bare tick: several rows can be ticked. */}
                <span
                  aria-hidden
                  className={cn(
                    'grid h-4 w-4 shrink-0 place-items-center rounded-sm border border-primary',
                    selected && 'bg-primary text-primary-foreground',
                  )}
                >
                  {selected && <Check className="h-3.5 w-3.5" />}
                </span>
                <span className="min-w-0 flex-1">{renderOption(row)}</span>
              </button>
            )
          })}
        </div>
      )}
      <p role="status" className={cn('text-xs text-muted-foreground', statusClassName)}>
        {status}
      </p>
    </>
  )
}

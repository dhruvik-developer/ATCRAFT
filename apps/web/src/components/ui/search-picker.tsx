/**
 * A field that chooses one row from a search on the server.
 *
 * Thirteen pickers - customer, vendor, item, bin, batch, dispatch, job-work
 * order, purchase order, goods receipt, RFQ, requisition - were each written
 * out by hand: a popover, a search box, a `role="listbox"` of `role="option"`
 * buttons and three status lines, about seventy lines apiece. None had a
 * keyboard model. Every option was its own Tab stop and the arrow keys did
 * nothing, so the keyboard operators this ERP is run by tabbed through a page of
 * results, and a screen reader met a listbox that did not behave as one, with
 * "Searching..." and "No match" sitting inside it among the options. The copies
 * had drifted too: "Searching...", "Loading bins...", a skeleton.
 *
 * Here once:
 *
 * - The closed field is a `combobox` named by the field's label and reading the
 *   current choice. Enter, Space, ArrowDown or ArrowUp opens it.
 * - Open, it is a search box over a `listbox` that is a single Tab stop (a
 *   roving tabindex). ArrowDown from the search box enters the list at the
 *   current choice or the first row; ArrowDown, ArrowUp, Home and End move;
 *   ArrowUp from the first row returns to the search box; Enter or Space
 *   chooses; typing a character in the list goes on into the search. Escape
 *   closes it and focus returns to the field (Radix). DOM focus moves rather
 *   than `aria-activedescendant` because the search box stays a plain textbox -
 *   the screen tests and the e2e specs address it by that role - and real
 *   focus is what every screen reader follows.
 * - A row the API would refuse is listed with its reason and can be reached, so
 *   it is read rather than silently missing, but cannot be chosen.
 * - "Searching...", the empty sentence, "type more to narrow" and, for a screen
 *   reader, the number of results are said in one `role="status"` region beside
 *   the listbox, never inside it. A failed search is a `FieldError` - an alert
 *   carrying the API's reason - and never passes for an empty result.
 * - Rows are at least 44px tall for a thumb, and the popover is never wider than
 *   the screen less its gutter.
 *
 * The read is the caller's. `query(term)` returns the key and the function for
 * one settled term as an object literal, so the ESLint rules that hold every
 * other query check it where it is written: the key rooted in `queryKeys`
 * (`literalQueryKey`), and the function handing TanStack's signal to its request
 * (`queryFnSignal`), which cancels the search a later keystroke superseded. The
 * picker debounces the term and reads only while it is open.
 */

import { useQuery, type QueryKey } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Search, X } from 'lucide-react'
import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'

import { FieldError } from '@/components/field-error'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Input, Label } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { plural } from '@/lib/format'
import { describeError, picker } from '@/lib/messages'
import { cn } from '@/lib/utils'

/** One page of a search: the rows, and whether the server holds more. */
export interface SearchPickerPage<T> {
  data: T[]
  pagination: { has_next: boolean }
}

/** The read behind a picker, for one settled search term. */
export interface SearchPickerQuery<T> {
  queryKey: QueryKey
  queryFn: (context: { signal: AbortSignal }) => Promise<SearchPickerPage<T>>
  staleTime: number
  /** False while the caller has nothing to search yet - no vendor chosen, say. */
  enabled?: boolean
}

/** Never wider than the screen less a 1rem gutter on each side. */
const WIDTHS = {
  default: 'w-[min(24rem,calc(100vw-2rem))]',
  wide: 'w-[min(26rem,calc(100vw-2rem))]',
} as const

export interface SearchPickerProps<T> {
  /** The trigger's id, which the field's label points at. */
  id: string
  /** Names the field, and its popover and listbox. */
  label: string
  /**
   * `visible` draws the label above the field; `sr-only` keeps it for a screen
   * reader in a toolbar with no room for it; `external` leaves it to the form,
   * which renders its own `<Label htmlFor={id}>`.
   */
  labelDisplay?: 'visible' | 'sr-only' | 'external'
  required?: boolean
  className?: string
  /** Added to the trigger's classes, typically a minimum width in a toolbar. */
  triggerClassName?: string
  /** What the closed field reads for the current choice; null shows the placeholder. */
  display: string | null
  placeholder: string
  invalid?: boolean
  disabled?: boolean
  /** The search box's accessible name, e.g. "Search customers". */
  searchLabel: string
  searchPlaceholder: string
  searchMaxLength?: number
  /** The read for one settled term; see the module comment. */
  query: (term: string) => SearchPickerQuery<T>
  getKey: (row: T) => string
  /** The key of the current choice, ticked in the list. */
  selectedKey: string | null
  /** A row listed with its reason that cannot be chosen. */
  isDisabled?: (row: T) => boolean
  renderOption: (row: T) => ReactNode
  /** Called with the chosen row; the picker then closes itself. */
  onSelect: (row: T) => void
  /** Said when the read returns no rows, given the settled term. */
  emptyText: string | ((term: string) => string)
  /** Said under a page that is not every match; false when `footer` pages instead. */
  moreText?: ((shown: number) => string) | false
  /** The listbox's name, when it reads better than the label ("Batches"). */
  listLabel?: string
  width?: keyof typeof WIDTHS
  /** The clear control beside the field; omitted, none is offered. */
  clear?: { label: string; onClear: () => void; disabled?: boolean }
  /** Told about every keystroke, for a caller that pages its read itself. */
  onTermChange?: (term: string) => void
  /** Drawn under the list, for a caller that pages its read itself. */
  footer?: (page: { hasNext: boolean; fetching: boolean }) => ReactNode
}

export function SearchPicker<T>({
  id,
  label,
  labelDisplay = 'visible',
  required = false,
  className,
  triggerClassName,
  display,
  placeholder,
  invalid = false,
  disabled = false,
  searchLabel,
  searchPlaceholder,
  searchMaxLength,
  query,
  getKey,
  selectedKey,
  isDisabled,
  renderOption,
  onSelect,
  emptyText,
  moreText = picker.more,
  listLabel,
  width = 'default',
  clear,
  onTermChange,
  footer,
}: SearchPickerProps<T>) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')

  // One request per keystroke would put a scan of the table behind every
  // character typed.
  const settledTerm = useDebouncedValue(term)

  const read = query(settledTerm)
  const results = useQuery({
    queryKey: read.queryKey,
    queryFn: read.queryFn,
    staleTime: read.staleTime,
    // Nothing is read until the field is opened, and never for a disabled one.
    enabled: open && !disabled && read.enabled !== false,
  })

  const changeTerm = (next: string) => {
    setTerm(next)
    onTermChange?.(next)
  }

  return (
    <div className={cn(labelDisplay !== 'external' && 'space-y-1.5', className)}>
      {labelDisplay !== 'external' && (
        <Label
          htmlFor={id}
          required={required}
          className={cn(labelDisplay === 'sr-only' && 'sr-only')}
        >
          {label}
        </Label>
      )}
      <div className="flex min-w-0 items-center gap-1">
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              id={id}
              type="button"
              variant="outline"
              role="combobox"
              aria-expanded={open}
              aria-label={label}
              aria-invalid={invalid}
              aria-required={required || undefined}
              disabled={disabled}
              onKeyDown={(event) => {
                // A closed combobox opens on the arrow keys as well as Enter.
                if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
                  event.preventDefault()
                  setOpen(true)
                }
              }}
              className={cn(
                'h-9 min-w-0 flex-1 justify-between font-normal',
                display === null && 'text-muted-foreground',
                invalid && 'border-destructive',
                triggerClassName,
              )}
            >
              <span className="truncate">{display ?? placeholder}</span>
              <ChevronsUpDown className="opacity-50" aria-hidden />
            </Button>
          </PopoverTrigger>

          <PopoverContent align="start" aria-label={label} className={cn(WIDTHS[width], 'p-0')}>
            <SearchPickerPanel
              term={term}
              onTermChange={changeTerm}
              settledTerm={settledTerm}
              searchLabel={searchLabel}
              searchPlaceholder={searchPlaceholder}
              searchMaxLength={searchMaxLength}
              rows={results.data?.data ?? []}
              hasNext={results.data?.pagination.has_next === true}
              loading={results.isLoading}
              fetching={results.isFetching}
              error={results.error}
              getKey={getKey}
              selectedKey={selectedKey}
              isDisabled={isDisabled}
              renderOption={renderOption}
              onChoose={(row) => {
                onSelect(row)
                setOpen(false)
              }}
              emptyText={emptyText}
              moreText={moreText}
              listLabel={listLabel ?? label}
              footer={footer}
            />
          </PopoverContent>
        </Popover>

        {clear && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={clear.label}
            disabled={clear.disabled}
            onClick={clear.onClear}
          >
            <X />
          </Button>
        )}
      </div>
    </div>
  )
}

/**
 * The open popover's body. Mounted only while open, so the row the keyboard
 * last reached is forgotten when it closes.
 */
function SearchPickerPanel<T>({
  term,
  onTermChange,
  settledTerm,
  searchLabel,
  searchPlaceholder,
  searchMaxLength,
  rows,
  hasNext,
  loading,
  fetching,
  error,
  getKey,
  selectedKey,
  isDisabled,
  renderOption,
  onChoose,
  emptyText,
  moreText,
  listLabel,
  footer,
}: {
  term: string
  onTermChange: (term: string) => void
  settledTerm: string
  searchLabel: string
  searchPlaceholder: string
  searchMaxLength?: number
  rows: T[]
  hasNext: boolean
  loading: boolean
  fetching: boolean
  error: unknown
  getKey: (row: T) => string
  selectedKey: string | null
  isDisabled?: (row: T) => boolean
  renderOption: (row: T) => ReactNode
  onChoose: (row: T) => void
  emptyText: string | ((term: string) => string)
  moreText: ((shown: number) => string) | false
  listLabel: string
  footer?: (page: { hasNext: boolean; fetching: boolean }) => ReactNode
}) {
  const listboxId = useId()
  const searchRef = useRef<HTMLInputElement>(null)
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [activeKey, setActiveKey] = useState<string | null>(null)

  const keys = rows.map(getKey)
  optionRefs.current.length = rows.length
  const activeIndex = activeKey === null ? -1 : keys.indexOf(activeKey)
  const selectedIndex = selectedKey === null ? -1 : keys.indexOf(selectedKey)
  // The list's one Tab stop: the row last reached, else the current choice,
  // else the first row (WAI-ARIA listbox).
  const stop = activeIndex >= 0 ? activeIndex : Math.max(selectedIndex, 0)

  const reach = (index: number) => {
    const option = optionRefs.current[index]
    if (!option) return
    setActiveKey(keys[index] ?? null)
    option.focus()
  }

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (rows.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      reach(stop)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      reach(rows.length - 1)
    }
  }

  const onListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
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
        if (index === 0) searchRef.current?.focus()
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
      case 'Backspace':
        event.preventDefault()
        onTermChange(term.slice(0, -1))
        searchRef.current?.focus()
        return
    }
    // A character typed in the list refines the search instead of being lost.
    // Space is not one: on an option it chooses.
    const typed =
      event.key.length === 1 &&
      event.key !== ' ' &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    if (typed) {
      event.preventDefault()
      onTermChange(term + event.key)
      searchRef.current?.focus()
    }
  }

  let status: ReactNode
  let statusClass: string
  if (loading) {
    status = (
      <>
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        {picker.searching}
      </>
    )
    statusClass = 'flex items-center justify-center gap-2 px-3 py-6'
  } else if (error) {
    // The alert below says it; an empty status keeps "no match" from being read.
    status = null
    statusClass = 'sr-only'
  } else if (rows.length === 0) {
    status = typeof emptyText === 'function' ? emptyText(settledTerm) : emptyText
    statusClass = 'px-3 py-6 text-center'
  } else if (hasNext && moreText) {
    status = moreText(rows.length)
    statusClass = 'border-t px-3 py-2 text-xs'
  } else {
    // The rows show a sighted reader that the search answered; this tells
    // everyone else, and how much there is to arrow through.
    status = plural(rows.length, 'result')
    statusClass = 'sr-only'
  }

  return (
    <>
      <div className="relative border-b p-2">
        <Search
          className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          ref={searchRef}
          autoFocus
          value={term}
          onChange={(event) => onTermChange(event.target.value)}
          onKeyDown={onSearchKeyDown}
          placeholder={searchPlaceholder}
          aria-label={searchLabel}
          aria-controls={rows.length > 0 ? listboxId : undefined}
          maxLength={searchMaxLength}
          autoComplete="off"
          className="pl-8"
        />
      </div>

      {rows.length > 0 && (
        <div
          id={listboxId}
          role="listbox"
          aria-label={listLabel}
          onKeyDown={onListKeyDown}
          className="max-h-64 overflow-y-auto overscroll-contain p-1"
        >
          {rows.map((row, index) => {
            const key = keys[index]!
            const chosen = key === selectedKey
            const unavailable = isDisabled?.(row) ?? false
            return (
              <button
                key={key}
                ref={(node) => {
                  optionRefs.current[index] = node
                }}
                type="button"
                role="option"
                aria-selected={chosen}
                aria-disabled={unavailable || undefined}
                tabIndex={index === stop ? 0 : -1}
                onFocus={() => setActiveKey(key)}
                onClick={() => {
                  if (!unavailable) onChoose(row)
                }}
                className={cn(
                  'flex min-h-11 w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm',
                  'hover:bg-accent focus-visible:bg-accent focus-visible:outline-none',
                  'aria-disabled:cursor-not-allowed aria-disabled:opacity-60 aria-disabled:hover:bg-transparent',
                )}
              >
                <Check className={cn('h-4 w-4 shrink-0', !chosen && 'invisible')} aria-hidden />
                <span className="min-w-0 flex-1">{renderOption(row)}</span>
              </button>
            )
          })}
        </div>
      )}

      <p role="status" className={cn('text-sm text-muted-foreground', statusClass)}>
        {status}
      </p>
      {!loading && error ? (
        <FieldError className="mt-0 px-3 py-6 text-sm" message={describeError(error)} />
      ) : null}
      {footer?.({ hasNext, fetching })}
    </>
  )
}

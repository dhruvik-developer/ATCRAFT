/**
 * Capturing unit labels at the moment stock enters.
 *
 * A serial is typed, scanned or printed ahead of the goods, so the input has to
 * take a pasted run and a hand-keyed list alike. The same control is used by
 * goods-in, production, assembly and stock adjustments so a scanner that ends a
 * label with a newline behaves the same everywhere.
 */

import { useState } from 'react'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { formatQuantity, toDecimal } from '@/lib/format'

import { normaliseSerial, SERIAL_BULK_MAX, splitSerialNumbers } from './serial-numbers'

export function SerialNumbersField({
  id,
  label = 'Serial numbers',
  value,
  onChange,
  expected,
  hint,
  error,
  rows = 3,
}: {
  id: string
  label?: string
  value: string
  onChange: (value: string) => void
  /** The accepted or produced quantity, when the one-per-unit rule applies. */
  expected?: string
  hint?: string
  error?: string
  rows?: number
}) {
  const [prefix, setPrefix] = useState('')
  const [start, setStart] = useState('1')
  const [end, setEnd] = useState('10')
  const [padding, setPadding] = useState('4')

  const numbers = splitSerialNumbers(value)
  const overLimit = numbers.length > SERIAL_BULK_MAX
  const expectedCount = expected ? toDecimal(expected) : null
  const countMismatch =
    expectedCount !== null &&
    expectedCount.greaterThan(0) &&
    !expectedCount.equals(numbers.length) &&
    numbers.length > 0

  const invalidRange =
    !Number.isInteger(Number(start)) ||
    !Number.isInteger(Number(end)) ||
    Number(end) < Number(start) ||
    Number(end) - Number(start) + 1 > SERIAL_BULK_MAX

  const addRange = () => {
    const from = Number(start)
    const to = Number(end)
    const width = Math.max(1, Math.min(12, Number(padding) || 1))
    if (invalidRange || from < 1 || to < 1 || !prefix.trim()) return
    const generated = Array.from(
      { length: to - from + 1 },
      (_, offset) => `${normaliseSerial(prefix)}${String(from + offset).padStart(width, '0')}`,
    )
    // Merge rather than replace: a label printed earlier may already be typed
    // above, and losing it because a second range was generated would be a
    // quiet hole in the register.
    const merged = new Set([...numbers, ...generated])
    onChange([...merged].join('\n'))
  }

  const describedBy = `${id}-count`
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Textarea
        id={id}
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={'One per line, or separated by commas\nSN-0001\nSN-0002'}
        className="font-mono text-xs"
        aria-invalid={Boolean(error) || overLimit || countMismatch}
        aria-describedby={describedBy}
      />
      <p
        id={describedBy}
        className={
          overLimit || countMismatch ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'
        }
      >
        {numbers.length} serial{numbers.length === 1 ? '' : 's'} captured
        {expectedCount !== null && expectedCount.greaterThan(0)
          ? `; ${formatQuantity(expected ?? '0')} expected, one per unit`
          : ''}
        {overLimit ? `. The API accepts at most ${SERIAL_BULK_MAX} at a time.` : '.'}
      </p>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      <FieldError message={error} />

      <details className="rounded-md border p-2">
        <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
          Generate a label range
        </summary>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <div className="min-w-[8rem] flex-1 space-y-1">
            <Label htmlFor={`${id}-prefix`} className="text-xs">
              Prefix
            </Label>
            <Input
              id={`${id}-prefix`}
              value={prefix}
              maxLength={32}
              onChange={(event) => setPrefix(event.target.value)}
            />
          </div>
          <div className="w-20 space-y-1">
            <Label htmlFor={`${id}-start`} className="text-xs">
              From
            </Label>
            <Input
              id={`${id}-start`}
              inputMode="numeric"
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </div>
          <div className="w-20 space-y-1">
            <Label htmlFor={`${id}-end`} className="text-xs">
              To
            </Label>
            <Input
              id={`${id}-end`}
              inputMode="numeric"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </div>
          <div className="w-20 space-y-1">
            <Label htmlFor={`${id}-padding`} className="text-xs">
              Digits
            </Label>
            <Input
              id={`${id}-padding`}
              inputMode="numeric"
              value={padding}
              onChange={(event) => setPadding(event.target.value)}
            />
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={addRange}
            disabled={invalidRange || !prefix.trim()}
          >
            Add range
          </Button>
        </div>
        {invalidRange && (
          <p className="mt-2 text-xs text-destructive">
            Choose whole numbers with To not before From, and at most {SERIAL_BULK_MAX} at a time.
          </p>
        )}
      </details>
    </div>
  )
}

import { useMemo } from 'react'

/** Joins the keys; a record keyed by id never holds this character in a key. */
const SEPARATOR = '\u0000'

/**
 * The keys of a record, as a Set that keeps its identity while only the values
 * change.
 *
 * A draft keyed by invoice id is a new object on every keystroke in one of its
 * amounts, so `useMemo(() => new Set(Object.keys(draft)), [draft])` is a new Set
 * on every keystroke - and everything memoised on it, such as a candidate
 * table's columns, is rebuilt with it. Keyed on the joined keys instead, the
 * Set changes only when an entry joins or leaves the record.
 */
export function useKeySet(record: Readonly<Record<string, unknown>>): ReadonlySet<string> {
  const keys = Object.keys(record)
  const count = keys.length
  const joined = keys.join(SEPARATOR)
  return useMemo(() => new Set(count === 0 ? [] : joined.split(SEPARATOR)), [count, joined])
}

/**
 * A value that settles after the user stops changing it.
 *
 * Eleven pickers held their own copy of the same three-line effect: a second
 * `useState` for the settled value, a `setTimeout` into it, and a cleanup that
 * clears the timer. Identical code eleven times is eleven places for the delay
 * to drift apart, and it already had - the delay was a bare `300` at every one
 * of them, so `SEARCH_DEBOUNCE_MS` governed only the data table.
 *
 * The value is trimmed on the way out because every caller is feeding it to a
 * `search` query parameter, where a trailing space is a different cache key and
 * the same result.
 */

import { useEffect, useState } from 'react'

import { SEARCH_DEBOUNCE_MS } from '@/lib/constants'

export function useDebouncedValue(value: string, ms: number = SEARCH_DEBOUNCE_MS): string {
  const [settled, setSettled] = useState(value.trim())

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value.trim()), ms)
    return () => clearTimeout(timer)
  }, [value, ms])

  return settled
}

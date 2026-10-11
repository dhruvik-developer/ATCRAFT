import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * Change several filters in ONE URL write.
 *
 * `setSearchParams` is not a React state setter: two calls in the same handler
 * both start from the URL captured at render, and only the last navigation
 * survives. That cost the finance screen its Clear button, and the item-masters
 * screen has several controls that must move two keys at once - drilling into a
 * branch sets the parent and drops the "top level only" scope together.
 *
 * Its own module, beside `masters-shared.tsx`, because that file exports
 * components and a hook beside them would defeat fast refresh.
 */
export function useFilterWriter() {
  const [, setSearchParams] = useSearchParams()

  return useCallback(
    (next: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (value === undefined || value === '') updated.delete(key)
            else updated.set(key, value)
          }
          // The page number belonged to the previous narrowing, not this one.
          updated.delete('page')
          return updated
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )
}

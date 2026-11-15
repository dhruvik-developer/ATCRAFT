import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * A URL parameter's next value: text or a number sets it, a list writes one
 * entry per value (a multi-valued key such as the dealer catalogue's `spec`),
 * and `undefined`, `''` or an empty list removes it.
 */
export type SearchParamValue = string | number | readonly string[] | undefined

/**
 * Write several URL parameters in ONE navigation - and, by default, send the
 * list they narrow back to its first page.
 *
 * `setSearchParams` is not a React state setter: every call in one handler
 * starts from the URL captured at render and navigates, so only the last
 * navigation survives. That is how a "Clear" button came to clear one filter of
 * six, and choosing a side came to drop the party chosen beside it. Every key a
 * handler changes therefore goes through one updater, and this is that
 * updater, once.
 *
 * `pageParam` is the key the narrowed list keeps its page under: `page`, or a
 * namespaced list's `<namespace>_page` - pass that list's `pageParam` from
 * `useListQuery`. Resetting a different list's key instead leaves this one on a
 * page the narrower result does not have, drawn as "No records". `null` keeps
 * every page where it is, for a write that chooses what a panel shows rather
 * than narrowing a list.
 */
export function useFilterWriter(pageParam: string | null = 'page') {
  const [, setSearchParams] = useSearchParams()

  return useCallback(
    (next: Readonly<Record<string, SearchParamValue>>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (Array.isArray(value)) {
              updated.delete(key)
              for (const entry of value) updated.append(key, entry)
            } else if (value === undefined || value === '') {
              updated.delete(key)
            } else {
              updated.set(key, String(value))
            }
          }
          // The page number belonged to the previous narrowing, not this one.
          if (pageParam !== null) updated.delete(pageParam)
          return updated
        },
        { replace: true },
      )
    },
    [pageParam, setSearchParams],
  )
}

/** Every key in `keys` removed: what a "Clear filters" button writes. */
export function cleared(keys: readonly string[]): Record<string, undefined> {
  return Object.fromEntries(keys.map((key) => [key, undefined]))
}

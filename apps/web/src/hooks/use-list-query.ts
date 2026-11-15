/**
 * Server-driven list state.
 *
 * Every list screen needs the same five things: page, page size, sort, search
 * and filters, all sent to the API rather than applied in the browser (master
 * rules 31/36/78). Centralising it means no screen accidentally fetches
 * everything and filters client-side.
 *
 * State lives in the URL query string, so a filtered view can be bookmarked,
 * shared with a colleague, and survives the back button - and every write to
 * it goes through one updater here, `written`: a list's own `setParam` and
 * `setParams`, and `useFilterWriter` for a screen's filter keys of its own.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from '@/lib/constants'
import type { ListParams } from '@/types/api'

export interface UseListQueryOptions {
  /** API path, e.g. `/inventory/balances`. */
  path: string
  /** React Query key factory for this resource. */
  queryKey: (params: unknown) => readonly unknown[]
  /** Isolate URL state when a screen contains several independent lists. */
  namespace?: string
  /** Filters that are not page/limit/sort/search. */
  filters?: Record<string, unknown>
  defaultLimit?: number
  defaultSortBy?: string
  defaultSortDir?: 'asc' | 'desc'
  /** Ask the API to skip the exact COUNT on very large tables. */
  withTotal?: boolean
  /** Report endpoints may escalate from inline rows to an HTTP 202 background job. */
  allowAccepted?: boolean
  enabled?: boolean
  staleTime?: number
  refetchInterval?: number | false
  /**
   * Opt back in to the refetch-on-focus the query client turns off globally.
   * Lists of mutable stock state need it: coming back to a tab that has been
   * open for an hour must not show yesterday's availability (master rules 34).
   */
  refetchOnWindowFocus?: boolean
}

/**
 * The rows before the first page arrives, and while a list is disabled: one
 * array for every list and every render. A fresh `[]` each render is a new
 * dependency for every `useMemo` and effect that reads `data`, so a screen
 * recomputed its derived rows on every render until the first page landed.
 * Frozen, because it is shared: a push into it would fill every empty list.
 */
const NO_ROWS = Object.freeze([]) as never[]

/**
 * A URL parameter's next value: text or a number sets it, a list writes one
 * entry per value (a multi-valued key such as the dealer catalogue's `spec`),
 * and `undefined`, `''` or an empty list removes it.
 */
export type SearchParamValue = string | number | readonly string[] | undefined

/**
 * `current` with every key in `next` written - and `pageParam`, unless it is
 * null, removed: the URL one navigation leaves.
 *
 * `setSearchParams` is not a React state setter: every call in one handler
 * starts from the URL captured at render and navigates, so only the last
 * navigation survives. That is how a "Clear" button came to clear one filter of
 * six, and choosing a side came to drop the party chosen beside it. Every key a
 * handler changes therefore goes through one updater, and this is that updater,
 * once.
 */
function written(
  current: URLSearchParams,
  next: Readonly<Record<string, SearchParamValue>>,
  pageParam: string | null,
): URLSearchParams {
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
}

/** A shared/bookmarked URL must not turn every list into an invalid API request. */
function positiveInteger(
  value: string | null,
  fallback: number,
  maximum = Number.MAX_SAFE_INTEGER,
) {
  if (value === null || !/^\d+$/.test(value)) return fallback
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= maximum ? parsed : fallback
}

export function useListQuery<T>({
  path,
  queryKey,
  namespace,
  filters = {},
  defaultLimit = DEFAULT_PAGE_SIZE,
  defaultSortBy,
  defaultSortDir = 'asc',
  withTotal = true,
  allowAccepted = false,
  enabled = true,
  staleTime,
  refetchInterval = false,
  refetchOnWindowFocus = false,
}: UseListQueryOptions) {
  const [searchParams, setSearchParams] = useSearchParams()
  const prefix = namespace ? `${namespace}_` : ''

  const page = positiveInteger(searchParams.get(`${prefix}page`), 1)
  const limit = positiveInteger(searchParams.get(`${prefix}limit`), defaultLimit, MAX_PAGE_SIZE)
  const search = searchParams.get(`${prefix}q`) ?? ''
  const sortBy = searchParams.get(`${prefix}sort`) ?? defaultSortBy
  const rawDirection = searchParams.get(`${prefix}dir`)
  const sortDir = rawDirection === 'asc' || rawDirection === 'desc' ? rawDirection : defaultSortDir

  const params: ListParams = useMemo(
    () => ({
      page,
      limit,
      with_total: withTotal,
      ...(search ? { search } : {}),
      ...(sortBy ? { sort_by: sortBy, sort_dir: sortDir } : {}),
      ...filters,
    }),
    // `filters` is typically an inline object; serialising it keeps the memo and
    // the query key stable instead of changing identity on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [page, limit, search, sortBy, sortDir, withTotal, JSON.stringify(filters)],
  )

  const query = useQuery({
    queryKey: queryKey(params),
    queryFn: ({ signal }) =>
      allowAccepted
        ? api.getPageOrAccepted<T>(path, { params, signal })
        : api.getPage<T>(path, { params, signal }),
    // Keeps the previous page visible while the next one loads, so the table
    // does not collapse to a skeleton on every page click.
    placeholderData: keepPreviousData,
    enabled,
    // Only forwarded when the caller supplied one. React Query merges options
    // over its defaults with a plain object spread, so an *explicit*
    // `staleTime: undefined` overwrites the client-wide default rather than
    // inheriting it - and `isStaleByTime` then defaults it to 0, making every
    // list that omits the option permanently stale. `refetchInterval` and
    // `refetchOnWindowFocus` above are safe because they default in the
    // signature; this one deliberately has no default so the policy stays in
    // `query-client.ts`.
    ...(staleTime === undefined ? {} : { staleTime }),
    refetchInterval,
    refetchOnWindowFocus,
  })

  const update = useCallback(
    (next: Record<string, string | number | undefined>) => {
      // This list's own keys, under its namespace; its page is one of them.
      const keyed = Object.fromEntries(
        Object.entries(next).map(([key, value]) => [`${prefix}${key}`, value]),
      )
      setSearchParams((current) => written(current, keyed, null), { replace: true })
    },
    [prefix, setSearchParams],
  )

  const pageEnvelope = query.data && 'pagination' in query.data ? query.data : undefined

  return {
    data: pageEnvelope?.data ?? NO_ROWS,
    pagination: pageEnvelope?.pagination,
    acceptedJob: query.data && !('pagination' in query.data) ? query.data.data : undefined,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    error: query.error as Error | null,
    refetch: query.refetch,

    page,
    limit,
    search,
    sortBy,
    sortDir,

    setPage: (value: number) => update({ page: value }),
    // Changing the page size must reset to page 1: page 7 of 50-row pages does
    // not exist once the size becomes 200.
    setLimit: (value: number) => update({ limit: value, page: 1 }),
    setSearch: (value: string) => update({ q: value || undefined, page: 1 }),
    setSort: (by: string | undefined, dir: 'asc' | 'desc') =>
      update({ sort: by, dir: by ? dir : undefined, page: 1 }),
    setParam: (key: string, value: string | number | undefined) =>
      update({ [key]: value, page: 1 }),
    /**
     * Change several parameters in ONE navigation.
     *
     * Calling `setParam` in a loop does not work and fails quietly: it goes
     * through `setSearchParams`, which is not a React state setter - every
     * functional updater in the same tick is computed from the `searchParams`
     * captured at the last render, so N calls all start from the same base URL
     * and only the last navigation survives. A "Clear filters" button written
     * that way clears exactly one filter and looks like it did nothing.
     */
    setParams: (next: Record<string, string | number | undefined>) => update({ ...next, page: 1 }),
    /**
     * The URL key this list keeps its page under: `page`, or `<namespace>_page`.
     *
     * For a screen that writes a filter key of its own into the URL and must
     * send *this* list back to its first page in the same navigation - not
     * another list on the same screen, whose page lives under another key.
     */
    pageParam: `${prefix}page`,
    params,
  }
}

/**
 * Write several URL parameters in ONE navigation - and, by default, send the
 * list they narrow back to its first page.
 *
 * `pageParam` is the key the narrowed list keeps its page under: `page`, or a
 * namespaced list's `<namespace>_page` - pass that list's `pageParam`. Resetting
 * a different list's key instead leaves this one on a page the narrower result
 * does not have, drawn as "No records". `null` keeps every page where it is,
 * for a write that chooses what a panel shows rather than narrowing a list.
 *
 * Here rather than in a module of its own: every lazy chunk is also named in
 * the app entry's preload map, so a separate one cost the first paint bytes
 * it never runs.
 */
export function useFilterWriter(pageParam: string | null = 'page') {
  const [, setSearchParams] = useSearchParams()

  return useCallback(
    (next: Readonly<Record<string, SearchParamValue>>) =>
      setSearchParams((current) => written(current, next, pageParam), { replace: true }),
    [pageParam, setSearchParams],
  )
}

/** Every key in `keys` removed: what a "Clear filters" button writes. */
export function cleared(keys: readonly string[]): Record<string, undefined> {
  return Object.fromEntries(keys.map((key) => [key, undefined]))
}

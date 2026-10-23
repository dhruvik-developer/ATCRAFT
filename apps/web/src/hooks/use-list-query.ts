/**
 * Server-driven list state.
 *
 * Every list screen needs the same five things: page, page size, sort, search
 * and filters, all sent to the API rather than applied in the browser (master
 * rules 31/36/78). Centralising it means no screen accidentally fetches
 * everything and filters client-side.
 *
 * State lives in the URL query string, so a filtered view can be bookmarked,
 * shared with a colleague, and survives the back button.
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
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (value === undefined || value === '') updated.delete(`${prefix}${key}`)
            else updated.set(`${prefix}${key}`, String(value))
          }
          return updated
        },
        { replace: true },
      )
    },
    [prefix, setSearchParams],
  )

  const pageEnvelope = query.data && 'pagination' in query.data ? query.data : undefined

  return {
    data: pageEnvelope?.data ?? [],
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
    params,
  }
}

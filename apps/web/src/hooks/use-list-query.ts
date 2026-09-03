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
import type { ListParams, PaginatedEnvelope } from '@/types/api'

export interface UseListQueryOptions {
  /** API path, e.g. `/inventory/balances`. */
  path: string
  /** React Query key factory for this resource. */
  queryKey: (params: unknown) => readonly unknown[]
  /** Filters that are not page/limit/sort/search. */
  filters?: Record<string, unknown>
  defaultLimit?: number
  defaultSortBy?: string
  defaultSortDir?: 'asc' | 'desc'
  /** Ask the API to skip the exact COUNT on very large tables. */
  withTotal?: boolean
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

export function useListQuery<T>({
  path,
  queryKey,
  filters = {},
  defaultLimit = 50,
  defaultSortBy,
  defaultSortDir = 'asc',
  withTotal = true,
  enabled = true,
  staleTime,
  refetchInterval = false,
  refetchOnWindowFocus = false,
}: UseListQueryOptions) {
  const [searchParams, setSearchParams] = useSearchParams()

  const page = Number(searchParams.get('page') ?? 1)
  const limit = Number(searchParams.get('limit') ?? defaultLimit)
  const search = searchParams.get('q') ?? ''
  const sortBy = searchParams.get('sort') ?? defaultSortBy
  const sortDir = (searchParams.get('dir') as 'asc' | 'desc' | null) ?? defaultSortDir

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
    queryFn: () => api.getPage<T>(path, { params }),
    // Keeps the previous page visible while the next one loads, so the table
    // does not collapse to a skeleton on every page click.
    placeholderData: keepPreviousData,
    enabled,
    staleTime,
    refetchInterval,
    refetchOnWindowFocus,
  })

  const update = useCallback(
    (next: Record<string, string | number | undefined>) => {
      setSearchParams(
        (current) => {
          const updated = new URLSearchParams(current)
          for (const [key, value] of Object.entries(next)) {
            if (value === undefined || value === '') updated.delete(key)
            else updated.set(key, String(value))
          }
          return updated
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  return {
    data: (query.data as PaginatedEnvelope<T> | undefined)?.data ?? [],
    pagination: (query.data as PaginatedEnvelope<T> | undefined)?.pagination,
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
    setParams: (next: Record<string, string | number | undefined>) =>
      update({ ...next, page: 1 }),
    params,
  }
}

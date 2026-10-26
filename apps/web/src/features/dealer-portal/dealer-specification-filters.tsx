import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/primitives'
import { useListQuery } from '@/hooks/use-list-query'
import { PICKER_PAGE_SIZE, SPECIFICATION_FILTER_MAX } from '@/lib/constants'
import { describeError } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import { DEALER_API } from './dealer-hooks'

/** The dealer reads a browse-authorized projection, never the staff master endpoint. */
export function DealerSpecificationFilters() {
  const [params, setParams] = useSearchParams()
  const [open, setOpen] = useState(() => params.has('spec'))
  const [error, setError] = useState<string>()
  const list = useListQuery<SchemaOf<'DealerSpecificationFilterRead'>>({
    path: `${DEALER_API}/catalog/specifications`,
    queryKey: (query) => queryKeys.dealerPortal.list('specification-filters', query),
    namespace: 'facets',
    withTotal: false,
    defaultLimit: PICKER_PAGE_SIZE,
    staleTime: staleTimes.reference,
    enabled: open,
  })
  const filters = params.getAll('spec')
  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className="mb-5 rounded-lg border p-4"
    >
      <summary className="cursor-pointer font-medium">
        Technical specifications{filters.length ? ` (${filters.length})` : ''}
      </summary>
      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {list.data.map((attribute) => {
          const prefix = `${attribute.code}=`
          const value =
            filters.find((filter) => filter.startsWith(prefix))?.slice(prefix.length) ?? ''
          return (
            <div key={attribute.id} className="min-w-0">
              <Label htmlFor={`dealer-filter-${attribute.id}`}>{attribute.name}</Label>
              <select
                id={`dealer-filter-${attribute.id}`}
                value={value}
                className="mt-1 h-11 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm"
                onChange={(event) => {
                  const next = filters.filter((filter) => !filter.startsWith(prefix))
                  if (event.target.value) next.push(`${prefix}${event.target.value}`)
                  if (next.length > SPECIFICATION_FILTER_MAX) {
                    setError(`Use at most ${SPECIFICATION_FILTER_MAX} specification filters.`)
                    return
                  }
                  setError(undefined)
                  setParams(
                    (current) => {
                      const updated = new URLSearchParams(current)
                      updated.delete('spec')
                      next.forEach((filter) => updated.append('spec', filter))
                      updated.delete('page')
                      return updated
                    },
                    { replace: true },
                  )
                }}
              >
                <option value="">Any {attribute.name.toLowerCase()}</option>
                {attribute.value_type === 'BOOLEAN' ? (
                  <>
                    <option value="true">Yes</option>
                    <option value="false">No</option>
                  </>
                ) : (
                  attribute.options.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))
                )}
              </select>
            </div>
          )
        })}
      </div>
      {list.isLoading && (
        <p className="mt-3 text-sm text-muted-foreground">Loading specification filters…</p>
      )}
      {list.error && <FieldError message={describeError(list.error)} />}
      <FieldError message={error} />
      {filters.length > 0 && (
        <Button
          type="button"
          variant="ghost"
          className="mt-3 min-h-11"
          onClick={() => {
            setError(undefined)
            setParams(
              (current) => {
                const updated = new URLSearchParams(current)
                updated.delete('spec')
                updated.delete('page')
                return updated
              },
              { replace: true },
            )
          }}
        >
          Clear specification filters
        </Button>
      )}
      {!list.isLoading && !list.error && !list.data.length && (
        <p className="text-sm text-muted-foreground">
          No technical specification filters are available.
        </p>
      )}
      {(list.page > 1 || list.pagination?.has_next) && (
        <nav
          aria-label="Specification filter pages"
          className="mt-3 flex flex-wrap items-center gap-2"
        >
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={list.page <= 1 || list.isFetching}
            onClick={() => list.setPage(list.page - 1)}
          >
            Previous filters
          </Button>
          <span className="text-xs text-muted-foreground">Page {list.page}</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!list.pagination?.has_next || list.isFetching}
            onClick={() => list.setPage(list.page + 1)}
          >
            More filters
          </Button>
        </nav>
      )}
    </details>
  )
}

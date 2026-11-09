import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { LoadError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Input, Label, NativeSelect } from '@/components/ui/primitives'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { picker } from '@/lib/messages'
import { staleTimes } from '@/lib/query-client'

/** Permission-specific description lookups share search/paging, never a downloaded master table. */
export function DescriptionPicker<T extends { id: string; code: string; name: string }>({
  label,
  path,
  queryKey,
  onChoose,
  excludedIds = [],
  disabled = false,
  params = {},
}: {
  label: string
  path: string
  queryKey: (params: unknown) => readonly unknown[]
  onChoose: (row: T) => void
  excludedIds?: readonly string[]
  disabled?: boolean
  params?: Record<string, unknown>
}) {
  const id = useId()
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [chosen, setChosen] = useState('')
  const term = useDebouncedValue(search)
  const request = {
    ...params,
    page,
    limit: PICKER_PAGE_SIZE,
    with_total: false,
    ...(term ? { search: term } : {}),
  }
  const result = useQuery({
    queryKey: queryKey(request),
    queryFn: ({ signal }) => api.getPage<T>(path, { params: request, signal }),
    staleTime: staleTimes.reference,
    enabled: !disabled,
  })
  const rows = result.data?.data ?? []
  const selected = rows.find((row) => row.id === chosen && !excludedIds.includes(row.id))
  return (
    <div className="space-y-2">
      <Label htmlFor={`${id}-search`}>Search {label.toLowerCase()}</Label>
      <Input
        id={`${id}-search`}
        value={search}
        onChange={(event) => {
          setSearch(event.target.value)
          setPage(1)
          setChosen('')
        }}
        disabled={disabled}
        className="min-h-11"
      />
      <div className="flex min-w-0 flex-wrap gap-2">
        <div className="min-w-0 grow basis-48">
          <Label htmlFor={`${id}-choice`} className="sr-only">
            {label}
          </Label>
          <NativeSelect
            size="touch"
            id={`${id}-choice`}
            value={selected?.id ?? ''}
            onChange={(event) => setChosen(event.target.value)}
            disabled={disabled || result.isFetching}
            className="min-w-0"
          >
            {/* A read for the typed term is a search, said as every search picker says it. */}
            <option value="">
              {result.isLoading ? picker.searching : `Choose ${label.toLowerCase()}`}
            </option>
            {rows
              .filter((row) => !excludedIds.includes(row.id))
              .map((row) => (
                <option key={row.id} value={row.id}>
                  {row.code} - {row.name}
                </option>
              ))}
          </NativeSelect>
        </div>
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={disabled || !selected || result.isFetching}
          onClick={() => {
            if (selected) {
              onChoose(selected)
              setChosen('')
            }
          }}
        >
          Choose {label.toLowerCase()}
        </Button>
      </div>
      {result.error && (
        <LoadError
          error={result.error}
          onRetry={() => void result.refetch()}
          retryClassName="min-h-11"
        />
      )}
      {result.data && !rows.length && (
        <p className="text-sm text-muted-foreground">No matching choices.</p>
      )}
      {(page > 1 || result.data?.pagination.has_next) && (
        <nav aria-label={`${label} choices`} className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={disabled || page <= 1 || result.isFetching}
            onClick={() => {
              setPage((current) => current - 1)
              setChosen('')
            }}
          >
            Previous choices
          </Button>
          <span className="text-xs text-muted-foreground">Page {page}</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={disabled || !result.data?.pagination.has_next || result.isFetching}
            onClick={() => {
              setPage((current) => current + 1)
              setChosen('')
            }}
          >
            More choices
          </Button>
        </nav>
      )}
    </div>
  )
}

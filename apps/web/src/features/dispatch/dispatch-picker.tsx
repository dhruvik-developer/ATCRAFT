/**
 * A dispatch, searched on the server.
 *
 * Raising a sales return used to offer the 100 latest delivered dispatches in a
 * plain select, so a return against the 101st could not be raised from the
 * screen at all and nothing said the list had been cut. This asks the server
 * instead: the caller's filters (`dispatch_type`, `status`) narrow the search,
 * the term matches the code, tracking id, LR, vehicle or courier, and a page
 * that holds more matches says so rather than passing for the whole answer.
 *
 * The chosen dispatch is resolved by id when the value arrives without a pick in
 * this session, so the trigger shows a code rather than a UUID.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { SearchPicker } from '@/components/ui/search-picker'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { formatDate } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { dispatchKeys, type DispatchDetail, type DispatchRow } from './dispatch-types'

/** The `GET /dispatch` filters a picker may fix; tenancy is never one of them. */
export interface DispatchPickerFilters {
  dispatch_type?: DispatchRow['dispatch_type']
  status?: DispatchRow['status']
  customer_id?: string
}

export function DispatchPicker({
  id,
  label,
  value,
  onChange,
  filters = {},
  placeholder = 'Search dispatches',
  invalid = false,
  disabled = false,
  className,
}: {
  id: string
  label: string
  value: string | null
  onChange: (dispatchId: string | null) => void
  filters?: DispatchPickerFilters
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  className?: string
}) {
  const [picked, setPicked] = useState<DispatchRow | null>(null)

  const detail = useQuery({
    queryKey: dispatchKeys.detail(value ?? ''),
    queryFn: ({ signal }) => api.get<DispatchDetail>(`/dispatch/${value}`, { signal }),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.transactional,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null

  return (
    <SearchPicker<DispatchRow>
      id={id}
      label={label}
      className={className}
      display={selected ? `${selected.code} · ${formatDate(selected.dispatch_date)}` : null}
      placeholder={placeholder}
      invalid={invalid}
      disabled={disabled}
      searchLabel="Search dispatches"
      searchPlaceholder="Search by code, tracking id, LR or vehicle"
      query={(term) => {
        const params = {
          limit: PICKER_PAGE_SIZE,
          with_total: false,
          sort_by: 'dispatch_date',
          sort_dir: 'desc',
          ...filters,
          ...(term ? { search: term } : {}),
        }
        return {
          queryKey: queryKeys.dispatch.list(params),
          queryFn: ({ signal }) => api.getPage<DispatchRow>('/dispatch', { params, signal }),
          staleTime: staleTimes.transactional,
        }
      }}
      getKey={(dispatch) => dispatch.id}
      selectedKey={value}
      onSelect={(dispatch) => {
        setPicked(dispatch)
        onChange(dispatch.id)
      }}
      renderOption={(dispatch) => (
        <>
          <span className="block truncate font-medium">{dispatch.code}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {formatDate(dispatch.dispatch_date)}
            {dispatch.tracking_id ? ` · ${dispatch.tracking_id}` : ''}
          </span>
        </>
      )}
      emptyText={(term) =>
        term ? 'No dispatch matches that search.' : 'No dispatch is available here.'
      }
      // Newest first, so a page that is not every match holds the latest ones.
      moreText={(shown) =>
        `Showing the latest ${shown}. Type more of the code to find an older one.`
      }
      clear={
        value && !disabled
          ? {
              label: `Clear ${label.toLowerCase()}`,
              onClear: () => {
                setPicked(null)
                onChange(null)
              },
            }
          : undefined
      }
    />
  )
}

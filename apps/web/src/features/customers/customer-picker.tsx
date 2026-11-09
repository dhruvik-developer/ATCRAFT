/**
 * Customer picker, searched on the server.
 *
 * A dealer network runs to thousands of accounts, so this never downloads the
 * list and filters it in the browser (master rules 31). The chosen customer is
 * resolved by id when the value arrives from a bookmarked URL, so a filter
 * restored from a link shows a name rather than a UUID.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Badge } from '@/components/ui/primitives'
import { SearchPicker } from '@/components/ui/search-picker'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'

import type { CustomerRow } from './customer-types'

export function CustomerPicker({
  id,
  label,
  value,
  onChange,
  hideLabel = false,
  placeholder = 'Any customer',
  invalid = false,
  disabled = false,
  className,
  channels,
}: {
  id: string
  label: string
  value: string | null
  onChange: (customerId: string | null) => void
  hideLabel?: boolean
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  className?: string
  /** Dealer bindings only offer accounts from these commercial channels. */
  channels?: readonly ('DEALER' | 'DISTRIBUTOR')[]
}) {
  const [picked, setPicked] = useState<CustomerRow | null>(null)
  const path = channels?.length ? '/customers/dealers' : '/customers'

  const detail = useQuery({
    queryKey: queryKeys.customers.detail(value ?? ''),
    queryFn: ({ signal }) => api.get<CustomerRow>(`/customers/${value}`, { signal }),
    enabled: Boolean(value) && picked?.id !== value,
    staleTime: staleTimes.master,
  })

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null

  return (
    <SearchPicker<CustomerRow>
      id={id}
      label={label}
      labelDisplay={hideLabel ? 'sr-only' : 'visible'}
      className={className}
      triggerClassName="min-w-[200px]"
      display={selected ? `${selected.code} - ${selected.name}` : null}
      placeholder={placeholder}
      invalid={invalid}
      disabled={disabled}
      searchLabel="Search customers"
      searchPlaceholder="Search by code, name or GSTIN"
      query={(term) => {
        const params = {
          limit: PICKER_PAGE_SIZE,
          with_total: false,
          is_active: true,
          sort_by: 'code',
          sort_dir: 'asc',
          ...(channels?.length === 1 ? { customer_type: channels[0] } : {}),
          ...(term ? { search: term } : {}),
        }
        return {
          queryKey: queryKeys.customers.list({ ...params, path }),
          queryFn: ({ signal }) => api.getPage<CustomerRow>(path, { params, signal }),
          staleTime: staleTimes.master,
        }
      }}
      getKey={(customer) => customer.id}
      selectedKey={value}
      onSelect={(customer) => {
        setPicked(customer)
        onChange(customer.id)
      }}
      renderOption={(customer) => (
        <>
          <span className="flex items-center gap-2">
            <span className="truncate font-medium">{customer.code}</span>
            {/* A blocked account is shown in the picker itself: finding out at
                confirm time wastes the whole order entry. */}
            {customer.is_blocked && <Badge variant="destructive">Blocked</Badge>}
          </span>
          <span className="block truncate text-xs text-muted-foreground">{customer.name}</span>
        </>
      )}
      emptyText={(term) =>
        term ? 'No customer matches that search.' : 'Type to search the customer master.'
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

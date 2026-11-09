/**
 * One vendor's job-work order, searched on the server.
 *
 * Recording a job-work bill used to offer that vendor's 200 newest orders in a
 * plain select, so a bill for an older order could not be recorded from the
 * screen and nothing said the list had been cut. This asks `GET /job-work` for
 * the vendor's orders matching the term (code or process description), newest
 * first, and says when there are more than one page holds.
 *
 * Every status is offered, as before: whether an order still has anything to
 * bill is answered by its billable lines, which the dialog reads next.
 */

import { useState } from 'react'

import { SearchPicker } from '@/components/ui/search-picker'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { humanise } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'

import { jobWorkKeys, useJobWorkOrder, type JobWorkOrderRow } from './job-work-model'

function orderLabel(order: Pick<JobWorkOrderRow, 'code' | 'status' | 'receipt_count'>): string {
  return `${order.code} - ${humanise(order.status)}${order.receipt_count === 0 ? ' (no receipts)' : ''}`
}

export function JobWorkOrderPicker({
  id,
  label,
  vendorId,
  value,
  onChange,
  required = false,
  disabled = false,
  placeholder = 'Search this vendor’s orders',
}: {
  id: string
  label: string
  /** The orders are this vendor's; without one there is nothing to search. */
  vendorId: string | null
  value: string | null
  /** The chosen row comes with the id: the caller needs its code and SAC. */
  onChange: (orderId: string | null, order: JobWorkOrderRow | null) => void
  required?: boolean
  disabled?: boolean
  placeholder?: string
}) {
  const [picked, setPicked] = useState<JobWorkOrderRow | null>(null)

  // Only when the value did not come from a pick here - a bookmarked or
  // restored selection still shows its code rather than a UUID.
  const detail = useJobWorkOrder(value ?? '', Boolean(value) && picked?.id !== value)

  const selected = value ? (picked?.id === value ? picked : (detail.data ?? null)) : null

  return (
    <SearchPicker<JobWorkOrderRow>
      id={id}
      label={label}
      required={required}
      display={selected ? orderLabel(selected) : null}
      placeholder={placeholder}
      disabled={disabled || !vendorId}
      searchLabel="Search job-work orders"
      searchPlaceholder="Search by order code or process"
      query={(term) => {
        const params = {
          vendor_id: vendorId,
          limit: PICKER_PAGE_SIZE,
          with_total: false,
          sort_by: 'created_at',
          sort_dir: 'desc',
          ...(term ? { search: term } : {}),
        }
        return {
          queryKey: jobWorkKeys.list(params),
          queryFn: ({ signal }) => api.getPage<JobWorkOrderRow>('/job-work', { params, signal }),
          staleTime: staleTimes.transactional,
        }
      }}
      getKey={(order) => order.id}
      selectedKey={value}
      onSelect={(order) => {
        setPicked(order)
        onChange(order.id, order)
      }}
      renderOption={(order) => (
        <>
          <span className="block truncate font-medium">{orderLabel(order)}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {order.process_description || humanise(order.process_type)}
          </span>
        </>
      )}
      emptyText={(term) =>
        term
          ? 'No order of this vendor matches that search.'
          : 'This vendor has no job-work orders.'
      }
      // Newest first, so a page that is not every match holds the newest ones.
      moreText={(shown) =>
        `Showing the newest ${shown}. Type more of the code to find an older order.`
      }
    />
  )
}

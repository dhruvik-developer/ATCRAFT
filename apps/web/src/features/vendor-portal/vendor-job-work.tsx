import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { FieldError, PageHeader, PanelLoader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle, Input, Label } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { formatDate, formatDateTime, formatQuantity, humanise } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'
import { useListQuery } from '@/hooks/use-list-query'

type JobWorkRow = SchemaOf<'VendorJobWorkOrderRow'>
type JobWorkDetail = SchemaOf<'VendorJobWorkOrderDetail'>
type JobWorkUpdate = SchemaOf<'VendorJobWorkUpdateRead'>

export function VendorJobWorkPage() {
  const query = useListQuery<JobWorkRow>({
    path: '/vendor-portal/job-work',
    queryKey: queryKeys.vendorPortal.jobWork,
    staleTime: staleTimes.transactional,
  })
  const columns: ColumnDef<JobWorkRow, unknown>[] = [
    {
      accessorKey: 'code',
      header: 'Order',
      cell: ({ row }) => (
        <Link
          className="font-medium text-primary underline-offset-4 hover:underline"
          to={`/vendor/job-work/${row.original.id}`}
        >
          {row.original.code}
        </Link>
      ),
    },
    {
      accessorKey: 'process_type',
      header: 'Process',
      cell: ({ row }) => humanise(row.original.process_type),
    },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: ({ row }) => <StatusBadge status={row.original.status} />,
    },
    {
      accessorKey: 'expected_return_date',
      header: 'Expected return',
      cell: ({ row }) => formatDate(row.original.expected_return_date),
    },
    {
      accessorKey: 'issued_quantity',
      header: 'Issued',
      cell: ({ row }) => formatQuantity(row.original.issued_quantity),
    },
    {
      accessorKey: 'received_quantity',
      header: 'Accounted',
      cell: ({ row }) => formatQuantity(row.original.received_quantity),
    },
    {
      accessorKey: 'pending_quantity',
      header: 'Still at vendor',
      cell: ({ row }) => formatQuantity(row.original.pending_quantity),
    },
  ]

  return (
    <>
      <PageHeader
        title="Job work"
        description="Track material sent to your company for processing and report progress."
      />
      <DataTable
        columns={columns}
        data={query.data}
        pagination={query.pagination}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onPageChange={query.setPage}
        onLimitChange={query.setLimit}
        onSearchChange={query.setSearch}
        onRefresh={() => void query.refetch()}
        searchValue={query.search}
        searchPlaceholder="Search job-work orders"
        emptyTitle="No job-work orders"
        emptyDescription="Orders issued to your vendor account will appear here."
      />
    </>
  )
}

export function VendorJobWorkDetailPage() {
  const { orderId = '' } = useParams()
  const queryClient = useQueryClient()
  const [updateType, setUpdateType] = useState<
    'IN_PROCESS' | 'READY_TO_RETURN' | 'DISPATCHED_BACK'
  >('IN_PROCESS')
  const [note, setNote] = useState('')
  const [expectedDate, setExpectedDate] = useState('')
  const query = useQuery({
    queryKey: queryKeys.vendorPortal.jobWorkOrder(orderId),
    queryFn: ({ signal }) =>
      api.get<JobWorkDetail>(`/vendor-portal/job-work/${orderId}`, { signal }),
    enabled: Boolean(orderId),
    staleTime: staleTimes.transactional,
  })
  const report = useMutation({
    mutationFn: () =>
      api.post<JobWorkUpdate>(`/vendor-portal/job-work/${orderId}/status`, {
        update_type: updateType,
        event_time: new Date().toISOString(),
        expected_date: expectedDate || null,
        note: note.trim() || null,
      }),
    onSuccess: async () => {
      setNote('')
      setExpectedDate('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.vendorPortal.jobWorkOrder(orderId) }),
        queryClient.invalidateQueries({ queryKey: ['vendor-portal', 'job-work'] }),
      ])
    },
  })

  if (query.isLoading) return <PanelLoader />
  if (query.error || !query.data) {
    return (
      <div role="alert" className="rounded-lg border border-destructive/30 p-4 text-sm">
        {describeError(query.error ?? new Error('Job-work order not found.'))}
      </div>
    )
  }
  const order = query.data
  const canReport = order.status === 'ISSUED' || order.status === 'PARTIALLY_RECEIVED'

  return (
    <div className="space-y-5">
      <PageHeader
        title={order.code}
        description={order.process_description || humanise(order.process_type)}
        breadcrumbs={[{ label: 'Job work', to: '/vendor/job-work' }, { label: order.code }]}
      />
      <Card>
        <CardHeader>
          <CardTitle>Order status</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-5">
          <StatusBadge status={order.status} />
          <span>Expected return: {formatDate(order.expected_return_date)}</span>
          <span>Issued: {formatQuantity(order.issued_quantity)}</span>
          <span>Accounted: {formatQuantity(order.received_quantity)}</span>
          <span>Still at vendor: {formatQuantity(order.pending_quantity)}</span>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Material</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead>
              <tr className="border-b text-muted-foreground">
                <th className="p-2">Item</th>
                <th className="p-2">Issued</th>
                <th className="p-2">Output received</th>
                <th className="p-2">Input accounted</th>
                <th className="p-2">Still at vendor</th>
              </tr>
            </thead>
            <tbody>
              {order.lines.map((line) => (
                <tr key={line.id} className="border-b last:border-0">
                  <td className="p-2">
                    {line.item_code} · {line.item_name} ({line.uom_code})
                  </td>
                  <td className="p-2">{formatQuantity(line.issued_quantity)}</td>
                  <td className="p-2">{formatQuantity(line.received_output_quantity)}</td>
                  <td className="p-2">{formatQuantity(line.received_quantity)}</td>
                  <td className="p-2">{formatQuantity(line.pending_quantity)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Delivery challans</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {order.challans.map((challan) => (
            <div key={challan.id} className="flex flex-wrap justify-between gap-2 border-b pb-2">
              <span>{challan.code}</span>
              <span>{formatDate(challan.challan_date)}</span>
              <span>{formatQuantity(challan.quantity)} issued</span>
              <span>Due {formatDate(challan.expected_return_date)}</span>
            </div>
          ))}
          {order.challans.length === 0 && (
            <p className="text-sm text-muted-foreground">No issued challans.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Receipts and quality outcome</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {order.receipts.map((receipt) => (
            <div key={receipt.id} className="flex flex-wrap justify-between gap-2 border-b pb-2">
              <span>{receipt.code}</span>
              <span>{formatDate(receipt.receipt_date)}</span>
              <span>{formatQuantity(receipt.output_quantity)} output</span>
              <span>{formatQuantity(receipt.rejected_quantity)} rejected</span>
              <span>
                {formatQuantity(receipt.returned_unprocessed_quantity)} unprocessed return
              </span>
            </div>
          ))}
          {order.receipts.length === 0 && (
            <p className="text-sm text-muted-foreground">No receipts have been recorded.</p>
          )}
        </CardContent>
      </Card>

      {canReport && (
        <Card>
          <CardHeader>
            <CardTitle>Report progress</CardTitle>
          </CardHeader>
          <CardContent>
            <form
              className="grid gap-4 sm:grid-cols-2"
              onSubmit={(event) => {
                event.preventDefault()
                report.mutate()
              }}
            >
              <div>
                <Label htmlFor="job-work-update-type">Progress</Label>
                <select
                  id="job-work-update-type"
                  className="mt-1.5 min-h-11 w-full rounded-md border bg-background px-3"
                  value={updateType}
                  onChange={(event) => {
                    const value = event.target.value
                    if (
                      value === 'IN_PROCESS' ||
                      value === 'READY_TO_RETURN' ||
                      value === 'DISPATCHED_BACK'
                    ) {
                      setUpdateType(value)
                    }
                  }}
                >
                  <option value="IN_PROCESS">In process</option>
                  <option value="READY_TO_RETURN">Ready to return</option>
                  <option value="DISPATCHED_BACK">Dispatched back</option>
                </select>
              </div>
              <div>
                <Label htmlFor="job-work-expected-date">Expected return date</Label>
                <Input
                  id="job-work-expected-date"
                  type="date"
                  value={expectedDate}
                  onChange={(event) => setExpectedDate(event.target.value)}
                  className="mt-1.5"
                />
              </div>
              <div className="sm:col-span-2">
                <Label htmlFor="job-work-update-note">Note</Label>
                <textarea
                  id="job-work-update-note"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  maxLength={500}
                  rows={3}
                  className="mt-1.5 w-full rounded-md border bg-background px-3 py-2"
                />
              </div>
              {report.error && (
                <FieldError
                  className="text-sm sm:col-span-2"
                  message={describeError(report.error)}
                />
              )}
              <Button type="submit" disabled={report.isPending} className="min-h-11 sm:col-span-2">
                {report.isPending ? 'Sending...' : 'Send progress update'}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Progress history</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {order.updates.map((update) => (
            <div key={update.id} className="flex flex-wrap justify-between gap-2 border-b pb-2">
              <span>{humanise(update.update_type)}</span>
              <span>{formatDateTime(update.event_time)}</span>
              {update.expected_date && <span>Expected {formatDate(update.expected_date)}</span>}
              {update.note && <span className="w-full text-muted-foreground">{update.note}</span>}
            </div>
          ))}
          {order.updates.length === 0 && (
            <p className="text-sm text-muted-foreground">No vendor progress updates yet.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

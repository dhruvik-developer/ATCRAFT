import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { ConfirmDialog, DescriptionList } from '@/components/shared'
import { DataTable } from '@/components/data-table'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDate, formatDateTime, formatMoney, humanise } from '@/lib/format'
import { describeError } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { InboundDocumentDialog } from './inbound-document-dialog'
import { InboundShipmentDialog } from './inbound-shipment-dialog'
import type { PurchaseOrderDetail } from './procurement-types'

type Shipment = SchemaOf<'InboundShipmentRead'>
type Attachment = SchemaOf<'InboundDocumentRead'>

type InboundPurchaseOrder = Pick<
  PurchaseOrderDetail,
  | 'id'
  | 'code'
  | 'status'
  | 'currency'
  | 'expected_delivery_date'
  | 'vendor_committed_date'
  | 'acknowledged_at'
  | 'ready_for_dispatch_at'
  | 'ready_packages'
  | 'vendor_decline_reason'
>

export function PurchaseOrderInboundPanel({ order }: { order: InboundPurchaseOrder }) {
  const { can } = useAuth()
  const client = useQueryClient()
  const [editing, setEditing] = useState<Shipment | 'new' | null>(null)
  const [upload, setUpload] = useState(false)
  const [adopting, setAdopting] = useState(false)
  const [delivering, setDelivering] = useState<Shipment | null>(null)
  const base = `/procurement/purchase-orders/${order.id}`
  const shipments = useListQuery<Shipment>({
    path: `${base}/shipments`,
    namespace: 'shipments',
    queryKey: (params) => queryKeys.procurement.shipments(order.id, params),
  })
  const documents = useListQuery<Attachment>({
    path: `${base}/documents`,
    namespace: 'documents',
    queryKey: (params) => queryKeys.procurement.documents(order.id, params),
  })
  const refresh = () => client.invalidateQueries({ queryKey: queryKeys.procurement.all })
  const errorToast = (error: unknown) => toast.error(describeError(error))
  const adopt = useMutation({
    mutationFn: () => api.post(`${base}/accept-vendor-date`),
    onError: errorToast,
    onSuccess: async () => {
      await refresh()
      toast.success('Vendor commitment adopted as the planning date')
      setAdopting(false)
    },
  })
  const delivered = useMutation({
    mutationFn: (shipment: Shipment) => api.post(`${base}/shipments/${shipment.id}/delivered`, {}),
    onError: errorToast,
    onSuccess: async () => {
      await refresh()
      toast.success('Shipment marked delivered')
      setDelivering(null)
    },
  })
  const download = useMutation({
    mutationFn: (file: Attachment) =>
      api.download(`/documents/${file.id}/download`, file.file_name),
    onError: errorToast,
  })
  const job = useReportJob()
  const pdf = useMutation({
    mutationFn: () => api.post<AcceptedJobData>(`${base}/pdf`),
    onError: errorToast,
    onSuccess: job.start,
  })
  const shipping = order.status === 'ACKNOWLEDGED' || order.status === 'PARTIALLY_RECEIVED'
  const canUpdate = can(P.PURCHASE_ORDER_UPDATE)
  const canAdopt =
    canUpdate &&
    shipping &&
    order.vendor_committed_date &&
    order.vendor_committed_date !== order.expected_delivery_date

  return (
    <Card>
      <CardHeader>
        <CardTitle>Vendor commitment and consignments</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {order.vendor_decline_reason && (
          <Alert variant="warning">
            <AlertDescription>
              Vendor declined: {order.vendor_decline_reason}. Use Cancel order or Send to vendor
              above to choose an assignee and send it again.
            </AlertDescription>
          </Alert>
        )}
        <DescriptionList
          columns={3}
          items={[
            { label: 'Planning delivery date', value: formatDate(order.expected_delivery_date) },
            { label: 'Vendor committed date', value: formatDate(order.vendor_committed_date) },
            { label: 'Vendor acknowledged', value: formatDateTime(order.acknowledged_at) },
            { label: 'Ready for dispatch', value: formatDateTime(order.ready_for_dispatch_at) },
            { label: 'Packages ready', value: order.ready_packages?.toString() ?? '—' },
          ]}
        />
        <div className="flex flex-wrap gap-2">
          {canAdopt && (
            <Button variant="outline" onClick={() => setAdopting(true)}>
              Adopt vendor date
            </Button>
          )}
          {shipping && canUpdate && <Button onClick={() => setEditing('new')}>Add shipment</Button>}
          {can(P.DOCUMENT_UPLOAD) && (
            <Button variant="outline" onClick={() => setUpload(true)}>
              Upload inbound document
            </Button>
          )}
          <Button
            variant="outline"
            disabled={pdf.isPending || job.phase === 'queued' || job.phase === 'running'}
            onClick={() => pdf.mutate()}
          >
            Generate purchase order PDF
          </Button>
        </div>
        <ReportJobPanel controller={job} label="Purchase order PDF" />
        <section aria-label="Numbered shipments" className="space-y-2">
          <h3 className="font-medium">Shipments</h3>
          <DataTable<Shipment>
            columns={[
              { accessorKey: 'shipment_number', header: 'Shipment', enableSorting: false },
              { accessorKey: 'courier_name', header: 'Courier', enableSorting: false },
              {
                accessorKey: 'tracking_reference',
                header: 'Tracking / LR',
                enableSorting: false,
                cell: ({ row }) => (
                  <div>
                    {row.original.tracking_reference ?? '—'}
                    <p className="text-xs text-muted-foreground">{row.original.lr_number}</p>
                  </div>
                ),
              },
              {
                id: 'dates',
                header: 'Dates',
                cell: ({ row }) => (
                  <div className="whitespace-nowrap text-xs">
                    <p>Dispatched {formatDateTime(row.original.shipped_at)}</p>
                    <p>Expected {formatDate(row.original.expected_delivery_date)}</p>
                    <p>Delivered {formatDateTime(row.original.delivered_at)}</p>
                  </div>
                ),
              },
              {
                id: 'details',
                header: 'Consignment',
                cell: ({ row }) => (
                  <div className="text-xs">
                    <p>
                      {row.original.packages ?? '—'} packages · {row.original.weight_kg ?? '—'} kg
                    </p>
                    <p>
                      {row.original.vehicle_number} {row.original.driver_name}{' '}
                      {row.original.driver_mobile}
                    </p>
                    <p>
                      {formatMoney(row.original.freight_amount, { currency: order.currency })}{' '}
                      freight
                    </p>
                    <p className="max-w-64 whitespace-pre-wrap break-words">{row.original.notes}</p>
                  </div>
                ),
              },
              {
                id: 'actions',
                header: 'Actions',
                cell: ({ row }) => (
                  <div className="flex flex-wrap gap-2">
                    {canUpdate && shipping && (
                      <Button size="sm" variant="outline" onClick={() => setEditing(row.original)}>
                        Edit shipment {row.original.shipment_number}
                      </Button>
                    )}
                    {can(P.GRN_CREATE) && !row.original.delivered_at && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDelivering(row.original)}
                      >
                        Mark shipment {row.original.shipment_number} delivered
                      </Button>
                    )}
                  </div>
                ),
              },
            ]}
            data={shipments.data}
            pagination={shipments.pagination}
            isLoading={shipments.isLoading}
            isFetching={shipments.isFetching}
            error={shipments.error}
            onPageChange={shipments.setPage}
            onLimitChange={shipments.setLimit}
            onRefresh={() => void shipments.refetch()}
            getRowId={(row) => row.id}
            emptyTitle="No shipments recorded"
          />
        </section>
        <section aria-label="Inbound documents" className="space-y-2">
          <h3 className="font-medium">Documents</h3>
          <DataTable<Attachment>
            columns={[
              { accessorKey: 'file_name', header: 'File', enableSorting: false },
              {
                accessorKey: 'kind',
                header: 'Kind',
                enableSorting: false,
                cell: ({ row }) => humanise(row.original.kind),
              },
              {
                id: 'shipment',
                header: 'Scope',
                cell: ({ row }) =>
                  row.original.shipment_id ? 'Shipment attachment' : 'Whole order',
              },
              {
                id: 'download',
                header: 'Download',
                cell: ({ row }) =>
                  can(P.DOCUMENT_VIEW) ? (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={download.isPending}
                      onClick={() => download.mutate(row.original)}
                    >
                      Download {row.original.file_name}
                    </Button>
                  ) : (
                    'Download permission required'
                  ),
              },
            ]}
            data={documents.data}
            pagination={documents.pagination}
            isLoading={documents.isLoading}
            isFetching={documents.isFetching}
            error={documents.error}
            onPageChange={documents.setPage}
            onLimitChange={documents.setLimit}
            onRefresh={() => void documents.refetch()}
            getRowId={(row) => row.id}
            emptyTitle="No inbound documents"
          />
        </section>
        {editing && (
          <InboundShipmentDialog
            scope="staff"
            order={order}
            shipment={editing === 'new' ? undefined : editing}
            onClose={() => setEditing(null)}
          />
        )}
        {upload && (
          <InboundDocumentDialog
            scope="staff"
            order={{
              id: order.id,
              code: order.code,
              shipments: shipments.page === 1 ? shipments.data : [],
              shipments_pagination: {
                limit: shipments.limit,
                has_next: shipments.page !== 1 || !!shipments.pagination?.has_next,
                has_previous: false,
              },
            }}
            canShip
            canInvoice
            onClose={() => setUpload(false)}
          />
        )}
        <ConfirmDialog
          open={adopting}
          onOpenChange={(open) => {
            if (!adopt.isPending) setAdopting(open)
          }}
          title="Adopt vendor delivery date?"
          description={`Use ${formatDate(order.vendor_committed_date)} as the planning date. This changes when MRP expects this supply.`}
          confirmLabel="Adopt date"
          loading={adopt.isPending}
          onConfirm={() => adopt.mutate()}
        />
        <ConfirmDialog
          open={!!delivering}
          onOpenChange={(open) => {
            if (!open && !delivered.isPending) setDelivering(null)
          }}
          title="Mark shipment delivered?"
          description="Record arrival at the gate now. Goods receiving and quality still need their own documents."
          confirmLabel="Mark delivered"
          loading={delivered.isPending}
          onConfirm={() => {
            if (delivering) delivered.mutate(delivering)
          }}
        />
      </CardContent>
    </Card>
  )
}

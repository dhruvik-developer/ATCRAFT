import { describeError, success } from '@/lib/messages'
import type { ColumnDef } from '@tanstack/react-table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatMoney, toDecimal } from '@/lib/format'
import { REASON_MIN_LENGTH } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { P } from '@/lib/permissions'
import { ApiError, api } from '@/lib/api-client'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

/** CreditNoteCancel permits a longer audit explanation than standard transitions. */
const CANCEL_REASON_MAX_LENGTH = 500

type CreditNoteRow = {
  id: string
  code: string
  sales_invoice_id: string
  customer_id: string
  note_date: string
  reason_type: string
  status: 'ISSUED' | 'CANCELLED'
  currency: string
  total_amount: string
  buyer_name: string
}

type CreditNoteDetail = CreditNoteRow & {
  sales_return_id: string | null
  taxable_amount: string
  tax_amount: string
  round_off_amount: string
  issued_at: string
  pdf_document_id: string | null
  cancelled_at: string | null
  cancellation_reason: string | null
  lines: Array<{
    id: string
    sales_invoice_line_id: string
    item_id: string
    hsn_code: string | null
    quantity: string | null
    taxable_amount: string
    tax_rate: string
    cgst_amount: string
    sgst_amount: string
    igst_amount: string
    tax_amount: string
    line_total: string
  }>
}

export default function CreditNotesPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<CreditNoteRow | null>(null)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  const detail = useQuery({
    queryKey: ['finance', 'credit-notes', 'detail', selected?.id ?? ''],
    queryFn: () => api.get<CreditNoteDetail>(`/finance/credit-notes/${selected?.id}`),
    enabled: Boolean(selected),
  })
  const job = useReportJob()
  const pdf = useMutation({
    mutationFn: () =>
      api.post<AcceptedJobData>(`/finance/credit-notes/${selected?.id}/pdf`, undefined),
    onSuccess: (accepted) => job.start(accepted),
  })
  const list = useListQuery<CreditNoteRow>({
    path: '/finance/credit-notes',
    queryKey: (params) => ['finance', 'credit-notes', params],
    defaultSortBy: 'note_date',
    defaultSortDir: 'desc',
    enabled: can(P.CREDIT_NOTE_VIEW),
  })
  const columns = useMemo<ColumnDef<CreditNoteRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Credit note',
        cell: ({ row }) => (
          <button
            type="button"
            className="font-medium hover:underline"
            onClick={() => setSelected(row.original)}
          >
            {row.original.code}
          </button>
        ),
      },
      {
        accessorKey: 'note_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.note_date),
      },
      { accessorKey: 'buyer_name', header: 'Customer' },
      { accessorKey: 'reason_type', header: 'Reason' },
      {
        accessorKey: 'total_amount',
        header: 'Total',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.total_amount, { currency: row.original.currency }),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [],
  )

  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      api.post<CreditNoteDetail>(`/finance/credit-notes/${id}/cancel`, { reason }),
    onSuccess: () => {
      setCancelOpen(false)
      setSelected(null)
      setCancelReason('')
      setReasonTouched(false)
      void queryClient.invalidateQueries({ queryKey: ['finance'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.salesReturns.all })
      toast.success(success.cancelled('Credit note'))
    },
  })
  const canCancel =
    can(P.CREDIT_NOTE_CANCEL) && (detail.data?.status ?? selected?.status) === 'ISSUED'
  const reasonInvalid =
    cancelReason.trim().length < REASON_MIN_LENGTH ||
    cancelReason.trim().length > CANCEL_REASON_MAX_LENGTH
  const serverReasonError =
    cancel.error instanceof ApiError && Boolean(cancel.error.fieldErrors.reason?.length)

  async function generatePdf() {
    if (!selected) return
    pdf.mutate()
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Credit notes"
        description="Customer credits issued against sales invoices."
        breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Credit notes' }]}
      />
      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        searchValue={list.search}
        searchPlaceholder="Search credit notes"
        onRefresh={() => void list.refetch()}
        emptyTitle="No credit notes"
        emptyDescription="Issued customer credits will appear here."
      />
      {selected && (
        <div className="rounded-lg border border-border bg-card p-4 text-sm">
          <div className="flex items-center justify-between">
            <strong>{selected.code}</strong>
            <Button
              variant="ghost"
              onClick={() => {
                job.reset()
                setSelected(null)
              }}
              disabled={cancel.isPending}
            >
              Close
            </Button>
          </div>
          <p className="mt-2 text-muted-foreground">
            {selected.buyer_name} ·{' '}
            {formatMoney(selected.total_amount, { currency: selected.currency })}
          </p>
          {detail.isLoading && <p className="mt-4 text-muted-foreground">Loading line details…</p>}
          {detail.error && <FieldError message={describeError(detail.error)} />}
          {detail.data && (
            <div className="mt-4 space-y-4">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <span className="text-muted-foreground">Taxable</span>
                  <p>
                    {formatMoney(detail.data.taxable_amount, { currency: detail.data.currency })}
                  </p>
                </div>
                <div>
                  <span className="text-muted-foreground">Tax</span>
                  <p>{formatMoney(detail.data.tax_amount, { currency: detail.data.currency })}</p>
                </div>
                {!toDecimal(detail.data.round_off_amount).isZero() && (
                  <div>
                    <span className="text-muted-foreground">Round off</span>
                    <p>
                      {formatMoney(detail.data.round_off_amount, {
                        currency: detail.data.currency,
                      })}
                    </p>
                  </div>
                )}
                <div>
                  <span className="text-muted-foreground">Invoice</span>
                  <p className="font-mono text-xs">{detail.data.sales_invoice_id}</p>
                </div>
              </div>
              <div
                className="overflow-x-auto"
                role="region"
                aria-label="Credit note line details"
                tabIndex={0}
              >
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="whitespace-nowrap p-2">Item</th>
                      <th className="whitespace-nowrap p-2">HSN</th>
                      <th className="whitespace-nowrap p-2">Quantity</th>
                      <th className="whitespace-nowrap p-2 text-right">Taxable</th>
                      <th className="whitespace-nowrap p-2 text-right">Tax</th>
                      <th className="whitespace-nowrap p-2 text-right">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.lines.map((line) => (
                      <tr key={line.id} className="border-b">
                        <td className="p-2 font-mono text-xs">{line.item_id}</td>
                        <td className="p-2">{line.hsn_code ?? '-'}</td>
                        <td className="p-2">{line.quantity ?? '-'}</td>
                        <td className="p-2 text-right tabular">
                          {formatMoney(line.taxable_amount, { currency: detail.data.currency })}
                        </td>
                        <td className="p-2 text-right tabular">
                          {formatMoney(line.tax_amount, { currency: detail.data.currency })}
                        </td>
                        <td className="p-2 text-right tabular">
                          {formatMoney(line.line_total, { currency: detail.data.currency })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {canCancel && (
            <Button
              variant="destructive"
              className="mt-4"
              disabled={detail.isLoading || Boolean(detail.error)}
              onClick={() => {
                cancel.reset()
                setCancelReason('')
                setReasonTouched(false)
                setCancelOpen(true)
              }}
            >
              Cancel credit note
            </Button>
          )}
          {can(P.CREDIT_NOTE_VIEW) && (
            <Button
              variant="outline"
              className="mt-4 ml-2"
              loading={pdf.isPending}
              onClick={() => void generatePdf()}
            >
              Generate PDF
            </Button>
          )}
          <div className="mt-4">
            <ReportJobPanel controller={job} label="Credit note PDF" />
          </div>
        </div>
      )}
      <ConfirmDialog
        open={cancelOpen && Boolean(selected)}
        onOpenChange={(open) => {
          if (!cancel.isPending) setCancelOpen(open)
        }}
        title={`Cancel credit note ${selected?.code ?? ''}?`}
        description="This reverses the credit note and restores the invoice balance. Record the cancellation reason for the audit history."
        confirmLabel="Confirm cancellation"
        destructive
        loading={cancel.isPending}
        onConfirm={() => {
          setReasonTouched(true)
          if (!reasonInvalid && selected && canCancel && !cancel.isPending)
            cancel.mutate({ id: selected.id, reason: cancelReason.trim() })
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="credit-note-cancel-reason" required>
            Cancellation reason
          </Label>
          <Textarea
            id="credit-note-cancel-reason"
            value={cancelReason}
            maxLength={CANCEL_REASON_MAX_LENGTH}
            disabled={cancel.isPending}
            aria-invalid={(reasonTouched && reasonInvalid) || serverReasonError}
            onChange={(event) => {
              setCancelReason(event.target.value)
              if (cancel.error) cancel.reset()
            }}
          />
          <FieldError
            message={
              reasonTouched && reasonInvalid
                ? `Enter between ${REASON_MIN_LENGTH} and ${CANCEL_REASON_MAX_LENGTH} characters.`
                : undefined
            }
          />
          {cancel.error && (
            <FieldError message={fieldErrorSummary(cancel.error) || describeError(cancel.error)} />
          )}
          {!canCancel && selected && (
            <FieldError message="Only an issued credit note can be cancelled with the cancellation permission." />
          )}
        </div>
      </ConfirmDialog>
    </div>
  )
}

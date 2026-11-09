import { describeError, success } from '@/lib/messages'
import type { ColumnDef } from '@tanstack/react-table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { CellButton, DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, LoadError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Label, Textarea } from '@/components/ui/primitives'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroller,
} from '@/components/ui/table'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { useListQuery } from '@/hooks/use-list-query'
import { EMPTY_VALUE, formatDate, formatMoney, formatQuantity, toDecimal } from '@/lib/format'
import { REASON_MIN_LENGTH } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { P } from '@/lib/permissions'
import { ApiError, api } from '@/lib/api-client'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { financeKeys } from './finance-types'

/** CreditNoteCancel permits a longer audit explanation than standard transitions. */
const CANCEL_REASON_MAX_LENGTH = 500

/** One row of `GET /finance/credit-notes`; it names its invoice and return by code. */
type CreditNoteRow = SchemaOf<'CreditNoteSummary'>

/** `GET /finance/credit-notes/{id}`: the header plus lines that name their items. */
type CreditNoteDetail = SchemaOf<'CreditNoteDetail'>

export default function CreditNotesPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<CreditNoteRow | null>(null)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  const detail = useQuery({
    queryKey: financeKeys.creditNote(selected?.id ?? ''),
    queryFn: ({ signal }) =>
      api.get<CreditNoteDetail>(`/finance/credit-notes/${selected?.id}`, { signal }),
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
    queryKey: financeKeys.creditNotes,
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
          <CellButton onClick={() => setSelected(row.original)}>{row.original.code}</CellButton>
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
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.customers.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.salesReturns.all })
      toast.success(success.cancelled('Credit note'))
    },
    // The confirmation shows the refusal under the reason; a toast would repeat it.
    meta: { suppressErrorToast: true },
  })
  const canCancel =
    can(P.CREDIT_NOTE_CANCEL) && (detail.data?.status ?? selected?.status) === 'ISSUED'
  const reasonInvalid =
    cancelReason.trim().length < REASON_MIN_LENGTH ||
    cancelReason.trim().length > CANCEL_REASON_MAX_LENGTH
  const serverReasonError =
    cancel.error instanceof ApiError && Boolean(cancel.error.fieldErrors.reason?.length)

  function closeDetail() {
    job.reset()
    setSelected(null)
  }

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
      {/* A dialog, like every sibling list's detail: opened from a row anywhere
          in a long list, a panel under the table would open off-screen. */}
      <Dialog
        open={Boolean(selected)}
        onOpenChange={(open) => {
          if (!open && !cancel.isPending) closeDetail()
        }}
      >
        {selected && (
          <DialogContent size="lg">
            <DialogHeader>
              <DialogTitle>{selected.code}</DialogTitle>
              <DialogDescription>
                {selected.buyer_name} ·{' '}
                {formatMoney(selected.total_amount, { currency: selected.currency })}
              </DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-4 text-sm">
              {detail.isLoading && <p className="text-muted-foreground">Loading line details…</p>}
              {detail.error && (
                <LoadError error={detail.error} onRetry={() => void detail.refetch()} />
              )}
              {detail.data && (
                <div className="space-y-4">
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <div>
                      <span className="text-muted-foreground">Taxable</span>
                      <p>
                        {formatMoney(detail.data.taxable_amount, {
                          currency: detail.data.currency,
                        })}
                      </p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Tax</span>
                      <p>
                        {formatMoney(detail.data.tax_amount, { currency: detail.data.currency })}
                      </p>
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
                      <p>{detail.data.sales_invoice_code ?? EMPTY_VALUE}</p>
                    </div>
                    {detail.data.sales_return_id && (
                      <div>
                        <span className="text-muted-foreground">Sales return</span>
                        <p>{detail.data.sales_return_code ?? EMPTY_VALUE}</p>
                      </div>
                    )}
                  </div>
                  <TableScroller label="Credit note line details">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="whitespace-nowrap">Item</TableHead>
                          <TableHead className="whitespace-nowrap">HSN</TableHead>
                          <TableHead align="right" className="whitespace-nowrap">
                            Quantity
                          </TableHead>
                          <TableHead align="right" className="whitespace-nowrap">
                            Taxable
                          </TableHead>
                          <TableHead align="right" className="whitespace-nowrap">
                            Tax
                          </TableHead>
                          <TableHead align="right" className="whitespace-nowrap">
                            Total
                          </TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {detail.data.lines.map((line) => (
                          <TableRow key={line.id}>
                            <TableCell>
                              <span className="block font-medium">
                                {line.item_code ?? EMPTY_VALUE}
                              </span>
                              {line.item_name && (
                                <span className="block max-w-[16rem] truncate text-xs text-muted-foreground">
                                  {line.item_name}
                                </span>
                              )}
                            </TableCell>
                            <TableCell>{line.hsn_code ?? EMPTY_VALUE}</TableCell>
                            <TableCell align="right">
                              {line.quantity ? formatQuantity(line.quantity) : EMPTY_VALUE}
                            </TableCell>
                            <TableCell align="right">
                              {formatMoney(line.taxable_amount, { currency: detail.data.currency })}
                            </TableCell>
                            <TableCell align="right">
                              {formatMoney(line.tax_amount, { currency: detail.data.currency })}
                            </TableCell>
                            <TableCell align="right">
                              {formatMoney(line.line_total, { currency: detail.data.currency })}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableScroller>
                </div>
              )}
              <ReportJobPanel controller={job} label="Credit note PDF" />
            </DialogBody>
            <DialogFooter>
              {canCancel && (
                <Button
                  variant="destructive"
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
                  loading={pdf.isPending}
                  onClick={() => void generatePdf()}
                >
                  Generate PDF
                </Button>
              )}
              <Button variant="outline" onClick={closeDetail} disabled={cancel.isPending}>
                Close
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
      <ConfirmDialog
        open={cancelOpen && Boolean(selected)}
        onOpenChange={(open) => {
          if (!cancel.isPending) setCancelOpen(open)
        }}
        title={`Cancel credit note ${selected?.code ?? ''}?`}
        description="This reverses the credit note and restores the invoice balance. Record the cancellation reason for the audit history."
        confirmLabel="Cancel credit note"
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

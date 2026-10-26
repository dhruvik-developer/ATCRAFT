import { useMutation, useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DescriptionList, FieldError, PageHeader, StatTile, StatusBadge } from '@/components/shared'
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
import { Badge, Card, CardContent, Input, Label, Textarea } from '@/components/ui/primitives'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDate, formatMoney, formatQuantity, toDecimal } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import { DEALER_API, dealerError, useDealerAction, useDealerRead } from './dealer-hooks'
import { dealerReturnPayload } from './dealer-payloads'
import { DealerCredit, DealerResult } from './dealer-shared'

export function DealerPdf({ resource, label = 'PDF' }: { resource: string; label?: string }) {
  const job = useReportJob()
  const queue = useMutation({
    mutationFn: () => api.post<AcceptedJobData>(`${DEALER_API}/${resource}/pdf`),
    onSuccess: job.start,
  })
  return (
    <div className="space-y-3">
      <Button
        variant="outline"
        className="min-h-11"
        onClick={() => queue.mutate()}
        disabled={queue.isPending || ['queued', 'running'].includes(job.phase)}
      >
        Generate {label}
      </Button>
      <ReportJobPanel controller={job} label={label} />
    </div>
  )
}

type Invoice = SchemaOf<'DealerInvoiceSummary'>
const invoiceColumns: ColumnDef<Invoice, unknown>[] = [
  {
    accessorKey: 'code',
    header: 'Invoice',
    cell: ({ row }) => (
      <Link to={`/dealer/invoices/${row.original.id}`} className="font-medium underline">
        {row.original.code}
      </Link>
    ),
  },
  {
    accessorKey: 'invoice_date',
    header: 'Date',
    cell: ({ row }) => formatDate(row.original.invoice_date),
    enableSorting: false,
  },
  {
    accessorKey: 'due_date',
    header: 'Due',
    cell: ({ row }) => (
      <div>
        {formatDate(row.original.due_date)}
        {row.original.overdue_days > 0 && (
          <Badge variant="destructive" className="ml-2">
            {row.original.overdue_days} days overdue
          </Badge>
        )}
      </div>
    ),
    enableSorting: false,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
    enableSorting: false,
  },
  {
    accessorKey: 'total_amount',
    header: 'Total',
    cell: ({ row }) => formatMoney(row.original.total_amount, { currency: row.original.currency }),
    enableSorting: false,
  },
  {
    accessorKey: 'balance_amount',
    header: 'Balance',
    cell: ({ row }) =>
      formatMoney(row.original.balance_amount, { currency: row.original.currency }),
    enableSorting: false,
  },
]

export function DealerInvoicesPage() {
  const list = useListQuery<Invoice>({
    path: `${DEALER_API}/invoices`,
    queryKey: (params) => queryKeys.dealerPortal.list('invoices', params),
    staleTime: staleTimes.transactional,
  })
  return (
    <>
      <PageHeader
        title="Invoices"
        description="Your issued invoices, payments and remaining balances."
      />
      <DataTable
        columns={invoiceColumns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSearchChange={list.setSearch}
        searchValue={list.search}
        onRefresh={() => void list.refetch()}
        emptyTitle="No invoices yet"
      />
    </>
  )
}

export function DealerInvoicePage() {
  const { invoiceId = '' } = useParams()
  const detail = useDealerRead<SchemaOf<'DealerInvoiceDetail'>>(
    `invoices/${invoiceId}`,
    Boolean(invoiceId),
  )
  const invoice = detail.data
  return (
    <>
      <PageHeader
        title={invoice?.code || 'Invoice'}
        breadcrumbs={[
          { label: 'Invoices', to: '/dealer/invoices' },
          { label: invoice?.code || 'Invoice' },
        ]}
        status={<StatusBadge status={invoice?.status} />}
      />
      <DealerResult loading={detail.isLoading} error={detail.error}>
        {invoice && (
          <div className="space-y-5">
            <DescriptionList
              items={[
                { label: 'Invoice date', value: formatDate(invoice.invoice_date) },
                { label: 'Due date', value: formatDate(invoice.due_date) },
                { label: 'Seller', value: invoice.seller_name },
                { label: 'GSTIN', value: invoice.seller_gstin || '—' },
                { label: 'Bill to', value: invoice.billing_address },
                { label: 'Ship to', value: invoice.shipping_address },
                {
                  label: 'Paid',
                  value: formatMoney(invoice.paid_amount, { currency: invoice.currency }),
                },
                {
                  label: 'Credits',
                  value: formatMoney(invoice.credited_amount, { currency: invoice.currency }),
                },
                {
                  label: 'Total',
                  value: formatMoney(invoice.total_amount, { currency: invoice.currency }),
                },
                {
                  label: 'Balance',
                  value: formatMoney(invoice.balance_amount, { currency: invoice.currency }),
                },
              ]}
            />
            <DealerPdf resource={`invoices/${invoiceId}`} label="invoice PDF" />
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">Invoice lines</caption>
                <thead>
                  <tr>
                    {['Description', 'HSN', 'Quantity', 'Price', 'Tax', 'Total'].map((name) => (
                      <th key={name} className="p-2 text-left">
                        {name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {invoice.lines.map((line) => (
                    <tr key={line.id} className="border-t">
                      <td className="p-2">{line.description}</td>
                      <td className="p-2">{line.hsn_code || '—'}</td>
                      <td className="p-2">
                        {formatQuantity(line.quantity)} {line.uom_code}
                      </td>
                      <td className="p-2">
                        {formatMoney(line.unit_price, { currency: invoice.currency })}
                      </td>
                      <td className="p-2">
                        {formatMoney(line.tax_amount, { currency: invoice.currency })}
                      </td>
                      <td className="p-2">
                        {formatMoney(line.line_total, { currency: invoice.currency })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <DescriptionList
              items={[
                {
                  label: 'Taxable amount',
                  value: formatMoney(invoice.taxable_amount, { currency: invoice.currency }),
                },
                {
                  label: 'CGST',
                  value: formatMoney(invoice.cgst_amount, { currency: invoice.currency }),
                },
                {
                  label: 'SGST',
                  value: formatMoney(invoice.sgst_amount, { currency: invoice.currency }),
                },
                {
                  label: 'IGST',
                  value: formatMoney(invoice.igst_amount, { currency: invoice.currency }),
                },
              ]}
            />
            <section>
              <h2 className="mb-3 font-semibold">Payments</h2>
              {invoice.payments.map((payment) => (
                <p key={payment.payment_id} className="border-t py-3 text-sm">
                  {payment.payment_code} · {formatDate(payment.payment_date)} ·{' '}
                  {formatMoney(payment.amount, { currency: invoice.currency })}
                </p>
              ))}
              {!invoice.payments.length && (
                <p className="text-sm text-muted-foreground">No payments recorded.</p>
              )}
            </section>
            <DealerCreditNotes invoiceId={invoiceId} />
          </div>
        )}
      </DealerResult>
    </>
  )
}

function DealerCreditNotes({ invoiceId }: { invoiceId?: string }) {
  const list = useListQuery<SchemaOf<'DealerCreditNoteSummary'>>({
    path: `${DEALER_API}/credit-notes`,
    queryKey: (params) => queryKeys.dealerPortal.list('credit-notes', params),
    namespace: 'credits',
    filters: { invoice_id: invoiceId },
    staleTime: staleTimes.transactional,
  })
  const columns = useMemo<ColumnDef<SchemaOf<'DealerCreditNoteSummary'>, unknown>[]>(
    () => [
      { accessorKey: 'code', header: 'Credit note', enableSorting: false },
      {
        accessorKey: 'note_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.note_date),
        enableSorting: false,
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
        enableSorting: false,
      },
      {
        accessorKey: 'total_amount',
        header: 'Amount',
        cell: ({ row }) => (
          <div>
            {formatMoney(row.original.total_amount, { currency: row.original.currency })}
            {!toDecimal(row.original.round_off_amount).isZero() && (
              <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 text-xs text-muted-foreground">
                <dt>Taxable</dt>
                <dd>
                  {formatMoney(row.original.taxable_amount, { currency: row.original.currency })}
                </dd>
                <dt>Tax</dt>
                <dd>{formatMoney(row.original.tax_amount, { currency: row.original.currency })}</dd>
                <dt>Round off</dt>
                <dd>
                  {formatMoney(row.original.round_off_amount, { currency: row.original.currency })}
                </dd>
              </dl>
            )}
          </div>
        ),
        enableSorting: false,
      },
      {
        id: 'pdf',
        header: 'PDF',
        cell: ({ row }) => (
          <DealerPdf
            resource={`credit-notes/${row.original.id}`}
            label={`${row.original.code} PDF`}
          />
        ),
      },
    ],
    [],
  )
  return (
    <section>
      <h2 className="mb-3 font-semibold">Credit notes</h2>
      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onRefresh={() => void list.refetch()}
        emptyTitle="No credit notes"
      />
    </section>
  )
}

export function DealerAccountPage() {
  const me = useDealerRead<SchemaOf<'DealerPortalMe'>>('me')
  const outstanding = useDealerRead<SchemaOf<'DealerOutstanding'>>('outstanding')
  const [params, setParams] = useSearchParams()
  const rawPage = Number(params.get('statement_page') || '1')
  const page = Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1
  const statementParams = {
    page,
    limit: DEFAULT_PAGE_SIZE,
    date_from: params.get('date_from') || undefined,
    date_to: params.get('date_to') || undefined,
  }
  const statement = useQuery({
    queryKey: queryKeys.dealerPortal.list('statement', statementParams),
    queryFn: ({ signal }) =>
      api.get<SchemaOf<'LedgerStatement'>>(`${DEALER_API}/statement`, {
        params: statementParams,
        signal,
      }),
    staleTime: staleTimes.transactional,
  })
  const setField = (key: string, value: string) =>
    setParams((current) => {
      const next = new URLSearchParams(current)
      if (value) next.set(key, value)
      else next.delete(key)
      if (key !== 'statement_page') next.delete('statement_page')
      return next
    })
  const position = outstanding.data
  return (
    <>
      <PageHeader
        title="Account"
        description="Your credit, receivables, account statement and credit notes."
        actions={
          <>
            <Button variant="outline" asChild>
              <Link to="/profile">Profile and security</Link>
            </Button>
            <Button variant="outline" asChild>
              <Link to="/notifications">Notifications</Link>
            </Button>
          </>
        }
      />
      <div className="space-y-6">
        <DealerResult loading={me.isLoading} error={me.error}>
          {me.data && <DealerCredit credit={me.data.credit} />}
        </DealerResult>
        <DealerResult loading={outstanding.isLoading} error={outstanding.error}>
          {position && (
            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile
                label="Overdue"
                value={formatMoney(position.overdue_amount, { currency: position.currency })}
                tone="warning"
              />
              <StatTile
                label="Not due"
                value={formatMoney(position.ageing_not_due, { currency: position.currency })}
              />
              <StatTile
                label="Total outstanding"
                value={formatMoney(position.ageing_total_outstanding, {
                  currency: position.currency,
                })}
              />
            </div>
          )}
        </DealerResult>
        {position && position.overdue_invoices.length > 0 && (
          <section>
            <h2 className="mb-3 font-semibold">Overdue invoices</h2>
            <ul className="space-y-2">
              {position.overdue_invoices.map((invoice) => (
                <li key={invoice.document_id} className="rounded border p-3">
                  <Link className="underline" to={`/dealer/invoices/${invoice.document_id}`}>
                    {invoice.document_code}
                  </Link>{' '}
                  · {formatDate(invoice.due_date)} ·{' '}
                  {formatMoney(invoice.outstanding_amount, { currency: invoice.currency })}
                </li>
              ))}
            </ul>
          </section>
        )}
        <section>
          <h2 className="mb-3 font-semibold">Statement</h2>
          <div className="mb-4 grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="statement-from">From</Label>
              <Input
                id="statement-from"
                type="date"
                value={params.get('date_from') || ''}
                onChange={(event) => setField('date_from', event.target.value)}
                max={params.get('date_to') || undefined}
                className="mt-1 min-h-11"
              />
            </div>
            <div>
              <Label htmlFor="statement-to">To</Label>
              <Input
                id="statement-to"
                type="date"
                value={params.get('date_to') || ''}
                onChange={(event) => setField('date_to', event.target.value)}
                min={params.get('date_from') || undefined}
                className="mt-1 min-h-11"
              />
            </div>
          </div>
          <DealerResult loading={statement.isLoading} error={statement.error}>
            {statement.data && (
              <>
                <DescriptionList
                  items={[
                    {
                      label: 'Opening balance',
                      value: formatMoney(statement.data.opening_balance),
                    },
                    {
                      label: 'Closing balance',
                      value: formatMoney(statement.data.closing_balance),
                    },
                  ]}
                />
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full text-sm">
                    <caption className="sr-only">Account statement</caption>
                    <thead>
                      <tr>
                        {['Date', 'Reference', 'Debit', 'Credit', 'Balance'].map((name) => (
                          <th key={name} className="p-2 text-left">
                            {name}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {statement.data.rows?.map((row) => (
                        <tr key={row.entry_id} className="border-t">
                          <td className="p-2">{formatDate(row.entry_date)}</td>
                          <td className="p-2">{row.reference_code || row.reference_type}</td>
                          <td className="p-2">{formatMoney(row.debit_amount)}</td>
                          <td className="p-2">{formatMoney(row.credit_amount)}</td>
                          <td className="p-2">{formatMoney(row.running_balance)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <nav aria-label="Statement pagination" className="mt-4 flex justify-between gap-3">
                  <Button
                    variant="outline"
                    disabled={page <= 1}
                    onClick={() => setField('statement_page', String(page - 1))}
                  >
                    Previous
                  </Button>
                  <span>Page {page}</span>
                  <Button
                    variant="outline"
                    disabled={(statement.data.rows?.length ?? 0) < DEFAULT_PAGE_SIZE}
                    onClick={() => setField('statement_page', String(page + 1))}
                  >
                    Next
                  </Button>
                </nav>
              </>
            )}
          </DealerResult>
        </section>
        <DealerCreditNotes />
      </div>
    </>
  )
}

export function DealerReturnsPage() {
  const list = useListQuery<SchemaOf<'DealerReturnSummary'>>({
    path: `${DEALER_API}/returns`,
    queryKey: (params) => queryKeys.dealerPortal.list('returns', params),
    staleTime: staleTimes.transactional,
  })
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<SchemaOf<'DealerEligibleDispatch'>>()
  const [detailId, setDetailId] = useState<string>()
  const [formError, setFormError] = useState<string>()
  const eligible = useListQuery<SchemaOf<'DealerEligibleDispatch'>>({
    path: `${DEALER_API}/returns/eligible-dispatches`,
    queryKey: (params) => queryKeys.dealerPortal.list('eligible-dispatches', params),
    namespace: 'eligible',
    enabled: open,
  })
  const detail = useDealerRead<SchemaOf<'DealerReturnDetail'>>(
    `returns/${detailId}`,
    Boolean(detailId),
  )
  const create = useDealerAction(
    (payload: SchemaOf<'DealerReturnCreate'>) =>
      api.post<SchemaOf<'DealerReturnDetail'>>(`${DEALER_API}/returns`, payload),
    'Return request submitted',
    () => {
      setOpen(false)
      setSelected(undefined)
    },
  )
  const columns = useMemo<ColumnDef<SchemaOf<'DealerReturnSummary'>, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Return',
        cell: ({ row }) => (
          <button
            type="button"
            onClick={() => setDetailId(row.original.id)}
            className="min-h-11 font-medium underline"
          >
            {row.original.code}
          </button>
        ),
        enableSorting: false,
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
        enableSorting: false,
      },
      {
        accessorKey: 'received_at',
        header: 'Received',
        cell: ({ row }) => formatDate(row.original.received_at),
        enableSorting: false,
      },
      {
        accessorKey: 'credited_amount',
        header: 'Credited',
        cell: ({ row }) => formatMoney(row.original.credited_amount),
        enableSorting: false,
      },
    ],
    [],
  )
  return (
    <>
      <PageHeader
        title="Returns"
        description="Request a return against delivered goods and follow its review."
        actions={<Button onClick={() => setOpen(true)}>Request return</Button>}
      />
      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onRefresh={() => void list.refetch()}
        emptyTitle="No return requests"
      />
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent size="lg">
          <form
            onSubmit={(event) => {
              event.preventDefault()
              if (!selected) return
              try {
                setFormError(undefined)
                create.mutate(dealerReturnPayload(selected, new FormData(event.currentTarget)))
              } catch (failure) {
                setFormError((failure as Error).message)
              }
            }}
          >
            <DialogHeader>
              <DialogTitle>Request a return</DialogTitle>
              <DialogDescription>
                Choose the dispatch and enter only the quantities you wish to return.
              </DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-4">
              {!selected ? (
                <DealerResult loading={eligible.isLoading} error={eligible.error}>
                  <div className="space-y-2">
                    {eligible.data.map((dispatch) => (
                      <Button
                        key={dispatch.id}
                        type="button"
                        variant="outline"
                        className="min-h-11 w-full justify-start"
                        onClick={() => setSelected(dispatch)}
                      >
                        {dispatch.code} · {formatDate(dispatch.dispatched_at)}
                      </Button>
                    ))}
                    {!eligible.data.length && (
                      <p className="text-sm text-muted-foreground">No eligible dispatches.</p>
                    )}
                    <div className="flex justify-between">
                      <Button
                        type="button"
                        variant="outline"
                        disabled={eligible.page <= 1}
                        onClick={() => eligible.setPage(eligible.page - 1)}
                      >
                        Previous
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        disabled={!eligible.pagination?.has_next}
                        onClick={() => eligible.setPage(eligible.page + 1)}
                      >
                        Next
                      </Button>
                    </div>
                  </div>
                </DealerResult>
              ) : (
                <>
                  <Button type="button" variant="outline" onClick={() => setSelected(undefined)}>
                    Change dispatch
                  </Button>
                  <h3 className="font-medium">{selected.code}</h3>
                  {selected.lines.map((line) => (
                    <Card key={line.dispatch_line_id}>
                      <CardContent className="space-y-3 p-4">
                        <p className="font-medium">{line.item_name}</p>
                        <p className="text-sm text-muted-foreground">
                          {line.item_code} · Eligible {formatQuantity(line.returnable_quantity)}
                        </p>
                        <div>
                          <Label htmlFor={`return-quantity-${line.dispatch_line_id}`}>
                            Return quantity
                          </Label>
                          <Input
                            id={`return-quantity-${line.dispatch_line_id}`}
                            name={`quantity-${line.dispatch_line_id}`}
                            inputMode="decimal"
                            className="mt-1 min-h-11"
                          />
                        </div>
                        <div>
                          <Label htmlFor={`return-reason-${line.dispatch_line_id}`}>Reason</Label>
                          <Input
                            id={`return-reason-${line.dispatch_line_id}`}
                            name={`reason-${line.dispatch_line_id}`}
                            maxLength={32}
                            className="mt-1 min-h-11"
                          />
                        </div>
                        <div>
                          <Label htmlFor={`return-note-${line.dispatch_line_id}`}>Line note</Label>
                          <Textarea
                            id={`return-note-${line.dispatch_line_id}`}
                            name={`note-${line.dispatch_line_id}`}
                            maxLength={2000}
                            className="mt-1"
                          />
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                  <div>
                    <Label htmlFor="return-notes">Notes</Label>
                    <Textarea id="return-notes" name="notes" maxLength={4000} className="mt-1" />
                  </div>
                </>
              )}
              <FieldError
                message={formError || (create.error ? dealerError(create.error) : undefined)}
              />
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!selected} loading={create.isPending}>
                Submit request
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={Boolean(detailId)}
        onOpenChange={(value) => {
          if (!value) setDetailId(undefined)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{detail.data?.code || 'Return request'}</DialogTitle>
            <DialogDescription>Return review and credited quantities.</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <DealerResult loading={detail.isLoading} error={detail.error}>
              {detail.data && (
                <div className="space-y-3">
                  <StatusBadge status={detail.data.status} />
                  <p className="text-sm">Credited {formatMoney(detail.data.credited_amount)}</p>
                  {detail.data.lines.map((line) => (
                    <Card key={line.id}>
                      <CardContent className="space-y-2 p-4">
                        <p>
                          Requested {formatQuantity(line.quantity)} · Credited{' '}
                          {formatQuantity(line.credited_quantity)}
                        </p>
                        <p className="text-sm">
                          {line.reason_code}: {line.note}
                        </p>
                        <p className="text-sm text-muted-foreground">
                          {line.requires_qc
                            ? 'Subject to quality inspection'
                            : 'No quality inspection required'}
                        </p>
                      </CardContent>
                    </Card>
                  ))}
                </div>
              )}
            </DealerResult>
          </DialogBody>
        </DialogContent>
      </Dialog>
    </>
  )
}

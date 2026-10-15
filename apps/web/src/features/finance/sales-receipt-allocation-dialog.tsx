import Decimal from 'decimal.js'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { DataTable } from '@/components/data-table'
import { DescriptionList, FieldError } from '@/components/shared'
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
import { Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { formatDate, formatMoney, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { toast } from '@/lib/toast'
import { paymentKeys, type PaymentDetail } from './payment-model'
import { salesInvoiceKeys, type SalesInvoiceSummary } from './sales-invoice-types'

export function SalesReceiptAllocationDialog({
  payment,
  onClose,
}: {
  payment: PaymentDetail
  onClose: () => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, { code: string; amount: string }>>(() =>
    Object.fromEntries(
      (payment.allocations ?? [])
        .filter((row) => row.sales_invoice_id)
        .map((row) => [
          row.sales_invoice_id!,
          { code: row.invoice_code ?? row.sales_invoice_id!, amount: row.amount },
        ]),
    ),
  )
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE)
  const [search, setSearch] = useState('')
  const params = {
    customer_id: payment.party_id,
    open_only: true,
    page,
    limit,
    search,
    with_total: true,
    sort_by: 'due_date',
    sort_dir: 'asc',
  }
  const candidates = useQuery({
    queryKey: salesInvoiceKeys.list(params),
    queryFn: () => api.getPage<SalesInvoiceSummary>('/finance/sales-invoices', { params }),
    enabled: can(P.SALES_INVOICE_VIEW),
    staleTime: 0,
  })
  const total = Object.values(draft).reduce(
    (sum, row) => sum.plus(toDecimal(row.amount)),
    toDecimal('0'),
  )
  const valid =
    total.lessThanOrEqualTo(toDecimal(payment.amount)) &&
    Object.values(draft).every(
      (row) => /^\d+(\.\d{1,2})?$/.test(row.amount) && toDecimal(row.amount).greaterThan(0),
    )
  const save = useMutation({
    mutationFn: () =>
      api.post<PaymentDetail>(`/finance/payments/${payment.id}/allocate`, {
        replace: true,
        allocations: Object.entries(draft).map(([id, row]) => ({
          sales_invoice_id: id,
          amount: row.amount,
          notes: null,
        })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: paymentKeys.all })
      void queryClient.invalidateQueries({ queryKey: ['finance'] })
      toast.success('Receipt allocation saved')
      onClose()
    },
  })
  const columns = useMemo<ColumnDef<SalesInvoiceSummary, unknown>[]>(
    () => [
      { id: 'code', header: 'Invoice', enableSorting: false, cell: ({ row }) => row.original.code },
      {
        id: 'due',
        header: 'Due',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.due_date),
      },
      {
        id: 'balance',
        header: 'Balance',
        enableSorting: false,
        cell: ({ row }) =>
          formatMoney(row.original.balance_amount, { currency: row.original.currency }),
      },
      {
        id: 'add',
        header: '',
        enableSorting: false,
        cell: ({ row }) => (
          <Button
            variant="outline"
            size="sm"
            disabled={Boolean(draft[row.original.id]) || row.original.currency !== payment.currency}
            onClick={() =>
              setDraft((current) => ({
                ...current,
                [row.original.id]: {
                  code: row.original.code,
                  amount: Decimal.max(
                    0,
                    Decimal.min(
                      toDecimal(row.original.balance_amount),
                      toDecimal(payment.amount).minus(total),
                    ),
                  ).toFixed(2),
                },
              }))
            }
          >
            Add invoice
          </Button>
        ),
      },
    ],
    [draft, payment.amount, payment.currency, total],
  )
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>Allocate receipt {payment.code}</DialogTitle>
          <DialogDescription>
            Apply the receipt to this customer's issued invoices. Removing all lines releases the
            whole allocation.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <DescriptionList
            items={[
              {
                label: 'Receipt',
                value: formatMoney(payment.amount, { currency: payment.currency }),
              },
              {
                label: 'Allocated',
                value: formatMoney(total.toFixed(2), { currency: payment.currency }),
              },
              {
                label: 'Unallocated',
                value: formatMoney(toDecimal(payment.amount).minus(total).toFixed(2), {
                  currency: payment.currency,
                }),
              },
            ]}
          />
          {Object.entries(draft).map(([id, row]) => (
            <div key={id} className="flex flex-wrap items-end gap-2">
              <div className="min-w-40 flex-1">
                <Label htmlFor={`receipt-${id}`}>{row.code}</Label>
                <Input
                  id={`receipt-${id}`}
                  inputMode="decimal"
                  value={row.amount}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      [id]: { ...row, amount: event.target.value },
                    }))
                  }
                />
              </div>
              <Button
                variant="outline"
                onClick={() =>
                  setDraft((current) => {
                    const next = { ...current }
                    delete next[id]
                    return next
                  })
                }
              >
                Remove
              </Button>
            </div>
          ))}
          {can(P.SALES_INVOICE_VIEW) && (
            <DataTable
              columns={columns}
              data={candidates.data?.data ?? []}
              pagination={candidates.data?.pagination}
              isLoading={candidates.isLoading}
              error={candidates.error}
              onPageChange={setPage}
              onLimitChange={(value) => {
                setLimit(value)
                setPage(1)
              }}
              searchValue={search}
              onSearchChange={(value) => {
                setSearch(value)
                setPage(1)
              }}
              getRowId={(row) => row.id}
              emptyTitle="No open customer invoices"
            />
          )}
          {!valid && (
            <FieldError message="Use positive amounts with up to two decimal places, within the receipt total." />
          )}
          {save.error && <FieldError message={save.error.message} />}
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          {can(P.PAYMENT_RECORD) && (
            <Button disabled={!valid} loading={save.isPending} onClick={() => save.mutate()}>
              Save allocation
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

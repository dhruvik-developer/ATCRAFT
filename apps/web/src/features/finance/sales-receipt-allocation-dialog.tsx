import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo, useState } from 'react'
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
import { useKeySet } from '@/hooks/use-key-set'
import { api } from '@/lib/api-client'
import { DEFAULT_PAGE_SIZE } from '@/lib/constants'
import { ROUNDED_MONEY_INPUT } from '@/lib/decimal-input'
import { formatDate, formatMoney, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success, validation } from '@/lib/messages'
import { toast } from '@/lib/toast'
import { financeKeys } from './finance-types'
import {
  allocatedByInvoice,
  allocationCeiling,
  MAX_ALLOCATIONS,
  paymentKeys,
  proposeAllocation,
  sumAmounts,
  type PaymentDetail,
} from './payment-model'
import { salesInvoiceKeys, type SalesInvoiceSummary } from './sales-invoice-types'

interface ReceiptAllocationDraft {
  code: string
  amount: string
  notes: string | null
  /** Existing settlements have no balance in PaymentAllocationRead. */
  ceiling: string | null
}

export function SalesReceiptAllocationDialog({
  payment,
  onClose,
}: {
  payment: PaymentDetail
  onClose: () => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, ReceiptAllocationDraft>>(() =>
    Object.fromEntries(
      (payment.allocations ?? [])
        .filter((row) => row.sales_invoice_id)
        .map((row) => [
          row.sales_invoice_id!,
          {
            code: row.invoice_code ?? row.sales_invoice_id!,
            amount: row.amount,
            notes: row.notes ?? null,
            ceiling: null,
          },
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
    queryFn: ({ signal }) =>
      api.getPage<SalesInvoiceSummary>('/finance/sales-invoices', { params, signal }),
    enabled: can(P.SALES_INVOICE_VIEW),
    staleTime: 0,
  })
  const total = sumAmounts(Object.values(draft).map((row) => row.amount))
  // A removed invoice added back is entitled to its own amount again: see
  // `allocationCeiling`.
  const previousAmounts = useMemo(
    () => allocatedByInvoice(payment.allocations),
    [payment.allocations],
  )
  const valid =
    Object.keys(draft).length <= MAX_ALLOCATIONS &&
    total.lessThanOrEqualTo(toDecimal(payment.amount)) &&
    Object.values(draft).every(
      (row) =>
        ROUNDED_MONEY_INPUT.test(row.amount) &&
        toDecimal(row.amount).greaterThan(0) &&
        (row.ceiling === null || toDecimal(row.amount).lessThanOrEqualTo(row.ceiling)),
    )
  const save = useMutation({
    mutationFn: () =>
      api.post<PaymentDetail>(`/finance/payments/${payment.id}/allocate`, {
        replace: true,
        allocations: Object.entries(draft).map(([id, row]) => ({
          sales_invoice_id: id,
          amount: row.amount,
          notes: row.notes,
        })),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: paymentKeys.all })
      void queryClient.invalidateQueries({ queryKey: financeKeys.all })
      toast.success(success.saved('Receipt allocation'))
      onClose()
    },
    // Said in the dialog, under the allocations; a toast would say it twice.
    meta: { suppressErrorToast: true },
  })
  /**
   * Put an invoice into the draft, filled to whatever the receipt has left.
   *
   * Through the functional updater, so the callback reads the draft it updates
   * and never depends on it: typing an amount must not rebuild the candidate
   * table's columns.
   */
  const addInvoice = useCallback(
    (invoice: SalesInvoiceSummary) =>
      setDraft((current) => {
        if (current[invoice.id] || Object.keys(current).length >= MAX_ALLOCATIONS) return current
        const ceiling = allocationCeiling(invoice.balance_amount, previousAmounts.get(invoice.id))
        const used = sumAmounts(Object.values(current).map((row) => row.amount))
        return {
          ...current,
          [invoice.id]: {
            code: invoice.code,
            notes: null,
            ceiling: ceiling.toFixed(2),
            amount: proposeAllocation(ceiling, payment.amount, used),
          },
        }
      }),
    [payment.amount, previousAmounts],
  )
  // Ids and one flag, not the draft: they change when a line joins, leaves or
  // fills the receipt, not on every keystroke.
  const draftIds = useKeySet(draft)
  const receiptFull = total.greaterThanOrEqualTo(toDecimal(payment.amount))
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
            disabled={
              draftIds.has(row.original.id) ||
              row.original.currency !== payment.currency ||
              draftIds.size >= MAX_ALLOCATIONS ||
              receiptFull
            }
            onClick={() => addInvoice(row.original)}
          >
            Add invoice
          </Button>
        ),
      },
    ],
    [addInvoice, draftIds, payment.currency, receiptFull],
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
            <FieldError
              message={`${validation.decimals('amounts above zero', 2)}, within the receipt total and invoice balance. A receipt may settle at most ${MAX_ALLOCATIONS} invoices.`}
            />
          )}
          {save.error && (
            <FieldError message={fieldErrorSummary(save.error) || describeError(save.error)} />
          )}
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

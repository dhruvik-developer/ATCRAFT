import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Decimal from 'decimal.js'
import { Loader2, Plus, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import type { VendorInvoiceSummary } from '@/features/finance/finance-types'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { formatMoney, toDecimal } from '@/lib/format'
import { toast } from '@/lib/toast'
import { staleTimes } from '@/lib/query-client'

import {
  purchaseReturnKeys,
  type PurchaseReturnDebitAllocationResult,
  type PurchaseReturnDetail,
} from './purchase-return-model'

type DraftLine = {
  vendor_invoice_id: string
  amount: string
  notes: string | null
  invoice_code: string
  vendor_invoice_number: string
}

const MONEY = /^\d{1,14}(\.\d{1,2})?$/

export function PurchaseReturnDebitAllocation({
  purchaseReturn,
}: {
  purchaseReturn: PurchaseReturnDetail
}) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<DraftLine[]>([])
  const [selectedInvoiceId, setSelectedInvoiceId] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const debouncedSearch = useDebouncedValue(search, 300)
  const resultKey = purchaseReturnKeys.debitAllocations(purchaseReturn.id)

  const allocations = useQuery({
    queryKey: resultKey,
    queryFn: () =>
      api.get<PurchaseReturnDebitAllocationResult>(
        `/finance/purchase-returns/${purchaseReturn.id}/debit-allocations`,
      ),
    staleTime: staleTimes.transactional,
  })

  const invoiceParams = useMemo(
    () => ({
      page,
      limit: PICKER_PAGE_SIZE,
      with_total: true,
      vendor_id: purchaseReturn.vendor_id,
      open_only: true,
      sort_by: 'due_date',
      sort_dir: 'asc' as const,
      ...(debouncedSearch ? { search: debouncedSearch } : {}),
    }),
    [page, purchaseReturn.vendor_id, debouncedSearch],
  )
  const invoices = useQuery({
    queryKey: ['finance', 'vendor-invoices', invoiceParams],
    queryFn: () =>
      api.getPage<VendorInvoiceSummary>('/finance/vendor-invoices', {
        params: invoiceParams,
      }),
    staleTime: staleTimes.transactional,
  })

  useEffect(() => {
    if (allocations.data) {
      setDraft(
        (allocations.data.allocations ?? []).map((row) => ({
          vendor_invoice_id: row.vendor_invoice_id,
          amount: String(row.amount),
          notes: row.notes,
          invoice_code: row.invoice_code,
          vendor_invoice_number: row.vendor_invoice_number,
        })),
      )
    }
  }, [allocations.data])

  const draftTotal = draft.reduce(
    (sum, row) => sum.plus(toDecimal(row.amount || '0')),
    toDecimal('0'),
  )
  const creditLimit = toDecimal(purchaseReturn.total_amount)
  const exceedsCredit = draftTotal.greaterThan(creditLimit)
  const invalidAmount = draft.some(
    (row) => !MONEY.test(row.amount.trim()) || toDecimal(row.amount).lessThanOrEqualTo(0),
  )

  const addInvoice = () => {
    const invoice = invoices.data?.data.find((row) => row.id === selectedInvoiceId)
    if (!invoice || draft.some((row) => row.vendor_invoice_id === invoice.id)) return
    const remaining = Decimal.max(0, creditLimit.minus(draftTotal))
    setDraft((current) => [
      ...current,
      {
        vendor_invoice_id: invoice.id,
        amount: Decimal.min(toDecimal(invoice.outstanding_amount), remaining).toFixed(2),
        notes: null,
        invoice_code: invoice.code,
        vendor_invoice_number: invoice.vendor_invoice_number,
      },
    ])
    setSelectedInvoiceId('')
  }

  const save = useMutation({
    mutationFn: () =>
      api.post<PurchaseReturnDebitAllocationResult>(
        `/finance/purchase-returns/${purchaseReturn.id}/debit-allocations`,
        {
          allocations: draft.map((row) => ({
            vendor_invoice_id: row.vendor_invoice_id,
            amount: row.amount.trim(),
            notes: row.notes,
          })),
          replace: true,
        },
      ),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ['finance'] })
      void queryClient.invalidateQueries({ queryKey: resultKey })
      toast.success(
        'Vendor debit note allocated',
        `${formatMoney(result.allocated_amount, { currency: purchaseReturn.currency })} applied; ${formatMoney(result.unallocated_amount, { currency: purchaseReturn.currency })} remains unapplied.`,
      )
    },
  })

  return (
    <section aria-label="Vendor debit note allocation" className="space-y-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">Vendor debit note allocation</h3>
          <p className="text-xs text-muted-foreground">
            Apply the posted vendor credit to approved invoices from this vendor.
          </p>
        </div>
        {allocations.data && (
          <p className="text-sm tabular">
            {formatMoney(allocations.data.allocated_amount, { currency: purchaseReturn.currency })}{' '}
            applied of{' '}
            {formatMoney(allocations.data.total_amount, { currency: purchaseReturn.currency })}
          </p>
        )}
      </div>

      {allocations.isLoading && <Skeleton className="h-8 w-full" />}
      {allocations.error && (
        <Alert variant="destructive">
          <AlertTitle>Could not load vendor debit allocations</AlertTitle>
          <AlertDescription>{allocations.error.message}</AlertDescription>
        </Alert>
      )}
      {save.error && (
        <Alert variant="destructive">
          <AlertTitle>Could not save this allocation</AlertTitle>
          <AlertDescription>{save.error.message}</AlertDescription>
        </Alert>
      )}

      {draft.map((row, index) => (
        <div
          key={row.vendor_invoice_id}
          className="flex flex-wrap items-center gap-2 rounded-md bg-muted/40 p-2"
        >
          <div className="min-w-40 flex-1">
            <span className="block text-sm font-medium">{row.vendor_invoice_number}</span>
            <span className="block font-mono text-2xs text-muted-foreground">
              {row.invoice_code}
            </span>
          </div>
          <Label className="sr-only" htmlFor={`debit-allocation-${row.vendor_invoice_id}`}>
            Amount for {row.invoice_code}
          </Label>
          <Input
            id={`debit-allocation-${row.vendor_invoice_id}`}
            inputMode="decimal"
            value={row.amount}
            onChange={(event) =>
              setDraft((current) =>
                current.map((line, lineIndex) =>
                  lineIndex === index ? { ...line, amount: event.target.value } : line,
                ),
              )
            }
            className="w-36 text-right tabular"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${row.invoice_code} allocation`}
            onClick={() =>
              setDraft((current) =>
                current.filter((line) => line.vendor_invoice_id !== row.vendor_invoice_id),
              )
            }
          >
            <X />
          </Button>
        </div>
      ))}

      <div className="grid gap-2 sm:grid-cols-[minmax(10rem,1fr)_minmax(14rem,2fr)_auto]">
        <Input
          aria-label="Search open vendor invoices"
          placeholder="Search invoice number"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            setPage(1)
          }}
        />
        <Select value={selectedInvoiceId} onValueChange={setSelectedInvoiceId}>
          <SelectTrigger aria-label="Choose vendor invoice">
            <SelectValue placeholder={invoices.isLoading ? 'Loading invoices' : 'Choose invoice'} />
          </SelectTrigger>
          <SelectContent>
            {(invoices.data?.data ?? [])
              .filter((invoice) => !draft.some((row) => row.vendor_invoice_id === invoice.id))
              .map((invoice) => (
                <SelectItem key={invoice.id} value={invoice.id}>
                  {invoice.vendor_invoice_number} - {invoice.code} ·{' '}
                  {formatMoney(invoice.outstanding_amount, { currency: invoice.currency })}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>
        <Button type="button" variant="outline" onClick={addInvoice} disabled={!selectedInvoiceId}>
          <Plus />
          Add invoice
        </Button>
      </div>

      {invoices.error && (
        <p role="alert" className="text-sm text-destructive">
          Could not load open vendor invoices: {invoices.error.message}
        </p>
      )}
      {(invoices.data?.pagination.has_previous || invoices.data?.pagination.has_next) && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>Search results are paginated.</span>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!invoices.data?.pagination.has_previous}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              Previous
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!invoices.data?.pagination.has_next}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
        <p className="text-sm tabular">
          Draft total: {formatMoney(draftTotal.toFixed(2), { currency: purchaseReturn.currency })} /{' '}
          {formatMoney(purchaseReturn.total_amount, { currency: purchaseReturn.currency })}
        </p>
        <Button
          type="button"
          onClick={() => save.mutate()}
          disabled={
            allocations.isLoading ||
            save.isPending ||
            invalidAmount ||
            exceedsCredit ||
            !allocations.data
          }
        >
          {save.isPending && <Loader2 className="animate-spin" />}
          Save allocation
        </Button>
      </div>
      {exceedsCredit && (
        <p role="alert" className="text-sm text-destructive">
          Allocations cannot exceed the posted return value.
        </p>
      )}
      {invalidAmount && (
        <p role="alert" className="text-sm text-destructive">
          Enter positive invoice amounts with up to two decimal places.
        </p>
      )}
      {!invoices.isLoading && invoices.data?.data.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No open invoices match. Search again or leave the vendor debit unallocated.
        </p>
      )}
    </section>
  )
}

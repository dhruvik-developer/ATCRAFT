import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { toDecimal } from '@/lib/format'
import { ok, page, renderScreen } from '@/test/harness'

import type {
  PurchaseReturnDebitAllocationResult,
  PurchaseReturnDetail,
} from './purchase-return-model'
import { PurchaseReturnDebitAllocation } from './purchase-return-debit-allocation'

const RETURN_ID = 'return-1'
const INVOICE_ID = 'invoice-1'
const ALLOCATIONS_PATH = `/finance/purchase-returns/${RETURN_ID}/debit-allocations`

const invoice = {
  id: INVOICE_ID,
  code: 'VINV-001',
  vendor_id: 'vendor-1',
  vendor_invoice_number: 'SUP-2026-001',
  invoice_date: '2026-05-01',
  due_date: '2026-06-01',
  status: 'APPROVED',
  match_status: 'MATCHED',
  currency: 'INR',
  subtotal: '100.00',
  tax_amount: '18.00',
  total_amount: '118.00',
  paid_amount: '0.00',
  debit_allocated_amount: '0.00',
  outstanding_amount: '118.00',
  purchase_order_id: null,
  goods_receipt_id: null,
  invoice_type: 'PURCHASE',
  job_work_order_id: null,
  cost_centre_id: null,
  created_at: '2026-05-01T09:00:00Z',
} as const

const existing: PurchaseReturnDebitAllocationResult = {
  purchase_return_id: RETURN_ID,
  total_amount: '100.00',
  allocated_amount: '40.00',
  unallocated_amount: '60.00',
  allocations: [
    {
      id: 'allocation-1',
      vendor_invoice_id: INVOICE_ID,
      invoice_code: invoice.code,
      vendor_invoice_number: invoice.vendor_invoice_number,
      amount: '40.00',
      notes: null,
    },
  ],
}

const purchaseReturn = {
  id: RETURN_ID,
  vendor_id: invoice.vendor_id,
  total_amount: '100.00',
  currency: 'INR',
} as PurchaseReturnDetail

describe('purchase-return debit allocation', () => {
  it('loads current allocations and submits the edited replacement set', async () => {
    let current = existing
    const { user, fetch } = renderScreen(
      <PurchaseReturnDebitAllocation purchaseReturn={purchaseReturn} />,
      {
        routes: {
          [`GET ${ALLOCATIONS_PATH}`]: () => ok(current),
          'GET /finance/vendor-invoices': page([invoice]),
          [`POST ${ALLOCATIONS_PATH}`]: ({ body }) => {
            const amount = (body as { allocations: { amount: string }[] }).allocations[0]!.amount
            current = {
              ...existing,
              allocated_amount: amount,
              unallocated_amount: toDecimal('100').minus(toDecimal(amount)).toFixed(2),
              allocations: [
                {
                  ...existing.allocations![0]!,
                  amount,
                },
              ],
            }
            return ok(current)
          },
        },
      },
    )

    const amount = await screen.findByRole('textbox', { name: `Amount for ${invoice.code}` })
    expect(amount).toHaveValue('40.00')
    await user.clear(amount)
    await user.type(amount, '30.00')
    await user.click(screen.getByRole('button', { name: 'Save allocation' }))

    await waitFor(() => fetch.requestFor('POST', ALLOCATIONS_PATH))
    expect(fetch.requestFor('POST', ALLOCATIONS_PATH)).toEqual({
      allocations: [{ vendor_invoice_id: INVOICE_ID, amount: '30.00', notes: null }],
      replace: true,
    })
    expect(await screen.findByText(/30\.00 applied of/)).toBeVisible()
  })
})

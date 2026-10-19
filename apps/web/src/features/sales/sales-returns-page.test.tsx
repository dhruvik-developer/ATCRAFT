import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import SalesReturnsPage, { type SalesReturnRow } from './sales-returns-page'

function salesReturnRow(overrides: Partial<SalesReturnRow> = {}): SalesReturnRow {
  return {
    id: 'sr-1',
    code: 'SR/2026-001',
    dispatch_id: 'dispatch-1',
    customer_id: 'customer-1',
    status: 'DRAFT',
    source: 'ERP',
    created_at: '2026-10-19T09:00:00Z',
    received_at: null,
    ...overrides,
  }
}

describe('SalesReturnsPage', () => {
  it('shows only valid actions for a draft return', async () => {
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: { 'GET /sales-returns': page([salesReturnRow()]) },
    })

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))

    expect(screen.getByRole('button', { name: 'Receive' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
  })

  it('shows close and cancel for a received return', async () => {
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: { 'GET /sales-returns': page([salesReturnRow({ status: 'RECEIVED' })]) },
    })

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))

    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Receive' })).not.toBeInTheDocument()
  })

  it('requests a cancellation reason before cancelling a draft return', async () => {
    const { user, fetch } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        'GET /sales-returns': page([salesReturnRow()]),
        'POST /sales-returns/sr-1/cancel': ok(salesReturnRow({ status: 'CANCELLED' })),
      },
    })

    vi.spyOn(window, 'prompt').mockReturnValue('Customer requested cancellation')

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() => fetch.requestFor('POST', '/sales-returns/sr-1/cancel'))
    expect(fetch.requestFor('POST', '/sales-returns/sr-1/cancel')).toEqual({
      reason: 'Customer requested cancellation',
    })
  })
})

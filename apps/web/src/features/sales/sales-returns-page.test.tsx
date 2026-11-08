import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { messages } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import SalesReturnsPage, { type SalesReturnRow } from './sales-returns-page'

function salesReturnRow(overrides: Partial<SalesReturnRow> = {}): SalesReturnRow {
  return {
    id: 'sr-1',
    code: 'SR/2026-001',
    dispatch_id: 'dispatch-1',
    customer_id: 'customer-1',
    status: 'DRAFT',
    source: 'ERP',
    notes: null,
    return_warehouse_id: null,
    received_at: null,
    cancelled_at: null,
    cancellation_reason: null,
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

  it('collects the cancellation reason in a dialog that opens empty', async () => {
    const { user, fetch } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        'GET /sales-returns': page([salesReturnRow()]),
        'POST /sales-returns/sr-1/cancel': ok(salesReturnRow({ status: 'CANCELLED' })),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))
    const trigger = screen.getByRole('button', { name: 'Cancel' })
    expect(trigger).toHaveClass('text-destructive')
    await user.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: 'Cancel SR/2026-001?' })
    const box = within(dialog).getByRole('textbox', { name: 'Reason' })
    const confirm = within(dialog).getByRole('button', { name: 'Cancel return' })
    // Nothing is suggested: the native prompt arrived pre-filled with
    // "Customer requested cancellation", and one OK audited it as the cause.
    expect(box).toHaveValue('')
    expect(confirm).toHaveClass('bg-destructive')

    await user.click(confirm)
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    await user.type(box, 'ab')
    await user.click(confirm)
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)

    await user.clear(box)
    await user.type(box, '  Raised against the wrong dispatch  ')
    await user.click(confirm)
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/sales-returns/sr-1/cancel')).toEqual({
        reason: 'Raised against the wrong dispatch',
      }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps a refused cancellation open with the reason the API gave', async () => {
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        'GET /sales-returns': page([salesReturnRow()]),
        'POST /sales-returns/sr-1/cancel': fail(
          422,
          'VALIDATION_ERROR',
          'Request validation failed',
          {
            fields: { reason: ['The reason must name what changed'] },
          },
        ),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    const dialog = await screen.findByRole('dialog', { name: 'Cancel SR/2026-001?' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 'Changed mind')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel return' }))

    expect(await within(dialog).findByText('The reason must name what changed')).toBeVisible()
    expect(screen.getByRole('dialog', { name: 'Cancel SR/2026-001?' })).toBeVisible()
    expect(within(dialog).getByRole('textbox', { name: 'Reason' })).toHaveValue('Changed mind')
  })

  it('sends nothing when the cancellation is dismissed', async () => {
    const { user, fetch } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: { 'GET /sales-returns': page([salesReturnRow()]) },
    })

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    const dialog = await screen.findByRole('dialog', { name: 'Cancel SR/2026-001?' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 'Not after all')
    await user.keyboard('{Escape}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeVisible()
  })

  it('saves per-serial QC dispositions for a received return', async () => {
    const row = salesReturnRow({ status: 'RECEIVED' })
    const { user, fetch } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        'GET /sales-returns': page([row]),
        'GET /sales-returns/sr-1': ok({
          ...row,
          lines: [
            {
              id: 'line-1',
              serial_ids: ['serial-1'],
              accepted_serial_ids: [],
              damaged_serial_ids: [],
              scrapped_serial_ids: [],
            },
          ],
        }),
        'POST /sales-returns/sr-1/units': ok({
          ...row,
          lines: [
            {
              id: 'line-1',
              serial_ids: ['serial-1'],
              accepted_serial_ids: ['serial-1'],
              damaged_serial_ids: [],
              scrapped_serial_ids: [],
            },
          ],
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'SR/2026-001' }))
    await user.click(await screen.findByRole('button', { name: 'Dispose serial units' }))
    await user.click(screen.getByRole('combobox', { name: 'Disposition for serial-1' }))
    await user.click(await screen.findByRole('option', { name: 'Accepted' }))
    await user.click(screen.getByRole('button', { name: 'Save dispositions' }))

    await waitFor(() => fetch.requestFor('POST', '/sales-returns/sr-1/units'))
    expect(fetch.requestFor('POST', '/sales-returns/sr-1/units')).toEqual({
      accepted: ['serial-1'],
      damaged: [],
      scrapped: [],
    })
  })
})

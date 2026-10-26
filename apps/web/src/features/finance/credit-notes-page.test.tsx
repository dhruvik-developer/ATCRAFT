import { act, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { fail, ok, page, renderScreen, type RouteBody, type RouteTable } from '@/test/harness'

import CreditNotesPage from './credit-notes-page'

function creditNote(overrides: Record<string, unknown> = {}) {
  return {
    id: 'credit-1',
    code: 'CN-001',
    sales_invoice_id: 'invoice-1',
    customer_id: 'customer',
    note_date: '2026-10-25',
    reason_type: 'SALES_RETURN',
    status: 'ISSUED',
    currency: 'INR',
    total_amount: '1.16',
    buyer_name: 'Customer',
    taxable_amount: '0.99',
    tax_amount: '0.18',
    round_off_amount: '0.00',
    lines: [],
    ...overrides,
  }
}

async function openCreditNote(
  options: {
    permissions?: string[]
    detail?: ReturnType<typeof creditNote>
    routes?: RouteTable
  } = {},
) {
  const result = renderScreen(<CreditNotesPage />, {
    permissions: options.permissions ?? [P.CREDIT_NOTE_VIEW, P.CREDIT_NOTE_CANCEL],
    routes: {
      'GET /finance/credit-notes': page([creditNote()]),
      'GET /finance/credit-notes/credit-1': ok(options.detail ?? creditNote()),
      ...options.routes,
    },
  })
  await result.user.click(await screen.findByRole('button', { name: 'CN-001' }))
  await screen.findByText('Taxable', { selector: 'span' })
  return result
}

describe('credit note settlement breakdown', () => {
  it.each(['0.00', '-0.01'])(
    'shows a nonzero rounding residual without changing the recorded tax (%s)',
    async (roundOff) => {
      await openCreditNote({
        permissions: [P.CREDIT_NOTE_VIEW],
        detail: creditNote({ round_off_amount: roundOff }),
      })
      expect(screen.getByText('Tax', { selector: 'span' }).nextElementSibling).toHaveTextContent(
        '0.18',
      )
      if (roundOff === '0.00') expect(screen.queryByText('Round off')).toBeNull()
      else expect(screen.getByText('Round off').nextElementSibling).toHaveTextContent(/-.*0\.01/)
    },
  )
})

describe('credit note cancellation', () => {
  it('requires the actual audit reason, prevents duplicate requests, and refreshes affected balances', async () => {
    let complete!: (body: RouteBody) => void
    const pending = new Promise<RouteBody>((resolve) => {
      complete = resolve
    })
    let cancelled = false
    const successToast = vi.spyOn(toast, 'success')
    const { user, fetch, queryClient } = await openCreditNote({
      routes: {
        'GET /finance/credit-notes': () =>
          page([creditNote({ status: cancelled ? 'CANCELLED' : 'ISSUED' })]),
        'POST /finance/credit-notes/credit-1/cancel': () => pending,
      },
    })
    // These screens may be unmounted when the note is reversed; their cached
    // balances still need to be stale when the user returns to them.
    const cachedKeys = [
      ['finance', 'invoices', 'invoice-1'],
      queryKeys.customers.detail('customer'),
      queryKeys.dealerPortal.outstanding,
      queryKeys.salesReturns.detail('return-1'),
    ]
    for (const queryKey of cachedKeys) {
      queryClient.setQueryDefaults(queryKey, { gcTime: Infinity })
      queryClient.setQueryData(queryKey, { balance: '1.16' })
    }

    await user.click(screen.getByRole('button', { name: 'Cancel credit note' }))
    const dialog = screen.getByRole('dialog', { name: 'Cancel credit note CN-001?' })
    const reason = within(dialog).getByRole('textbox', { name: /Cancellation reason/ })
    const confirm = within(dialog).getByRole('button', { name: 'Confirm cancellation' })
    await user.click(confirm)
    expect(within(dialog).getByText('Enter between 3 and 500 characters.')).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    await user.click(reason)
    await user.paste('  Duplicate credit issued in error  ')
    await user.click(confirm)
    expect(fetch.requestFor('POST', '/finance/credit-notes/credit-1/cancel')).toEqual({
      reason: 'Duplicate credit issued in error',
    })
    expect(confirm).toBeDisabled()
    expect(reason).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.click(confirm)
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(1)

    cancelled = true
    await act(async () => {
      complete(ok(creditNote({ status: 'CANCELLED' })))
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
    expect(await screen.findByText('Cancelled')).toBeVisible()
    for (const queryKey of cachedKeys) {
      expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(true)
    }
    expect(successToast).toHaveBeenCalledWith('Credit note cancelled')
  })

  it.each([
    {
      response: fail(422, 'VALIDATION_ERROR', 'Invalid cancellation', {
        fields: { reason: ['Explain why this credit must be reversed.'] },
      }),
      message: 'Explain why this credit must be reversed.',
      invalidReason: true,
    },
    {
      response: fail(409, 'CONFLICT', 'This credit note has already been cancelled.'),
      message: 'This credit note has already been cancelled.',
      invalidReason: false,
    },
  ])(
    'keeps the reason and shows a refused cancellation inline ($invalidReason)',
    async ({ response, message, invalidReason }) => {
      const { user, fetch } = await openCreditNote({
        routes: { 'POST /finance/credit-notes/credit-1/cancel': response },
      })
      await user.click(screen.getByRole('button', { name: 'Cancel credit note' }))
      const dialog = screen.getByRole('dialog')
      const reason = within(dialog).getByRole('textbox', { name: /Cancellation reason/ })
      await user.click(reason)
      await user.paste('Duplicate credit issued in error')
      await user.click(within(dialog).getByRole('button', { name: 'Confirm cancellation' }))
      expect(await within(dialog).findByText(message)).toBeVisible()
      expect(reason).toHaveValue('Duplicate credit issued in error')
      expect(reason).toHaveAttribute('aria-invalid', String(invalidReason))
      expect(within(dialog).getByRole('button', { name: 'Confirm cancellation' })).toBeEnabled()
      expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
      await user.click(reason)
      await user.paste(' with supporting evidence')
      expect(within(dialog).queryByText(message)).toBeNull()
      expect(reason).toHaveAttribute('aria-invalid', 'false')
    },
  )

  it.each([
    { permissions: [P.CREDIT_NOTE_VIEW], status: 'ISSUED', label: 'a reader' },
    {
      permissions: [P.CREDIT_NOTE_VIEW, P.CREDIT_NOTE_CANCEL],
      status: 'CANCELLED',
      label: 'an already cancelled detail despite a stale issued list row',
    },
  ])('hides cancellation for $label', async ({ permissions, status }) => {
    const { fetch } = await openCreditNote({ permissions, detail: creditNote({ status }) })
    expect(screen.queryByRole('button', { name: 'Cancel credit note' })).toBeNull()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
})

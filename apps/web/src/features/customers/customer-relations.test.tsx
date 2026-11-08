/**
 * Removing a customer address: the reason the API records, collected fresh.
 *
 * The removal confirms through the shared `ReasonDialog`. Two things are worth
 * pinning at this call site rather than only in the component's own tests: the
 * reason travels in the query string (a DELETE body is dropped by proxies), and
 * a successful removal closes the dialog from outside - which the panel's old
 * private copy did not notice, so the next removal opened with the last
 * reason already typed in, ready to be audited against the wrong address.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { messages } from '@/lib/messages'
import { ok, page, renderScreen } from '@/test/harness'

import { AddressesPanel } from './customer-relations'
import type { CustomerAddress } from './customer-types'

const CUSTOMER_ID = 'cust-1'

function address(overrides: Partial<CustomerAddress>): CustomerAddress {
  return {
    id: 'addr-1',
    customer_id: CUSTOMER_ID,
    address_type: 'SHIPPING',
    label: 'Head office',
    address_line1: '12 Ring Road',
    address_line2: null,
    landmark: null,
    city: 'Rajkot',
    state: 'Gujarat',
    state_code: '24',
    postal_code: '360001',
    country: 'India',
    gstin: null,
    contact_person: null,
    phone: null,
    email: null,
    is_default: false,
    is_active: true,
    notes: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

describe('removing a customer address', () => {
  it('refuses a short reason, sends a real one as a query parameter, and forgets it', async () => {
    const removed: string[] = []
    const { user, fetch } = renderScreen(<AddressesPanel customerId={CUSTOMER_ID} canEdit />, {
      routes: {
        [`GET /customers/${CUSTOMER_ID}/addresses`]: page([
          address({}),
          address({ id: 'addr-2', label: 'Warehouse', address_line1: '4 GIDC' }),
        ]),
        [`DELETE /customers/${CUSTOMER_ID}/addresses/addr-1`]: ({ url }: { url: URL }) => {
          removed.push(url.searchParams.get('reason') ?? '')
          return ok(null)
        },
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Remove address Head office' }))
    let dialog = await screen.findByRole('dialog', { name: 'Remove this address?' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 'no')
    await user.click(within(dialog).getByRole('button', { name: 'Remove address' }))
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'DELETE')).toBe(false)

    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 't trading here')
    await user.click(within(dialog).getByRole('button', { name: 'Remove address' }))
    await waitFor(() => expect(removed).toEqual(['not trading here']))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // The next removal starts from an empty box, not from the last reason.
    await user.click(screen.getByRole('button', { name: 'Remove address Warehouse' }))
    dialog = await screen.findByRole('dialog', { name: 'Remove this address?' })
    expect(within(dialog).getByRole('textbox', { name: 'Reason' })).toHaveValue('')
    expect(within(dialog).queryByText(messages.errors.reasonTooShort)).not.toBeInTheDocument()
  })
})

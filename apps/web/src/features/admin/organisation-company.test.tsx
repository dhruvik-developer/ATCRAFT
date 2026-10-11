/**
 * The company's bank details and invoice terms.
 *
 * Printed on every invoice the company sends, so where customers pay is
 * decided here - and redirecting it is how invoice-redirection fraud starts.
 * What is pinned:
 *
 * - **The card saves only its own fields.** Its PATCH carries the six bank and
 *   invoice fields and nothing else, so a bank change is never a side effect of
 *   correcting the address in the card above, and the audit row it writes says
 *   exactly that.
 * - **Identifiers go out as the bank writes them.** No spaces and upper case -
 *   which is also what the API stores - so a number keyed in the groups printed
 *   on a cheque leaf is neither refused nor saved differently.
 * - **A bad IFSC never reaches the network,** and a blank field is sent as the
 *   null that clears it.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ok, renderScreen } from '@/test/harness'

import type { CompanyRecord } from './admin-types'
import { CompanyTab } from './organisation-company'

const COMPANY: CompanyRecord = {
  id: 'co-atcraft',
  code: 'ATCRAFT',
  name: 'Atcraft Hardware',
  legal_name: 'Atcraft Hardware Private Limited',
  gstin: null,
  pan: null,
  cin: null,
  email: null,
  phone: null,
  website: null,
  address_line1: null,
  address_line2: null,
  city: 'Rajkot',
  state: 'Gujarat',
  state_code: '24',
  postal_code: null,
  country: 'India',
  currency: 'INR',
  financial_year_start_month: 4,
  timezone: 'Asia/Kolkata',
  is_active: true,
  logo_document_id: null,
  bank_name: null,
  bank_account_name: null,
  bank_account_number: null,
  bank_ifsc: null,
  bank_branch: null,
  invoice_terms: null,
  created_at: '2026-04-01T00:00:00Z',
  updated_at: '2026-04-01T00:00:00Z',
}

const BANKED: CompanyRecord = {
  ...COMPANY,
  bank_name: 'HDFC Bank',
  bank_account_name: 'Atcraft Hardware Private Limited',
  bank_account_number: '50100123456789',
  bank_ifsc: 'HDFC0001234',
  bank_branch: 'Kalawad Road, Rajkot',
  invoice_terms: 'Payment within 30 days of the invoice date.',
}

/** The bank card, found by its own heading rather than by position. */
async function bankCard(): Promise<HTMLElement> {
  const heading = await screen.findByText('Bank & invoice')
  return heading.closest('form') as HTMLElement
}

describe('the bank and invoice card', () => {
  it('sends its six fields and nothing else, identifiers as the bank writes them', async () => {
    const { user, fetch } = renderScreen(<CompanyTab canManage />, {
      routes: {
        'GET /organization/company': ok(COMPANY),
        'PATCH /organization/company': ok(BANKED),
      },
    })

    const card = await bankCard()
    await user.type(within(card).getByRole('textbox', { name: 'Bank' }), 'HDFC Bank')
    await user.type(within(card).getByRole('textbox', { name: 'Branch' }), 'Kalawad Road, Rajkot')
    // Lower case, and spaced the way a cheque leaf prints it.
    await user.type(within(card).getByRole('textbox', { name: 'IFSC' }), 'hdfc 0001 234')
    await user.type(
      within(card).getByRole('textbox', { name: 'Account name' }),
      'Atcraft Hardware Private Limited',
    )
    await user.type(
      within(card).getByRole('textbox', { name: 'Account number' }),
      '5010 0123 4567 89',
    )
    await user.type(
      within(card).getByRole('textbox', { name: 'Invoice terms' }),
      'Payment within 30 days of the invoice date.',
    )
    await user.click(within(card).getByRole('button', { name: /save bank details/i }))

    const body = await waitFor(() => fetch.requestFor('PATCH', '/organization/company'))
    expect(body).toEqual({
      bank_name: 'HDFC Bank',
      bank_account_name: 'Atcraft Hardware Private Limited',
      bank_account_number: '50100123456789',
      bank_ifsc: 'HDFC0001234',
      bank_branch: 'Kalawad Road, Rajkot',
      invoice_terms: 'Payment within 30 days of the invoice date.',
    })
  })

  it('refuses an IFSC of the wrong shape before anything reaches the network', async () => {
    const { user, fetch } = renderScreen(<CompanyTab canManage />, {
      routes: { 'GET /organization/company': ok(COMPANY) },
    })

    const card = await bankCard()
    // The fifth character is the reserved zero; this one has a one there.
    await user.type(within(card).getByRole('textbox', { name: 'IFSC' }), 'HDFC1001234')
    await user.click(within(card).getByRole('button', { name: /save bank details/i }))

    expect(await within(card).findByText(/not a valid IFSC/i)).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })

  it('clears a field with null and leaves the rest as they were', async () => {
    const { user, fetch } = renderScreen(<CompanyTab canManage />, {
      routes: {
        'GET /organization/company': ok(BANKED),
        'PATCH /organization/company': ok({ ...BANKED, bank_branch: null }),
      },
    })

    const card = await bankCard()
    const branch = within(card).getByRole('textbox', { name: 'Branch' })
    expect(branch).toHaveValue('Kalawad Road, Rajkot')
    await user.clear(branch)
    await user.click(within(card).getByRole('button', { name: /save bank details/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/organization/company'),
    )) as Record<string, unknown>
    expect(body.bank_branch).toBeNull()
    expect(body.bank_ifsc).toBe('HDFC0001234')
    expect(body.bank_account_number).toBe('50100123456789')
  })

  it('is a plain list, with no Save, without company.manage', async () => {
    renderScreen(<CompanyTab canManage={false} />, {
      routes: { 'GET /organization/company': ok(BANKED) },
    })

    expect(await screen.findByText('HDFC0001234')).toBeVisible()
    expect(screen.getByText('50100123456789')).toBeVisible()
    expect(screen.getByText('Payment within 30 days of the invoice date.')).toBeVisible()
    expect(screen.queryByRole('button', { name: /save bank details/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'IFSC' })).not.toBeInTheDocument()
  })
})

/**
 * Editing one vendor after another from the register.
 *
 * The edit dialog stays mounted between openings and the form it holds is
 * reset from the record each opening reads. It used to skip that reset while
 * the record had not arrived, so when the next vendor's read failed the form
 * still held the vendor edited last - and Save sent its name, legal name and
 * bank details to the vendor on screen (`PATCH /vendors/{id}` writes whatever it
 * is given; only a GSTIN collision ever refused it).
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody } from '@/test/harness'

import type { VendorRow } from './vendor-types'
import VendorsPage from './vendors-page'

const STAMP = '2026-10-01T00:00:00Z'

function vendor(id: string, code: string, name: string, bank: string): VendorRow {
  return {
    id,
    code,
    name,
    legal_name: `${name} Pvt Ltd`,
    vendor_types: ['RAW_MATERIAL_SUPPLIER'],
    gstin: null,
    pan: null,
    msme_number: null,
    payment_terms_days: 30,
    credit_limit: null,
    lead_time_days: 7,
    currency: 'INR',
    rating: null,
    rating_updated_at: null,
    rating_sample_size: 0,
    is_approved: true,
    approved_at: null,
    approved_by_id: null,
    is_active: true,
    bank_name: bank,
    bank_branch: `${bank} Main`,
    bank_account_name: `${name} A/c`,
    bank_account_number_masked: 'XXXX1234',
    bank_ifsc: null,
    bank_swift: null,
    address_line1: null,
    address_line2: null,
    city: 'Rajkot',
    state: 'Gujarat',
    state_code: null,
    postal_code: null,
    country: 'India',
    contact_person: null,
    email: null,
    phone: null,
    mobile: null,
    website: null,
    notes: null,
    contact_count: 0,
    category_count: 0,
    created_at: STAMP,
    updated_at: STAMP,
    deleted_at: null,
  }
}

const ALPHA = vendor('ven-a', 'VEN-A', 'Alpha Metals', 'Alpha Bank')
const BETA = vendor('ven-b', 'VEN-B', 'Beta Castings', 'Beta Bank')
const READ_FAILED = fail(503, 'SERVICE_UNAVAILABLE', 'The vendor master is unavailable')

/** The register, with Alpha readable and Beta answered by `betaRead`. */
function renderRegister(betaRead: () => RouteBody) {
  return renderScreen(
    <>
      <VendorsPage />
      <Toaster />
    </>,
    {
      permissions: [P.VENDOR_VIEW, P.VENDOR_UPDATE],
      errorNets: true,
      allowUnrouted: true,
      routes: {
        'GET /vendors': page([ALPHA, BETA]),
        'GET /vendors/ven-a': ok(ALPHA),
        'GET /vendors/ven-b': betaRead,
        'PATCH /vendors/ven-b': ({ body }) => ok({ ...BETA, ...(body as object) }),
      },
    },
  )
}

/** Edit Alpha, then cancel it: the dialog closes but stays mounted with Alpha's form. */
async function editAlphaThenCancel(user: ReturnType<typeof renderRegister>['user']) {
  await user.click(await screen.findByRole('button', { name: 'Edit VEN-A' }))
  const dialog = await screen.findByRole('dialog', { busy: false })
  await waitFor(() =>
    expect(within(dialog).getByLabelText(/trading name/i)).toHaveValue('Alpha Metals'),
  )
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
}

describe('editing the next vendor after the previous one', () => {
  it('shows a failed read in place of the form, never the vendor edited last, and saves nothing', async () => {
    const { user, fetch } = renderRegister(() => READ_FAILED)
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit VEN-B' }))
    const dialog = await screen.findByRole('dialog', { busy: false })

    expect(await within(dialog).findByText('Could not load this vendor')).toBeVisible()
    expect(within(dialog).getByText(/The vendor master is unavailable/)).toBeVisible()
    expect(screen.queryByDisplayValue('Alpha Metals')).toBeNull()
    expect(screen.queryByDisplayValue('Alpha Bank')).toBeNull()
    expect(within(dialog).queryByLabelText(/trading name/i)).toBeNull()
    expect(within(dialog).queryByRole('button', { name: 'Save changes' })).toBeNull()
    // Said once, in the dialog: the read opts out of the shell's toast.
    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(within(toasts).queryAllByRole('alert')).toHaveLength(0)
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })

  it('fills the form from the vendor it opened once a retry reads it, and saves that vendor', async () => {
    let reads = 0
    const { user, fetch } = renderRegister(() => (++reads === 1 ? READ_FAILED : ok(BETA)))
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit VEN-B' }))
    const dialog = await screen.findByRole('dialog', { busy: false })
    await user.click(await within(dialog).findByRole('button', { name: 'Try again' }))

    await waitFor(() =>
      expect(within(dialog).getByLabelText(/trading name/i)).toHaveValue('Beta Castings'),
    )
    expect(within(dialog).queryByText('Could not load this vendor')).toBeNull()
    expect(reads).toBe(2)
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))

    const body = (await waitFor(() => fetch.requestFor('PATCH', '/vendors/ven-b'))) as Record<
      string,
      unknown
    >
    expect(body).toMatchObject({
      name: 'Beta Castings',
      legal_name: 'Beta Castings Pvt Ltd',
      bank_name: 'Beta Bank',
      bank_branch: 'Beta Bank Main',
    })
  })
})

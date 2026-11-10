/**
 * Editing one transporter after another from the master list.
 *
 * The edit dialog stays mounted between openings. It used to skip resetting
 * its form while the record it opened on had not arrived, so when the next
 * transporter's read failed the form still held the one edited last - and Save
 * sent that name and contact person to the transporter on screen, which
 * `PATCH /transporters/{id}` accepts without a check that could catch it.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody } from '@/test/harness'

import type { Transporter } from './transporter-model'
import TransportersPage from './transporters-page'

const STAMP = '2026-10-01T00:00:00Z'

function transporter(id: string, code: string, name: string): Transporter {
  return {
    id,
    code,
    name,
    transporter_type: 'TRANSPORT',
    gstin: null,
    pan: null,
    contact_person: `${name} desk`,
    email: null,
    phone: null,
    mobile: null,
    website: null,
    address_line1: null,
    city: 'Rajkot',
    state: null,
    state_code: null,
    postal_code: null,
    country: 'India',
    tracking_url_template: null,
    notes: null,
    is_active: true,
    deleted_at: null,
    created_at: STAMP,
    updated_at: STAMP,
  }
}

const ALPHA = transporter('tr-a', 'TR-A', 'Alpha Roadways')
const BETA = transporter('tr-b', 'TR-B', 'Beta Logistics')
const READ_FAILED = fail(503, 'SERVICE_UNAVAILABLE', 'The carrier master is unavailable')

/** The master list, with Alpha readable and Beta answered by `betaRead`. */
function renderMaster(betaRead: () => RouteBody) {
  return renderScreen(
    <>
      <TransportersPage />
      <Toaster />
    </>,
    {
      permissions: [P.TRANSPORTER_VIEW, P.TRANSPORTER_MANAGE],
      errorNets: true,
      allowUnrouted: true,
      routes: {
        'GET /transporters': page([ALPHA, BETA]),
        'GET /transporters/tr-a': ok(ALPHA),
        'GET /transporters/tr-b': betaRead,
        'PATCH /transporters/tr-b': ({ body }) => ok({ ...BETA, ...(body as object) }),
      },
    },
  )
}

/** Edit Alpha, then cancel it: the dialog closes but stays mounted with Alpha's form. */
async function editAlphaThenCancel(user: ReturnType<typeof renderMaster>['user']) {
  await user.click(await screen.findByRole('button', { name: 'Edit TR-A' }))
  const dialog = await screen.findByRole('dialog')
  await waitFor(() => expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Alpha Roadways'))
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
}

describe('editing the next transporter after the previous one', () => {
  it('shows a failed read in place of the form, never the transporter edited last, and saves nothing', async () => {
    const { user, fetch } = renderMaster(() => READ_FAILED)
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit TR-B' }))
    const dialog = await screen.findByRole('dialog')

    expect(await within(dialog).findByText('Could not load this transporter')).toBeVisible()
    expect(within(dialog).getByText(/The carrier master is unavailable/)).toBeVisible()
    expect(screen.queryByDisplayValue('Alpha Roadways')).toBeNull()
    expect(screen.queryByDisplayValue('Alpha Roadways desk')).toBeNull()
    expect(within(dialog).queryByLabelText(/^name/i)).toBeNull()
    expect(within(dialog).queryByRole('button', { name: 'Save changes' })).toBeNull()
    // Said once, in the dialog: the read opts out of the shell's toast.
    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(within(toasts).queryAllByRole('alert')).toHaveLength(0)
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })

  it('fills the form from the transporter it opened once a retry reads it, and saves that one', async () => {
    let reads = 0
    const { user, fetch } = renderMaster(() => (++reads === 1 ? READ_FAILED : ok(BETA)))
    await editAlphaThenCancel(user)

    await user.click(screen.getByRole('button', { name: 'Edit TR-B' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(await within(dialog).findByRole('button', { name: 'Try again' }))

    await waitFor(() =>
      expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Beta Logistics'),
    )
    expect(within(dialog).queryByText('Could not load this transporter')).toBeNull()
    expect(reads).toBe(2)
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))

    const body = (await waitFor(() => fetch.requestFor('PATCH', '/transporters/tr-b'))) as Record<
      string,
      unknown
    >
    expect(body).toMatchObject({ name: 'Beta Logistics', contact_person: 'Beta Logistics desk' })
  })
})

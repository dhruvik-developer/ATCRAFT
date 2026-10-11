/**
 * The dialogs that create, edit and retire a manufacturer.
 *
 * The request is what matters, so the assertions read it: the code is sent on
 * a create and never on an edit, blank optional text posts null rather than an
 * empty string (so "no website" is stored one way, not two), and a retirement
 * the server refuses shows the server's sentence - which names whether items,
 * vendor links or brands are in the way - word for word.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, renderScreen, type RouteTable } from '@/test/harness'

import { ManufacturerFormDialog, ManufacturerRetireDialog } from './manufacturer-dialogs'

const MAKER = {
  id: 'maker-1',
  code: 'CYLINDER-WORKS',
  name: 'Demo Cylinder Works',
  legal_name: 'Demo Cylinder Works Private Limited',
  country: 'India',
  website: 'https://cylinders.example',
  notes: null,
  is_active: true,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}

function renderForm(maker: typeof MAKER | null, routes: RouteTable = {}) {
  return renderScreen(
    <ManufacturerFormDialog open onOpenChange={() => {}} manufacturer={maker} />,
    { permissions: [P.MANUFACTURER_MANAGE], routes },
  )
}

describe('creating a manufacturer', () => {
  it('posts the code and the name, India by default, and null for blank text', async () => {
    const { user, fetch } = renderForm(null, { 'POST /products/manufacturers': ok(MAKER) })

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'CYLINDER-WORKS')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Demo Cylinder Works')
    await user.click(screen.getByRole('button', { name: /create manufacturer/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', '/products/manufacturers'),
    )) as Record<string, unknown>
    expect(body).toEqual({
      code: 'CYLINDER-WORKS',
      name: 'Demo Cylinder Works',
      legal_name: null,
      country: 'India',
      website: null,
      notes: null,
      is_active: true,
    })
  })

  it('refuses a maker with no name before anything reaches the network', async () => {
    const { user, fetch } = renderForm(null)

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'M1')
    await user.click(screen.getByRole('button', { name: /create manufacturer/i }))

    expect(await screen.findByText('A name is required.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })
})

describe('editing a manufacturer', () => {
  it('sends no code, and clears a website the operator emptied', async () => {
    const { user, fetch } = renderForm(MAKER, {
      'PATCH /products/manufacturers/maker-1': ok({ ...MAKER, website: null }),
    })

    await user.clear(screen.getByLabelText('Website'))
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/products/manufacturers/maker-1'),
    )) as Record<string, unknown>
    expect(body).not.toHaveProperty('code')
    expect(body.website).toBeNull()
    expect(body.legal_name).toBe('Demo Cylinder Works Private Limited')
  })

  it('locks the code field', () => {
    renderForm(MAKER)

    expect(screen.getByRole('textbox', { name: 'Code' })).toBeDisabled()
  })
})

describe('retiring a manufacturer', () => {
  it('sends the reason in the query string', async () => {
    const reasons: (string | null)[] = []
    const { user } = renderScreen(
      <ManufacturerRetireDialog manufacturer={MAKER} onOpenChange={() => {}} />,
      {
        permissions: [P.MANUFACTURER_MANAGE],
        routes: {
          'DELETE /products/manufacturers/maker-1': ({ url }) => {
            reasons.push(url.searchParams.get('reason'))
            return ok(null)
          },
        },
      },
    )

    await user.type(screen.getByLabelText(/reason/i), 'Closed down')
    await user.click(screen.getByRole('button', { name: 'Retire manufacturer' }))

    await waitFor(() => expect(reasons).toEqual(['Closed down']))
  })

  it('shows which holders are in the way, in the server words', async () => {
    const refusal =
      'Manufacturer CYLINDER-WORKS is still named by live item-vendor links and brands, so it cannot be retired. Change those first.'
    const { user } = renderScreen(
      <ManufacturerRetireDialog manufacturer={MAKER} onOpenChange={() => {}} />,
      {
        permissions: [P.MANUFACTURER_MANAGE],
        routes: {
          'DELETE /products/manufacturers/maker-1': fail(422, 'REFERENCED_RECORD_IN_USE', refusal),
        },
      },
    )

    await user.type(screen.getByLabelText(/reason/i), 'Closed down')
    await user.click(screen.getByRole('button', { name: 'Retire manufacturer' }))

    const dialog = screen.getByRole('dialog')
    expect(await within(dialog).findByText(refusal)).toBeVisible()
  })
})

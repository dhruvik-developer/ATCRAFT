/**
 * The dialogs that create, edit and retire a brand.
 *
 * Three properties decide whether these are safe to hand to an administrator,
 * and none is visible in a screenshot - so the assertions read the request:
 *
 * **The edit sends no code.** `BrandUpdate` forbids `code`; a PATCH carrying it
 * is refused outright, because the code identifies the row everywhere it is
 * quoted. The field is locked on an edit and absent from the body.
 *
 * **"No owner" posts null.** An empty string would be a foreign key the server
 * must reject; an absent field would leave a stale owner in place.
 *
 * **A refused retirement says why, verbatim.** "Still carried by live items"
 * names exactly what has to change first; rewording it would lose that.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { BrandFormDialog, BrandRetireDialog } from './brand-dialogs'

const BRAND = {
  id: 'brand-1',
  code: 'ATCRAFT-PRO',
  name: 'ATCRAFT PRO',
  description: 'The premium line.',
  manufacturer_id: 'maker-1',
  sort_order: 10,
  is_active: true,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}

const MAKERS = page([
  {
    id: 'maker-1',
    code: 'ATCRAFT',
    name: 'Atcraft Works',
    legal_name: null,
    country: 'India',
    website: null,
    notes: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  },
])

function renderForm(brand: typeof BRAND | null, routes: RouteTable = {}, permissions?: string[]) {
  return renderScreen(<BrandFormDialog open onOpenChange={() => {}} brand={brand} />, {
    permissions: permissions ?? [P.BRAND_MANAGE, P.MANUFACTURER_VIEW],
    routes: { 'GET /products/manufacturers': MAKERS, ...routes },
  })
}

describe('creating a brand', () => {
  it('posts the code, the name and a null owner when "No owner" is left', async () => {
    const { user, fetch } = renderForm(null, { 'POST /products/brands': ok(BRAND) })

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'ATCRAFT-PRO')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'ATCRAFT PRO')
    await user.click(screen.getByRole('button', { name: /create brand/i }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/products/brands'))) as Record<
      string,
      unknown
    >
    expect(body).toEqual({
      code: 'ATCRAFT-PRO',
      name: 'ATCRAFT PRO',
      manufacturer_id: null,
      sort_order: 0,
      description: null,
      is_active: true,
    })
  })

  it('posts the chosen owner as an id', async () => {
    const { user, fetch } = renderForm(null, { 'POST /products/brands': ok(BRAND) })

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'ATCRAFT-PRO')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'ATCRAFT PRO')
    await user.click(screen.getByRole('combobox', { name: /owner/i }))
    await user.click(await screen.findByRole('option', { name: 'Atcraft Works' }))
    await user.click(screen.getByRole('button', { name: /create brand/i }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/products/brands'))) as Record<
      string,
      unknown
    >
    expect(body.manufacturer_id).toBe('maker-1')
  })

  it('refuses a position outside 0 to 9999 before anything reaches the network', async () => {
    const { user, fetch } = renderForm(null)

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'B')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Brand')
    const position = screen.getByLabelText('Position in the list')
    await user.clear(position)
    await user.type(position, '10000')
    await user.click(screen.getByRole('button', { name: /create brand/i }))

    expect(await screen.findByText('A whole number from 0 to 9999.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('does not ask for the manufacturer list without the right to read it', async () => {
    // `manufacturer.view` is a right of its own; firing the request anyway
    // would earn a 403 the user can do nothing about. The harness turns an
    // unrouted request into a failure, so its absence is the assertion.
    const { fetch } = renderScreen(<BrandFormDialog open onOpenChange={() => {}} brand={null} />, {
      permissions: [P.BRAND_MANAGE],
    })

    await screen.findByRole('dialog')
    expect(fetch.calls.map((call) => call.path)).not.toContain('/products/manufacturers')
  })
})

describe('editing a brand', () => {
  it('sends the editable fields without the code, which identifies the row', async () => {
    const { user, fetch } = renderForm(BRAND, {
      'PATCH /products/brands/brand-1': ok({ ...BRAND, name: 'ATCRAFT Professional' }),
    })

    const name = screen.getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'ATCRAFT Professional')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/products/brands/brand-1'),
    )) as Record<string, unknown>
    expect(body).not.toHaveProperty('code')
    expect(body).toEqual({
      name: 'ATCRAFT Professional',
      manufacturer_id: 'maker-1',
      sort_order: 10,
      description: 'The premium line.',
      is_active: true,
    })
  })

  it('locks the code field rather than offering a change the API refuses', () => {
    renderForm(BRAND)

    expect(screen.getByRole('textbox', { name: 'Code' })).toBeDisabled()
  })

  it('keeps the recorded owner for an editor who cannot read manufacturers', async () => {
    const { user, fetch } = renderScreen(
      <BrandFormDialog open onOpenChange={() => {}} brand={BRAND} />,
      {
        permissions: [P.BRAND_MANAGE],
        routes: { 'PATCH /products/brands/brand-1': ok(BRAND) },
      },
    )

    await user.click(screen.getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/products/brands/brand-1'),
    )) as Record<string, unknown>
    expect(body.manufacturer_id).toBe('maker-1')
  })

  it('puts a duplicate-code refusal on the code field', async () => {
    const { user } = renderForm(null, {
      'POST /products/brands': fail(
        409,
        'DUPLICATE_DOCUMENT',
        "Brand code 'ATCRAFT' is already used in this company.",
        { fields: { code: ["Brand code 'ATCRAFT' is already used in this company."] } },
      ),
    })

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'ATCRAFT')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Atcraft')
    await user.click(screen.getByRole('button', { name: /create brand/i }))

    const dialog = screen.getByRole('dialog')
    expect(
      await within(dialog).findByText("Brand code 'ATCRAFT' is already used in this company."),
    ).toBeVisible()
  })
})

describe('retiring a brand', () => {
  it('sends the reason as a query parameter, not a body', async () => {
    const reasons: (string | null)[] = []
    const { user } = renderScreen(<BrandRetireDialog brand={BRAND} onOpenChange={() => {}} />, {
      permissions: [P.BRAND_MANAGE],
      routes: {
        'DELETE /products/brands/brand-1': ({ url }) => {
          reasons.push(url.searchParams.get('reason'))
          return ok(null)
        },
      },
    })

    await user.type(screen.getByLabelText(/reason/i), 'Line withdrawn')
    await user.click(screen.getByRole('button', { name: 'Retire brand' }))

    await waitFor(() => expect(reasons).toEqual(['Line withdrawn']))
  })

  it('holds a too-short reason back before the round trip', async () => {
    const { user, fetch } = renderScreen(
      <BrandRetireDialog brand={BRAND} onOpenChange={() => {}} />,
      { permissions: [P.BRAND_MANAGE] },
    )

    await user.type(screen.getByLabelText(/reason/i), 'no')
    await user.click(screen.getByRole('button', { name: 'Retire brand' }))

    expect(await screen.findByText('Record at least three characters of reason')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'DELETE')).toBe(false)
  })

  it('shows the in-use refusal verbatim, because it says what to change', async () => {
    const refusal =
      'Brand ATCRAFT-PRO is still carried by live items, so it cannot be retired. Move those items to another brand first.'
    const { user } = renderScreen(<BrandRetireDialog brand={BRAND} onOpenChange={() => {}} />, {
      permissions: [P.BRAND_MANAGE],
      routes: {
        'DELETE /products/brands/brand-1': fail(422, 'REFERENCED_RECORD_IN_USE', refusal),
      },
    })

    await user.type(screen.getByLabelText(/reason/i), 'Rebranding')
    await user.click(screen.getByRole('button', { name: 'Retire brand' }))

    const dialog = screen.getByRole('dialog')
    expect(await within(dialog).findByText(refusal)).toBeVisible()
    expect(within(dialog).getByText('The brand was not retired')).toBeVisible()
  })
})

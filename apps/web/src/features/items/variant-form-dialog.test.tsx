/**
 * The variant create/edit dialog.
 *
 * Two things here are worth a test far more than the rendering is.
 *
 * **1. The request bodies.** Creation posts the code and the deltas as decimal
 * strings; an edit PATCHes everything but the code, which the API refuses to
 * change. `cost_delta` rides along only for a principal holding
 * `item.cost_view` - sending it as `"0"` for anyone else would zero a real
 * costing difference nobody was shown.
 *
 * **2. The one rule the API enforces on creation.** A variant with no
 * distinguishing attribute is a duplicate of its parent, so the dialog refuses
 * an empty finish/size/colour combination before anything is sent.
 *
 * One Radix `<Select>` is opened below (the finish picker); the rest are left
 * closed - see the note in `items-page.test.tsx` for why each open is
 * expensive under jsdom.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { VariantFormDialog } from './variant-form-dialog'
import type { ItemVariant } from './item-model'

const ITEM_ID = '01a00000-0000-7000-8000-0000000000i1'

const ATTRIBUTES = page([
  {
    id: 'att-brushed',
    attribute: 'FINISH',
    code: 'BRUSHED',
    name: 'Brushed',
    sort_order: 0,
    is_active: true,
  },
  {
    id: 'att-large',
    attribute: 'SIZE',
    code: 'LARGE',
    name: 'Large',
    sort_order: 0,
    is_active: true,
  },
  {
    id: 'att-red',
    attribute: 'COLOUR',
    code: 'RED',
    name: 'Red',
    sort_order: 0,
    is_active: true,
  },
])

const VARIANT: ItemVariant = {
  id: 'var-brushed',
  item_id: ITEM_ID,
  code: 'RM-001-BR',
  name: 'Brass rod 12mm - brushed',
  finish_id: 'att-brushed',
  size_id: null,
  colour_id: null,
  price_delta: '15.000000',
  cost_delta: '4.0000',
  barcode: '8901234567890',
  is_active: true,
}

function renderDialog(options: {
  permissions: string[]
  routes: RouteTable
  variant?: ItemVariant | null
}) {
  return renderScreen(
    <VariantFormDialog
      open
      onOpenChange={() => {}}
      itemId={ITEM_ID}
      itemCode="RM-001"
      variant={options.variant ?? null}
    />,
    { permissions: options.permissions, routes: options.routes },
  )
}

describe('creating a variant', () => {
  it('posts the code, the attributes and both deltas as decimal strings', async () => {
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_CREATE, P.ITEM_COST_VIEW, P.ITEM_ATTRIBUTE_VIEW],
      routes: {
        'GET /products/attributes': ATTRIBUTES,
        [`POST /products/items/${ITEM_ID}/variants`]: ok(VARIANT),
      },
    })

    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'RM-001-BR')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Name' }),
      'Brass rod 12mm - brushed',
    )
    await user.click(within(dialog).getByRole('combobox', { name: /^finish$/i }))
    await user.click(await screen.findByRole('option', { name: 'Brushed' }))
    const price = within(dialog).getByRole('textbox', { name: 'Price delta' })
    await user.clear(price)
    await user.type(price, '15')
    const cost = within(dialog).getByRole('textbox', { name: 'Cost delta' })
    await user.clear(cost)
    await user.type(cost, '4')
    await user.type(within(dialog).getByRole('textbox', { name: 'Barcode' }), '8901234567890')
    await user.click(within(dialog).getByRole('button', { name: /create variant/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/products/items/${ITEM_ID}/variants`),
    )) as Record<string, unknown>
    expect(body).toEqual({
      code: 'RM-001-BR',
      name: 'Brass rod 12mm - brushed',
      finish_id: 'att-brushed',
      size_id: null,
      colour_id: null,
      price_delta: '15',
      cost_delta: '4',
      barcode: '8901234567890',
      is_active: true,
    })
  })

  it('omits cost_delta entirely for a principal without item.cost_view', async () => {
    // The field is not rendered for them, so the form holds its empty
    // default. Sending that default would post "0" over a real costing
    // difference - a silent write performed by a user who was never shown the
    // number.
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_CREATE, P.ITEM_ATTRIBUTE_VIEW],
      routes: {
        'GET /products/attributes': ATTRIBUTES,
        [`POST /products/items/${ITEM_ID}/variants`]: ok({ ...VARIANT, cost_delta: '0.0000' }),
      },
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('textbox', { name: 'Cost delta' })).not.toBeInTheDocument()

    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'RM-001-LG')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Large')
    await user.click(within(dialog).getByRole('combobox', { name: /^size$/i }))
    await user.click(await screen.findByRole('option', { name: 'Large' }))
    await user.click(within(dialog).getByRole('button', { name: /create variant/i }))

    await waitFor(() => fetch.requestFor('POST', `/products/items/${ITEM_ID}/variants`))
    const body = fetch.requestFor('POST', `/products/items/${ITEM_ID}/variants`) as Record<
      string,
      unknown
    >
    expect('cost_delta' in body).toBe(false)
    expect(body.finish_id).toBeNull()
    expect(body.size_id).toBe('att-large')
  })

  it('refuses a variant with no distinguishing attribute before anything is sent', async () => {
    // The API rejects a variant that differs in nothing, and a server message
    // against `finish_id` would puzzle the user - the refusal is stated where
    // the choice is made.
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_CREATE],
      routes: { 'GET /products/attributes': ATTRIBUTES },
    })

    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'RM-001-XX')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'No difference')
    await user.click(within(dialog).getByRole('button', { name: /create variant/i }))

    expect(
      await within(dialog).findByText(
        'A variant must differ in at least one of finish, size or colour.',
      ),
    ).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })
})

describe('editing a variant', () => {
  it('patches without the code, which the dialog locks', async () => {
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_UPDATE, P.ITEM_COST_VIEW],
      routes: {
        'GET /products/attributes': ATTRIBUTES,
        'PATCH /products/variants/var-brushed': ok({ ...VARIANT, name: 'Brushed, oiled' }),
      },
      variant: VARIANT,
    })

    const dialog = await screen.findByRole('dialog')
    // The code is quoted beside the parent on price lists, so the API refuses
    // to change it and the field is locked rather than sent.
    expect(within(dialog).getByRole('textbox', { name: 'Code' })).toBeDisabled()

    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Brushed, oiled')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    await waitFor(() => fetch.requestFor('PATCH', '/products/variants/var-brushed'))
    const body = fetch.requestFor('PATCH', '/products/variants/var-brushed') as Record<
      string,
      unknown
    >
    expect(body.code).toBeUndefined()
    expect(body.name).toBe('Brushed, oiled')
    // Seeded from the variant, not from the lists: the selects still hold what
    // the row carries even though none was opened.
    expect(body.finish_id).toBe('att-brushed')
    expect(body.size_id).toBeNull()
    expect(body.price_delta).toBe('15.000000')
    expect(body.cost_delta).toBe('4.0000')
    expect(body.barcode).toBe('8901234567890')
  })

  it('omits cost_delta for a principal without item.cost_view', async () => {
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_UPDATE],
      routes: {
        'GET /products/attributes': ATTRIBUTES,
        'PATCH /products/variants/var-brushed': ok(VARIANT),
      },
      variant: VARIANT,
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('textbox', { name: 'Cost delta' })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    await waitFor(() => fetch.requestFor('PATCH', '/products/variants/var-brushed'))
    const body = fetch.requestFor('PATCH', '/products/variants/var-brushed') as Record<
      string,
      unknown
    >
    // Omitted, not zeroed: the recorded costing difference survives an edit by
    // someone who may not see it.
    expect('cost_delta' in body).toBe(false)
  })
})

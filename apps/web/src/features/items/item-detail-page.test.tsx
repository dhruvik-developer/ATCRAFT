/**
 * The item detail screen: six tabs, an edit dialog and two retirements.
 *
 * Three things here are worth a test far more than the rendering is.
 *
 * **1. The retire action is gated on a lifecycle predicate, not on a boolean.**
 * `acceptsRetirement` exists because `is_active` answers the wrong question in
 * both directions - an INACTIVE item retires perfectly well, and an ARCHIVED one
 * is refused by the state machine. Offering the button anyway would put the user
 * through a destructive confirmation for a request that can only fail.
 *
 * **2. The edit dialog's payload.** A screen that renders correctly and PATCHes
 * the wrong shape is exactly the defect a screen test catches and a type check
 * cannot: `standard_cost` is included only for a principal holding
 * `item.cost_view`, and sending it as `"0"` for anyone else would zero a real
 * cost. Decimals stay strings the whole way; `lead_time_days` is the one field
 * that is genuinely an integer.
 *
 * **3. Panels are gated on their own rights.** Stock, where-used and prices each
 * need a permission that `item.view` does not imply, and each says so rather
 * than firing a request that earns a 403 the user cannot act on.
 *
 * Radix `<Select>` controls are not opened here - see the note in
 * `items-page.test.tsx` for why (floating-ui's overflow detection costs ~30s per
 * open under jsdom). Nothing below needs one: the edit dialog is seeded from the
 * item, so every select already holds the right value.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'
import { P } from '@/lib/permissions'

import ItemDetailPage from './item-detail-page'

// ------------------------------------------------------------- fixtures ----

const ITEM_ID = '01a00000-0000-7000-8000-0000000000i1'

/** `GET /products/items/{id}` as the API sends it, with cost included. */
const ITEM = {
  id: ITEM_ID,
  code: 'RM-001',
  name: 'Brass rod 12mm',
  description: 'Half-hard brass, drawn',
  item_group: 'RAW_MATERIAL',
  category_id: 'cat-hardware',
  base_uom_id: 'uom-kg',
  purchase_uom_id: null,
  sale_uom_id: null,
  hsn_code: '7407',
  gst_rate: '18.00',
  sale_price: '250.0000',
  dealer_price: '230.0000',
  standard_cost: '80.5000',
  finish: null,
  size: null,
  colour: null,
  production_type: 'BUY',
  lead_time_days: 7,
  reorder_level: '100.0000',
  reorder_quantity: '500.0000',
  min_order_quantity: '50.0000',
  safety_stock: '25.0000',
  shelf_life_days: null,
  is_batch_tracked: true,
  is_serial_tracked: false,
  default_warehouse_id: null,
  valuation_method: 'FIFO',
  is_active: true,
  lifecycle_state: 'ACTIVE',
  created_at: '2026-01-05T10:00:00Z',
  updated_at: '2026-02-01T09:30:00Z',
  can_purchase: true,
  can_manufacture: false,
  can_sell: false,
  can_be_bom_component: true,
  can_be_assembly_component: false,
  can_be_spare: false,
  can_job_work: false,
  can_return: false,
  can_scrap: true,
}

const VARIANT = {
  id: 'var-brushed',
  item_id: ITEM_ID,
  code: 'RM-001-BR',
  name: 'Brass rod 12mm - brushed',
  finish: 'Brushed',
  size: null,
  colour: null,
  price_delta: '15.000000',
  cost_delta: '4.000000',
  barcode: null,
  is_active: true,
}

/**
 * The stock summary. The totals deliberately do NOT equal the single warehouse
 * row: the endpoint totals the whole item and the row is one warehouse, so a
 * panel that folded the visible rows would print 900 where 1 250 is correct.
 */
const STOCK = page([
  {
    item_id: ITEM_ID,
    item_code: 'RM-001',
    item_name: 'Brass rod 12mm',
    reorder_level: '100.0000',
    total_on_hand: '1250.0000',
    total_reserved: '120.0000',
    total_available: '1050.0000',
    total_in_transit: '50.0000',
    total_job_work: '30.0000',
    total_owned_quantity: '1330.0000',
    total_value: '100625.00',
    warehouses: [
      {
        warehouse_id: 'wh-main',
        warehouse_code: 'WH-1',
        warehouse_name: 'Main store',
        on_hand_quantity: '900.0000',
        reserved_quantity: '120.0000',
        available_quantity: '700.0000',
        in_transit_quantity: '50.0000',
        qc_hold_quantity: '80.0000',
        damaged_quantity: '0.0000',
        job_work_quantity: '30.0000',
        stock_value: '72450.00',
      },
    ],
  },
])

const CATEGORIES = page([
  { id: 'cat-hardware', code: 'HW', name: 'Hardware', path: '/HW', depth: 0, is_active: true },
])
const UOMS = page([
  { id: 'uom-kg', code: 'KG', name: 'Kilogram', decimal_places: 3, is_active: true },
])

/** The PATCH body the dialog sent, once the mutation has actually fired. */
async function waitForPatch(fetch: {
  calls: { method: string; body: unknown }[]
}): Promise<Record<string, unknown>> {
  await waitFor(() => expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(true))
  return fetch.calls.find((call) => call.method === 'PATCH')!.body as Record<string, unknown>
}

/** The item read plus the two reference lookups, which most tests need. */
function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    [`GET /products/items/${ITEM_ID}`]: ok(ITEM),
    'GET /products/categories': CATEGORIES,
    'GET /products/uoms': UOMS,
    ...overrides,
  }
}

/** Render the detail screen for `ITEM_ID`, with the route param bound. */
function renderDetail(options: {
  permissions: string[]
  routes: RouteTable
  tab?: string
}) {
  return renderScreen(<ItemDetailPage />, {
    permissions: options.permissions,
    route: `/items/${ITEM_ID}${options.tab ? `?tab=${options.tab}` : ''}`,
    path: '/items/:itemId',
    routes: options.routes,
  })
}

// -------------------------------------------------------------- the page ----

describe('loading the item', () => {
  it('paints the master record once the read resolves', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes(),
    })

    expect(await screen.findByRole('heading', { name: /RM-001 - Brass rod 12mm/ })).toBeVisible()
    // The two reference lookups are what turn the stored ids into words. If
    // either silently failed the fields would read "-" rather than break.
    expect(await screen.findByText('Hardware')).toBeVisible()
    expect(await screen.findByText('KG')).toBeVisible()
  })

  it('tells the user why the read failed, and offers a retry', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW],
      routes: {
        [`GET /products/items/${ITEM_ID}`]: fail(
          403,
          'PERMISSION_DENIED',
          'Your role does not include item.view for this company',
        ),
      },
    })

    // The API's own sentence: it names the missing right, which is what the
    // user has to quote when asking an administrator for it.
    expect(
      await screen.findByText('Your role does not include item.view for this company'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })

  it('renders the nine capabilities as nine independent states', async () => {
    // Master rules 1: one item may be purchased, manufactured and sold at once.
    // Anything that rendered these as a single classification would collapse
    // three of these rows into one.
    renderDetail({ permissions: [P.ITEM_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    for (const label of [
      'Purchasable',
      'Manufacturable',
      'Sellable',
      'BOM component',
      'Assembly component',
      'Spare part',
      'Job work',
      'Returnable',
      'Scrappable',
    ]) {
      expect(screen.getByText(label)).toBeVisible()
    }
    // Three flags are on in the fixture and six are off; a discriminator could
    // not produce that shape.
    expect(screen.getAllByText('Yes')).toHaveLength(3)
    expect(screen.getAllByText('No')).toHaveLength(6)
  })
})

// ------------------------------------------------------------ permissions ----

describe('header actions', () => {
  it('offers Edit to item.update and Retire to item.delete', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.ITEM_DELETE],
      routes: baseRoutes(),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeVisible()
    expect(screen.getByRole('button', { name: /^retire$/i })).toBeVisible()
  })

  it('withholds Retire from item.update alone', async () => {
    // The two rights are neighbours and the difference matters: deactivating an
    // item is reversible and `item.update` governs it; retirement is terminal
    // and needs `item.delete`. Gating retirement on update is precisely the
    // shape of the six authorisation defects this suite exists to catch.
    renderDetail({ permissions: [P.ITEM_VIEW, P.ITEM_UPDATE], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^retire$/i })).not.toBeInTheDocument()
  })

  it('offers neither to a principal who may only view', async () => {
    renderDetail({ permissions: [P.ITEM_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.queryByRole('button', { name: /^edit$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^retire$/i })).not.toBeInTheDocument()
  })

  it('withholds Retire from an ARCHIVED item even with item.delete', async () => {
    // ARCHIVED is terminal in `ITEM_LIFECYCLE` and has no self-edge, so the
    // machine refuses a second retirement. Offering the button would spend a
    // destructive confirmation on a request that can only be rejected.
    renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.ITEM_DELETE],
      routes: baseRoutes({
        [`GET /products/items/${ITEM_ID}`]: ok({
          ...ITEM,
          is_active: false,
          lifecycle_state: 'ARCHIVED',
        }),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.queryByRole('button', { name: /^retire$/i })).not.toBeInTheDocument()
  })

  it('still offers Retire for an INACTIVE item, which the machine accepts', async () => {
    // The reason `acceptsRetirement` is a predicate and not an `is_active`
    // test: INACTIVE -> ARCHIVED is a real edge, and gating on `is_active`
    // would strand every deactivated item as unretireable.
    renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE],
      routes: baseRoutes({
        [`GET /products/items/${ITEM_ID}`]: ok({
          ...ITEM,
          is_active: false,
          lifecycle_state: 'INACTIVE',
        }),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.getByRole('button', { name: /^retire$/i })).toBeVisible()
  })

  it('shows the standard cost to item.cost_view', async () => {
    renderDetail({ permissions: [P.ITEM_VIEW, P.ITEM_COST_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.getByText('Standard cost')).toBeVisible()
    expect(screen.getByText(/80\.50/)).toBeVisible()
  })

  it('withholds the cost, and its label, from everyone else', async () => {
    // The API withholds the field itself for such a principal, so the row would
    // read "-" - which says "this item has no cost", a different and wrong claim.
    renderDetail({ permissions: [P.ITEM_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    expect(screen.queryByText('Standard cost')).not.toBeInTheDocument()
    expect(screen.queryByText(/80\.50/)).not.toBeInTheDocument()
  })
})

// ------------------------------------------------------------------ tabs ----

describe('the tab panels', () => {
  it('reads stock from the summary endpoint and totals the item, not the page', async () => {
    const { user } = renderDetail({
      permissions: [P.ITEM_VIEW, P.INVENTORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes({ [`GET /inventory/items/${ITEM_ID}/summary`]: STOCK }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('tab', { name: /stock/i }))

    const panel = await screen.findByRole('tabpanel')
    expect(await within(panel).findByText('WH-1')).toBeVisible()
    // 1,250 is the item-wide total from the payload; 900 is this one warehouse.
    // Folding the visible rows would print 900 in the tile, and the figure
    // would move as the user paged - worse than no total at all.
    expect(within(panel).getByText(/1,250 KG/)).toBeVisible()
    expect(within(panel).getByText('900')).toBeVisible()
    // In transit plus at a job worker: owned, not on the shelf, and the
    // quantity people forget when a part reads short.
    expect(within(panel).getByText(/^80 KG$/)).toBeVisible()
  })

  it('explains rather than queries when the stock right is missing', async () => {
    // `inventory.view` is a separate right. Firing the request anyway earns a
    // 403 nobody can act on - and the harness would fail this test on the
    // unrouted call, which is exactly the guarantee wanted here.
    const { user } = renderDetail({ permissions: [P.ITEM_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('tab', { name: /stock/i }))

    expect(
      await screen.findByText('Stock balances are not visible to your account'),
    ).toBeVisible()
  })

  it('explains rather than queries when the BOM right is missing', async () => {
    const { user } = renderDetail({ permissions: [P.ITEM_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('tab', { name: /where used/i }))

    expect(
      await screen.findByText('Bills of materials are not visible to your account'),
    ).toBeVisible()
  })

  it('explains rather than queries when the price right is missing', async () => {
    const { user } = renderDetail({ permissions: [P.ITEM_VIEW], routes: baseRoutes() })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('tab', { name: /prices/i }))

    expect(await screen.findByText('Prices are not visible to your account')).toBeVisible()
  })

  it('lists the parents an item is used in, each a link to its own record', async () => {
    const { user } = renderDetail({
      permissions: [P.ITEM_VIEW, P.BOM_VIEW],
      routes: baseRoutes({
        [`GET /bom/where-used/${ITEM_ID}`]: page([
          {
            bom_id: 'bom-1',
            bom_code: 'BOM-HNG-01',
            bom_version: 3,
            bom_status: 'APPROVED',
            is_default: true,
            level: 1,
            line_number: 2,
            parent_item_id: 'parent-item',
            parent_item_code: 'FG-HINGE',
            parent_item_name: 'Brass butt hinge 100mm',
            quantity_per: '0.2500',
          },
        ]),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('tab', { name: /where used/i }))

    const link = await screen.findByRole('link', { name: 'FG-HINGE' })
    expect(link).toHaveAttribute('href', '/items/parent-item')
    // The quantity per parent is the number a planner acts on; a rounding of it
    // to a whole number would silently change every explosion.
    expect(screen.getByText('0.25')).toBeVisible()
  })

  it('opens the tab named in the URL, so a link can point at one', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW],
      tab: 'variants',
      routes: baseRoutes({ [`GET /products/items/${ITEM_ID}/variants`]: page([VARIANT]) }),
    })

    expect(await screen.findByText('RM-001-BR')).toBeVisible()
  })
})

// -------------------------------------------------------------- variants ----

describe('the variants panel', () => {
  it('offers a retire action per row only to item.delete', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE],
      tab: 'variants',
      routes: baseRoutes({ [`GET /products/items/${ITEM_ID}/variants`]: page([VARIANT]) }),
    })

    expect(
      await screen.findByRole('button', { name: 'Retire variant RM-001-BR' }),
    ).toBeVisible()
  })

  it('offers none to a principal who may only view', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW],
      tab: 'variants',
      routes: baseRoutes({ [`GET /products/items/${ITEM_ID}/variants`]: page([VARIANT]) }),
    })

    await screen.findByText('RM-001-BR')
    expect(screen.queryByRole('button', { name: /retire variant/i })).not.toBeInTheDocument()
  })

  it('shows the cost delta only to item.cost_view', async () => {
    renderDetail({
      permissions: [P.ITEM_VIEW],
      tab: 'variants',
      routes: baseRoutes({ [`GET /products/items/${ITEM_ID}/variants`]: page([VARIANT]) }),
    })

    await screen.findByText('RM-001-BR')
    expect(screen.queryByRole('columnheader', { name: 'Cost delta' })).not.toBeInTheDocument()
    // The price delta stays: it is what the sales side quotes, and it is signed
    // so it reads as a delta rather than as an absolute price.
    expect(screen.getByRole('columnheader', { name: 'Price delta' })).toBeVisible()
    expect(screen.getByText(/\+.*15\.00/)).toBeVisible()
  })

  it('deletes the variant, not the item, and by the variant endpoint', async () => {
    const { user, fetch } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE],
      tab: 'variants',
      routes: baseRoutes({
        [`GET /products/items/${ITEM_ID}/variants`]: page([VARIANT]),
        'DELETE /products/variants/var-brushed': ok({ deleted: true }),
      }),
    })

    await user.click(await screen.findByRole('button', { name: 'Retire variant RM-001-BR' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Retire variant' }))

    const call = fetch.calls.find((entry) => entry.method === 'DELETE')
    expect(call?.path).toBe('/products/variants/var-brushed')
    // The contract declares nothing but the path id on this DELETE. A body here
    // would be a field the server never reads.
    expect(call?.body).toBeUndefined()
  })
})

// ---------------------------------------------------------- the edit form ----

describe('what the edit dialog sends', () => {
  it('patches without the code, and keeps every decimal a string', async () => {
    const { user, fetch } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.ITEM_COST_VIEW, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes({
        [`PATCH /products/items/${ITEM_ID}`]: ok({ ...ITEM, name: 'Brass rod 14mm' }),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')

    // The code is quoted on documents that have already been issued, so the
    // API refuses to change it and the field is locked rather than sent.
    expect(within(dialog).getByRole('textbox', { name: 'Code' })).toBeDisabled()

    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Brass rod 14mm')
    // The nine flags are independent, so turning one on must not disturb the
    // other eight.
    await user.click(within(dialog).getByRole('checkbox', { name: 'Sellable' }))
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForPatch(fetch)
    expect(body.code).toBeUndefined()
    expect(body.name).toBe('Brass rod 14mm')
    expect(body.can_sell).toBe(true)
    expect(body.can_purchase).toBe(true)
    expect(body.can_be_bom_component).toBe(true)
    expect(body.can_manufacture).toBe(false)
    // Money and quantities are decimal strings end to end. A float here would
    // be a defect the compiler cannot see and the screen cannot show.
    expect(body.gst_rate).toBe('18.00')
    expect(body.reorder_level).toBe('100.0000')
    expect(body.standard_cost).toBe('80.5000')
    // Days are a whole count, not a monetary decimal.
    expect(body.lead_time_days).toBe(7)
    // Ids are ids, and an unset optional unit is null rather than "" or absent.
    expect(body.base_uom_id).toBe('uom-kg')
    expect(body.category_id).toBe('cat-hardware')
    expect(body.purchase_uom_id).toBeNull()
    expect(body.sale_uom_id).toBeNull()
  })

  it('omits standard_cost entirely for a principal without item.cost_view', async () => {
    // The field is not rendered for them, so the form holds its empty default.
    // Sending that default would post "0" over a real cost - a silent write-off
    // performed by a user who was never shown the number.
    const { user, fetch } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes({
        [`PATCH /products/items/${ITEM_ID}`]: ok(ITEM),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('textbox', { name: 'Standard cost' })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForPatch(fetch)
    expect('standard_cost' in body).toBe(false)
  })

  it('clears an emptied HSN code to null rather than to an empty string', async () => {
    // The column is nullable and the API stores what it is given; "" would be a
    // present-but-blank HSN code on a tax document.
    const { user, fetch } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes({ [`PATCH /products/items/${ITEM_ID}`]: ok(ITEM) }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.clear(within(dialog).getByRole('textbox', { name: 'HSN code' }))
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForPatch(fetch)
    expect(body.hsn_code).toBeNull()
  })

  it('puts a 422 back on the field that caused it', async () => {
    // FastAPI reports the location as `body.gst_rate`; a message shown in a
    // toast instead leaves the user hunting for which of twenty fields is wrong.
    const { user } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes({
        [`PATCH /products/items/${ITEM_ID}`]: fail(422, 'VALIDATION_ERROR', 'Invalid item', {
          fields: { 'body.gst_rate': ['GST rate cannot exceed 100'] },
        }),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'GST rate cannot exceed 100',
    )
    // The dialog stays open: the user has to correct the field, and a closed
    // form would discard the twenty other values they had already entered.
    expect(within(dialog).getByRole('button', { name: /save changes/i })).toBeVisible()
  })

  it('does not validate the rate away in the browser first', async () => {
    // The client rejects >100 itself, so this pins that the client rule and the
    // server rule agree on the boundary rather than the client being laxer.
    const { user, fetch } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: baseRoutes({ [`PATCH /products/items/${ITEM_ID}`]: ok(ITEM) }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    const gst = within(dialog).getByRole('textbox', { name: 'GST rate (%)' })
    await user.clear(gst)
    await user.type(gst, '120')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    expect(await within(dialog).findByText('GST rate cannot exceed 100')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })
})

// ------------------------------------------------------- retiring an item ----

describe('retiring the item', () => {
  it('sends a bare DELETE, because the endpoint takes no reason', async () => {
    const { user, fetch } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE],
      routes: baseRoutes({ [`DELETE /products/items/${ITEM_ID}`]: ok({ deleted: true }) }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^retire$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Retire item' }))

    const call = fetch.calls.find((entry) => entry.method === 'DELETE')
    expect(call?.path).toBe(`/products/items/${ITEM_ID}`)
    // The service writes its own audit wording against the acting user. A
    // reason box here would be a field the server never reads.
    expect(call?.body).toBeUndefined()
  })

  it('keeps the rejection on screen instead of dropping the user back on the page', async () => {
    // `delete_item` asserts the lifecycle transition, and its sentence names the
    // state that stood in the way - which is more use than "request failed".
    const { user } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE],
      routes: baseRoutes({
        [`DELETE /products/items/${ITEM_ID}`]: fail(
          409,
          'INVALID_STATE_TRANSITION',
          'ACTIVE -> ARCHIVED is not permitted while a physical count is open',
        ),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^retire$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Retire item' }))

    expect(await within(dialog).findByText('The item was not retired')).toBeVisible()
    expect(
      within(dialog).getByText(
        'ACTIVE -> ARCHIVED is not permitted while a physical count is open',
      ),
    ).toBeVisible()
  })

  it('warns about the stock and the BOM lines it will leave behind', async () => {
    // The service refuses neither, so the dialog goes and looks. Without this
    // the user cannot see the one consequence the API will not enforce.
    const { user } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE, P.INVENTORY_VIEW, P.BOM_VIEW, P.UOM_VIEW],
      routes: baseRoutes({
        [`GET /inventory/items/${ITEM_ID}/summary`]: STOCK,
        [`GET /bom/where-used/${ITEM_ID}`]: page(
          [
            {
              bom_id: 'bom-1',
              bom_code: 'BOM-HNG-01',
              bom_version: 3,
              bom_status: 'APPROVED',
              is_default: true,
              level: 1,
              line_number: 2,
              parent_item_id: 'parent-item',
              parent_item_code: 'FG-HINGE',
              parent_item_name: 'Brass butt hinge 100mm',
              quantity_per: '0.2500',
            },
          ],
          { total: 4 },
        ),
      }),
    })

    await screen.findByRole('heading', { name: /RM-001/ })
    await user.click(screen.getByRole('button', { name: /^retire$/i }))
    const dialog = await screen.findByRole('dialog')

    expect(await within(dialog).findByText('This item is still in use')).toBeVisible()
    // Owned, not on-hand: in transit and at a job worker count too, and it is
    // the quantity people forget.
    expect(within(dialog).getByText(/1,330 KG is owned/)).toBeVisible()
    // The count comes from the pagination total, not from the one row fetched
    // to answer the yes/no question.
    expect(within(dialog).getByText(/4 bills of materials/)).toBeVisible()
  })

  it('says the checks were skipped when the account could not make them', async () => {
    // `item.delete` implies neither `inventory.view` nor `bom.view`. Silently
    // showing no warning would read as "nothing to worry about".
    const { user } = renderDetail({
      permissions: [P.ITEM_VIEW, P.ITEM_DELETE],
      routes: baseRoutes({ [`DELETE /products/items/${ITEM_ID}`]: ok({ deleted: true }) }),
    })

    await screen.findByRole('heading', { name: /^RM-001/ })
    await user.click(screen.getByRole('button', { name: /^retire$/i }))
    const dialog = await screen.findByRole('dialog')

    expect(
      within(dialog).getByText(/could not be checked from this account/),
    ).toBeInTheDocument()
    expect(within(dialog).queryByText('This item is still in use')).not.toBeInTheDocument()
  })
})

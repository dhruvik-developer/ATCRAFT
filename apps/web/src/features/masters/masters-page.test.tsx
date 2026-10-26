/**
 * Item master data: categories, units, conversions and the dated price list.
 *
 * Four small masters on one screen, and each has a rule that is invisible in a
 * rendering test but expensive in production. This file pins the four.
 *
 * **1. A tab is only rendered when its read right is held, and its write
 * affordances only when its write right is.** `category.view` / `.manage`,
 * `uom.view` / `.manage`, `price.view` / `.manage` are six separate rights, and
 * a control gated on the wrong one is how every authorisation defect in this
 * codebase has shipped.
 *
 * **2. A PATCH here ignores nulls.** `update_category`, `update_uom` and
 * `update_price` all loop `if value is not None`, so a text field is cleared by
 * sending `""` and a parent is promoted to the root by the dedicated
 * `reparent_to_root` flag. Sending `null` looks right, type-checks, and silently
 * does nothing - which is why the assertions below read the body rather than the
 * screen.
 *
 * **3. A conversion factor is directional.** `factor` is how many `to` units
 * make ONE `from` unit. Read the other way round it does not fail - it quietly
 * multiplies or divides every quantity derived from it. So both the grid and the
 * form have to state the equation, and both are asserted here.
 *
 * **4. Every list is server-driven.** Filters, paging and search are query
 * parameters; nothing is narrowed in the browser.
 *
 * Radix `<Select>` controls are not opened - see the note in
 * `items-page.test.tsx`: floating-ui's overflow detection costs roughly thirty
 * seconds per open under jsdom. Filters are therefore seeded through the URL,
 * which is where these controls keep their state, and both halves of the loop
 * are asserted: the URL becomes the request, and the closed control shows what
 * the URL holds.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { fail, ok, page, renderScreen } from '@/test/harness'
import { P } from '@/lib/permissions'

import MastersPage from './masters-page'

// ------------------------------------------------------------- fixtures ----

const ITEM_ID = '01a00000-0000-7000-8000-0000000000i1'

/** A two-level tree, in the path order the API returns it in. */
const CATEGORIES = [
  {
    id: 'cat-hw',
    code: 'HW',
    name: 'Hardware',
    description: 'Architectural hardware',
    parent_id: null,
    path: '/HW',
    depth: 0,
    is_active: true,
  },
  {
    id: 'cat-hinge',
    code: 'HW-HG',
    name: 'Hinges',
    description: null,
    parent_id: 'cat-hw',
    path: '/HW/HW-HG',
    depth: 1,
    is_active: true,
  },
]

const UOMS = [
  {
    id: 'uom-kg',
    code: 'KG',
    name: 'Kilogram',
    uom_category: 'WEIGHT',
    is_base: true,
    decimal_places: 3,
    is_active: true,
  },
  {
    id: 'uom-g',
    code: 'G',
    name: 'Gram',
    uom_category: 'WEIGHT',
    is_base: false,
    decimal_places: 0,
    is_active: true,
  },
]

const CONVERSIONS = [
  {
    id: 'conv-kg-g',
    from_uom_id: 'uom-kg',
    to_uom_id: 'uom-g',
    factor: '1000.00000000',
    is_active: true,
  },
]

const PRICES = [
  {
    id: 'price-sale',
    item_id: ITEM_ID,
    variant_id: null,
    price_type: 'SALE',
    currency: 'INR',
    price: '250.000000',
    min_quantity: '0.0000',
    valid_from: '2026-01-01',
    valid_to: null,
    is_active: true,
    notes: null,
  },
  {
    id: 'price-dealer',
    item_id: ITEM_ID,
    variant_id: null,
    price_type: 'DEALER',
    currency: 'INR',
    price: '230.000000',
    min_quantity: '10.0000',
    valid_from: '2026-01-01',
    valid_to: '2026-06-30',
    is_active: true,
    notes: 'Q1 dealer deal',
  },
]

/** Records the query string of every request to `path`. */
function listSpy(path: string, rows: unknown[]) {
  const queries: URLSearchParams[] = []
  return {
    routes: {
      [`GET ${path}`]: ({ url }: { url: URL }) => {
        queries.push(url.searchParams)
        return page(rows)
      },
    },
    last() {
      const params = queries[queries.length - 1]
      expect(params, `expected the screen to have queried ${path}`).toBeDefined()
      return params!
    },
  }
}

/** The body of the first request made with `method`, once it has been sent. */
async function waitForBody(
  fetch: { calls: { method: string; body: unknown }[] },
  method: string,
): Promise<Record<string, unknown>> {
  await waitFor(() => expect(fetch.calls.some((call) => call.method === method)).toBe(true))
  return fetch.calls.find((call) => call.method === method)!.body as Record<string, unknown>
}

// ------------------------------------------------------------ which tabs ----

describe('which tabs exist', () => {
  it('shows only the masters the account may read', async () => {
    // Units and conversions are both `uom.view`, so they appear together; the
    // category and price tabs have rights of their own.
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    const tabs = await screen.findAllByRole('tab')
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Units', 'Conversions'])
  })

  it('adds the category and price tabs with their own rights', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.PRICE_VIEW],
      routes: { 'GET /products/categories': page(CATEGORIES) },
    })

    const tabs = await screen.findAllByRole('tab')
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Categories', 'Prices'])
  })

  it('says so plainly when the account may read none of them', async () => {
    // An empty tab strip would read as a broken page; naming the three rights
    // tells the user what to ask an administrator for.
    renderScreen(<MastersPage />, { permissions: [] })

    expect(await screen.findByText('Nothing here for this account')).toBeVisible()
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })

  it('falls back to a readable tab when the URL names one the account cannot open', async () => {
    // A bookmarked link is shared between colleagues with different roles.
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      route: '/masters?tab=prices',
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    expect(await screen.findByRole('tab', { name: 'Units', selected: true })).toBeVisible()
  })

  it('drops one tab filters when switching to another', async () => {
    // Every tab drives its paging, search and filters through the same URL
    // keys. Carrying `status=inactive` from the category tree into the unit
    // list would apply one tab's narrowing to another tab's data.
    const units = listSpy('/products/uoms', UOMS)
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.UOM_VIEW],
      route: '/masters?tab=categories&status=inactive',
      routes: { 'GET /products/categories': page(CATEGORIES), ...units.routes },
    })

    await screen.findByText('Hinges')
    await user.click(screen.getByRole('tab', { name: 'Units' }))

    await screen.findByText('Kilogram')
    expect(units.last().has('is_active')).toBe(false)
  })
})

// ------------------------------------------------------------ categories ----

describe('the category tree', () => {
  it('renders the hierarchy the materialised path describes', async () => {
    // Rows arrive in depth-first path order and the indentation reproduces the
    // tree - but the indentation is decorative, so the level is also stated in
    // words for anyone not reading the layout.
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW],
      routes: { 'GET /products/categories': page(CATEGORIES) },
    })

    expect(await screen.findByText('Hardware')).toBeVisible()
    expect(screen.getByText('Hinges')).toBeVisible()
    expect(screen.getByText('Top level')).toBeVisible()
    // `depth` is 0-based on the wire and an operator counts from one.
    expect(screen.getByText('Level 2')).toBeVisible()
  })

  it('sends the drill-down and the scope as the server understands them', async () => {
    const categories = listSpy('/products/categories', CATEGORIES)
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW],
      route: '/masters?tab=categories&parent_id=cat-hw&status=inactive',
      routes: {
        ...categories.routes,
        'GET /products/categories/cat-hw': ok(CATEGORIES[0]),
      },
    })

    await screen.findByText('Hinges')
    const params = categories.last()
    expect(params.get('parent_id')).toBe('cat-hw')
    expect(params.get('is_active')).toBe('false')
    // `only_roots` wins over `parent_id` server-side, so the two are never sent
    // together - a drill-down that also asked for roots would silently ignore
    // the branch the user clicked into.
    expect(params.has('only_roots')).toBe(false)
  })

  it('sends only_roots for the top-level scope, and no parent with it', async () => {
    const categories = listSpy('/products/categories', [CATEGORIES[0]])
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW],
      route: '/masters?tab=categories&scope=roots',
      routes: categories.routes,
    })

    await screen.findByText('Hardware')
    expect(categories.last().get('only_roots')).toBe('true')
    expect(categories.last().has('parent_id')).toBe(false)
  })

  it('names the branch being drilled into rather than showing its id', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW],
      route: '/masters?tab=categories&parent_id=cat-hw',
      routes: {
        'GET /products/categories': page([CATEGORIES[1]]),
        'GET /products/categories/cat-hw': ok(CATEGORIES[0]),
      },
    })

    // "Inside 0192f3c1-..." tells the user nothing, and the drill-down survives
    // a bookmark, so the name has to be fetched rather than remembered.
    expect(await screen.findByRole('button', { name: /inside HW/i })).toBeVisible()
  })

  it('offers create and edit only to category.manage', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.CATEGORY_MANAGE],
      routes: { 'GET /products/categories': page(CATEGORIES) },
    })

    await screen.findByText('Hardware')
    expect(screen.getByRole('button', { name: /new category/i })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Edit HW' })).toBeVisible()
  })

  it('offers neither to a principal who may only read the tree', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW],
      routes: { 'GET /products/categories': page(CATEGORIES) },
    })

    await screen.findByText('Hardware')
    expect(screen.queryByRole('button', { name: /new category/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit HW' })).not.toBeInTheDocument()
    // Drilling in is a read, so it stays available to a reader.
    expect(screen.getByRole('button', { name: 'More actions for HW' })).toBeVisible()
  })

  it('names the parent when a child is created inside a branch', async () => {
    // The parent is always explicit on the wire. Inferring it from whatever the
    // grid happens to be filtered to is how a sub-category ends up at the root.
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.CATEGORY_MANAGE],
      route: '/masters?tab=categories&parent_id=cat-hw',
      routes: {
        // The dialog's parent picker and the grid read the same endpoint with
        // different parameters: the picker asks for the whole tree
        // (`with_total=false`), the grid for the branch the user is inside.
        'GET /products/categories': ({ url }: { url: URL }) =>
          url.searchParams.get('with_total') === 'false' ? page(CATEGORIES) : page([CATEGORIES[1]]),
        'GET /products/categories/cat-hw': ok(CATEGORIES[0]),
        'POST /products/categories': ok({ id: 'cat-knob', code: 'HW-KN', name: 'Knobs' }),
      },
    })

    await screen.findByText('Hinges')
    await user.click(screen.getByRole('button', { name: /new category/i }))
    const dialog = await screen.findByRole('dialog')

    // The user can see which parent they are creating under before they type.
    expect(within(dialog).getByRole('combobox', { name: /parent category/i })).toHaveTextContent(
      'HW - Hardware',
    )

    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'HW-KN')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Knobs')
    await user.click(within(dialog).getByRole('button', { name: /create category/i }))

    const body = await waitForBody(fetch, 'POST')
    expect(body.parent_id).toBe('cat-hw')
    expect(body.code).toBe('HW-KN')
    expect(body.name).toBe('Knobs')
    // An unentered description is null on a create, where the column is
    // genuinely nullable - unlike the PATCH below, where null means "leave it".
    expect(body.description).toBeNull()
    expect(body.is_active).toBe(true)
  })

  it('creates at the top level with a null parent when no branch is open', async () => {
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.CATEGORY_MANAGE],
      routes: {
        'GET /products/categories': page(CATEGORIES),
        'POST /products/categories': ok({ id: 'cat-fit', code: 'FIT', name: 'Fittings' }),
      },
    })

    await screen.findByText('Hardware')
    await user.click(screen.getByRole('button', { name: /new category/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'FIT')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Fittings')
    await user.click(within(dialog).getByRole('button', { name: /create category/i }))

    // On a CREATE a null parent is meaningful and correct; the sentinel the
    // Radix select needs must not reach the wire.
    expect((await waitForBody(fetch, 'POST')).parent_id).toBeNull()
  })

  it('patches an edited child with its parent restated, never with null', async () => {
    // `update_category` skips null fields, so a null `parent_id` is not "no
    // change" to the reader of this code - it is a silently dropped field. The
    // form restates the parent it holds instead.
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.CATEGORY_MANAGE],
      routes: {
        'GET /products/categories': page(CATEGORIES),
        'PATCH /products/categories/cat-hinge': ok(CATEGORIES[1]),
      },
    })

    await screen.findByText('Hinges')
    await user.click(screen.getByRole('button', { name: 'Edit HW-HG' }))
    const dialog = await screen.findByRole('dialog')

    // The code is not editable: historical reports group on it.
    expect(within(dialog).getByRole('textbox', { name: 'Code' })).toBeDisabled()

    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Butt hinges')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForBody(fetch, 'PATCH')
    expect(body.code).toBeUndefined()
    expect(body.name).toBe('Butt hinges')
    expect(body.parent_id).toBe('cat-hw')
    expect('reparent_to_root' in body).toBe(false)
  })

  it('clears a description with an empty string, which is the only thing that works', async () => {
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.CATEGORY_MANAGE],
      routes: {
        'GET /products/categories': page(CATEGORIES),
        'PATCH /products/categories/cat-hw': ok(CATEGORIES[0]),
      },
    })

    await screen.findByText('Hardware')
    await user.click(screen.getByRole('button', { name: 'Edit HW' }))
    const dialog = await screen.findByRole('dialog')
    await user.clear(within(dialog).getByRole('textbox', { name: 'Description' }))
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForBody(fetch, 'PATCH')
    expect(body.description).toBe('')
    // A top-level node keeps its place through the dedicated flag, because a
    // null parent would be dropped rather than applied.
    expect(body.reparent_to_root).toBe(true)
    expect('parent_id' in body).toBe(false)
  })

  it('puts a 422 back on the field that caused it', async () => {
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.CATEGORY_VIEW, P.CATEGORY_MANAGE],
      routes: {
        'GET /products/categories': page(CATEGORIES),
        'POST /products/categories': fail(422, 'VALIDATION_ERROR', 'Invalid category', {
          fields: { code: ['A category with this code already exists'] },
        }),
      },
    })

    await screen.findByText('Hardware')
    await user.click(screen.getByRole('button', { name: /new category/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'HW')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Hardware again')
    await user.click(within(dialog).getByRole('button', { name: /create category/i }))

    // On the field, not in a toast: the user has to change that one value, and
    // a toast leaves them guessing which of five fields is wrong.
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'A category with this code already exists',
    )
  })
})

// ----------------------------------------------------------------- units ----

describe('units of measure', () => {
  it('names the base unit of each family on the row', async () => {
    // Every conversion in a family resolves through its base unit, so which one
    // it is belongs on the row rather than buried in the edit form.
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      route: '/masters?tab=units',
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    expect(await screen.findByText('Kilogram')).toBeVisible()
    expect(screen.getByText('Base')).toBeVisible()
    expect(screen.getAllByText('Weight')).toHaveLength(2)
  })

  it('offers create and edit only to uom.manage', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=units',
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    await screen.findByText('Kilogram')
    expect(screen.getByRole('button', { name: /new unit/i })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Edit KG' })).toBeVisible()
  })

  it('offers neither to a principal who may only read them', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      route: '/masters?tab=units',
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    await screen.findByText('Kilogram')
    expect(screen.queryByRole('button', { name: /new unit/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit KG' })).not.toBeInTheDocument()
  })

  it('posts decimal places as a whole number, not a decimal string', async () => {
    // It is a count of places, not a quantity: `UomCreate.decimal_places` is an
    // integer bounded 0..6, and a string here is a 422.
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=units',
      routes: {
        'GET /products/uoms': page(UOMS),
        'POST /products/uoms': ok({ id: 'uom-pcs', code: 'PCS', name: 'Pieces' }),
      },
    })

    await screen.findByText('Kilogram')
    await user.click(screen.getByRole('button', { name: /new unit/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'PCS')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Pieces')
    const decimals = within(dialog).getByRole('spinbutton', { name: 'Decimal places' })
    await user.clear(decimals)
    await user.type(decimals, '0')
    await user.click(within(dialog).getByRole('button', { name: /create unit/i }))

    const body = await waitForBody(fetch, 'POST')
    expect(body.decimal_places).toBe(0)
    expect(body.code).toBe('PCS')
    expect(body.uom_category).toBe('COUNT')
    expect(body.is_base).toBe(false)
  })

  it('refuses more than six decimal places before the round trip', async () => {
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=units',
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    await screen.findByText('Kilogram')
    await user.click(screen.getByRole('button', { name: /new unit/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'X')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Excess')
    const decimals = within(dialog).getByRole('spinbutton', { name: 'Decimal places' })
    await user.clear(decimals)
    await user.type(decimals, '9')
    await user.click(within(dialog).getByRole('button', { name: /create unit/i }))

    expect(await within(dialog).findByText(/Between 0 and 6 decimal places/)).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('locks the base flag on the unit that already holds it', async () => {
    // The service refuses to clear it outright - a family without a base unit
    // leaves every conversion in it unresolvable - so the control is disabled
    // rather than offering a press that can only be rejected.
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=units',
      routes: { 'GET /products/uoms': page(UOMS) },
    })

    await screen.findByText('Kilogram')
    await user.click(screen.getByRole('button', { name: 'Edit KG' }))
    const dialog = await screen.findByRole('dialog')

    expect(
      within(dialog).getByRole('checkbox', { name: /base unit of this family/i }),
    ).toBeDisabled()
    expect(within(dialog).getByText(/promote another unit instead of clearing this/i)).toBeVisible()
  })

  it('patches a unit without its code or its measurement family', async () => {
    // `UomUpdate` carries neither. Moving a unit between families would orphan
    // every conversion in both, and the code is stamped on printed documents.
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=units',
      routes: {
        'GET /products/uoms': page(UOMS),
        'PATCH /products/uoms/uom-g': ok(UOMS[1]),
      },
    })

    await screen.findByText('Gram')
    await user.click(screen.getByRole('button', { name: 'Edit G' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('textbox', { name: 'Code' })).toBeDisabled()

    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Gramme')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForBody(fetch, 'PATCH')
    expect(body.name).toBe('Gramme')
    expect('code' in body).toBe(false)
    expect('uom_category' in body).toBe(false)
  })
})

// ----------------------------------------------------------- conversions ----

describe('unit conversions', () => {
  it('states the equation in both directions rather than a bare factor', async () => {
    // A factor read backwards is the most expensive mistake this screen can
    // make: nothing fails, every derived quantity is simply wrong. The resolver
    // inverts a stored pair on demand, so the operator is shown both readings.
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
      },
    })

    expect(await screen.findByText('1 KG = 1000 G')).toBeVisible()
    expect(screen.getByText('1 G = 0.001 KG')).toBeVisible()
  })

  it('names the units in the delete affordance, and offers it only to uom.manage', async () => {
    // "Remove" on its own is not a decision anyone can make; the accessible
    // name carries the equation about to disappear.
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
      },
    })

    expect(
      await screen.findByRole('button', { name: 'Remove the conversion 1 KG = 1000 G' }),
    ).toBeVisible()
  })

  it('withholds the delete affordance from a reader', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
      },
    })

    await screen.findByText('1 KG = 1000 G')
    expect(screen.queryByRole('button', { name: /remove the conversion/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new conversion/i })).not.toBeInTheDocument()
  })

  it('deletes by id and states the consequence in real units', async () => {
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
        'DELETE /products/uom-conversions/conv-kg-g': ok({ deleted: true }),
      },
    })

    await user.click(
      await screen.findByRole('button', { name: 'Remove the conversion 1 KG = 1000 G' }),
    )
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/1 KG = 1000 G/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Remove conversion' }))

    expect(fetch.calls.find((call) => call.method === 'DELETE')?.path).toBe(
      '/products/uom-conversions/conv-kg-g',
    )
  })

  it('tells the operator which way round the factor is read', async () => {
    // The whole design of this dialog is about direction. If this sentence goes
    // missing, the field becomes an unlabelled number with two plausible
    // meanings that differ by a factor of a million.
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
      },
    })

    await screen.findByText('1 KG = 1000 G')
    await user.click(screen.getByRole('button', { name: /new conversion/i }))
    const dialog = await screen.findByRole('dialog')

    const factor = within(dialog).getByRole('textbox', { name: 'Factor' })
    // Attached to the field, not merely near it: a screen reader user must get
    // the direction when they reach the input, not if they happen to explore.
    const describedBy = factor.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      'How many to units make one from unit. Kilograms to grams is 1000, not 0.001.',
    )
  })

  it('will not offer a target unit before the source family is known', async () => {
    // A conversion may only relate two units of one family, and the service
    // refuses anything else. Kilos to pieces is not a unit conversion at all.
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW, P.UOM_MANAGE],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
      },
    })

    await screen.findByText('1 KG = 1000 G')
    await user.click(screen.getByRole('button', { name: /new conversion/i }))
    const dialog = await screen.findByRole('dialog')

    expect(within(dialog).getByRole('combobox', { name: 'To unit' })).toBeDisabled()
    expect(within(dialog).getByRole('combobox', { name: 'From unit' })).toBeEnabled()
  })

  it('will not ask the server to check a conversion until it has a real question', async () => {
    // The check dialog is the authoritative reading of a factor because the
    // server answers it. An incomplete request would just be a 422.
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.UOM_VIEW],
      route: '/masters?tab=conversions',
      routes: {
        'GET /products/uom-conversions': page(CONVERSIONS),
        'GET /products/uoms': page(UOMS),
      },
    })

    await screen.findByText('1 KG = 1000 G')
    await user.click(screen.getByRole('button', { name: /check a conversion/i }))
    const dialog = await screen.findByRole('dialog')

    expect(within(dialog).getByRole('button', { name: 'Convert' })).toBeDisabled()
  })
})

// ---------------------------------------------------------------- prices ----

describe('the price list', () => {
  it('asks for an item first, because a price list belongs to one', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW],
      route: '/masters?tab=prices',
      routes: {},
    })

    expect(await screen.findByText('Choose an item')).toBeVisible()
  })

  it('explains the missing item.view right rather than showing a dead picker', async () => {
    // Searching the item master is `item.view`, a different right from the
    // `price.view` that reads this list.
    renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW],
      route: '/masters?tab=prices',
      routes: {},
    })

    expect(await screen.findByText('An item cannot be chosen here')).toBeVisible()
  })

  it('sends the channel, variant and status filters to the server', async () => {
    const prices = listSpy(`/products/items/${ITEM_ID}/prices`, PRICES)
    renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW],
      route: `/masters?tab=prices&item_id=${ITEM_ID}&price_type=DEALER&variant_id=var-brushed&status=active`,
      routes: prices.routes,
    })

    await screen.findByText('30 Jun 2026')
    expect(prices.last().get('price_type')).toBe('DEALER')
    expect(prices.last().get('variant_id')).toBe('var-brushed')
    expect(prices.last().get('is_active')).toBe('true')
  })

  it('offers edit and remove only to price.manage', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW, P.PRICE_MANAGE],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: { [`GET /products/items/${ITEM_ID}/prices`]: page(PRICES) },
    })

    // Named by what the row is, so two rows on one item are distinguishable.
    expect(
      await screen.findByRole('button', { name: 'Edit the Sale price valid from 2026-01-01' }),
    ).toBeVisible()
    expect(
      screen.getByRole('button', { name: 'Remove the Dealer price valid from 2026-01-01' }),
    ).toBeVisible()
  })

  it('offers neither to a principal who may only read prices', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: { [`GET /products/items/${ITEM_ID}/prices`]: page(PRICES) },
    })

    await screen.findByText('30 Jun 2026')
    expect(screen.queryByRole('button', { name: /^edit the /i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^remove the /i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new price/i })).not.toBeInTheDocument()
  })

  it('does not claim an unnameable variant price is item-wide', async () => {
    // Reached when the account holds `price.view` but not `item.view`, so the
    // variant list was never fetched. "Every variant" would be a false claim
    // about which orders this price applies to.
    renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: {
        [`GET /products/items/${ITEM_ID}/prices`]: page([
          { ...PRICES[0], id: 'price-var', variant_id: 'var-brushed' },
          PRICES[1],
        ]),
      },
    })

    expect(await screen.findByText('One variant')).toBeVisible()
    expect(screen.getByText('Every variant')).toBeVisible()
  })

  it('posts money as a decimal string and an absent end date as null', async () => {
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW, P.PRICE_MANAGE],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: {
        [`GET /products/items/${ITEM_ID}/prices`]: page([]),
        [`POST /products/items/${ITEM_ID}/prices`]: ok({
          id: 'price-new',
          price_type: 'SALE',
          price: '199.500000',
          currency: 'INR',
        }),
      },
    })

    await screen.findByText('No prices for this item')
    await user.click(screen.getAllByRole('button', { name: /new price/i })[0]!)
    const dialog = await screen.findByRole('dialog')

    await user.type(within(dialog).getByRole('textbox', { name: 'Price' }), '199.50')
    const currency = within(dialog).getByRole('textbox', { name: 'Currency' })
    await user.clear(currency)
    await user.type(currency, 'usd')
    await user.click(within(dialog).getByRole('button', { name: /add price/i }))

    const body = await waitForBody(fetch, 'POST')
    // Never parsed into a double anywhere on the way to the wire - the trailing
    // zero survives, which a float would not.
    expect(body.price).toBe('199.50')
    expect(body.min_quantity).toBe('0')
    expect(body.currency).toBe('USD')
    // A price with no end date runs until it is superseded; null is how the
    // create endpoint is told so.
    expect(body.valid_to).toBeNull()
    expect(body.notes).toBeNull()
    // No variant chosen means the price covers every variant of the item.
    expect(body.variant_id).toBeNull()
    expect(body.price_type).toBe('SALE')
    expect(body.valid_from).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('patches without the three fields that define which price a row is', async () => {
    // `PriceUpdate` carries no `price_type`, `currency` or `variant_id`.
    // Changing them would re-point a price at another channel, and quotations
    // already raised on it would stop being explicable.
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW, P.PRICE_MANAGE],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: {
        [`GET /products/items/${ITEM_ID}/prices`]: page(PRICES),
        'PATCH /products/prices/price-sale': ok(PRICES[0]),
      },
    })

    await user.click(
      await screen.findByRole('button', { name: 'Edit the Sale price valid from 2026-01-01' }),
    )
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('combobox', { name: 'Channel' })).toBeDisabled()
    expect(within(dialog).getByRole('textbox', { name: 'Currency' })).toBeDisabled()

    const amount = within(dialog).getByRole('textbox', { name: 'Price' })
    await user.clear(amount)
    await user.type(amount, '275.25')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = await waitForBody(fetch, 'PATCH')
    expect(body.price).toBe('275.25')
    expect('price_type' in body).toBe(false)
    expect('currency' in body).toBe(false)
    expect('variant_id' in body).toBe(false)
    // An empty note is cleared with "", because a null would be dropped.
    expect(body.notes).toBe('')
    // An open-ended row stays open-ended by omitting the field rather than
    // sending a null that would suggest it had been cleared.
    expect('valid_to' in body).toBe(false)
  })

  it('refuses to reopen a closed price instead of appearing to', async () => {
    // `update_price` applies only non-null fields, so an end date that has been
    // set cannot be removed through this endpoint. Letting the emptied field
    // submit would look like it worked and change nothing.
    const { user, fetch } = renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW, P.PRICE_MANAGE],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: {
        [`GET /products/items/${ITEM_ID}/prices`]: page(PRICES),
        'PATCH /products/prices/price-dealer': ok(PRICES[1]),
      },
    })

    await user.click(
      await screen.findByRole('button', { name: 'Edit the Dealer price valid from 2026-01-01' }),
    )
    const dialog = await screen.findByRole('dialog')
    await user.clear(within(dialog).getByLabelText(/valid to/i))
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'This endpoint cannot reopen a closed price',
    )
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })

  it('surfaces an overlap rejection the form cannot predict', async () => {
    // Two active rows may not overlap on channel, variant, slab and dates at
    // once. The clash is a business rule with no field to land on, so it must
    // not be swallowed by the field-error handler.
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.PRICE_VIEW, P.PRICE_MANAGE],
      route: `/masters?tab=prices&item_id=${ITEM_ID}`,
      routes: {
        [`GET /products/items/${ITEM_ID}/prices`]: page(PRICES),
        'PATCH /products/prices/price-sale': fail(
          409,
          'CONFLICT',
          'An active SALE price already covers 2026-01-01 for this slab',
        ),
      },
    })

    await user.click(
      await screen.findByRole('button', { name: 'Edit the Sale price valid from 2026-01-01' }),
    )
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    // The dialog stays open so the entered values survive and the user can
    // change the window rather than retyping the row.
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /save changes/i })).toBeVisible(),
    )
  })
})

// ------------------------------------------------- brands and manufacturers ----

const MAKER_ROWS = [
  {
    id: 'maker-own',
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
]

const BRAND_ROWS = [
  {
    id: 'brand-house',
    code: 'ATCRAFT',
    name: 'Atcraft House Brand',
    description: 'The house brand.',
    manufacturer_id: 'maker-own',
    sort_order: 0,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  },
]

describe('the brand and manufacturer tabs', () => {
  it('appear with their own read rights, after the attributes and before prices', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.ITEM_ATTRIBUTE_VIEW, P.BRAND_VIEW, P.MANUFACTURER_VIEW, P.PRICE_VIEW],
      allowUnrouted: true,
    })

    const tabs = await screen.findAllByRole('tab')
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      'Attributes',
      'Specifications',
      'Brands',
      'Manufacturers',
      'Prices',
    ])
  })

  it('open for a holder of brand.view alone, and list the server rows', async () => {
    const brands = listSpy('/products/brands', BRAND_ROWS)
    renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW],
      route: '/masters?tab=brands',
      routes: brands.routes,
    })

    expect(await screen.findByRole('tab', { name: 'Brands', selected: true })).toBeVisible()
    expect(await screen.findByText('Atcraft House Brand')).toBeVisible()
    // No sort was asked for, so the server's own picker order applies.
    expect(brands.last().has('sort_by')).toBe(false)
  })

  it('names the owner only for an account that may read manufacturers', async () => {
    const { fetch } = renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW],
      route: '/masters?tab=brands',
      routes: { 'GET /products/brands': page(BRAND_ROWS) },
    })

    expect(await screen.findByText('Not in the active list')).toBeVisible()
    expect(fetch.calls.map((call) => call.path)).not.toContain('/products/manufacturers')
  })

  it('resolves the owner when the account may read manufacturers', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW, P.MANUFACTURER_VIEW],
      route: '/masters?tab=brands',
      routes: {
        'GET /products/brands': page(BRAND_ROWS),
        'GET /products/manufacturers': page(MAKER_ROWS),
      },
    })

    // Once in the owner cell; the owner filter's closed control shows "Any owner".
    expect(await screen.findByText('Atcraft Works')).toBeVisible()
  })

  it('offers New brand and the row actions only to brand.manage', async () => {
    const reader = renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW],
      route: '/masters?tab=brands',
      routes: { 'GET /products/brands': page(BRAND_ROWS) },
    })
    await screen.findByText('Not in the active list')
    expect(screen.queryByRole('button', { name: /new brand/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit ATCRAFT' })).not.toBeInTheDocument()
    reader.unmount()

    renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW, P.BRAND_MANAGE],
      route: '/masters?tab=brands',
      routes: { 'GET /products/brands': page(BRAND_ROWS) },
    })
    expect(await screen.findByRole('button', { name: 'Edit ATCRAFT' })).toBeVisible()
    expect(screen.getAllByRole('button', { name: /new brand/i }).length).toBeGreaterThan(0)
  })

  it('sends the status filter from the URL to the server', async () => {
    const brands = listSpy('/products/brands', BRAND_ROWS)
    renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW],
      route: '/masters?tab=brands&status=inactive',
      routes: brands.routes,
    })

    await screen.findByText('Not in the active list')
    expect(brands.last().get('is_active')).toBe('false')
  })

  it('sorts on the whitelisted columns and offers no sort on the owner', async () => {
    const brands = listSpy('/products/brands', BRAND_ROWS)
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW],
      route: '/masters?tab=brands',
      routes: brands.routes,
    })

    await screen.findByText('Not in the active list')
    const owner = screen.getByRole('columnheader', { name: /owner/i })
    expect(within(owner).queryByRole('button')).not.toBeInTheDocument()

    const name = screen.getByRole('columnheader', { name: /name/i })
    await user.click(within(name).getByRole('button'))
    await waitFor(() => expect(brands.last().get('sort_by')).toBe('name'))
  })

  it('retires through the row menu and shows the in-use refusal verbatim', async () => {
    const refusal =
      'Brand ATCRAFT is still carried by live items, so it cannot be retired. Move those items to another brand first.'
    const reasons: (string | null)[] = []
    const { user } = renderScreen(<MastersPage />, {
      permissions: [P.BRAND_VIEW, P.BRAND_MANAGE],
      route: '/masters?tab=brands',
      routes: {
        'GET /products/brands': page(BRAND_ROWS),
        'DELETE /products/brands/brand-house': ({ url }) => {
          reasons.push(url.searchParams.get('reason'))
          return fail(422, 'REFERENCED_RECORD_IN_USE', refusal)
        },
      },
    })

    await user.click(await screen.findByRole('button', { name: 'More actions for ATCRAFT' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Retire brand' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/reason/i), 'Rebranding')
    await user.click(within(dialog).getByRole('button', { name: 'Retire brand' }))

    expect(await within(dialog).findByText(refusal)).toBeVisible()
    expect(reasons).toEqual(['Rebranding'])
  })

  it('lists manufacturers for a holder of manufacturer.view alone', async () => {
    const makers = listSpy('/products/manufacturers', MAKER_ROWS)
    renderScreen(<MastersPage />, {
      permissions: [P.MANUFACTURER_VIEW],
      route: '/masters?tab=manufacturers',
      routes: makers.routes,
    })

    expect(await screen.findByRole('tab', { name: 'Manufacturers', selected: true })).toBeVisible()
    expect(await screen.findByText('Atcraft Works')).toBeVisible()
    expect(screen.queryByRole('button', { name: /new manufacturer/i })).not.toBeInTheDocument()
  })

  it('offers New manufacturer to manufacturer.manage', async () => {
    renderScreen(<MastersPage />, {
      permissions: [P.MANUFACTURER_VIEW, P.MANUFACTURER_MANAGE],
      route: '/masters?tab=manufacturers',
      routes: { 'GET /products/manufacturers': page(MAKER_ROWS) },
    })

    expect(
      (await screen.findAllByRole('button', { name: /new manufacturer/i })).length,
    ).toBeGreaterThan(0)
  })
})

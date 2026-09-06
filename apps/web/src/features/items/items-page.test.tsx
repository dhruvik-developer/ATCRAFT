/**
 * The item master list.
 *
 * The property this file exists to protect is the one master rules 31/36 state
 * and that a screen test is uniquely able to check: **every filter on this
 * screen is a query parameter, and the browser never receives more than the page
 * it asked for.** A list that quietly fetched everything and narrowed it in
 * `useMemo` would look identical on screen and identical in a snapshot; the only
 * thing that gives it away is the request, so nearly every assertion below reads
 * `fetch.calls` rather than the DOM.
 *
 * Second, permissions. Six authorisation defects have shipped in this codebase
 * and every one was a control gated on the wrong right. The harness runs the
 * real `AuthProvider`, so the paired renders here - once with a right, once
 * without - are genuinely testing what the user would be allowed to see.
 *
 * **Why the `<Select>` filters are driven through the URL rather than clicked.**
 * Every `SelectContent` in this application renders at `position="popper"`, and
 * floating-ui's overflow detection costs ~30 seconds *per open* under jsdom
 * (measured: `computePosition` with a single `flip()` middleware takes 13s on
 * this runner, and Radix runs four such middlewares). Opening one control would
 * cost more than this whole file. So the filter tests seed the URL - which is
 * where these controls keep their state anyway - and assert both halves of the
 * loop: that the URL becomes the request, and that the closed control shows the
 * value the URL holds.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { fail, ok, page, renderScreen } from '@/test/harness'
import { P } from '@/lib/permissions'

import ItemsPage from './items-page'

// ------------------------------------------------------------- fixtures ----

const BRASS_ROD = {
  id: '01a00000-0000-7000-8000-0000000000i1',
  code: 'RM-001',
  name: 'Brass rod 12mm',
  item_group: 'RAW_MATERIAL',
  category_id: 'cat-hardware',
  base_uom_id: 'uom-kg',
  production_type: 'BUY',
  sale_price: '250.0000',
  is_active: true,
  standard_cost: '80.5000',
  can_purchase: true,
  can_manufacture: false,
  can_sell: false,
  can_be_bom_component: true,
  can_be_assembly_component: false,
  can_be_spare: false,
  can_job_work: false,
}

const CATEGORIES = page([
  { id: 'cat-hardware', code: 'HW', name: 'Hardware', path: '/HW', depth: 0, is_active: true },
])
const UOMS = page([
  { id: 'uom-kg', code: 'KG', name: 'Kilogram', decimal_places: 3, is_active: true },
])

/** Records the query string of every list request so a test can read it back. */
function listSpy(rows: unknown[] = [BRASS_ROD]) {
  const queries: URLSearchParams[] = []
  return {
    queries,
    handler: ({ url }: { url: URL }) => {
      queries.push(url.searchParams)
      return page(rows)
    },
    /** The most recent request's parameters, asserted to exist. */
    last() {
      const params = queries[queries.length - 1]
      expect(params, 'expected the screen to have queried /products/items').toBeDefined()
      return params!
    },
  }
}

// ------------------------------------------------------------- rendering ----

describe('the grid', () => {
  it('links each row to its own detail page', async () => {
    // A real link, not only a row click: this screen is driven from the
    // keyboard all day, and a `<tr onClick>` alone is unreachable that way.
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    const link = await screen.findByRole('link', { name: 'RM-001' })
    expect(link).toHaveAttribute('href', `/items/${BRASS_ROD.id}`)
  })

  it('resolves category and unit ids into names when both lookups are permitted', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.CATEGORY_VIEW, P.UOM_VIEW],
      routes: {
        'GET /products/items': page([BRASS_ROD]),
        'GET /products/categories': CATEGORIES,
        'GET /products/uoms': UOMS,
      },
    })

    // The list projection carries bare ids. If the join broke, the cells would
    // read "-" on every row rather than fail loudly.
    expect(await screen.findByText('Hardware')).toBeVisible()
    expect(screen.getByText('KG')).toBeVisible()
  })

  it('asks for neither lookup when the account cannot read them', async () => {
    // `category.view` and `uom.view` are separate rights from `item.view`. A
    // storekeeper holding only the latter would earn two 403s they can do
    // nothing about, and the harness turns an unrouted request into a failure -
    // so the absence of these two routes IS the assertion.
    const { fetch } = renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(fetch.calls.map((call) => call.path)).not.toContain('/products/categories')
    expect(fetch.calls.map((call) => call.path)).not.toContain('/products/uoms')
  })

  it('gives the capability icons a text alternative', async () => {
    // Six glyphs in a 3.5mm column are unreadable to a screen reader and
    // ambiguous to everyone else; the row states the enabled ones in words.
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    expect(await screen.findByText('Purchasable, BOM component')).toBeInTheDocument()
  })

  it('says nothing matched, and offers the way out, when the page is empty', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.ITEM_CREATE],
      routes: { 'GET /products/items': page([]) },
    })

    expect(await screen.findByText('No items found')).toBeVisible()
    // Two "New item" buttons - the header's and the empty state's - and the
    // point is that an empty grid is not a dead end.
    expect(screen.getAllByRole('button', { name: /new item/i }).length).toBeGreaterThan(1)
  })

  it('surfaces the API sentence when the list fails, with a way to retry', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: {
        'GET /products/items': fail(503, 'SERVICE_UNAVAILABLE', 'The database is failing over'),
      },
    })

    // The server's own wording, not "request failed": it is the only thing that
    // tells the user whether waiting will help.
    expect(await screen.findByText('The database is failing over')).toBeVisible()
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })
})

// ------------------------------------------------------------ permissions ----

describe('permission gating', () => {
  it('offers New item to a principal holding item.create', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.ITEM_CREATE],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.getByRole('button', { name: /new item/i })).toBeVisible()
  })

  it('withholds it from a principal who may only view', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.queryByRole('button', { name: /new item/i })).not.toBeInTheDocument()
  })

  it('shows the standard cost column to a principal holding item.cost_view', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.ITEM_COST_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    expect(await screen.findByRole('columnheader', { name: 'Standard cost' })).toBeVisible()
    expect(screen.getByText(/80\.50/)).toBeVisible()
  })

  it('hides the column, and the figure, from everyone else', async () => {
    // The API withholds `standard_cost` from such a principal anyway, so the
    // column would be blank on every row. Rendering the column regardless would
    // read as "this item has no cost", which is a different and wrong claim.
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.queryByRole('columnheader', { name: 'Standard cost' })).not.toBeInTheDocument()
    expect(screen.queryByText(/80\.50/)).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------- filters ----

describe('filters are sent to the server', () => {
  it('defaults to active items only, because that is what the endpoint answers', async () => {
    const list = listSpy()
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': list.handler },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    // `is_active` is two-valued server-side and always defaults to active.
    // Omitting it here would show a retired item on the default view.
    expect(list.last().get('is_active')).toBe('true')
    expect(list.last().get('page')).toBe('1')
    expect(list.last().get('limit')).toBe('50')
  })

  it('passes category, group and status straight through as query parameters', async () => {
    const list = listSpy()
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      route: '/items?category_id=cat-hardware&item_group=COMPONENT&is_active=false',
      routes: { 'GET /products/items': list.handler },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    const params = list.last()
    // The ids are ids, not names, and the enum is the wire value rather than
    // the humanised label the control displays.
    expect(params.get('category_id')).toBe('cat-hardware')
    expect(params.get('item_group')).toBe('COMPONENT')
    expect(params.get('is_active')).toBe('false')
  })

  it('reflects the active filter back on the closed control', async () => {
    // The other half of the loop: a bookmarked or shared URL must not leave the
    // toolbar reading "All groups" while the grid shows a narrowed list.
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      route: '/items?item_group=COMPONENT&is_active=false',
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.getByRole('combobox', { name: 'Filter by item group' })).toHaveTextContent(
      'Component',
    )
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toHaveTextContent(
      'Inactive only',
    )
  })

  it('sends several capability flags at once, because they are independent', async () => {
    // Master rules 1: the nine flags are independent booleans, so "purchasable
    // AND manufacturable" is a real question. Anything that collapsed them into
    // one discriminator would send only the last.
    const list = listSpy()
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      route: '/items?can_purchase=true&can_manufacture=true&can_job_work=true',
      routes: { 'GET /products/items': list.handler },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    const params = list.last()
    expect(params.get('can_purchase')).toBe('true')
    expect(params.get('can_manufacture')).toBe('true')
    expect(params.get('can_job_work')).toBe('true')
    // A flag that is off is absent, not `false`: sending false would ask the
    // API for items that *cannot* be sold, which is a different question.
    expect(params.has('can_sell')).toBe(false)
  })

  it('counts the active filters on the capability control', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      route: '/items?can_purchase=true&can_manufacture=true',
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.getByRole('button', { name: /capabilities/i })).toHaveTextContent('2')
  })

  it('hides Clear while nothing is filtered', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.queryByRole('button', { name: /^clear$/i })).not.toBeInTheDocument()
  })

  it('offers Clear once a filter is active', async () => {
    renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      route: '/items?item_group=COMPONENT',
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(screen.getByRole('button', { name: /^clear$/i })).toBeVisible()
  })

  it('sends the search term to the server and returns to page 1', async () => {
    // Searching a table of tens of thousands of rows in the browser is the
    // pattern master rules 31 forbids; and staying on page 7 of a result set
    // that no longer has seven pages shows the user an empty grid.
    const list = listSpy()
    const { user } = renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW],
      route: '/items?page=7',
      routes: { 'GET /products/items': list.handler },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    expect(list.last().get('page')).toBe('7')

    await user.type(screen.getByRole('textbox', { name: /search code or name/i }), 'brass')

    await waitFor(() => expect(list.last().get('search')).toBe('brass'))
    expect(list.last().get('page')).toBe('1')
  })
})

// ----------------------------------------------------------- create dialog ----

describe('the create dialog', () => {
  it('opens only for a principal who may create, and asks for the code', async () => {
    const { user } = renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.ITEM_CREATE],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    await user.click(screen.getByRole('button', { name: /new item/i }))

    const dialog = await screen.findByRole('dialog')
    // The code is writable here and nowhere else - the edit dialog disables it,
    // because the code is printed on documents that have already left.
    expect(within(dialog).getByRole('textbox', { name: 'Code' })).toBeEnabled()
  })

  it('refuses to post without a base unit rather than letting the server say so', async () => {
    // Every item needs one, and an item created without it would be unusable on
    // any document. Catching it here saves a round trip and keeps the message
    // on the field.
    const { user, fetch } = renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.ITEM_CREATE],
      routes: { 'GET /products/items': page([BRASS_ROD]) },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    await user.click(screen.getByRole('button', { name: /new item/i }))
    const dialog = await screen.findByRole('dialog')

    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'RM-002')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Brass rod 14mm')
    await user.click(within(dialog).getByRole('button', { name: /create item/i }))

    expect(await within(dialog).findByText('Base unit is required')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('sends the chosen finish as an id, and omits the ones left unset', async () => {
    // The three were free text and API-only before the attribute master landed,
    // so the form had no way to set them at all: a client could define their
    // finishes and then not use them. What matters here is the shape on the
    // wire - an id, or absent - because a "No finish" option that posted an
    // empty string would be a foreign key the server has to reject.
    const { user, fetch } = renderScreen(<ItemsPage />, {
      permissions: [P.ITEM_VIEW, P.ITEM_CREATE, P.UOM_VIEW, P.ITEM_ATTRIBUTE_VIEW],
      routes: {
        'GET /products/items': page([BRASS_ROD]),
        'GET /products/uoms': UOMS,
        'GET /products/attributes': page([
          {
            id: 'att-brushed',
            attribute: 'FINISH',
            code: 'BRUSHED',
            name: 'Brushed',
            sort_order: 0,
            is_active: true,
          },
        ]),
        'POST /products/items': ok(BRASS_ROD),
      },
    })

    await screen.findByRole('link', { name: 'RM-001' })
    await user.click(screen.getByRole('button', { name: /new item/i }))
    const dialog = await screen.findByRole('dialog')

    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'RM-003')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Brass rod 16mm')

    await user.click(within(dialog).getByRole('combobox', { name: /base unit/i }))
    await user.click(await screen.findByRole('option', { name: /KG/ }))

    await user.click(within(dialog).getByRole('combobox', { name: /^finish$/i }))
    await user.click(await screen.findByRole('option', { name: 'Brushed' }))

    await user.click(within(dialog).getByRole('button', { name: /create item/i }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/products/items'))) as Record<
      string,
      unknown
    >

    expect(body.finish_id).toBe('att-brushed')
    // Left on "No size" - null, not '' - so the server stores no reference
    // rather than refusing a foreign key that cannot resolve.
    expect(body.size_id).toBeNull()
    expect(body.colour_id).toBeNull()
  })
})

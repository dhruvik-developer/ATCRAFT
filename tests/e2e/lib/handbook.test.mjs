import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createScreenshotLookup,
  findHandbookDetailLink,
  handbookDetailListRoute,
  handbookRouteShape,
  handbookTemplateCategory,
  handbookViewDelayMs,
  resolveHandbookDetailRoute,
} from './handbook.mjs'

const categories = { route: '/masters?tab=categories', file: 'categories.png' }
const organisation = { route: '/admin/organisation', file: 'company.png' }
const originalImages = [categories, organisation]

for (const route of [
  '/masters?tab=brands',
  '/masters?tab=manufacturers',
  '/admin/organisation?tab=cost-centres',
  '/admin/organisation?tab=financial-years',
]) {
  test(`does not illustrate ${route} using another tab`, () => {
    assert.equal(createScreenshotLookup(originalImages)(route), null)
  })
}

test('uses an exact tab capture and preserves its state metadata', () => {
  const brands = { route: '/masters?tab=brands', file: 'brands.png', state: 'Captured Brands tab' }
  const lookup = createScreenshotLookup([...originalImages, brands])
  assert.equal(lookup(brands.route), brands)
  assert.equal(lookup(brands.route).state, 'Captured Brands tab')
})

test('only the intentional default Masters view can use its Categories image', () => {
  const lookup = createScreenshotLookup(originalImages)
  assert.equal(lookup('/masters'), categories)
  assert.equal(lookup('/admin/organisation'), organisation)
  assert.equal(lookup('/missing'), null)
  const explicit = { route: '/masters', file: 'default.png' }
  assert.equal(createScreenshotLookup([...originalImages, explicit])('/masters'), explicit)
})

test('normalises UUIDs and parameter aliases while preserving tab identity', () => {
  const shot = {
    route: '/items/01234567-89ab-cdef-0123-456789abcdef?tab=inventory',
    file: 'inventory.png',
  }
  const lookup = createScreenshotLookup([shot])
  assert.equal(handbookRouteShape(shot.route), '/items/:id?tab=inventory')
  assert.equal(handbookRouteShape('/items/:itemId?tab=inventory'), '/items/:id?tab=inventory')
  assert.equal(lookup('/items/:itemId?tab=inventory'), shot)
  assert.equal(lookup('/items/:id?tab=inventory'), shot)
  assert.equal(lookup('/items/:id?tab=prices'), null)
  assert.equal(lookup('/items/:id'), null)
})

test('retains the first captured view and the simulated authentication state', () => {
  const simulated = {
    route: '/change-password',
    file: 'temporary.png',
    state: 'Restricted identity simulated in this browser; password unchanged',
  }
  const later = { ...simulated, file: 'later.png' }
  assert.equal(createScreenshotLookup([simulated, later])('/change-password'), simulated)
})

test('uses the detail link identity with the original template query', () => {
  assert.equal(
    resolveHandbookDetailRoute('/items/:itemId?tab=inventory', '/items/record-id?previous=ignored'),
    '/items/record-id?tab=inventory',
  )
  assert.equal(
    resolveHandbookDetailRoute('/items/:itemId', '/items/record-id?previous=ignored'),
    '/items/record-id',
  )
})

test('selects a populated product on staff and dealer lists while preserving template tabs', () => {
  for (const [template, list] of [
    ['/items/:itemId?tab=specifications', '/items'],
    ['/items/:itemId?tab=accessories', '/items'],
    ['/dealer/catalog/:itemId', '/dealer/catalog'],
  ]) {
    assert.equal(
      handbookDetailListRoute(template, list, ' DM-FG-LEVER-AB '),
      `${list}?q=DM-FG-LEVER-AB`,
    )
  }
})

test('leaves unrelated lists and unspecified product selection unchanged', () => {
  assert.equal(handbookDetailListRoute('/sales-orders/:id', '/sales-orders', 'FG'), '/sales-orders')
  assert.equal(handbookDetailListRoute('/items/:id', '/items', ''), '/items')
  assert.equal(handbookDetailListRoute('/items/:id', '/items'), '/items')
})

test('selects the same revision through staff and vendor lists without filtering unrelated records', () => {
  for (const list of ['/procurement/purchase-orders', '/vendor/orders']) {
    assert.equal(
      handbookDetailListRoute(`${list}/:id`, `${list}?page=2&search=old`, 'ITEM', ' PO/2026-27/00021 '),
      `${list}?q=PO%2F2026-27%2F00021`,
    )
  }
  assert.equal(handbookDetailListRoute('/sales-orders/:id', '/sales-orders', 'ITEM', 'PO'), '/sales-orders')
  assert.equal(handbookDetailListRoute('/items/:id', '/items', 'ITEM', 'PO'), '/items?q=ITEM')
})

test('encodes product search characters and resets an obsolete list page', () => {
  assert.equal(
    handbookDetailListRoute('/items/:id', '/items?page=3&search=old', 'A&B /+'),
    '/items?q=A%26B+%2F%2B',
  )
})

test('requires the selected product code in the actual rendered staff/dealer link', () => {
  const first = { href: '/items/01234567-89ab-cdef-0123-456789abcdef', text: 'OTHER-CODE' }
  const wanted = { href: '/items/11234567-89ab-cdef-0123-456789abcdef', text: 'DM-FG-LEVER-AB' }
  assert.equal(findHandbookDetailLink([first, wanted], '/items/', 'DM-FG-LEVER-AB'), wanted.href)
  assert.equal(findHandbookDetailLink([first], '/items/', 'DM-FG-LEVER-AB'), undefined)
  assert.equal(findHandbookDetailLink([first], '/items/'), first.href)
  const dealer = {
    ...wanted,
    href: wanted.href.replace('/items/', '/dealer/catalog/'),
    text: 'DM-FG-LEVER-AB\nLever Handle',
  }
  assert.equal(findHandbookDetailLink([dealer], '/dealer/catalog/', 'DM-FG-LEVER-AB'), dealer.href)
})

test('refuses partial product codes and non-detail links', () => {
  const similar = { href: '/items/01234567-89ab-cdef-0123-456789abcdef', text: 'DM-FG-LEVER-AB-2' }
  assert.equal(findHandbookDetailLink([similar], '/items/', 'DM-FG-LEVER-AB'), undefined)
  assert.equal(
    findHandbookDetailLink(
      [{ href: '/items/new', text: 'DM-FG-LEVER-AB' }],
      '/items/',
      'DM-FG-LEVER-AB',
    ),
    undefined,
  )
  assert.equal(
    findHandbookDetailLink(
      [{ ...similar, href: '/elsewhere/01234567-89ab-cdef-0123-456789abcdef' }],
      '/items/',
    ),
    undefined,
  )
})

test('selects a template illustration only for the category-template view', () => {
  assert.equal(
    handbookTemplateCategory('/masters?view=templates&tab=specifications', ' HANDLE-LEVER '),
    'HANDLE-LEVER',
  )
  assert.equal(handbookTemplateCategory('/masters?tab=specifications', 'HANDLE-LEVER'), null)
  assert.equal(
    handbookTemplateCategory('/items?tab=specifications&view=templates', 'HANDLE-LEVER'),
    null,
  )
  assert.equal(handbookTemplateCategory('/masters?tab=specifications&view=templates', ' '), null)
})

test('defaults capture pacing to zero and accepts bounded milliseconds', () => {
  assert.equal(handbookViewDelayMs(), 0)
  assert.equal(handbookViewDelayMs(' '), 0)
  assert.equal(handbookViewDelayMs('0'), 0)
  assert.equal(handbookViewDelayMs(' 6000 '), 6000)
  assert.equal(handbookViewDelayMs('60000'), 60_000)
})

test('refuses invalid capture pacing instead of silently flooding the API', () => {
  for (const value of ['-1', '1.5', '1e3', 'Infinity', 'NaN', '60001', '9007199254740992'])
    assert.throws(() => handbookViewDelayMs(value), /HANDBOOK_VIEW_DELAY_MS/)
})

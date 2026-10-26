import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createScreenshotLookup,
  handbookDetailListRoute,
  handbookRouteShape,
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
      `${list}?search=DM-FG-LEVER-AB`,
    )
  }
})

test('leaves unrelated lists and unspecified product selection unchanged', () => {
  assert.equal(handbookDetailListRoute('/sales-orders/:id', '/sales-orders', 'FG'), '/sales-orders')
  assert.equal(handbookDetailListRoute('/items/:id', '/items', ''), '/items')
  assert.equal(handbookDetailListRoute('/items/:id', '/items'), '/items')
})

test('encodes product search characters and resets an obsolete list page', () => {
  assert.equal(
    handbookDetailListRoute('/items/:id', '/items?page=3&search=old', 'A&B /+'),
    '/items?search=A%26B+%2F%2B',
  )
})

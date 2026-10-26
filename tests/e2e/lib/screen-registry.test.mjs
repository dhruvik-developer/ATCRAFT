import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

import {
  detailScreenSources,
  parseScreenRoutes,
  registeredScreens,
  staticScreenRoutes,
} from './screen-registry.mjs'
import { routeAudience } from './audiences.mjs'

test('static coverage includes staff and both portal audiences, retaining public shared pages', () => {
  const result = staticScreenRoutes([
    '/items',
    '/vendor',
    '/dealer/orders',
    '/dealer/orders/:id',
    '/change-password',
    '/profile',
  ])
  assert.deepEqual(result, ['/items', '/vendor', '/dealer/orders', '/profile'])
})

test('the actual application catalogue includes all new portal and inbound screens', () => {
  const staticRoutes = staticScreenRoutes(registeredScreens)
  for (const route of [
    '/vendor',
    '/vendor/shipments',
    '/vendor/receipts',
    '/vendor/invoices',
    '/vendor/payments',
    '/dealer',
    '/dealer/account',
    '/dealer/returns',
    '/dealer/wishlist',
    '/promotions',
    '/procurement/inbound-tracking',
  ])
    assert.ok(staticRoutes.includes(route), `${route} is missing`)
  assert.ok(registeredScreens.includes('/dealer/orders/:orderId'))
})

test('a missing or duplicated registry fails rather than scanning an empty application', () => {
  assert.throws(() => parseScreenRoutes(''), /empty or contains duplicate/)
  assert.throws(() => parseScreenRoutes("  '/': () => home\n  '/': () => other"), /duplicate/)
})

test('every dynamic screen resolves from a real GET collection in its own API audience', () => {
  const contract = JSON.parse(
    readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8'),
  )
  const loginPath = Object.keys(contract.paths).find((path) => path.endsWith('/auth/login'))
  assert.ok(loginPath)
  const prefix = loginPath.slice(0, -'/auth/login'.length)
  const dynamic = registeredScreens.filter((route) => route.includes(':'))
  assert.deepEqual(Object.keys(detailScreenSources).sort(), dynamic.sort())
  for (const [route, source] of Object.entries(detailScreenSources)) {
    const operation = contract.paths[`${prefix}${source}`]?.get
    assert.ok(operation, `${route} uses a missing GET collection: ${source}`)
    const audience = routeAudience(route)
    if (audience !== 'staff')
      assert.ok(source.startsWith(`/${audience}-portal/`), `${route} crosses API audience`)
  }
})

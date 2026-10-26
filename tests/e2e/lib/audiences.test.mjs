import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

import { accountForAudience, portalIdentityPath, routeAudience } from './audiences.mjs'

test('portal routes use bound principals; similarly named staff routes remain staff', () => {
  for (const route of ['/vendor', '/vendor/orders/:id', '/vendor/invoices?unpaid_only=true'])
    assert.equal(routeAudience(route), 'vendor')
  for (const route of ['/dealer', '/dealer/catalog', '/dealer/orders/:id'])
    assert.equal(routeAudience(route), 'dealer')
  for (const route of ['/vendors', '/dealers', '/profile', '/notifications', '/vendor-invoices'])
    assert.equal(routeAudience(route), 'staff')
})

test('missing portal credentials fail explicitly instead of falling back to administrator', () => {
  assert.throws(() => accountForAudience('dealer', {}), /E2E_DEALER_EMAIL and E2E_DEALER_PASSWORD/)
  assert.throws(
    () => accountForAudience('vendor', { E2E_VENDOR_EMAIL: 'vendor@example.com' }),
    /E2E_VENDOR_PASSWORD/,
  )
  assert.throws(() => accountForAudience('unknown', {}), /Unknown screen audience/)
})

test('each portal has independent credentials and no administrator bootstrap fallback', () => {
  const environment = {
    E2E_VENDOR_EMAIL: 'vendor@example.com',
    E2E_VENDOR_PASSWORD: 'private-test-value',
    E2E_DEALER_EMAIL: 'dealer@example.com',
    E2E_DEALER_PASSWORD: 'another-test-value',
    E2E_DEALER_BOOTSTRAP_PASSWORD: 'initial-test-value',
  }
  assert.deepEqual(accountForAudience('vendor', environment), {
    email: environment.E2E_VENDOR_EMAIL,
    password: environment.E2E_VENDOR_PASSWORD,
    bootstrapPassword: '',
  })
  assert.equal(accountForAudience('dealer', environment).bootstrapPassword, 'initial-test-value')
})

test('cookie-restored portal home identity proofs use actual GET routes in the API contract', () => {
  const contract = JSON.parse(
    readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8'),
  )
  const loginPath = Object.keys(contract.paths).find((path) => path.endsWith('/auth/login'))
  assert.ok(loginPath)
  const prefix = loginPath.slice(0, -'/auth/login'.length)
  for (const audience of ['vendor', 'dealer'])
    assert.ok(contract.paths[`${prefix}${portalIdentityPath(audience)}`]?.get)
  assert.throws(() => portalIdentityPath('staff'), /Unknown portal audience/)
})

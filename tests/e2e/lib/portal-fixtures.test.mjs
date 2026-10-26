import assert from 'node:assert/strict'
import test from 'node:test'

import { assertPortalData, ensurePortalAccounts } from './portal-fixtures.mjs'

const environment = {
  E2E_VENDOR_EMAIL: 'vendor@example.com',
  E2E_VENDOR_PASSWORD: 'test-only-vendor',
  E2E_DEALER_EMAIL: 'dealer@example.com',
  E2E_DEALER_PASSWORD: 'test-only-dealer',
}
const roles = [
  { id: 'vendor-role', code: 'VENDOR_OWNER' },
  { id: 'dealer-role', code: 'DEALER_OWNER' },
]

function stub(t, handler) {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const parsed = new URL(url)
    const body = init.body ? JSON.parse(init.body) : undefined
    calls.push({
      path: parsed.pathname,
      method: init.method,
      body,
      authorization: init.headers.Authorization,
    })
    const data = await handler(parsed, init, body)
    return Response.json({ success: true, data })
  })
  return calls
}

test('existing bound test users are reused without password changes', async (t) => {
  const calls = stub(t, (url) =>
    url.pathname.endsWith('/roles')
      ? roles
      : [
          {
            email: url.searchParams.get('search'),
            vendor_id: 'vendor',
            customer_id: 'customer',
            is_active: true,
          },
        ],
  )
  await ensurePortalAccounts('admin-token', environment)
  assert.equal(calls.length, 3)
  assert.ok(calls.every((call) => call.method === 'GET'))
})

test('new test users bind to populated demo entities with a qualified vendor scope', async (t) => {
  const calls = stub(t, (url, init, body) => {
    if (url.pathname.endsWith('/roles')) return roles
    if (url.pathname.endsWith('/vendors')) return [{ code: 'DM-VEN-PACK', id: 'packing-vendor' }]
    if (url.pathname.endsWith('/customers')) return [{ code: 'DM-DLR-VASTU', id: 'vastu-dealer' }]
    if (url.pathname.endsWith('/categories'))
      return [{ item_category_id: 'packing-category', item_category_code: 'PACKING' }]
    if (init.method === 'GET') return []
    return { id: `${body.code ?? body.email}-id` }
  })
  await ensurePortalAccounts('admin-token', environment)
  const users = calls.filter((call) => call.method === 'POST' && call.path.endsWith('/users'))
  assert.equal(users.length, 2)
  assert.equal(users[0].body.vendor_id, 'packing-vendor')
  assert.equal(users[0].body.customer_id, undefined)
  assert.deepEqual(users[0].body.role_ids, ['vendor-role'])
  assert.deepEqual(users[0].body.allowed_category_ids, ['packing-category'])
  assert.equal(users[1].body.customer_id, 'vastu-dealer')
  assert.deepEqual(users[1].body.role_ids, ['dealer-role'])
  assert.ok(calls.every((call) => !call.path.endsWith('/reset-password')))
  assert.ok(
    calls.filter((call) => call.method === 'POST').every((call) => call.path.endsWith('/users')),
  )
})

test('missing populated demo data fails before creating an empty portal fixture', async (t) => {
  const calls = stub(t, (url) => (url.pathname.endsWith('/roles') ? roles : []))
  await assert.rejects(ensurePortalAccounts('admin-token', environment), /DM-VEN-PACK is missing/)
  assert.ok(calls.every((call) => call.method === 'GET'))
})

test('a vendor cannot be provisioned with an unqualified category scope', async (t) => {
  const calls = stub(t, (url) => {
    if (url.pathname.endsWith('/roles')) return roles
    if (url.pathname.endsWith('/vendors')) return [{ code: 'DM-VEN-PACK', id: 'packing-vendor' }]
    return []
  })
  await assert.rejects(ensurePortalAccounts('admin-token', environment), /no PACKING qualification/)
  assert.ok(calls.every((call) => call.method === 'GET'))
})

for (const audience of ['vendor', 'dealer']) {
  test(`${audience} coverage proves populated scoped lists and details with its own token`, async (t) => {
    const binding = audience === 'vendor' ? 'vendor_id' : 'customer_id'
    const orders = audience === 'vendor' ? 'purchase-orders' : 'orders'
    const calls = stub(t, (url) => {
      if (url.pathname.endsWith('/auth/me')) return { [binding]: 'own-party', is_superuser: false }
      if (url.searchParams.has('page'))
        return [{ id: url.pathname.endsWith('/invoices') ? 'invoice-1' : 'order-1' }]
      return { id: url.pathname.split('/').at(-1), [binding]: 'own-party' }
    })
    assert.deepEqual(await assertPortalData(audience, 'own-portal-token'), {
      [orders]: 'order-1',
      invoices: 'invoice-1',
    })
    assert.equal(calls.length, 5)
    assert.ok(calls.every((call) => call.authorization === 'Bearer own-portal-token'))
    assert.ok(calls.some((call) => call.path.endsWith(`/${audience}-portal/${orders}/order-1`)))
  })
}

test('empty portal lists fail rather than providing passing empty-screen coverage', async (t) => {
  const calls = stub(t, (url) =>
    url.pathname.endsWith('/auth/me') ? { vendor_id: 'own-vendor', is_superuser: false } : [],
  )
  await assert.rejects(assertPortalData('vendor', 'vendor-token'), /no visible purchase-orders/)
  assert.equal(calls.length, 2)
  assert.ok(calls.every((call) => call.method === 'GET'))
})

test('a staff administrator cannot stand in for a portal principal', async (t) => {
  stub(t, () => ({ customer_id: 'customer', is_superuser: true }))
  await assert.rejects(assertPortalData('dealer', 'admin-token'), /must be a non-superuser/)
})

test('a returned detail must match the portal list and principal binding', async (t) => {
  stub(t, (url) => {
    if (url.pathname.endsWith('/auth/me')) return { vendor_id: 'own-vendor', is_superuser: false }
    if (url.searchParams.has('page')) return [{ id: 'order-1' }]
    return { id: 'order-1', vendor_id: 'foreign-vendor' }
  })
  await assert.rejects(assertPortalData('vendor', 'vendor-token'), /does not match its scoped list/)
})

test('an existing incompatible identity fails instead of being rebound or reset', async (t) => {
  const calls = stub(t, (url) =>
    url.pathname.endsWith('/roles')
      ? roles
      : [
          {
            email: environment.E2E_VENDOR_EMAIL,
            is_active: true,
            vendor_id: null,
          },
        ],
  )
  await assert.rejects(
    ensurePortalAccounts('admin-token', environment),
    /Existing accounts are never reset/,
  )
  assert.ok(calls.every((call) => call.method === 'GET'))
})

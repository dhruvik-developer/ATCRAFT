import { EventEmitter } from 'node:events'
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  allowsReadOnlyRequest,
  createNavigator,
  isReadinessDialogOpener,
  protectReadOnlyContext,
  SESSION_ACTIONS,
} from './navigation.mjs'

function request(previous = null, type = 'fetch') {
  return { resourceType: () => type, redirectedFrom: () => previous }
}

function fixture(context) {
  const page = new EventEmitter()
  let now = 0
  context.mock.method(Date, 'now', () => now)
  page.url = () => 'http://localhost/documents'
  page.waitForTimeout = async (milliseconds) => {
    now += milliseconds
  }
  return { page, ...createNavigator(page) }
}

test('finishing a request allows the screen to settle', async (context) => {
  const { page, settle } = fixture(context)
  const query = request()
  page.emit('request', query)
  page.emit('requestfinished', query)
  await settle()
})

test('a CSP-blocked redirect releases its original request', async (context) => {
  const { page, settle } = fixture(context)
  const collection = request()
  const blockedRedirect = request(collection)
  page.emit('request', collection)
  // Chromium can block the redirect before emitting its request event.
  page.emit('requestfailed', blockedRedirect)
  await settle()
})

test('following a redirect replaces the original pending request', async (context) => {
  const { page, settle } = fixture(context)
  const original = request()
  const redirected = request(original)
  page.emit('request', original)
  page.emit('request', redirected)
  page.emit('requestfinished', redirected)
  await settle()
})

test('failure clears a whole redirect chain without hiding a separate hung request', async (context) => {
  const { page, settle } = fixture(context)
  const original = request()
  const redirected = request(request(original))
  page.emit('request', original)
  page.emit('request', request())
  page.emit('requestfailed', redirected)
  await assert.rejects(settle(), /Screen requests did not settle within 20 seconds/)
})

test('background image requests do not hold the route loading check open', async (context) => {
  const { page, settle } = fixture(context)
  page.emit('request', request(null, 'image'))
  await settle()
})

test('a stylesheet that never finishes fails the loading check', async (context) => {
  const { page, settle } = fixture(context)
  page.emit('request', request(null, 'stylesheet'))
  await assert.rejects(settle(), /Screen requests did not settle within 20 seconds/)
})

test('readiness probes form openers and refuses immediate business submissions', () => {
  for (const label of [
    'New purchase order',
    'Add shipment',
    'Create rework draft',
    'Register serials',
  ]) {
    assert.equal(isReadinessDialogOpener(label, 'button'), true)
    assert.equal(isReadinessDialogOpener(label, 'submit'), false)
  }
  for (const label of ['Add to cart', 'Add to wishlist', 'Save changes', 'Place order request']) {
    assert.equal(isReadinessDialogOpener(label, 'button'), false)
    assert.equal(isReadinessDialogOpener(label, 'submit'), false)
  }
})

test('read-only checks permit only exact same-origin session writes under the configured prefix', () => {
  const api = 'http://localhost:18081/custom-api/'
  for (const method of ['GET', 'HEAD', 'OPTIONS'])
    assert.equal(allowsReadOnlyRequest(method, `${api}sales`, api), true)
  for (const action of SESSION_ACTIONS)
    assert.equal(allowsReadOnlyRequest('POST', `${api}auth/${action}`, api), true)
  for (const path of ['sales', 'dealer-portal/cart', 'auth/change-password', 'auth/login/extra'])
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE'])
      assert.equal(allowsReadOnlyRequest(method, `${api}${path}`, api), false)
  assert.equal(
    allowsReadOnlyRequest('POST', 'http://localhost:18081/api/v1/auth/login', api),
    false,
  )
  assert.equal(allowsReadOnlyRequest('POST', 'http://other-host/custom-api/auth/login', api), false)
  assert.equal(allowsReadOnlyRequest('DELETE', `${api}auth/logout`, api), false)
})

test('the browser guard refuses writes before dispatch and records no payload or query secrets', async () => {
  let handler
  const context = {
    route: async (_pattern, callback) => {
      handler = callback
    },
  }
  const blocked = await protectReadOnlyContext(context, 'http://localhost/api/v1')
  let continued = 0
  let aborted = 0
  const route = (method, url) => ({
    request: () => ({ method: () => method, url: () => url }),
    continue: async () => {
      continued++
    },
    abort: async (reason) => {
      assert.equal(reason, 'blockedbyclient')
      aborted++
    },
  })
  await handler(route('POST', 'http://localhost/api/v1/auth/login'))
  await handler(route('POST', 'http://localhost/api/v1/dealer-portal/cart?token=secret'))
  assert.equal(continued, 1)
  assert.equal(aborted, 1)
  assert.deepEqual(blocked, ['Blocked write: POST /api/v1/dealer-portal/cart'])
})

test('a separately configured web proxy can create sessions without permitting third-party writes', () => {
  const api = 'http://localhost:8000/api/v1'
  const web = 'http://localhost:4173'
  for (const origin of ['http://localhost:8000', web]) {
    assert.equal(allowsReadOnlyRequest('POST', `${origin}/api/v1/auth/login`, api, web), true)
    assert.equal(
      allowsReadOnlyRequest('POST', `${origin}/api/v1/dealer-portal/cart`, api, web),
      false,
    )
  }
  assert.equal(
    allowsReadOnlyRequest('POST', 'http://other-host/api/v1/auth/login', api, web),
    false,
  )
})

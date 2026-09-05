/**
 * Tenant isolation, exercised the way an attacker would: with a real session.
 *
 * `company_id` comes from the verified token and never from the request, and the
 * repositories scope every query by it. That is enforced and unit-tested. What
 * had never been checked is the whole stack end to end - a second real tenant,
 * a real login, and real ids belonging to somebody else pushed at every read
 * endpoint the API exposes.
 *
 * The distinction matters because the unit tests construct a `TenantContext`
 * directly. They cannot catch a route that reads an id from the path and hands
 * it to a repository method that forgot its scope, and they cannot catch a
 * response that leaks a name or a code in an error message before the scope is
 * applied.
 *
 * Three things are checked for every id belonging to tenant A, requested by
 * tenant B:
 *
 *   LEAK       B got 200 and the row back. A cross-tenant read. Critical.
 *   ID-ECHO    B was refused, but the error body quoted something about the
 *              row - a code, a name - which confirms it exists and to whom.
 *   ok         404 or 403, and the body says nothing about the row.
 *
 * A 404 rather than a 403 is the better answer here: 403 confirms the row
 * exists, which is itself a small leak.
 */

import { writeFileSync } from 'node:fs'

import { ADMIN_EMAIL, ADMIN_PASSWORD, API_URL as API } from '../lib/env.mjs'

// Tenant A is the suite's administrator unless told otherwise; tenant B has to
// be created by the caller, as nothing in the seed makes a second company.
// Both accounts must have completed their forced password change: a
// temporary-password principal is refused every route this script reads.
const A = {
  email: process.env.ISO_A_EMAIL ?? ADMIN_EMAIL,
  password: process.env.ISO_A_PASSWORD ?? ADMIN_PASSWORD,
  label: 'Atcraft',
}
const B = {
  email: process.env.ISO_B_EMAIL ?? 'admin@vastu.example',
  password: process.env.ISO_B_PASSWORD ?? 'VastuPass!Atcraft2026',
  label: 'Vastu',
}

async function login({ email, password, label }) {
  const response = await fetch(`${API}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  const body = await response.json()
  if (!response.ok) throw new Error(`${label} could not sign in: ${JSON.stringify(body)}`)
  const me = await fetch(`${API}/auth/me`, {
    headers: { Authorization: `Bearer ${body.data.access_token}` },
  }).then((r) => r.json())
  return { token: body.data.access_token, companyId: me.data.company_id, label }
}

async function get(token, path) {
  const response = await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` } })
  const body = await response.json().catch(() => ({}))
  return { status: response.status, body }
}

/** List endpoints whose first row gives us a real id belonging to tenant A. */
const RESOURCES = [
  ['item', '/products/items', '/products/items/{id}'],
  ['category', '/products/categories', '/products/categories/{id}'],
  ['warehouse', '/warehouses', '/warehouses/{id}'],
  ['location', '/warehouses/locations', '/warehouses/locations/{id}'],
  ['customer', '/customers', '/customers/{id}'],
  ['vendor', '/vendors', '/vendors/{id}'],
  ['transporter', '/transporters', '/transporters/{id}'],
  ['bom', '/bom', '/bom/{id}'],
  ['routing', '/routing', '/routing/{id}'],
  ['sales order', '/sales', '/sales/{id}'],
  ['purchase order', '/procurement/purchase-orders', '/procurement/purchase-orders/{id}'],
  ['goods receipt', '/procurement/goods-receipts', '/procurement/goods-receipts/{id}'],
  ['rfq', '/procurement/rfqs', '/procurement/rfqs/{id}'],
  ['quotation', '/procurement/quotations', '/procurement/quotations/{id}'],
  ['purchase return', '/procurement/purchase-returns', '/procurement/purchase-returns/{id}'],
  ['production order', '/production', '/production/{id}'],
  // Work orders have no GET-by-id - only PATCH and a status POST - so isolation
  // is checked through the nested list, keyed on the parent production order.
  ['work orders of an order', '/production', '/production/{id}/work-orders'],
  ['assembly order', '/assembly', '/assembly/{id}'],
  ['job work order', '/job-work', '/job-work/{id}'],
  ['inspection', '/quality/inspections', '/quality/inspections/{id}'],
  ['qc template', '/quality/templates', '/quality/templates/{id}'],
  ['packing order', '/packing', '/packing/{id}'],
  ['dispatch', '/dispatch', '/dispatch/{id}'],
  ['vendor invoice', '/finance/vendor-invoices', '/finance/vendor-invoices/{id}'],
  ['payment', '/finance/payments', '/finance/payments/{id}'],
  ['account', '/finance/accounts', '/finance/accounts/{id}'],
  ['document', '/documents', '/documents/{id}'],
  ['user', '/users', '/users/{id}'],
]

const a = await login(A)
const b = await login(B)
console.log(`${a.label} company ${a.companyId}`)
console.log(`${b.label} company ${b.companyId}`)
if (a.companyId === b.companyId) throw new Error('both accounts are in the same company')
console.log()

const results = []

for (const [name, listPath, detailPath] of RESOURCES) {
  // Take a real id from tenant A.
  const list = await get(a.token, `${listPath}?limit=1`)
  const row = Array.isArray(list.body?.data) ? list.body.data[0] : null
  if (!row?.id) {
    results.push({ name, verdict: 'no-fixture', detail: `A has no ${name} to test with` })
    console.log(`${'no-fixture'.padEnd(10)} ${name.padEnd(18)} (nothing of A's to ask for)`)
    continue
  }

  // A must be able to read its own, or the fixture proves nothing.
  const own = await get(a.token, detailPath.replace('{id}', row.id))
  // Now B asks for it.
  const cross = await get(b.token, detailPath.replace('{id}', row.id))

  const serialised = JSON.stringify(cross.body ?? {})
  const identifying = [row.code, row.name, row.email, row.file_name].filter(
    (v) => typeof v === 'string' && v.length > 3,
  )
  const echoed = identifying.filter((v) => serialised.includes(v))

  // A LIST endpoint answering 200 with an empty array is not a leak - it is the
  // tenant filter working, and it discloses less than a 404 would, since a 404
  // would confirm the parent id exists. Only a non-empty payload is a leak.
  const crossRows = Array.isArray(cross.body?.data) ? cross.body.data : null
  const returnedSomething = crossRows === null ? cross.status === 200 : crossRows.length > 0

  let verdict
  if (own.status !== 200) verdict = 'fixture-unreadable'
  else if (cross.status === 200 && returnedSomething) verdict = 'LEAK'
  else if (echoed.length) verdict = 'ID-ECHO'
  else verdict = 'ok'

  results.push({
    name,
    id: row.id,
    ownStatus: own.status,
    crossStatus: cross.status,
    echoed,
    verdict,
  })
  const flag = verdict === 'ok' || verdict.startsWith('no') ? ' ' : '!'
  console.log(
    `${flag}${verdict.padEnd(18)} ${name.padEnd(18)} A=${own.status} B=${cross.status}` +
      (echoed.length ? `  echoed: ${echoed.join(', ')}` : ''),
  )
}

// --- the other direction: can B's token be made to act as A by asking? -------
// company_id must come from the token and nothing else. These try the obvious
// ways a client might attempt to override it.
const overrides = [
  ['query parameter', `/products/items?limit=5&company_id=${a.companyId}`],
  ['header', '/products/items?limit=5'],
]
for (const [how, path] of overrides) {
  const headers = { Authorization: `Bearer ${b.token}` }
  if (how === 'header') headers['X-Company-Id'] = a.companyId
  const response = await fetch(`${API}${path}`, { headers })
  const body = await response.json().catch(() => ({}))
  const rows = Array.isArray(body?.data) ? body.data : []
  const leaked = rows.some((r) => typeof r.code === 'string' && r.code.startsWith('DM-'))
  results.push({ name: `company_id via ${how}`, verdict: leaked ? 'LEAK' : 'ok', rows: rows.length })
  console.log(
    `${leaked ? '!' : ' '}${(leaked ? 'LEAK' : 'ok').padEnd(18)} company_id via ${how.padEnd(18)} rows=${rows.length}`,
  )
}

writeFileSync(
  process.env.ISO_OUT ?? 'tenant-isolation.json',
  JSON.stringify({ a: a.companyId, b: b.companyId, results }, null, 2),
  'utf-8',
)

const leaks = results.filter((r) => r.verdict === 'LEAK')
const echoes = results.filter((r) => r.verdict === 'ID-ECHO')
const noFixture = results.filter((r) => r.verdict === 'no-fixture')
console.log(
  `\n${results.length} checks | leaks=${leaks.length} id-echoes=${echoes.length} ` +
    `untested-for-lack-of-data=${noFixture.length}`,
)
if (leaks.length) {
  console.log('\nCROSS-TENANT READS:')
  for (const l of leaks) console.log(`  ${l.name} (${l.id})`)
  process.exitCode = 1
}

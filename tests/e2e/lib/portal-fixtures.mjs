/** Provision dedicated browser-test principals through normal audited APIs. */
import { accountForAudience } from './audiences.mjs'
import { API_URL } from './env.mjs'

const demoBindings = {
  vendor: { resource: '/vendors', code: 'DM-VEN-PACK' },
  dealer: { resource: '/customers', code: 'DM-DLR-VASTU' },
}

async function request(token, method, path, body) {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const result = await response.json()
  if (!response.ok || !result.success)
    throw new Error(
      `Portal fixture ${method} ${path} failed (${response.status}): ${result.error?.message ?? 'Request failed'}`,
    )
  return result.data
}

export async function ensurePortalAccounts(
  token,
  environment = process.env,
  audiences = ['vendor', 'dealer'],
) {
  const roles = await request(token, 'GET', '/roles?page=1&limit=200')
  for (const audience of audiences) {
    const account = accountForAudience(audience, environment)
    const users = await request(
      token,
      'GET',
      `/users?page=1&limit=100&search=${encodeURIComponent(account.email)}`,
    )
    const existing = users.find((user) => user.email.toLowerCase() === account.email.toLowerCase())
    const binding = audience === 'vendor' ? 'vendor_id' : 'customer_id'
    if (existing) {
      if (!existing[binding] || !existing.is_active)
        throw new Error(
          `The configured ${audience} test account is inactive or has no ${binding}. Existing accounts are never reset.`,
        )
      continue
    }
    const role = roles.find((row) => row.code === `${audience.toUpperCase()}_OWNER`)
    if (!role) throw new Error(`Seed the ${audience} roles before running browser tests.`)
    const { code, resource } = demoBindings[audience]
    const masters = await request(token, 'GET', `${resource}?page=1&limit=100&search=${code}`)
    const master = masters.find((row) => row.code === code)
    if (!master)
      throw new Error(
        `Populated ${audience} fixture ${code} is missing. Run python -m src.core.demo_data before browser setup; an empty portal is not passing coverage.`,
      )
    let categories = []
    if (audience === 'vendor') {
      const qualifications = await request(
        token,
        'GET',
        `/vendors/${master.id}/categories?page=1&limit=100`,
      )
      const packing = qualifications.find((row) => row.item_category_code === 'PACKING')
      if (!packing)
        throw new Error(
          `Vendor fixture ${code} has no PACKING qualification. Complete the demonstration seed before browser setup.`,
        )
      categories = [packing.item_category_id]
    }
    await request(token, 'POST', '/users', {
      email: account.email,
      password: account.password,
      full_name: `E2E ${audience} owner`,
      [binding]: master.id,
      role_ids: [role.id],
      must_change_password: false,
      ...(audience === 'vendor' ? { allowed_category_ids: categories } : {}),
    })
  }
}

/** Verify populated list and detail coverage using the portal's own token. */
export async function assertPortalData(audience, token) {
  if (!['vendor', 'dealer'].includes(audience))
    throw new Error(`Unknown portal audience: ${audience}`)
  const profile = await request(token, 'GET', '/auth/me')
  const binding = audience === 'vendor' ? 'vendor_id' : 'customer_id'
  const other = audience === 'vendor' ? 'customer_id' : 'vendor_id'
  if (profile.is_superuser || !profile[binding] || profile[other])
    throw new Error(
      `The ${audience} browser fixture must be a non-superuser bound only by ${binding}.`,
    )
  const base = `/${audience}-portal`
  const resources = audience === 'vendor' ? ['purchase-orders', 'invoices'] : ['orders', 'invoices']
  const verified = {}
  for (const resource of resources) {
    const path = `${base}/${resource}`
    const rows = await request(token, 'GET', `${path}?page=1&limit=1`)
    const row = rows?.[0]
    if (!row?.id)
      throw new Error(
        `The configured ${audience} account has no visible ${resource}. Seed populated demo fixtures or use a populated, correctly scoped account; existing identities are never rebound.`,
      )
    const detail = await request(token, 'GET', `${path}/${row.id}`)
    if (detail.id !== row.id || (detail[binding] && detail[binding] !== profile[binding]))
      throw new Error(`The ${audience} ${resource} detail does not match its scoped list.`)
    verified[resource] = row.id
  }
  return verified
}

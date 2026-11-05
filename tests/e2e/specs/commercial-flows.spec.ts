/** API-backed journeys for commercial features whose screens must agree with the ledger. */
import { randomUUID } from 'node:crypto'

import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: 'serial' })

const RUN = randomUUID().replaceAll('-', '').slice(0, 8).toUpperCase()
let token = ''
let shared: Page

test.beforeAll(async ({ browser }) => {
  token = await readyAdminToken()
  const context = await browser.newContext({ baseURL: BASE_URL })
  shared = await context.newPage()
  await signInThroughForm(shared)
})

test.afterAll(async () => {
  await shared?.context().close()
})

async function apiData<T>(
  request: APIRequestContext,
  method: 'get' | 'post',
  path: string,
  payload?: unknown,
): Promise<T> {
  const response = await request[method](`${API_URL}${path}`, {
    headers: {
      Authorization: ['Bearer', token].join(' '),
      ...(payload ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(payload ? { data: payload } : {}),
  })
  const body = await response.json()
  expect(
    response.ok(),
    `${method.toUpperCase()} ${path} -> ${response.status()}: ${JSON.stringify(body)}`,
  ).toBeTruthy()
  return body.data as T
}

test('a brand-tagged item is returned by the brand filter in the item list', async ({
  request,
}) => {
  interface IdRecord {
    id: string
  }
  interface Unit extends IdRecord {
    code: string
  }
  interface ItemRecord extends IdRecord {
    code: string
    brand_id: string
  }

  const code = `E2E-BRAND-${RUN}`
  const brand = await apiData<IdRecord>(request, 'post', '/products/brands', {
    code,
    name: `E2E Brand ${RUN}`,
  })
  const units = await apiData<Unit[]>(request, 'get', '/products/uoms?limit=50')
  const unit = units.find((row) => row.code === 'NOS') ?? units[0]
  expect(unit, 'the seeded unit of measure is missing').toBeTruthy()

  const item = await apiData<ItemRecord>(request, 'post', '/products/items', {
    code: `ITEM-${code}`,
    name: `E2E branded item ${RUN}`,
    item_group: 'COMPONENT',
    base_uom_id: unit.id,
    production_type: 'BUY',
    can_purchase: true,
    can_sell: true,
    brand_id: brand.id,
  })
  expect(item.brand_id).toBe(brand.id)

  const filtered = await apiData<ItemRecord[]>(
    request,
    'get',
    `/products/items?brand_id=${brand.id}`,
  )
  expect(
    filtered.some((row) => row.id === item.id && row.brand_id === brand.id),
    'the API brand filter did not return the tagged item',
  ).toBeTruthy()

  await shared.goto(`/items?brand_id=${brand.id}`)
  await shared.getByPlaceholder('Search code or name...').fill(item.code)
  const row = shared.getByRole('row').filter({ hasText: item.code })
  await expect(row).toBeVisible({ timeout: 15_000 })
  await expect(row).toContainText(`E2E Brand ${RUN}`)
})

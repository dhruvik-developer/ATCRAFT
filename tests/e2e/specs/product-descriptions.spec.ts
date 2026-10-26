/** Descriptive product data persists through staff forms and the scoped dealer catalog. */
import { randomUUID } from 'node:crypto'

import { expect, test, type Locator, type Page } from '@playwright/test'

import type { SchemaOf } from '../../../apps/web/src/types/api'
import { accountForAudience } from '../lib/audiences.mjs'
import { API_URL, BASE_URL } from '../lib/env.mjs'
import { createNavigator, errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: 'serial' })

const run = randomUUID().replaceAll('-', '').slice(0, 10).toUpperCase()
const names = {
  material: `E2E ${run} material`,
  backset: `E2E ${run} backset`,
  fire: `E2E ${run} fire rated`,
  parent: `E2E ${run} product with technical descriptions`,
  included: `E2E ${run} fixing screw kit included in the box`,
  optional: `E2E ${run} matching optional rose`,
}

let administrator = ''
let category: SchemaOf<'CategoryOut'>
let parent: SchemaOf<'ItemDetail'>
let included: SchemaOf<'ItemDetail'>
let optional: SchemaOf<'ItemDetail'>
let material: SchemaOf<'SpecificationAttributeRead'>
let backset: SchemaOf<'SpecificationAttributeRead'>
let fire: SchemaOf<'SpecificationAttributeRead'>

async function api<T>(
  method: 'GET' | 'POST',
  path: string,
  data?: unknown,
  token = administrator,
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  })
  const body = await response.json()
  expect(response.ok, `${method} ${path}: ${response.status} ${body.error?.message ?? ''}`).toBe(
    true,
  )
  expect(body.success, `${method} ${path} did not return the API envelope`).toBe(true)
  return body.data as T
}

async function fitsPage(page: Page) {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
  ).toBeLessThanOrEqual(1)
  expect(await errorToastFailures(page)).toEqual([])
}

async function chooseDescription(
  page: Page,
  label: 'Category' | 'Specification',
  id: string,
  code: string,
) {
  await page
    .getByRole('textbox', {
      name: `Search ${label.toLowerCase()}`,
      exact: true,
    })
    .fill(code)
  const choice = page.getByRole('combobox', { name: label, exact: true })
  await expect(choice.locator(`option[value="${id}"]`)).toHaveCount(1)
  await expect(choice).toBeEnabled()
  await choice.selectOption(id)
  await page.getByRole('button', { name: `Choose ${label.toLowerCase()}`, exact: true }).click()
}

async function addAccessory(
  page: Page,
  item: SchemaOf<'ItemDetail'>,
  relation: 'INCLUDED' | 'OPTIONAL',
) {
  await page.getByRole('button', { name: 'Add accessory', exact: true }).first().click()
  const dialog = page.getByRole('dialog', {
    name: 'Add accessory',
    exact: true,
  })
  await dialog.getByRole('combobox', { name: 'Relation', exact: true }).selectOption(relation)
  await dialog.getByRole('combobox', { name: 'Accessory item', exact: true }).click()
  await page.getByRole('textbox', { name: 'Search items', exact: true }).fill(item.code)
  await page.getByRole('option').filter({ hasText: item.code }).click()
  await dialog
    .getByRole('textbox', { name: 'Quantity', exact: true })
    .fill(relation === 'INCLUDED' ? '2' : '1')
  await dialog.getByRole('button', { name: 'Add accessory', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('cell', { name: item.code, exact: true })).toBeVisible()
}

async function dealerFacet(
  page: Page,
  name: string,
  settle: () => Promise<void>,
): Promise<Locator> {
  const field = page.getByRole('combobox', { name, exact: true })
  // Facets are server-paged. An accumulated local fixture set must not force
  // the test to assume its new attribute is on the first page.
  for (let pageNumber = 0; pageNumber < 20; pageNumber += 1) {
    if (await field.count()) return field
    const more = page.getByRole('button', {
      name: 'More filters',
      exact: true,
    })
    await expect(more).toBeEnabled()
    await more.click()
    await settle()
  }
  throw new Error(`Specification filter ${name} was not found in the first 20 facet pages.`)
}

test.beforeAll(async () => {
  administrator = await readyAdminToken()
  const [unit] = await api<SchemaOf<'UomOut'>[]>('GET', '/products/uoms?is_active=true&limit=1')
  expect(unit?.id, 'The baseline seed must provide an active unit of measure').toBeTruthy()
  category = await api<SchemaOf<'CategoryOut'>>('POST', '/products/categories', {
    code: `E2E-D5-CAT-${run}`,
    name: `E2E ${run} descriptions category`,
  } satisfies Partial<SchemaOf<'CategoryCreate'>>)
  const createItem = (suffix: string, name: string, sellable: boolean, dealerPrice: string) =>
    api<SchemaOf<'ItemDetail'>>('POST', '/products/items', {
      code: `E2E-D5-${suffix}-${run}`,
      name,
      item_group: 'SPARE',
      base_uom_id: unit.id,
      ...(suffix === 'PRODUCT' ? { category_id: category.id } : {}),
      can_sell: sellable,
      can_purchase: true,
      can_manufacture: false,
      sale_price: dealerPrice,
      dealer_price: dealerPrice,
    } satisfies Partial<SchemaOf<'ItemCreate'>>)
  ;[parent, included, optional] = await Promise.all([
    createItem('PRODUCT', names.parent, true, '100'),
    createItem('KIT', names.included, false, '0'),
    createItem('ROSE', names.optional, true, '25'),
  ])
  ;[backset, fire] = await Promise.all([
    api<SchemaOf<'SpecificationAttributeRead'>>('POST', '/products/specification-attributes', {
      code: `E2E-D5-NUMBER-${run}`,
      name: names.backset,
      value_type: 'NUMBER',
      unit: 'mm',
    } satisfies Partial<SchemaOf<'SpecificationAttributeCreate'>>),
    api<SchemaOf<'SpecificationAttributeRead'>>('POST', '/products/specification-attributes', {
      code: `E2E-D5-FIRE-${run}`,
      name: names.fire,
      value_type: 'BOOLEAN',
      is_filterable: true,
    } satisfies Partial<SchemaOf<'SpecificationAttributeCreate'>>),
  ])
})

test('a governed Option master and required category template persist through staff forms', async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await signInThroughForm(page)
  await page.goto(`${BASE_URL}/masters?tab=specifications`)
  await page.getByRole('button', { name: 'New specification', exact: true }).first().click()
  const dialog = page.getByRole('dialog', {
    name: 'New specification',
    exact: true,
  })
  const code = `E2E-D5-MATERIAL-${run}`
  await dialog.getByRole('textbox', { name: 'Code', exact: true }).fill(code)
  await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill(names.material)
  await dialog.getByRole('combobox', { name: 'Value type', exact: true }).selectOption('OPTION')
  await dialog
    .getByRole('checkbox', {
      name: 'Available as a catalog filter',
      exact: true,
    })
    .check()
  let submitted = false
  const onRequest = (request: import('@playwright/test').Request) => {
    if (
      request.method() === 'POST' &&
      new URL(request.url()).pathname.endsWith('/products/specification-attributes')
    )
      submitted = true
  }
  page.on('request', onRequest)
  await dialog.getByRole('button', { name: 'Create specification', exact: true }).click()
  await expect(
    dialog.getByText('Enter at least one option, one per line.', {
      exact: true,
    }),
  ).toBeVisible()
  expect(submitted, 'The browser must refuse an Option master without options before posting').toBe(
    false,
  )
  page.off('request', onRequest)
  await dialog.getByRole('textbox', { name: 'Permitted options', exact: true }).fill('Brass\nSteel')
  await dialog.getByRole('button', { name: 'Create specification', exact: true }).click()
  await expect(dialog).toBeHidden()
  const attributes = await api<SchemaOf<'SpecificationAttributeRead'>[]>(
    'GET',
    `/products/specification-attributes?search=${encodeURIComponent(code)}&limit=5`,
  )
  material = attributes.find((attribute) => attribute.code === code)!
  expect(material?.options).toEqual(['Brass', 'Steel'])

  await page.getByRole('button', { name: 'Category templates', exact: true }).click()
  await chooseDescription(page, 'Category', category.id, category.code)
  for (const [index, attribute] of [material, backset, fire].entries()) {
    await chooseDescription(page, 'Specification', attribute.id, attribute.code)
    await page.getByRole('checkbox', { name: 'Required warning', exact: true }).nth(index).check()
  }
  await page.getByRole('button', { name: 'Save category template', exact: true }).click()
  await expect
    .poll(
      async () =>
        (
          await api<SchemaOf<'CategorySpecificationsRead'>>(
            'GET',
            `/products/categories/${category.id}/specifications`,
          )
        ).specifications.filter((row) => row.is_required).length,
    )
    .toBe(3)
  await fitsPage(page)
})

test('zero, false and both accessory relations survive staff saves and responsive navigation', async ({
  page,
}) => {
  await signInThroughForm(page)
  const navigation = createNavigator(page)
  await navigation.navigate(`/items/${parent.id}?tab=specifications`)
  await expect(page.getByText('Required specifications are missing', { exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: `${names.backset} (mm)`, exact: true }).fill('0')
  await page.getByRole('combobox', { name: names.fire, exact: true }).selectOption('false')
  await page.getByRole('combobox', { name: names.material, exact: true }).selectOption('Brass')
  await page.getByRole('button', { name: 'Save specifications', exact: true }).click()
  await expect
    .poll(
      async () =>
        (
          await api<SchemaOf<'ItemSpecificationsRead'>>(
            'GET',
            `/products/items/${parent.id}/specifications`,
          )
        ).values?.length ?? 0,
    )
    .toBe(3)
  const saved = await api<SchemaOf<'ItemSpecificationsRead'>>(
    'GET',
    `/products/items/${parent.id}/specifications`,
  )
  expect(saved.missing_required_count).toBe(0)
  expect(saved.values?.find((row) => row.specification_attribute_id === fire.id)?.value_bool).toBe(
    false,
  )
  expect(
    saved.values?.find((row) => row.specification_attribute_id === backset.id)?.value_number,
  ).toMatch(/^0(?:\.0+)?$/)

  await navigation.navigate(`/items/${parent.id}?tab=accessories`)
  await addAccessory(page, included, 'INCLUDED')
  await addAccessory(page, optional, 'OPTIONAL')
  const accessories = await api<SchemaOf<'ItemAccessoryRead'>[]>(
    'GET',
    `/products/items/${parent.id}/accessories`,
  )
  expect(accessories.map((row) => [row.accessory_item_id, row.relation])).toEqual([
    [included.id, 'INCLUDED'],
    [optional.id, 'OPTIONAL'],
  ])
  expect(await api<unknown[]>('GET', `/inventory/ledger?item_id=${parent.id}&limit=1`)).toEqual([])
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: width === 768 ? 1024 : 900 })
    await navigation.navigate(`/items/${parent.id}?tab=specifications`)
    await expect(
      page.getByRole('textbox', { name: `${names.backset} (mm)`, exact: true }),
    ).toHaveValue('0.0000')
    await expect(page.getByRole('combobox', { name: names.fire, exact: true })).toHaveValue('false')
    await fitsPage(page)
    await navigation.navigate(`/items/${parent.id}?tab=accessories`)
    await expect(page.getByRole('cell', { name: included.code, exact: true })).toBeVisible()
    await fitsPage(page)
  }
})

test('a bound dealer filters technical values and reads informational and priced accessories', async ({
  page,
}) => {
  const account = accountForAudience('dealer')
  const token = await readyAdminToken(account)
  const identity = await api<SchemaOf<'MeResponse'>>('GET', '/auth/me', undefined, token)
  expect(identity.is_superuser).toBe(false)
  expect(identity.customer_id).toBeTruthy()
  const staffReads: string[] = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith(`${new URL(API_URL).pathname}/products/`))
      staffReads.push(request.url())
  })
  await signInThroughForm(page, account)
  const navigation = createNavigator(page)
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: width === 768 ? 1024 : 900 })
    await navigation.navigate(`/dealer/catalog?search=${encodeURIComponent(parent.code)}`)
    await page.getByText('Technical specifications', { exact: true }).click()
    await navigation.settle()
    await expect(page.getByRole('combobox', { name: 'Accessories', exact: true })).toBeVisible()
    const fireFilter = await dealerFacet(page, names.fire, navigation.settle)
    await fireFilter.selectOption('true')
    await expect(page.getByText('No matching products', { exact: true })).toBeVisible()
    await fireFilter.selectOption('false')
    await expect(page.getByRole('link', { name: names.parent })).toBeVisible()
    await (await dealerFacet(page, names.material, navigation.settle)).selectOption('Brass')
    await page.getByRole('combobox', { name: 'Accessories', exact: true }).selectOption('true')
    await expect(page.getByRole('link', { name: names.parent })).toBeVisible()
    expect(new URL(page.url()).searchParams.getAll('spec')).toEqual(
      expect.arrayContaining([`${fire.code}=false`, `${material.code}=Brass`]),
    )
    await fitsPage(page)
    await page.getByRole('link', { name: names.parent }).click()
    const inTheBox = page.getByRole('group', {
      name: names.included,
      exact: true,
    })
    await expect(inTheBox.getByText(/In the box/)).toBeVisible()
    await expect(inTheBox.getByRole('link')).toHaveCount(0)
    await expect(inTheBox.getByRole('button', { name: 'Add to cart', exact: true })).toHaveCount(0)
    const addOn = page.getByRole('group', {
      name: names.optional,
      exact: true,
    })
    await expect(addOn.getByRole('link', { name: names.optional, exact: true })).toHaveAttribute(
      'href',
      `/dealer/catalog/${optional.id}`,
    )
    await expect(addOn.getByRole('button', { name: 'Add to cart', exact: true })).toBeEnabled()
    await expect(addOn.getByText(/25\.00/)).toBeVisible()
    const specifications = page.locator('section').filter({
      has: page.getByRole('heading', { name: 'Specifications', exact: true }),
    })
    await expect(specifications.getByText('No', { exact: true })).toBeVisible()
    await expect(specifications.getByText('0 mm', { exact: true })).toBeVisible()
    await fitsPage(page)
  }
  expect(staffReads, 'The dealer must read its scoped description facade').toEqual([])
})

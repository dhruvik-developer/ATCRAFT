/**
 * Capture the handbook without submitting business documents or changing passwords.
 * Screen descriptions come from content.json and must cover the route registry.
 * Use an already configured test administrator and the populated local installation.
 *
 * node scripts/capture-screens.mjs --check  validates coverage without a browser.
 * node scripts/capture-screens.mjs          refreshes screens/ and its index.
 */
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

import { chromium } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { accountForAudience, routeAudience } from '../lib/audiences.mjs'
import {
  findHandbookDetailLink,
  handbookDetailListRoute,
  handbookRouteShape,
  handbookTemplateCategory,
  handbookViewDelayMs,
  resolveHandbookDetailRoute,
} from '../lib/handbook.mjs'
import {
  allowsReadOnlyRequest,
  createNavigator,
  errorToastFailures,
  protectReadOnlyContext,
  SESSION_ACTIONS,
} from '../lib/navigation.mjs'
import { signInThroughForm } from '../lib/session.mjs'
import { assertPortalData } from '../lib/portal-fixtures.mjs'
import { registeredScreens } from '../lib/screen-registry.mjs'

const HANDBOOK = fileURLToPath(new URL('../../../docs/handbook/', import.meta.url))
const OUT = path.join(HANDBOOK, 'screens')
const VIEWPORT = { width: 1600, height: 1000 }
const PREFERRED_ITEM_CODE = process.env.HANDBOOK_ITEM_CODE
const VIEW_DELAY_MS = handbookViewDelayMs(process.env.HANDBOOK_VIEW_DELAY_MS)
const PUBLIC = ['/login', '/forgot-password', '/reset-password']
const DETAIL_LISTS = {
  '/items/:id': ['/items', '/items/'],
  '/bom/:id': ['/bom', '/bom/'],
  '/sales-orders/:id': ['/sales-orders', '/sales-orders/'],
  '/mrp/runs/:id': ['/mrp', '/mrp/runs/'],
  '/procurement/purchase-orders/:id': [
    '/procurement/purchase-orders',
    '/procurement/purchase-orders/',
  ],
  '/procurement/rfqs/:id': ['/procurement/rfqs', '/procurement/rfqs/'],
  '/production/:id': ['/production', '/production/'],
  '/job-work/:id': ['/job-work', '/job-work/'],
  '/vendor/orders/:id': ['/vendor/orders', '/vendor/orders/'],
  '/vendor/invoices/:id': ['/vendor/invoices', '/vendor/invoices/'],
  '/dealer/catalog/:id': ['/dealer/catalog', '/dealer/catalog/'],
  '/dealer/orders/:id': ['/dealer/orders', '/dealer/orders/'],
  '/dealer/invoices/:id': ['/dealer/invoices', '/dealer/invoices/'],
}
const API_PATH = new URL(API_URL).pathname.replace(/\/$/, '')
const authPath = (action) => `${API_PATH}/auth/${action}`
const canonical = (route) => handbookRouteShape(route.split('?')[0])
const allowsRequest = (method, pathname, apiPath = API_PATH) =>
  allowsReadOnlyRequest(method, new URL(pathname, API_URL).href, new URL(apiPath, API_URL).href)

const content = JSON.parse(await readFile(path.join(HANDBOOK, 'content.json'), 'utf8'))
const screens = content.chapters
  .flatMap((chapter) => chapter.screens)
  .map((screen, index) => ({
    route: screen.route,
    title: screen.title,
    file:
      screen.route === '/login'
        ? '00-login.png'
        : `screen-${String(index + 1).padStart(3, '0')}-${screen.route.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'dashboard'}.png`,
  }))
const registered = registeredScreens
const described = new Set(screens.map((screen) => canonical(screen.route)))
for (const route of [...registered, ...PUBLIC]) {
  if (!described.has(canonical(route))) throw new Error(`Handbook is missing ${route}`)
}
for (const screen of screens) {
  if (![...registered, ...PUBLIC].some((route) => canonical(route) === canonical(screen.route)))
    throw new Error(`Handbook route is no longer registered: ${screen.route}`)
  if (screen.route.includes(':') && !DETAIL_LISTS[canonical(screen.route)])
    throw new Error(`No populated detail resolver for ${screen.route}`)
}
if (new Set(screens.map((screen) => screen.file)).size !== screens.length)
  throw new Error('Screenshot filenames must be unique.')

if (process.argv.includes('--check')) {
  // Check the guard against the actual API surface, including future write routes.
  const contract = JSON.parse(
    await readFile(new URL('../../../openapi.json', import.meta.url), 'utf8'),
  )
  const contractLogin = Object.keys(contract.paths).find((route) => route.endsWith('/auth/login'))
  if (!contractLogin) throw new Error('The API contract has no session login route.')
  const contractPrefix = contractLogin.slice(0, -'/auth/login'.length)
  let guardedWrites = 0
  for (const [pathname, operations] of Object.entries(contract.paths)) {
    const configuredPath = pathname.startsWith(`${contractPrefix}/`)
      ? `${API_PATH}${pathname.slice(contractPrefix.length)}`
      : pathname
    for (const method of ['post', 'put', 'patch', 'delete']) {
      if (
        !operations[method] ||
        (method === 'post' && SESSION_ACTIONS.some((action) => configuredPath === authPath(action)))
      )
        continue
      if (allowsRequest(method.toUpperCase(), configuredPath))
        throw new Error(`Capture permits a business write: ${method} ${configuredPath}`)
      guardedWrites++
    }
  }
  const customPrefix = '/offline-custom-prefix'
  if (
    !guardedWrites ||
    !allowsRequest('GET', `${API_PATH}/products/items`) ||
    !allowsRequest('POST', authPath('login')) ||
    allowsRequest('POST', authPath('change-password')) ||
    !allowsRequest('POST', `${customPrefix}/auth/login`, customPrefix) ||
    allowsRequest('POST', `${customPrefix}/sales`, customPrefix) ||
    allowsRequest('POST', `${customPrefix}/auth/change-password`, customPrefix) ||
    allowsRequest('POST', '/api/v1/auth/login', customPrefix)
  )
    throw new Error('Capture request guard validation failed.')
  console.log(
    `${screens.length} handbook views cover ${registered.length} application routes and ${PUBLIC.length} public routes; ${guardedWrites} non-session API writes blocked.`,
  )
} else {
  const browser = await chromium.launch()
  const captured = []
  const skipped = []
  const allBlockedWrites = []
  const resolved = new Map()
  const hashes = new Map()
  let context

  async function protectedContext() {
    const next = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: 2,
      serviceWorkers: 'block',
    })
    // Authentication sessions may be created/refreshed. Everything else is read-only,
    // including password changes, marking notifications read and document submission.
    const blockedWrites = await protectReadOnlyContext(next, API_URL, BASE_URL)
    allBlockedWrites.push(blockedWrites)
    return next
  }

  async function photograph(page, screen, route, state) {
    const errors = await errorToastFailures(page)
    if (errors.length) throw new Error(errors.join('; '))
    const bytes = await page.screenshot({ path: path.join(OUT, screen.file) })
    const digest = createHash('sha256').update(bytes).digest('hex')
    hashes.set(digest, [...(hashes.get(digest) ?? []), screen.route])
    captured.push({
      file: screen.file,
      route,
      template: screen.route,
      title: screen.title,
      ...(state ? { state } : {}),
    })
    console.log(`  ${screen.route}`)
  }

  try {
    await mkdir(OUT, { recursive: true })
    const publicContext = await protectedContext()
    try {
      const page = await publicContext.newPage()
      for (const screen of screens.filter((screen) => PUBLIC.includes(screen.route))) {
        // A non-secret preview token displays the form without requesting or using
        // a real reset link. The form is never submitted and writes are blocked.
        const route =
          screen.route === '/reset-password'
            ? '/reset-password#token=handbook-preview-only'
            : screen.route
        await page.goto(`${BASE_URL}${route}`, { waitUntil: 'networkidle' })
        await page.locator('main').getByRole('heading').first().waitFor()
        await photograph(
          page,
          screen,
          screen.route,
          screen.route === '/reset-password'
            ? 'Unsubmitted form with a non-secret preview token'
            : undefined,
        )
      }
    } finally {
      await publicContext.close()
    }

    context = await protectedContext()
    let page = await context.newPage()
    let { navigate, settle } = createNavigator(page)
    const failures = []
    page.on('pageerror', (error) => failures.push(`render: ${error.message}`))
    page.on('response', (response) => {
      if (response.status() >= 400 && new URL(response.url()).pathname.startsWith(`${API_PATH}/`))
        failures.push(`HTTP ${response.status()} ${new URL(response.url()).pathname}`)
    })
    const loginResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === authPath('login') &&
        response.request().method() === 'POST',
    )
    await signInThroughForm(page)
    const login = await (await loginResponse).json()
    const identityResponse = await page.request.get(`${API_URL}/auth/me`, {
      headers: { Authorization: `Bearer ${login.data.access_token}` },
    })
    if (!identityResponse.ok()) throw new Error('Cannot read the capture account identity.')
    const identity = await identityResponse.json()
    await settle()
    const sessions = new Map([['staff', { page, navigate, settle }]])

    for (const screen of screens.filter((screen) => !PUBLIC.includes(screen.route))) {
      // This is request pacing between views; readiness still depends on the
      // navigator's settled requests and visible route content, never this delay.
      if (VIEW_DELAY_MS) await delay(VIEW_DELAY_MS)
      const audience = routeAudience(screen.route)
      if (!sessions.has(audience)) {
        const roleContext = await protectedContext()
        const rolePage = await roleContext.newPage()
        const navigation = createNavigator(rolePage)
        rolePage.on('pageerror', (error) => failures.push(`render: ${error.message}`))
        rolePage.on('response', (response) => {
          if (
            response.status() >= 400 &&
            new URL(response.url()).pathname.startsWith(`${API_PATH}/`)
          )
            failures.push(`HTTP ${response.status()} ${new URL(response.url()).pathname}`)
        })
        const roleLoginResponse = rolePage.waitForResponse(
          (response) =>
            new URL(response.url()).pathname === authPath('login') &&
            response.request().method() === 'POST',
        )
        await signInThroughForm(rolePage, accountForAudience(audience))
        const roleLogin = await (await roleLoginResponse).json()
        await assertPortalData(audience, roleLogin.data.access_token)
        await navigation.settle()
        sessions.set(audience, { page: rolePage, ...navigation })
      }
      ;({ page, navigate, settle } = sessions.get(audience))
      failures.length = 0
      try {
        if (screen.route === '/change-password') {
          const forced = await protectedContext()
          try {
            const forcedPage = await forced.newPage()
            await forcedPage.route(`**${authPath('me')}`, (route) =>
              route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                  ...identity,
                  data: { ...identity.data, must_change_password: true },
                }),
              }),
            )
            await signInThroughForm(forcedPage, {
              allowTemporaryPassword: true,
            })
            await forcedPage.getByRole('heading', { name: 'Choose a new password' }).waitFor()
            await photograph(
              forcedPage,
              screen,
              screen.route,
              'Restricted identity simulated in this browser; password unchanged',
            )
          } finally {
            await forced.close()
          }
          continue
        }
        let route = screen.route
        if (route.includes(':')) {
          const key = canonical(route)
          if (!resolved.has(key)) {
            const [listRoute, prefix] = DETAIL_LISTS[key]
            await navigate(handbookDetailListRoute(screen.route, listRoute, PREFERRED_ITEM_CODE))
            const links = await page
              .locator(`main a[href^="${prefix}"]`)
              .evaluateAll((links) =>
                links.map((link) => ({ href: link.getAttribute('href'), text: link.innerText })),
              )
            const itemCode = ['/items/:id', '/dealer/catalog/:id'].includes(key)
              ? PREFERRED_ITEM_CODE
              : undefined
            resolved.set(key, findHandbookDetailLink(links, prefix, itemCode))
            if (!resolved.get(key) && key === '/procurement/rfqs/:id') {
              // RFQ codes are spans: this verified first-cell row handler only
              // navigates to its detail page. Do not click an actions menu.
              const firstCell = page
                .locator('main table tbody tr.cursor-pointer')
                .first()
                .locator('td')
                .first()
              if (await firstCell.count()) {
                await firstCell.click()
                await page.waitForURL(
                  (url) =>
                    url.pathname.startsWith(prefix) &&
                    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
                      url.pathname.slice(prefix.length),
                    ),
                )
                await settle()
                resolved.set(key, new URL(page.url()).pathname)
              }
            }
          }
          const detailRoute = resolved.get(key)
          if (!detailRoute) throw new Error(`No populated detail fixture for ${screen.route}`)
          route = resolveHandbookDetailRoute(screen.route, detailRoute)
        }
        await navigate(route)
        if (new URL(page.url()).pathname !== route.split('?')[0])
          throw new Error(`Unexpected redirect while capturing ${route}`)
        const categoryCode = handbookTemplateCategory(
          screen.route,
          process.env.HANDBOOK_CATEGORY_CODE,
        )
        if (categoryCode) {
          await page
            .getByRole('textbox', { name: 'Search category', exact: true })
            .fill(categoryCode)
          const categoryPicker = page.getByRole('combobox', { name: 'Category', exact: true })
          await page.waitForFunction(
            (code) =>
              [...document.querySelectorAll('select option')].some((option) =>
                option.textContent.startsWith(`${code} - `),
              ),
            categoryCode,
          )
          const choices = await categoryPicker
            .locator('option')
            .evaluateAll(
              (options, code) =>
                options
                  .filter((option) => option.textContent.startsWith(`${code} - `))
                  .map((option) => option.value),
              categoryCode,
            )
          if (choices.length !== 1)
            throw new Error(`No unique category fixture for ${categoryCode}`)
          await categoryPicker.selectOption(choices[0])
          await page.getByRole('button', { name: 'Choose category', exact: true }).click()
          await settle()
        }
        if (failures.length) throw new Error(failures.join('; '))
        await photograph(page, screen, route)
      } catch (error) {
        skipped.push(`${screen.route}: ${String(error).split('\n')[0]}`)
        console.error(`  FAILED ${screen.route}`)
      }
    }
    skipped.push(...allBlockedWrites.flat())
    const duplicates = [...hashes.values()].filter((routes) => routes.length > 2)
    for (const routes of duplicates) skipped.push(`Identical screenshots: ${routes.join(', ')}`)
    await writeFile(
      path.join(OUT, 'index.json'),
      JSON.stringify(
        {
          capturedAt: new Date().toISOString(),
          registeredRoutes: registered.length,
          expectedViews: screens.length,
          captured,
          skipped,
        },
        null,
        2,
      ) + '\n',
    )
    console.log(`${captured.length}/${screens.length} views captured; ${skipped.length} failures.`)
    if (skipped.length || captured.length !== screens.length) process.exitCode = 1
  } finally {
    await context?.close()
    await browser.close()
  }
}

/**
 * Read-only UI readiness checks against a populated, running application.
 * Routes come from the application's loader registry so new screens cannot
 * silently escape the audit. Detail pages must have data; a skipped detail
 * page is not a passing check. Tabs and primary forms are exercised as views,
 * without submitting documents or changing stock.
 *
 * npm run test:readiness
 * READINESS_WIDTHS=320,667 READINESS_ROUTES=/items,/assembly npm run test:readiness
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { chromium } from '@playwright/test'
import { API_URL, BASE_URL } from '../lib/env.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

const registry = readFileSync(
  new URL('../../../apps/web/src/app/route-loaders.ts', import.meta.url),
  'utf8',
)
const registered = [...registry.matchAll(/^\s*'([^']+)':\s*\(\)\s*=>/gm)].map((match) => match[1])
if (registered.length === 0) throw new Error('No screens found in the route registry.')
const details = {
  '/items/:itemId': '/products/items',
  '/bom/:bomId': '/bom',
  '/sales-orders/:orderId': '/sales',
  '/procurement/purchase-orders/:poId': '/procurement/purchase-orders',
  '/procurement/rfqs/:rfqId': '/procurement/rfqs',
  '/production/:orderId': '/production',
  '/job-work/:orderId': '/job-work',
  '/mrp/runs/:runId': '/mrp/runs',
}
const widths = (process.env.READINESS_WIDTHS ?? '375,768,1440').split(',').map(Number)
if (widths.some((width) => !Number.isInteger(width) || width < 280))
  throw new Error('Invalid viewport width.')
const filters = process.env.READINESS_ROUTES?.split(',') ?? []
const output = process.env.SWEEP_OUT ?? 'sweep-ui-readiness.json'
const shots = 'readiness-artifacts'
mkdirSync(shots, { recursive: true })
const token = await readyAdminToken()
const routes = []
for (const route of registered) {
  if (
    filters.length &&
    !filters.some((prefix) => route === prefix || route.startsWith(`${prefix}/`))
  )
    continue
  if (!route.includes(':')) {
    routes.push({ template: route, path: route })
    continue
  }
  if (!details[route]) throw new Error(`No detail data resolver for ${route}`)
  const response = await fetch(`${API_URL}${details[route]}?page=1&limit=1`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const body = await response.json()
  if (!response.ok || !body.data?.[0]?.id)
    throw new Error(`Detail screen has no usable fixture: ${route} (${response.status})`)
  routes.push({
    template: route,
    path: route.replace(/:[^/]+/, body.data[0].id),
  })
}
if (!routes.length) throw new Error('No screens match the requested route filters.')

/** Kept inside the browser: scrollable tables may overflow internally. */
function measureLayout() {
  const width = innerWidth
  const offenders = []
  for (const element of document.querySelectorAll('body *')) {
    if (element.closest('[role="dialog"], [data-radix-popper-content-wrapper], .sr-only')) continue
    const box = element.getBoundingClientRect()
    if (
      !box.width ||
      !box.height ||
      getComputedStyle(element).visibility === 'hidden' ||
      box.right <= width + 1
    )
      continue
    let scrolls = false
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (
        ['auto', 'scroll'].includes(getComputedStyle(parent).overflowX) &&
        parent.scrollWidth > parent.clientWidth
      ) {
        scrolls = true
        break
      }
    }
    if (!scrolls)
      offenders.push({
        tag: element.tagName,
        text: element.textContent?.trim().slice(0, 80),
        over: Math.round(box.right - width),
      })
  }
  const dialog = document.querySelector('[role="dialog"]')
  let form = null
  if (dialog) {
    const box = dialog.getBoundingClientRect()
    form = {
      sideways: box.left < -1 || box.right > width + 1,
      clipped: box.top < -1 || box.bottom > innerHeight + 1,
    }
  }
  return {
    overflow: Math.max(0, document.documentElement.scrollWidth - width),
    offenders: offenders.sort((a, b) => b.over - a.over).slice(0, 3),
    form,
  }
}

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
})
const page = await context.newPage()
const results = []
if (!filters.length) {
  const publicContext = await browser.newContext()
  const publicPage = await publicContext.newPage()
  for (const width of widths) {
    await publicPage.setViewportSize({
      width,
      height: width === 667 ? 375 : width >= 1000 ? 900 : width >= 700 ? 1024 : 667,
    })
    for (const route of ['/login', '/forgot-password', '/reset-password']) {
      await publicPage.goto(`${BASE_URL}${route}`, {
        waitUntil: 'networkidle',
      })
      await publicPage.locator('h1, h2').first().waitFor()
      const layout = await publicPage.evaluate(measureLayout)
      const flags = layout.overflow > 1 || layout.offenders.length ? ['page-overflow'] : []
      results.push({ width, route, ...layout, flags })
      console.log(`${flags.length ? 'FAIL' : 'ok'} ${width} ${route}`)
    }
  }
  await publicContext.close()
}
let pending = 0
const relevant = (request) =>
  ['fetch', 'xhr', 'script', 'stylesheet'].includes(request.resourceType())
page.on('request', (request) => {
  if (relevant(request)) pending += 1
})
const finished = (request) => {
  if (relevant(request)) pending = Math.max(0, pending - 1)
}
page.on('requestfinished', finished)
page.on('requestfailed', finished)
async function settle() {
  let quiet = null
  const start = Date.now()
  while (Date.now() - start < 20_000) {
    if (pending === 0) {
      quiet ??= Date.now()
      if (Date.now() - quiet > 400) return
    } else quiet = null
    await page.waitForTimeout(100)
  }
  throw new Error('Screen requests did not settle within 20 seconds.')
}
async function navigate(route) {
  await page.evaluate((path) => {
    history.pushState({}, '', path)
    dispatchEvent(new PopStateEvent('popstate'))
  }, route)
  await settle()
  await page.locator('h1, h2').first().waitFor({ timeout: 20_000 })
}
await signInThroughForm(page)
await settle()
const openerPattern = /^(new|add|create|raise|record|invite|upload|register)\b/i
for (const width of widths) {
  await page.setViewportSize({
    width,
    height: width === 667 ? 375 : width >= 1000 ? 900 : width >= 700 ? 1024 : 667,
  })
  for (const route of routes) {
    const failures = []
    const onError = (error) => failures.push(`render: ${error.message}`)
    const onResponse = (response) => {
      if (new URL(response.url()).pathname.startsWith('/api/') && response.status() >= 400)
        failures.push(
          `HTTP ${response.status()} ${response.request().method()} ${new URL(response.url()).pathname}`,
        )
    }
    const onRequestFailure = (request) => {
      const path = new URL(request.url()).pathname
      const reason = request.failure()?.errorText ?? 'network request failed'
      // Navigation and TanStack Query cancel superseded reads deliberately.
      if (reason.includes('ERR_ABORTED')) return
      if (path.startsWith('/api/') || ['script', 'stylesheet'].includes(request.resourceType()))
        failures.push(`network: ${request.method()} ${path}: ${reason}`)
    }
    page.on('pageerror', onError)
    page.on('response', onResponse)
    page.on('requestfailed', onRequestFailure)
    try {
      await navigate(route.path)
      const tabNames = await page.locator('main').getByRole('tab').allTextContents()
      for (const tabName of [
        null,
        ...new Set(tabNames.map((name) => name.trim()).filter(Boolean)),
      ]) {
        if (tabName) {
          const tab = page.locator('main').getByRole('tab', { name: tabName, exact: true }).first()
          if (!(await tab.isVisible()) || !(await tab.isEnabled())) continue
          await tab.click()
          await settle()
        }
        const layout = await page.evaluate(measureLayout)
        const flags = [...new Set(failures)]
        if (layout.overflow > 1 || layout.offenders.length) flags.push('page-overflow')
        if (
          await page
            .getByRole('heading', {
              name: /something went wrong|you do not have access/i,
            })
            .count()
        )
          flags.push('render-boundary')
        if (new URL(page.url()).pathname === '/login') flags.push('lost-session')
        let form = null
        const openers = page.locator('main').getByRole('button', { name: openerPattern })
        for (let index = 0; index < (await openers.count()); index += 1) {
          const opener = openers.nth(index)
          if (!(await opener.isVisible()) || !(await opener.isEnabled())) continue
          const label = (await opener.textContent())?.trim()
          await opener.click()
          await page.getByRole('dialog').first().waitFor({ timeout: 8000 })
          await settle()
          form = {
            opener: label,
            ...(await page.evaluate(measureLayout)).form,
          }
          if (form.sideways || form.clipped) flags.push('dialog-overflow')
          await page.keyboard.press('Escape')
          await page.getByRole('dialog').first().waitFor({ state: 'hidden' })
          break
        }
        // Requests made by the opened form are part of its readiness too.
        for (const failure of failures) if (!flags.includes(failure)) flags.push(failure)
        const result = {
          width,
          route: route.path,
          template: route.template,
          tab: tabName,
          ...layout,
          form,
          flags,
        }
        results.push(result)
        if (
          flags.length ||
          (width === 375 &&
            ['/', '/items', '/assembly', '/sales-orders', '/production/shift-roster'].includes(
              route.path,
            ))
        )
          await page.screenshot({
            path: `${shots}/${width}-${results.length}.png`,
            fullPage: false,
          })
        console.log(
          `${flags.length ? 'FAIL' : 'ok'} ${width} ${route.template}${tabName ? ` [${tabName}]` : ''}${flags.length ? `: ${flags.join('; ')}` : ''}`,
        )
        failures.length = 0
      }
    } catch (error) {
      results.push({
        width,
        ...route,
        flags: [...new Set(failures), String(error)],
      })
      console.log(`FAIL ${width} ${route.template}: ${String(error).slice(0, 250)}`)
      await page.keyboard.press('Escape').catch(() => {})
    } finally {
      page.off('pageerror', onError)
      page.off('response', onResponse)
      page.off('requestfailed', onRequestFailure)
    }
  }
}
await browser.close()
writeFileSync(
  output,
  JSON.stringify(
    {
      baseUrl: BASE_URL,
      registeredScreens: registered.length,
      testedRoutes: routes.length,
      results,
    },
    null,
    2,
  ),
)
const failed = results.filter((result) => result.flags.length)
console.log(
  `${results.length} screen/tab/viewport checks; ${failed.length} failed. Report: ${output}`,
)
process.exitCode = failed.length ? 1 : 0

/**
 * UI readiness checks after audited test-account fixture preparation.
 * Routes come from the application's loader registry so new screens cannot
 * silently escape the audit. Detail pages must have data; a skipped detail
 * page is not a passing check. Tabs and primary forms are exercised as views,
 * without submitting documents or changing stock.
 *
 * npm run test:readiness
 * READINESS_WIDTHS=320,667 READINESS_ROUTES=/items,/assembly npm run test:readiness
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { chromium } from '@playwright/test'
import { API_URL, BASE_URL } from '../lib/env.mjs'
import { accountForAudience, routeAudience } from '../lib/audiences.mjs'
import {
  createNavigator,
  errorToastFailures,
  isReadinessDialogOpener,
  protectReadOnlyContext,
} from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'
import { detailScreenSources as details, registeredScreens } from '../lib/screen-registry.mjs'
import { assertPortalData, ensurePortalAccounts } from '../lib/portal-fixtures.mjs'

const registered = registeredScreens
const widths = (process.env.READINESS_WIDTHS ?? '375,768,1440').split(',').map(Number)
if (widths.some((width) => !Number.isInteger(width) || width < 280))
  throw new Error('Invalid viewport width.')
const filters = process.env.READINESS_ROUTES?.split(',') ?? []
const output = process.env.SWEEP_OUT ?? 'sweep-ui-readiness.json'
const shots = 'readiness-artifacts'
mkdirSync(shots, { recursive: true })
const token = await readyAdminToken()
const selectedScreens = registered.filter(
  (route) =>
    !filters.length || filters.some((prefix) => route === prefix || route.startsWith(`${prefix}/`)),
)
const portalAudiences = [...new Set(selectedScreens.map(routeAudience))].filter(
  (audience) => audience !== 'staff',
)
if (portalAudiences.length) await ensurePortalAccounts(token, process.env, portalAudiences)
const tokens = new Map([['staff', token]])
const routes = []
for (const route of selectedScreens) {
  if (!route.includes(':')) {
    routes.push({ template: route, path: route })
    continue
  }
  if (!details[route]) throw new Error(`No detail data resolver for ${route}`)
  const audience = routeAudience(route)
  if (!tokens.has(audience)) {
    const portalToken = await readyAdminToken(accountForAudience(audience))
    await assertPortalData(audience, portalToken)
    tokens.set(audience, portalToken)
  }
  const response = await fetch(`${API_URL}${details[route]}?page=1&limit=1`, {
    headers: { Authorization: `Bearer ${tokens.get(audience)}` },
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
let forcedIdentity = null
if (routes.some((route) => route.template === '/change-password')) {
  const response = await fetch(`${API_URL}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const body = await response.json()
  if (!response.ok || !body.success) throw new Error('Cannot resolve the password-screen identity.')
  forcedIdentity = {
    ...body,
    data: { ...body.data, must_change_password: true },
  }
}

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
  serviceWorkers: 'block',
})
let blockedWrites = await protectReadOnlyContext(context, API_URL, BASE_URL)
let page = await context.newPage()
let { navigate, settle } = createNavigator(page)
const sessions = new Map([['staff', { page, navigate, settle, blockedWrites }]])
const results = []
if (!filters.length) {
  const publicContext = await browser.newContext({ serviceWorkers: 'block' })
  const publicBlockedWrites = await protectReadOnlyContext(publicContext, API_URL, BASE_URL)
  const publicPage = await publicContext.newPage()
  for (const width of widths) {
    await publicPage.setViewportSize({
      width,
      height: width === 667 ? 375 : width >= 1000 ? 900 : width >= 700 ? 1024 : 667,
    })
    for (const route of ['/login', '/forgot-password', '/reset-password']) {
      const writeStart = publicBlockedWrites.length
      await publicPage.goto(`${BASE_URL}${route}`, {
        waitUntil: 'networkidle',
      })
      await publicPage.locator('h1, h2').first().waitFor()
      const layout = await publicPage.evaluate(measureLayout)
      const flags = [
        ...(await errorToastFailures(publicPage)),
        ...publicBlockedWrites.slice(writeStart),
      ]
      if (layout.overflow > 1 || layout.offenders.length) flags.push('page-overflow')
      results.push({ width, route, ...layout, flags })
      console.log(`${flags.length ? 'FAIL' : 'ok'} ${width} ${route}`)
    }
  }
  await publicContext.close()
}
await signInThroughForm(page)
await settle()
const openerPattern = /^(new|add|create|raise|record|invite|upload|register)\b/i
for (const width of widths) {
  for (const session of sessions.values())
    await session.page.setViewportSize({
      width,
      height: width === 667 ? 375 : width >= 1000 ? 900 : width >= 700 ? 1024 : 667,
    })
  for (const route of routes) {
    const audience = routeAudience(route.template)
    if (!sessions.has(audience)) {
      const roleContext = await browser.newContext({
        viewport: { width, height: page.viewportSize().height },
        serviceWorkers: 'block',
      })
      const roleBlockedWrites = await protectReadOnlyContext(roleContext, API_URL, BASE_URL)
      const rolePage = await roleContext.newPage()
      const navigation = createNavigator(rolePage)
      if (!tokens.has(audience)) {
        const portalToken = await readyAdminToken(accountForAudience(audience))
        await assertPortalData(audience, portalToken)
        tokens.set(audience, portalToken)
      }
      await signInThroughForm(rolePage, accountForAudience(audience))
      await navigation.settle()
      sessions.set(audience, { page: rolePage, ...navigation, blockedWrites: roleBlockedWrites })
    }
    ;({ page, navigate, settle, blockedWrites } = sessions.get(audience))
    if (route.template === '/change-password') {
      // Model the restricted identity in a fresh session without changing the
      // account, submitting a password, or disturbing the normal sweep session.
      const forcedContext = await browser.newContext({
        viewport: page.viewportSize(),
        serviceWorkers: 'block',
      })
      const forcedBlockedWrites = await protectReadOnlyContext(forcedContext, API_URL, BASE_URL)
      const forcedPage = await forcedContext.newPage()
      try {
        await forcedPage.route('**/api/v1/auth/me', async (request) => {
          await request.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(forcedIdentity),
          })
        })
        await signInThroughForm(forcedPage, { allowTemporaryPassword: true })
        await forcedPage.getByRole('heading', { name: 'Choose a new password' }).waitFor()
        const layout = await forcedPage.evaluate(measureLayout)
        const flags = [...(await errorToastFailures(forcedPage)), ...forcedBlockedWrites]
        if (layout.overflow > 1 || layout.offenders.length) flags.push('page-overflow')
        results.push({
          width,
          ...route,
          state: 'temporary-password',
          ...layout,
          flags,
        })
        console.log(`${flags.length ? 'FAIL' : 'ok'} ${width} ${route.template}`)
      } catch (error) {
        results.push({ width, ...route, flags: [String(error), ...forcedBlockedWrites] })
        console.log(`FAIL ${width} ${route.template}: ${String(error).slice(0, 250)}`)
      } finally {
        await forcedContext.close()
      }
      continue
    }
    const writeStart = blockedWrites.length
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
        const flags = [...new Set([...failures, ...(await errorToastFailures(page))])]
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
        if (new URL(page.url()).pathname !== route.path.split('?')[0])
          flags.push('unexpected-route')
        let form = null
        const openers = page.locator('main').getByRole('button', { name: openerPattern })
        for (let index = 0; index < (await openers.count()); index += 1) {
          const opener = openers.nth(index)
          if (!(await opener.isVisible()) || !(await opener.isEnabled())) continue
          const label = (await opener.textContent())?.trim()
          if (!isReadinessDialogOpener(label ?? '', await opener.getAttribute('type'))) continue
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
        for (const failure of [
          ...failures,
          ...(await errorToastFailures(page)),
          ...blockedWrites.slice(writeStart),
        ])
          if (!flags.includes(failure)) flags.push(failure)
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
        flags: [...new Set([...failures, ...blockedWrites.slice(writeStart)]), String(error)],
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
const blockedWriteAttempts = [...sessions.values()].flatMap((session) => session.blockedWrites)
writeFileSync(
  output,
  JSON.stringify(
    {
      baseUrl: BASE_URL,
      registeredScreens: registered.length,
      testedRoutes: routes.length,
      blockedWrites: blockedWriteAttempts,
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
process.exitCode = failed.length || blockedWriteAttempts.length ? 1 : 0

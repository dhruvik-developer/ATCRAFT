/**
 * Navigate an authenticated SPA without reloading its session on every screen.
 * Create this before login so requests already in flight are tracked too.
 *
 * @param {import('@playwright/test').Page} page
 */
export function createNavigator(page) {
  const pending = new Set()
  const relevant = (request) =>
    ['fetch', 'xhr', 'script', 'stylesheet'].includes(request.resourceType())
  const finished = (request) => {
    // A CSP-blocked redirect can report failure only for its final request,
    // even when Playwright never emitted `request` for that blocked URL.
    // Release that request's ancestors too; unrelated requests stay pending.
    for (let current = request; current; current = current.redirectedFrom()) {
      pending.delete(current)
    }
  }
  page.on('request', (request) => {
    const previous = request.redirectedFrom()
    if (previous) finished(previous)
    if (relevant(request)) pending.add(request)
  })
  page.on('requestfinished', finished)
  page.on('requestfailed', finished)

  async function settle() {
    let quiet = null
    const start = Date.now()
    while (Date.now() - start < 20_000) {
      if (pending.size === 0) {
        quiet ??= Date.now()
        if (Date.now() - quiet >= 400) return
      } else quiet = null
      await page.waitForTimeout(100)
    }
    throw new Error(`Screen requests did not settle within 20 seconds: ${page.url()}`)
  }

  async function navigate(route) {
    const expected = new URL(route, page.url())
    await page.evaluate((path) => {
      history.pushState({}, '', path)
      dispatchEvent(new PopStateEvent('popstate'))
    }, route)
    // A quiet network alone could describe a blank screen or a lazy fallback.
    // Wait for route content, then let its queries and React updates finish.
    const heading = page.locator('main').getByRole('heading').first()
    await heading.waitFor({ state: 'visible', timeout: 20_000 })
    await settle()
    await heading.waitFor({ state: 'visible', timeout: 20_000 })
    const current = new URL(page.url())
    if (current.pathname !== expected.pathname || current.search !== expected.search) {
      throw new Error(
        `Expected ${expected.pathname}${expected.search}, reached ${current.pathname}${current.search}`,
      )
    }
  }

  return { navigate, settle }
}

/**
 * A successful HTTP status can still fail the client's response contract.
 * Read the rendered error notifications so a sweep cannot pass that screen.
 *
 * @param {import('@playwright/test').Page} page
 * @returns {Promise<string[]>}
 */
export async function errorToastFailures(page) {
  const messages = await page.locator('[data-toast-variant="error"]').allTextContents()
  return messages
    .map((message) => message.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .map((message) => `ui-error: ${message}`)
}

/** Inspect form openers without submitting an inline business form. */
export function isReadinessDialogOpener(label, type) {
  return (
    type !== 'submit' &&
    /^(new|add|create|raise|record|invite|upload|register)\b/i.test(label) &&
    !/^add to (cart|wishlist)\b/i.test(label)
  )
}

export const SESSION_ACTIONS = ['login', 'refresh', 'logout', 'mfa/verify']

/** Read-only browser checks may create sessions, but never business records. */
export function allowsReadOnlyRequest(method, requestUrl, apiUrl, browserUrl = apiUrl) {
  if (['GET', 'HEAD', 'OPTIONS'].includes(method)) return true
  const request = new URL(requestUrl)
  const api = new URL(apiUrl)
  const prefix = api.pathname.replace(/\/$/, '')
  return (
    method === 'POST' &&
    [api.origin, new URL(browserUrl).origin].includes(request.origin) &&
    SESSION_ACTIONS.some((action) => request.pathname === `${prefix}/auth/${action}`)
  )
}

/** Return every refused write so a prevented transaction still fails the check. */
export async function protectReadOnlyContext(context, apiUrl, browserUrl = apiUrl) {
  const blockedWrites = []
  await context.route('**/*', async (route) => {
    const request = route.request()
    if (allowsReadOnlyRequest(request.method(), request.url(), apiUrl, browserUrl)) {
      await route.continue()
      return
    }
    blockedWrites.push(`Blocked write: ${request.method()} ${new URL(request.url()).pathname}`)
    await route.abort('blockedbyclient')
  })
  return blockedWrites
}

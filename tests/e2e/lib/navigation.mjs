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
  page.on('request', (request) => {
    if (relevant(request)) pending.add(request)
  })
  const finished = (request) => pending.delete(request)
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

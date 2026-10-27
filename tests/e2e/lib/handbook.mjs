/** Preserve the requested tab while normalising detail IDs and parameter names. */
export function handbookRouteShape(route) {
  const [pathname, query] = route.split('?')
  const shaped = pathname
    .split('/')
    .map((segment) =>
      segment.startsWith(':') || /^[0-9a-f-]{20,}$/i.test(segment) ? ':id' : segment,
    )
    .join('/')
  return shaped + (query ? `?${query}` : '')
}

/** Return the captured record so simulated-state metadata stays with its image. */
export function createScreenshotLookup(captured) {
  const byRoute = new Map()
  for (const shot of captured) {
    for (const key of [shot.route, handbookRouteShape(shot.route)]) {
      if (!byRoute.has(key)) byRoute.set(key, shot)
    }
  }
  return (route) =>
    byRoute.get(route) ??
    byRoute.get(handbookRouteShape(route)) ??
    (route === '/masters' ? byRoute.get('/masters?tab=categories') : null) ??
    null
}

/** A list's detail link supplies the ID; the handbook template supplies its tab. */
export function resolveHandbookDetailRoute(template, detailRoute) {
  const queryIndex = template.indexOf('?')
  return detailRoute.split('?')[0] + (queryIndex >= 0 ? template.slice(queryIndex) : '')
}

/** Select one populated product for staff/dealer illustrations without changing data. */
export function handbookDetailListRoute(template, listRoute, itemCode) {
  const shape = handbookRouteShape(template.split('?')[0])
  if (!itemCode?.trim() || !['/items/:id', '/dealer/catalog/:id'].includes(shape)) return listRoute
  const url = new URL(listRoute, 'http://handbook.invalid')
  // useListQuery reads the browser's q parameter and sends search to the API.
  url.searchParams.set('q', itemCode.trim())
  url.searchParams.delete('search')
  url.searchParams.delete('page')
  return `${url.pathname}${url.search}`
}

/** Never substitute another record when a specific demonstration item is requested. */
export function findHandbookDetailLink(links, prefix, itemCode) {
  return links.find(
    (link) =>
      link.href?.startsWith(prefix) &&
      /^[0-9a-f-]{20,}$/i.test(link.href.slice(prefix.length).split('/')[0]) &&
      (!itemCode?.trim() || link.text.trim().split(/\s+/).includes(itemCode.trim())),
  )?.href
}

/** A category choice only loads its template; it never saves changed values. */
export function handbookTemplateCategory(route, categoryCode) {
  const url = new URL(route, 'http://handbook.invalid')
  return url.pathname === '/masters' &&
    url.searchParams.get('tab') === 'specifications' &&
    url.searchParams.get('view') === 'templates'
    ? categoryCode?.trim() || null
    : null
}

/** Pace read-only captures on installations with stricter API rate limits. */
export function handbookViewDelayMs(value) {
  const text = value?.trim() ?? ''
  if (!text) return 0
  const delay = Number(text)
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(delay) || delay > 60_000)
    throw new RangeError('HANDBOOK_VIEW_DELAY_MS must be an integer from 0 to 60000.')
  return delay
}

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

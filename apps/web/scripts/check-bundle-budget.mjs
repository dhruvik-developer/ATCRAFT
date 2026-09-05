/**
 * Bundle budgets, enforced.
 *
 * `chunkSizeWarningLimit` in the Vite config prints a warning and exits zero, so
 * it stops nothing: a chunk can double between two releases and the build stays
 * green. This fails instead.
 *
 * The budgets below are set a little above the sizes measured when this was
 * written, so a normal feature lands without ceremony and a step change does
 * not. They are ratchets, not aspirations - when a budget is genuinely too small
 * for what the app now does, raise it here in a commit that says why, which is a
 * decision somebody made rather than a limit that quietly stopped applying.
 *
 * What is measured is **gzipped** size, because that is what a user downloads.
 * Raw byte counts on disk overstate JavaScript by roughly threefold and would
 * make the numbers here meaningless as a proxy for load time.
 *
 * Run: node scripts/check-bundle-budget.mjs   (after `npm run build`)
 */

import { gzipSync } from 'node:zlib'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

const DIST = path.resolve(import.meta.dirname, '..', 'dist', 'assets')

/**
 * Budgets in KB, gzipped.
 *
 * `entry` is what every visitor downloads before anything renders, so it is the
 * number that decides first paint on a slow connection and is kept tightest.
 * Vendor chunks are split out precisely so they cache across deploys; they are
 * allowed to be larger because they change rarely.
 * `route` covers every lazily-loaded feature page - one screen should never be
 * a large download on its own, and a route chunk that grows past this usually
 * means a heavy library was imported at the top level instead of inside the
 * component that needs it.
 */
const BUDGETS = {
  entry: { pattern: /^index-.*\.js$/, kb: 80, label: 'app entry' },
  react: { pattern: /^react-vendor-.*\.js$/, kb: 70, label: 'react vendor' },
  query: { pattern: /^query-vendor-.*\.js$/, kb: 45, label: 'query vendor' },
  // clsx/tailwind-merge/cva, behind `cn()`. Tiny, but on the first-paint path,
  // which is why vite.config.ts names it rather than letting Rollup place it.
  ui: { pattern: /^ui-vendor-.*\.js$/, kb: 10, label: 'ui vendor' },
  form: { pattern: /^form-vendor-.*\.js$/, kb: 40, label: 'form vendor' },
  chart: { pattern: /^chart-vendor-.*\.js$/, kb: 130, label: 'chart vendor' },
  // Raised from 30 when the Radix overlay families were split out of one
  // `overlays.tsx` module into one module each. The eager shell now imports
  // `ui/dropdown-menu` and `ui/tooltip` directly instead of through the barrel,
  // which moved Select, Dialog, Popover and Tabs out of the entry chunk every
  // visitor downloads before anything renders (68.1 -> 59.1 KB gz) and into
  // this one, which is fetched only by the screens that open an overlay.
  shared: { pattern: /^shared-.*\.js$/, kb: 36, label: 'shared components' },
}

/** No single lazily-loaded route chunk may exceed this, gzipped. */
const ROUTE_CHUNK_KB = 20

/**
 * Nor may opening one cost more than this once its static imports are followed.
 * Higher than ROUTE_CHUNK_KB because a route legitimately shares a feature
 * module or two with its neighbours; low enough that statically importing a
 * vendor chunk fails.
 */
const ROUTE_CLOSURE_KB = 40

/**
 * Total JavaScript a first-time visitor downloads to see the dashboard.
 *
 * Ratcheted from 280 to 210 once the measurement stopped under-counting and the
 * two things inflating it were removed: the login form no longer pulls
 * react-hook-form, zod and @hookform/resolvers onto the first paint to check
 * that two boxes are non-empty, and the Radix overlay families are split so the
 * entry carries only the dropdown menu and tooltip the shell actually renders.
 * Together that took the measured first load from 220 KB to 193 KB. Kept just
 * above it so an ordinary change has room and a 20 KB import does not.
 */
const FIRST_LOAD_KB = 210

const gzipKb = (file) => gzipSync(readFileSync(file)).length / 1024

let files
try {
  files = readdirSync(DIST).filter((f) => f.endsWith('.js'))
} catch {
  console.error(`No build found at ${DIST}. Run \`npm run build\` first.`)
  process.exit(2)
}

const measured = files.map((name) => ({
  name,
  kb: gzipKb(path.join(DIST, name)),
  raw: statSync(path.join(DIST, name)).size / 1024,
}))

const failures = []
const named = new Set()

for (const [key, budget] of Object.entries(BUDGETS)) {
  const match = measured.find((m) => budget.pattern.test(m.name))
  if (!match) {
    // A missing chunk is a build-shape change, not a pass. Silently skipping it
    // is how a budget stops applying without anyone noticing.
    failures.push(
      `${budget.label}: no chunk matched ${budget.pattern} - has the build split changed?`,
    )
    continue
  }
  named.add(match.name)
  const over = match.kb > budget.kb
  console.log(
    `${over ? 'OVER' : 'ok  '} ${budget.label.padEnd(20)} ${match.kb.toFixed(1).padStart(7)} KB gz` +
      ` / budget ${budget.kb} KB`,
  )
  if (over) {
    failures.push(
      `${budget.label} is ${match.kb.toFixed(1)} KB gzipped, over its ${budget.kb} KB budget ` +
        `(${match.name})`,
    )
  }
  void key
}

const routeChunks = measured.filter((m) => !named.has(m.name)).sort((a, b) => b.kb - a.kb)
const fatRoutes = routeChunks.filter((m) => m.kb > ROUTE_CHUNK_KB)

console.log(`\nroute chunks: ${routeChunks.length}, largest ${routeChunks[0]?.kb.toFixed(1)} KB gz`)
for (const chunk of routeChunks.slice(0, 5)) {
  console.log(`  ${chunk.name.padEnd(46)} ${chunk.kb.toFixed(1).padStart(6)} KB gz`)
}
for (const chunk of fatRoutes) {
  failures.push(
    `route chunk ${chunk.name} is ${chunk.kb.toFixed(1)} KB gzipped, over the ` +
      `${ROUTE_CHUNK_KB} KB per-route budget`,
  )
}

// The chunk graph, read out of the built output rather than assumed from names.
// Rollup writes a static import as `from"./<chunk>.js"` or `import"./<chunk>.js"`
// and a dynamic one only as `import("./<chunk>.js")`. That distinction is the
// whole point: a static import is paid for whether or not the user goes there.
const STATIC_IMPORT = /(?:from|import)\s*"\.\/([^"]+\.js)"/g
const DYNAMIC_IMPORT = /import\("\.\/([^"]+\.js)"\)/g
const sourceOf = new Map(
  measured.map((m) => [m.name, readFileSync(path.join(DIST, m.name), 'utf8')]),
)
const importsOf = new Map(
  measured.map((m) => [
    m.name,
    new Set(Array.from(sourceOf.get(m.name).matchAll(STATIC_IMPORT), (x) => x[1])),
  ]),
)
const dynamicFrom = (name) =>
  new Set(Array.from(sourceOf.get(name).matchAll(DYNAMIC_IMPORT), (x) => x[1]))
const kbOf = new Map(measured.map((m) => [m.name, m.kb]))

/** Every chunk reachable from `name` through static imports, including itself. */
function closure(name) {
  const seen = new Set()
  const stack = [name]
  while (stack.length) {
    const current = stack.pop()
    if (seen.has(current) || !importsOf.has(current)) continue
    seen.add(current)
    for (const next of importsOf.get(current)) stack.push(next)
  }
  return seen
}

// First load: everything the browser fetches before the dashboard can paint,
// derived from the graph instead of listed by name. A name list goes stale in
// silence, and this one had: it omitted `ui-vendor`, which index.html
// module-preloads, and the dashboard chunk, which `/` cannot render without -
// so the gate under-counted exactly what it claimed to measure.
//
// It is the entry plus its static closure (the vendor chunks index.html
// preloads), and the dashboard route chunk plus its own, since `/` is
// `lazy(routeLoaders['/'])` and still blocks the first useful screen.
// chart-vendor falls outside this set only while nothing on the path statically
// imports it; the per-screen closure budget below is what enforces that, rather
// than a comment asserting it.
const entryChunk = measured.find((m) => /^index-.*\.js$/.test(m.name))
const dashboardChunk = measured.find((m) => /^dashboard-page-/.test(m.name))
if (!entryChunk) failures.push('no index-*.js entry chunk - has the build split changed?')
if (!dashboardChunk) failures.push('no dashboard-page chunk - has the route split changed?')
const firstLoadNames = new Set([
  ...(entryChunk ? closure(entryChunk.name) : []),
  ...(dashboardChunk ? closure(dashboardChunk.name) : []),
])
const firstLoad = [...firstLoadNames].reduce((total, n) => total + (kbOf.get(n) ?? 0), 0)

// Screens are budgeted on their *static dependency closure*, not their own
// bytes. Opening one downloads that chunk and everything it statically imports,
// so a 7 KB page that statically imports a 103 KB charting vendor costs 110 KB
// while a size check on the file alone reports 7 KB and passes. That is exactly
// how /finance and /quality came to pull recharts for a table-only default tab
// with every budget reporting green. Chunks already in the first load are free
// here: the browser has them before any route is opened.

// Which chunks are *screens*, derived from the build rather than from a naming
// convention: the entry dynamically imports one chunk per route, and a route
// chunk in turn dynamically imports its own panels. So a chunk the first load
// dynamically imports is a screen, and one imported by a screen is a panel
// behind a click. The distinction matters because a chart panel is *supposed*
// to pull the charting vendor - the user asked for a chart - while a screen
// that pulls it before its default tab renders is the defect.
const screens = new Set()
for (const name of firstLoadNames) for (const route of dynamicFrom(name)) screens.add(route)

// A shared vendor chunk is not charged to the screen that happens to need it:
// it has its own budget above and is fetched once for every screen that uses
// it, so adding 26 KB of form-vendor to each of thirty screens prices a cache
// hit as thirty downloads. What is charged is the screen's own feature code and
// anything screen-specific it drags along.
const vendorNames = new Set(
  measured.filter((m) => /-vendor-.*\.js$/.test(m.name)).map((m) => m.name),
)
const withClosure = routeChunks
  .filter((chunk) => screens.has(chunk.name))
  .map((chunk) => {
    const reached = [...closure(chunk.name)]
    const own = reached.filter((n) => !firstLoadNames.has(n) && !vendorNames.has(n))
    return {
      ...chunk,
      closureKb: own.reduce((total, n) => total + (kbOf.get(n) ?? 0), 0),
      pulls: own.filter((n) => n !== chunk.name && (kbOf.get(n) ?? 0) > 20),
      vendors: reached.filter((n) => vendorNames.has(n) && !firstLoadNames.has(n)),
    }
  })
for (const chunk of withClosure.filter((c) => c.closureKb > ROUTE_CLOSURE_KB)) {
  const via = chunk.pulls.length ? ` (pulls ${chunk.pulls.join(', ')})` : ''
  failures.push(
    `opening ${chunk.name} downloads ${chunk.closureKb.toFixed(1)} KB gzipped of feature code, ` +
      `over the ${ROUTE_CLOSURE_KB} KB per-screen closure budget${via}`,
  )
}

// The charting library must stay behind a click. It is the heaviest chunk in
// the build - larger than the entry and react-vendor together - and only two
// screens draw a chart at all, on a tab that is not their default. A screen
// that statically imports it pays for it before its first table renders, which
// is what /finance and /quality did while every budget above reported green.
// Stated as its own rule because the size budgets cannot express "not on this
// path": a lazily-imported chart panel legitimately pulls it, and does not
// appear here because a panel is not a screen.
for (const chunk of withClosure) {
  const charting = chunk.vendors.filter((n) => /^chart-vendor-/.test(n))
  if (!charting.length) continue
  failures.push(
    `opening ${chunk.name} statically pulls ${charting.join(', ')} ` +
      `(${charting.reduce((t, n) => t + (kbOf.get(n) ?? 0), 0).toFixed(1)} KB gz) before its ` +
      'default view renders. Import the charting panel with `lazy()`.',
  )
}

const heaviest = [...withClosure].sort((a, b) => b.closureKb - a.closureKb)[0]
if (heaviest) {
  console.log(
    `\nheaviest screen with its static deps: ${heaviest.name} ` +
      `${heaviest.closureKb.toFixed(1)} KB gz / budget ${ROUTE_CLOSURE_KB} KB ` +
      `(${screens.size} screens measured)`,
  )
}

console.log(
  `
first load (entry + dashboard + their static deps): ${firstLoad.toFixed(1)} KB gz` +
    ` / budget ${FIRST_LOAD_KB} KB`,
)
if (firstLoad > FIRST_LOAD_KB) {
  failures.push(
    `first load is ${firstLoad.toFixed(1)} KB gzipped, over the ${FIRST_LOAD_KB} KB budget`,
  )
}

const totalKb = measured.reduce((t, m) => t + m.kb, 0)
console.log(`total JavaScript: ${totalKb.toFixed(1)} KB gz across ${measured.length} chunks`)

if (failures.length) {
  console.error(`\n${failures.length} budget failure(s):`)
  for (const f of failures) console.error(`  - ${f}`)
  console.error(
    '\nIf the growth is justified, raise the budget in this file in the same commit, ' +
      'with a note saying what it bought.',
  )
  process.exit(1)
}
console.log('\nAll bundle budgets met.')

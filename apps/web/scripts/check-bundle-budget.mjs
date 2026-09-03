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
  form: { pattern: /^form-vendor-.*\.js$/, kb: 40, label: 'form vendor' },
  chart: { pattern: /^chart-vendor-.*\.js$/, kb: 130, label: 'chart vendor' },
  shared: { pattern: /^shared-.*\.js$/, kb: 30, label: 'shared components' },
}

/** No single lazily-loaded route chunk may exceed this, gzipped. */
const ROUTE_CHUNK_KB = 20

/** Total JavaScript a first-time visitor downloads to see the dashboard. */
const FIRST_LOAD_KB = 280

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
    failures.push(`${budget.label}: no chunk matched ${budget.pattern} - has the build split changed?`)
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

// First load: the entry plus the vendor chunks it needs, but NOT chart-vendor,
// which is imported lazily by the screens that draw charts.
const firstLoad = measured
  .filter((m) => /^(index|react-vendor|query-vendor|form-vendor|shared)-/.test(m.name))
  .reduce((total, m) => total + m.kb, 0)
console.log(`\nfirst load (entry + core vendors): ${firstLoad.toFixed(1)} KB gz / budget ${FIRST_LOAD_KB} KB`)
if (firstLoad > FIRST_LOAD_KB) {
  failures.push(`first load is ${firstLoad.toFixed(1)} KB gzipped, over the ${FIRST_LOAD_KB} KB budget`)
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

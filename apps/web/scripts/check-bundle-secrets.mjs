/**
 * Nothing secret ships to a browser.
 *
 * Everything in `dist/` is public. It is served to anyone who loads the page,
 * it is cached by their browser and by every proxy in between, and it stays
 * readable long after the deployment that produced it is gone. So a credential
 * that reaches this directory is not "in the frontend" - it is published.
 *
 * Vite makes that easy to do by accident. Any variable prefixed `VITE_` is
 * inlined into the bundle at build time, by design, with no warning; and the
 * ergonomics of a `.env` file mean a database URL and an API base URL sit three
 * lines apart. One mistyped prefix is a permanent disclosure.
 *
 * Grepping the source is not the same check. What is verified here is the built
 * artefact, after minification and after every substitution Vite performs, which
 * is the only thing that can honestly answer "is this in the bundle".
 *
 * Two kinds of finding:
 *
 *   HIGH-CONFIDENCE   a real secret shape - a JWT, a private key header, a
 *                     Postgres or Redis URL carrying a password. These fail the
 *                     build. There is no legitimate reason for any of them to
 *                     be in a browser bundle.
 *
 *   SUSPICIOUS        a hardcoded password-like or demo-credential literal.
 *                     These fail too, but list what matched, because the honest
 *                     answer is sometimes "that is a translation string".
 *                     Silence them by name in ALLOWED below, with a reason.
 *
 * Run: node scripts/check-bundle-secrets.mjs   (after `npm run build`)
 */

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import path from 'node:path'

const DIST = path.resolve(import.meta.dirname, '..', 'dist')

/**
 * Patterns that are a disclosure whatever the context.
 *
 * Each is deliberately anchored on structure rather than on a keyword, because
 * a keyword search finds the word "password" in a form label and misses
 * `postgres://atcraft:hunter2@db:5432/erp` entirely.
 */
const HIGH_CONFIDENCE = [
  {
    name: 'JWT',
    // Three base64url segments. A signed token in a bundle is either a
    // long-lived credential or a developer's own session, and both are bad.
    re: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g,
    why: 'a signed JWT - anyone who loads the page can replay it',
  },
  {
    name: 'private key',
    re: /-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----/g,
    why: 'a private key',
  },
  {
    name: 'database URL with password',
    // The password position must be non-empty; `postgres://user@host` alone is
    // a hostname, not a credential.
    re: /\b(?:postgres(?:ql)?|mysql|mongodb)(?:\+\w+)?:\/\/[^\s:'"@/]+:[^\s'"@/]+@/g,
    why: 'a database connection string including its password',
  },
  {
    name: 'redis URL with password',
    re: /\bredis(?:s)?:\/\/[^\s:'"@/]*:[^\s'"@/]+@/g,
    why: 'a Redis connection string including its password',
  },
  {
    name: 'AWS access key id',
    re: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g,
    why: 'an AWS access key id',
  },
  {
    name: 'Slack or GitHub token',
    re: /\b(?:xox[baprs]-[0-9A-Za-z-]{10,}|gh[pousr]_[0-9A-Za-z]{36,})\b/g,
    why: 'a service token',
  },
  {
    name: 'PEM certificate body',
    re: /-----BEGIN CERTIFICATE-----/g,
    why: 'an embedded certificate',
  },
]

/**
 * Credential-shaped literals. Lower confidence, still fatal by default.
 *
 * The demo-password entry is the one that matters most for this project: the
 * demo data and the seed script publish known logins, and a login screen that
 * helpfully pre-fills them ships those logins to the client's production site.
 */
const SUSPICIOUS = [
  {
    name: 'assigned password literal',
    // `password: "something"` / `password = 'something'`, at least 6 chars.
    // Excludes empty strings and the single-character placeholders a form uses.
    re: /\b(?:password|passwd|pwd|secret|api_?key|apikey|access_?token|private_?key)\b\s*[:=]\s*["'`]([^"'`\s]{6,})["'`]/gi,
    why: 'a hardcoded credential literal',
  },
  {
    name: 'seeded demo credential',
    // The shapes this repo's own seed and demo data actually use.
    re: /\b(?:Atcraft@|Change[Mm]e|ChangeMe123|admin123|password123|demo1234|Passw0rd)[A-Za-z0-9@#$%^&*!._-]*/g,
    why: "a demo or default password - src/core/seed.py's logins are public knowledge",
  },
  {
    name: 'basic auth in a URL',
    re: /\bhttps?:\/\/[^\s:'"@/]+:[^\s'"@/]{3,}@/g,
    why: 'HTTP basic-auth credentials embedded in a URL',
  },
]

/**
 * Known-good matches, by the exact text that matched.
 *
 * Add an entry only with a reason, and only when the match is genuinely not a
 * credential. An allowance is a decision somebody made; a loosened regex is a
 * check that quietly stopped applying.
 */
const ALLOWED = new Map([
  // e.g. ['password: "current"', 'a form field name in the change-password dialog'],
])

/** Every text-ish file under dist, recursively. */
function collect(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry)
    const stat = statSync(full)
    if (stat.isDirectory()) {
      out.push(...collect(full))
      continue
    }
    // Fonts and images cannot carry a readable credential and are large enough
    // to make scanning them slow for nothing.
    if (/\.(woff2?|ttf|eot|png|jpe?g|gif|webp|avif|ico|mp4|webm)$/i.test(entry)) continue
    out.push(full)
  }
  return out
}

/** A short, non-disclosing excerpt. The finding is the location, not the value. */
function redact(match) {
  const text = String(match)
  if (text.length <= 12) return text
  return `${text.slice(0, 6)}...${text.slice(-4)} (${text.length} chars)`
}

if (!existsSync(DIST)) {
  console.error(`check-bundle-secrets: no dist/ at ${DIST} - run \`vite build\` first.`)
  process.exit(1)
}

const files = collect(DIST)
if (files.length === 0) {
  console.error('check-bundle-secrets: dist/ is empty - refusing to report a clean bundle.')
  process.exit(1)
}

const findings = []

for (const file of files) {
  const relative = path.relative(DIST, file)
  const content = readFileSync(file, 'utf8')

  for (const { name, re, why } of HIGH_CONFIDENCE) {
    for (const match of content.matchAll(re)) {
      findings.push({ severity: 'HIGH', name, why, file: relative, match: match[0] })
    }
  }
  for (const { name, re, why } of SUSPICIOUS) {
    for (const match of content.matchAll(re)) {
      if (ALLOWED.has(match[0])) continue
      findings.push({ severity: 'SUSPICIOUS', name, why, file: relative, match: match[0] })
    }
  }
}

const scanned = `${files.length} file${files.length === 1 ? '' : 's'}`

if (findings.length === 0) {
  console.log(`✓ no credentials in the bundle (${scanned} scanned)`)
  process.exit(0)
}

console.error(`\nCredential-shaped content in the built bundle (${scanned} scanned):\n`)
for (const f of findings) {
  console.error(`  [${f.severity}] ${f.name} in ${f.file}`)
  console.error(`           ${f.why}`)
  console.error(`           matched: ${redact(f.match)}`)
}
console.error(
  '\nEverything in dist/ is public and cached indefinitely. Remove the value,\n' +
    'rotate it if it was ever real, and rebuild. If a match is genuinely not a\n' +
    'credential, add it to ALLOWED in this script with the reason.\n'
)
process.exit(1)

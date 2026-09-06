/**
 * Build the client handbook: one PDF, every screen, with a screenshot of each
 * taken from the running application.
 *
 * Renders an HTML document in a real browser and prints it. Playwright is
 * already here for the end-to-end tests and its `page.pdf()` is a real Chromium
 * print, so the fonts, the page breaks and the images are the ones a reader will
 * see - no separate PDF library, and nothing to keep in step with the CSS.
 *
 * Landscape A4, because every screenshot is a 16:10 desktop and a portrait page
 * would print each one at half the width with white above and below it.
 *
 *   node scripts/build-handbook.mjs
 *
 * Inputs:  docs/handbook/screens/*.png   from capture-screens.mjs
 *          docs/handbook/content.json    the written chapters
 * Output:  docs/handbook/Atcraft-ERP-Handbook.pdf
 */

import { readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from '@playwright/test'

const HANDBOOK = fileURLToPath(new URL('../../../docs/handbook/', import.meta.url))
const SCREENS = path.join(HANDBOOK, 'screens')

const content = JSON.parse(await readFile(path.join(HANDBOOK, 'content.json'), 'utf8'))
const shots = JSON.parse(await readFile(path.join(SCREENS, 'index.json'), 'utf8'))

/**
 * Screenshot filename by route.
 *
 * The chapters name routes the way the application declares them - with a
 * parameter, `/items/:id` - while a capture necessarily names a real record,
 * `/items/01a071...`. Both are reduced to the same shape so the two meet: every
 * id-looking segment becomes `:id`, and the query string is dropped for a
 * second, looser key.
 */
function shapes(route) {
  const withoutQuery = route.split('?')[0]
  const parameterised = withoutQuery
    .split('/')
    .map((segment) => (/^[0-9a-f-]{20,}$/i.test(segment) ? ':id' : segment))
    .join('/')
  return [route, withoutQuery, parameterised]
}

const byRoute = new Map()
for (const shot of shots.captured) {
  for (const key of shapes(shot.route)) if (!byRoute.has(key)) byRoute.set(key, shot.file)
}
// The sign-in screen is captured before the walk begins, so it is not in the index.
byRoute.set('/login', '00-login.png')

/** `:orderId`, `:bomId` and friends all mean the same thing as `:id` here. */
function lookup(route) {
  for (const key of shapes(route.replace(/:[A-Za-z]+/g, ':id'))) {
    const file = byRoute.get(key)
    if (file) return file
  }
  return null
}

const escape = (value) =>
  String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')

/** A screenshot as a data URI, so the PDF carries its own images. */
async function image(file) {
  const data = await readFile(path.join(SCREENS, file))
  return `data:image/png;base64,${data.toString('base64')}`
}

const images = new Map()
for (const shot of shots.captured) images.set(shot.file, await image(shot.file))
images.set('00-login.png', await image('00-login.png'))

/** Screens that genuinely cannot be photographed, and the honest reason. */
const NO_SHOT = {
  '/change-password':
    'This screen only appears when a password change is actually required, so it '
    + 'cannot be reached - or photographed - by an account that is already set up.',
  '/procurement/rfqs/:id':
    'The demonstration data contains no request for quotation to open. The screen '
    + 'is built and works; there was simply no document to photograph.',
}

function figure(route, caption) {
  const file = lookup(route)
  if (!file || !images.has(file)) {
    const why = NO_SHOT[route.replace(/:[A-Za-z]+/g, ':id')]
    return `<p class="missing">${escape(why ?? 'No screenshot was captured for this screen.')}</p>`
  }
  return `<figure><img src="${images.get(file)}" alt="${escape(caption)}" /></figure>`
}

// -----------------------------------------------------------------------------
// The document
// -----------------------------------------------------------------------------

const { frame, chapters } = content

const contentsRows = chapters
  .map(
    (chapter) =>
      `<li><span class="c-title">${escape(chapter.groupTitle)}</span>` +
      `<span class="c-screens">${chapter.screens.length} screens</span></li>`,
  )
  .join('')

const rolesRows = frame.roles
  .map(
    (role) =>
      `<tr><td class="role">${escape(role.role)}</td><td>${escape(role.forWhom)}</td>` +
      `<td>${escape(role.canDo)}</td></tr>`,
  )
  .join('')

const journeySteps = frame.endToEnd
  .map(
    (step) => `
      <div class="journey-step">
        <div class="j-num">${step.step}</div>
        <div class="j-body">
          <h3>${escape(step.title)}</h3>
          <p class="j-where">${escape(step.route)}</p>
          <p>${escape(step.what)}</p>
          <p class="j-effect"><strong>What it changes:</strong> ${escape(step.effect)}</p>
        </div>
      </div>`,
  )
  .join('')

function screenPage(screen) {
  const actions = screen.keyActions
    .map(
      (a) =>
        `<li><strong>${escape(a.action)}</strong> — ${escape(a.how)}` +
        (a.note ? `<span class="note">${escape(a.note)}</span>` : '') +
        `</li>`,
    )
    .join('')

  const flow = (screen.flow ?? []).map((step) => `<li>${escape(step)}</li>`).join('')
  const rules = (screen.rules ?? []).map((rule) => `<li>${escape(rule)}</li>`).join('')

  return `
    <section class="screen">
      <header class="screen-head">
        <div>
          <h2>${escape(screen.title)}</h2>
          <p class="where">${escape(screen.route)}</p>
        </div>
        <p class="who">${escape(screen.whoUses)}</p>
      </header>

      <div class="screen-body">
        ${figure(screen.route, screen.title)}
        <div class="intro">
          <p class="purpose">${escape(screen.purpose)}</p>
          <p class="perm"><strong>Who may use it:</strong> ${escape(screen.permission)}</p>
        </div>
      </div>

      <div class="guide">
        <h4>What you can do here</h4>
        <ul class="actions">${actions}</ul>
        ${flow ? `<h4>Step by step</h4><ol class="flow">${flow}</ol>` : ''}
        ${rules ? `<h4>Rules this screen enforces</h4><ul class="rules">${rules}</ul>` : ''}
      </div>
    </section>`
}

const chapterPages = chapters
  .map(
    (chapter) => `
    <section class="chapter-open">
      <h1>${escape(chapter.groupTitle)}</h1>
      <p class="chapter-intro">${escape(chapter.groupIntro)}</p>
    </section>
    ${chapter.screens.map(screenPage).join('')}`,
  )
  .join('')

const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<style>
  @page { size: A4 landscape; margin: 12mm 14mm; }

  :root {
    --ink: #16191f;
    --muted: #5b6472;
    --line: #dfe3e9;
    --brand: #14448c;
    --accent: #eef3fb;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0; color: var(--ink);
    font: 10.5pt/1.5 "Segoe UI", -apple-system, system-ui, sans-serif;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  h1, h2, h3, h4 { margin: 0 0 .35em; line-height: 1.25; }

  /* --- cover ------------------------------------------------------------ */
  .cover {
    height: 175mm; display: flex; flex-direction: column; justify-content: center;
    page-break-after: always; padding-left: 6mm;
    border-left: 5mm solid var(--brand);
  }
  .cover .mark {
    width: 22mm; height: 22mm; border-radius: 5mm; background: var(--brand);
    color: #fff; font-size: 30pt; font-weight: 700;
    display: grid; place-items: center; margin-bottom: 10mm;
  }
  .cover h1 { font-size: 34pt; letter-spacing: -.5pt; }
  .cover .sub { font-size: 15pt; color: var(--muted); margin: 3mm 0 0; max-width: 170mm; }
  .cover .foot { margin-top: 14mm; font-size: 10pt; color: var(--muted); }

  /* --- general page ------------------------------------------------------ */
  section { page-break-after: always; }
  section:last-child { page-break-after: auto; }

  .chapter-open {
    height: 172mm; display: flex; flex-direction: column; justify-content: center;
    border-top: 3px solid var(--brand);
  }
  .chapter-open h1 { font-size: 26pt; color: var(--brand); }
  .chapter-intro { font-size: 13pt; color: var(--muted); max-width: 190mm; line-height: 1.6; }

  /* --- a screen: one page, image beside its guidance ---------------------- */
  /* min-height, not height, and nothing is clipped. A screen with a long list of
     rules runs onto a second page - which is the right trade: a handbook that
     quietly drops a rule is worse than one that is a few pages longer. */
  /* No height at all: page-break-after already gives each screen its own page,
     and a forced minimum only pushed the rules box onto a second one. Content
     decides, and nothing is clipped - a screen with an unusually long list of
     rules is allowed to run over rather than lose any of them. */
  .screen { display: flex; flex-direction: column; }
  .screen-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 8mm; border-bottom: 2px solid var(--brand); padding-bottom: 1.5mm; margin-bottom: 3mm;
    flex: 0 0 auto;
  }
  .screen-head h2 { font-size: 15pt; color: var(--brand); }
  .where { margin: 0; font-size: 8pt; color: var(--muted); font-family: ui-monospace, Consolas, monospace; }
  .who { margin: 0; font-size: 8.5pt; color: var(--muted); text-align: right; max-width: 75mm; flex: 0 0 auto; }

  /* Landscape is wide, not tall. A 16:10 screenshot printed the full width of
     the page is 168mm high and leaves nothing for the words, which is how the
     first draft became 215 pages. Beside it, the image is 96mm high and the
     guidance sits in the space the page already had. */
  /* The screenshot and what the screen is for share the top of the page; the
     guidance flows beneath in two columns across the full width. Written as one
     narrow column beside the image it ran to 440mm on the busier screens - four
     times the height of the picture it was explaining. */
  .screen-body { display: grid; grid-template-columns: 158mm 1fr; gap: 6mm; align-items: start; }
  .intro { padding-top: 1mm; }
  .purpose { margin: 0 0 3mm; font-size: 9.5pt; }

  .guide { margin-top: 4mm; columns: 2; column-gap: 8mm; font-size: 9pt; }
  .guide h4 { break-after: avoid; }
  .guide ul, .guide ol { break-inside: auto; }
  .guide li { break-inside: avoid; }

  figure { margin: 0; border: 1px solid var(--line); border-radius: 1.5mm; overflow: hidden; }
  figure img { display: block; width: 100%; }

  h4 {
    font-size: 8.5pt; text-transform: uppercase; letter-spacing: .4pt;
    color: var(--brand); margin: 0 0 1.5mm;
  }

  ul, ol { margin: 0 0 2.5mm; padding-left: 4.5mm; }
  li { margin-bottom: 1.2mm; }
  .actions .note { display: block; color: var(--muted); font-size: 8.5pt; }
  .rules li { color: #4a3a12; }

  .perm {
    margin: 0; padding: 2mm 2.5mm; background: var(--accent);
    border-radius: 1.5mm; font-size: 8.5pt;
  }
  .missing {
    color: var(--muted); font-style: italic; padding: 8mm; border: 1px dashed var(--line);
    border-radius: 1.5mm; font-size: 9.5pt;
  }

  /* --- contents ---------------------------------------------------------- */
  .contents ol { list-style: none; padding: 0; }
  .contents li {
    display: flex; justify-content: space-between; gap: 6mm;
    padding: 2mm 0; border-bottom: 1px dotted var(--line); font-size: 12pt;
  }
  .c-screens { color: var(--muted); font-size: 10pt; white-space: nowrap; }

  /* --- the worked example ------------------------------------------------ */
  .journey-step { display: flex; gap: 5mm; margin-bottom: 4mm; break-inside: avoid; }
  .j-num {
    flex: 0 0 9mm; height: 9mm; border-radius: 50%; background: var(--brand); color: #fff;
    display: grid; place-items: center; font-weight: 700; font-size: 11pt;
  }
  .j-body h3 { font-size: 12pt; }
  .j-where { margin: 0 0 1mm; font-size: 8.5pt; color: var(--muted); font-family: ui-monospace, Consolas, monospace; }
  .j-body p { margin: 0 0 1mm; }
  .j-effect { color: var(--brand); }

  table { width: 100%; border-collapse: collapse; font-size: 10pt; }
  th, td { text-align: left; padding: 2mm 3mm; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { background: var(--accent); color: var(--brand); font-size: 9pt; text-transform: uppercase; letter-spacing: .3pt; }
  td.role { font-weight: 600; white-space: nowrap; }

  .lead { font-size: 12pt; max-width: 200mm; }
  .callout {
    background: var(--accent); border-left: 3px solid var(--brand);
    padding: 3mm 4mm; border-radius: 0 2mm 2mm 0; margin: 4mm 0;
  }
</style></head>
<body>

<section class="cover">
  <div class="mark">A</div>
  <h1>Atcraft Manufacturing ERP</h1>
  <p class="sub">A guide to every screen, what it is for, and how your team will use it.</p>
  <p class="foot">Prepared for the owner and staff of Atcraft &middot; every screenshot in this
  document was taken from the working system.</p>
</section>

<section>
  <h1>What this system is</h1>
  <p class="lead">${escape(frame.systemSummary)}</p>
  <div class="callout"><p>${escape(frame.whatItReplaces)}</p></div>
  <h4>Signing in</h4>
  <p>${escape(frame.signIn)}</p>
  ${figure('/login', 'The sign-in screen')}
</section>

<section class="contents">
  <h1>What is in this guide</h1>
  <ol>${contentsRows}</ol>
  <h4 style="margin-top:6mm">Finding your way around</h4>
  <p>${escape(frame.navigation)}</p>
</section>

<section>
  <h1>Your business, end to end</h1>
  <p class="lead">One order, from the customer asking for it to the goods leaving the
  building — and what each step changes.</p>
  ${journeySteps}
</section>

${chapterPages}

<section>
  <h1>Who does what</h1>
  <p class="lead">Every person signs in as themselves. What they can see and change is
  decided by their role.</p>
  <table>
    <thead><tr><th>Role</th><th>For whom</th><th>What they may do</th></tr></thead>
    <tbody>${rolesRows}</tbody>
  </table>
</section>

<section>
  <h1>What you can change yourselves</h1>
  <p class="lead">${escape(frame.configurable)}</p>
  <div class="callout">
    <h4>When a screen refuses something</h4>
    <p>${escape(frame.gettingHelp)}</p>
  </div>
</section>

</body></html>`

await writeFile(path.join(HANDBOOK, 'handbook.html'), html)

const browser = await chromium.launch()
const page = await browser.newPage()
await page.setContent(html, { waitUntil: 'load' })
await page.emulateMedia({ media: 'print' })

const out = path.join(HANDBOOK, 'Atcraft-ERP-Handbook.pdf')
await page.pdf({
  path: out,
  format: 'A4',
  landscape: true,
  printBackground: true,
  margin: { top: '12mm', bottom: '12mm', left: '14mm', right: '14mm' },
  displayHeaderFooter: true,
  headerTemplate: '<div></div>',
  footerTemplate: `
    <div style="width:100%;font:8pt 'Segoe UI',sans-serif;color:#5b6472;
                padding:0 14mm;display:flex;justify-content:space-between;">
      <span>Atcraft Manufacturing ERP — guide</span>
      <span class="pageNumber"></span>
    </div>`,
})
await browser.close()

const screenCount = chapters.reduce((n, c) => n + c.screens.length, 0)
console.log(`\n${out}`)
console.log(`${chapters.length} chapters, ${screenCount} screens, ${images.size} screenshots`)

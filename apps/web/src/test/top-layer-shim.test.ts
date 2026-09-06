/**
 * The shim in `setup.ts` that keeps `Element.prototype.matches` from recursing.
 *
 * Why this deserves its own test: without the shim, four test files paid about
 * 644 seconds between them - 14 tests at 43-57 s each - and every one of them
 * looked like an ordinary slow test rather than a defect with a cause. The
 * failure mode is silent and expensive, and it arrives disguised: a timeout in
 * whichever screen test happens to open a dropdown that week.
 *
 * jsdom uses nwsapi as a library and never calls its `install()`, so nwsapi's
 * `_matches` never holds a native matcher and `matchesNative` falls back to
 * `node.matches` - which is nwsapi. `isModal` is
 * `matchesNative(node, ':modal') || isFullscreen(node)`, so the query re-enters
 * itself; the RangeError from the blown stack is swallowed by nwsapi's own
 * try/catch, and each unwinding frame starts the other branch again. One
 * `matches(':modal')` costs ~1.7 million internal calls.
 *
 * So the assertion that matters is a TIME one. It is the only way to observe
 * the difference: the shimmed and unshimmed answers are identical - `false` -
 * and differ only by six orders of magnitude of work.
 */

import { describe, expect, it } from 'vitest'

/**
 * Generous on purpose. One unshimmed call was measured at 595-740 ms and an
 * ordinary selector at ~0.003 ms, so anything in between is unambiguous. This
 * must not flake on a loaded box, and 50 ms is ~12x under the failure it
 * catches while ~16,000x over the passing cost.
 */
const BUDGET_MS = 50

describe('the top-layer selectors', () => {
  it('answer without recursing into nwsapi', () => {
    const element = document.createElement('div')
    document.body.appendChild(element)

    const started = performance.now()
    const answers = [
      element.matches(':modal'),
      element.matches(':fullscreen'),
      element.matches(':popover-open'),
    ]
    const elapsed = performance.now() - started

    // jsdom has no top layer, so false is also the correct answer - the shim
    // changes only how long it takes to say so.
    expect(answers).toEqual([false, false, false])
    expect(elapsed).toBeLessThan(BUDGET_MS)

    element.remove()
  })

  it('answer the same way through querySelector, which Floating UI also reaches', () => {
    // isTopLayer asks via `element.matches`, but a regression in nwsapi would
    // show up in any selector path; this keeps the pin from being too narrow.
    const started = performance.now()

    expect(document.querySelector(':modal')).toBeNull()

    expect(performance.now() - started).toBeLessThan(BUDGET_MS)
  })
})

describe('everything else', () => {
  it('still goes to the real matcher', () => {
    const element = document.createElement('div')
    element.className = 'foo'
    element.setAttribute('data-x', '1')

    expect(element.matches('div.foo[data-x]')).toBe(true)
    expect(element.matches('span')).toBe(false)
    expect(element.matches(':not(span)')).toBe(true)
  })

  it('leaves :open and :closed alone, because those have real semantics', () => {
    // Several of the per-file copies this shim replaced also swallowed these
    // two. nwsapi answers them from a portable branch - a <details> or <dialog>
    // whose `open` is true - before it ever reaches the recursing path, so
    // returning false for them would be a genuine behaviour change bought for
    // no speed at all.
    const details = document.createElement('details')
    details.open = true

    expect(details.matches(':open')).toBe(true)
    expect(details.matches(':closed')).toBe(false)
  })

  it('does not mistake a selector that merely contains a shimmed name', () => {
    const element = document.createElement('div')
    element.className = 'modal'

    // `.modal` is a class, not `:modal`; a substring match instead of an exact
    // one would answer false here and break every dialog test in the suite.
    expect(element.matches('.modal')).toBe(true)
  })
})

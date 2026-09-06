import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => cleanup())

// jsdom implements neither of these, and both are touched during a normal
// render of the layout (theme detection) and the virtualised table.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

// jsdom implements neither the Pointer Capture API nor `scrollIntoView`, and
// Radix's select, popover, menu and tooltip primitives call both while opening.
// Without them, operating a real control throws inside the event handler and the
// failure reads as "no option found" - which looks like a missing option rather
// than a missing browser API, and pushes a test towards asserting state instead
// of operating the control a user operates.
const elementProto = window.Element.prototype as unknown as Record<string, unknown>
elementProto.hasPointerCapture ??= () => false
elementProto.setPointerCapture ??= () => {}
elementProto.releasePointerCapture ??= () => {}
elementProto.scrollIntoView ??= () => {}

// jsdom has no top layer, so `:modal`, `:fullscreen` and `:popover-open` are
// false for every element in it. Answering them here is about TIME, not
// correctness - the answer is the same one nwsapi eventually gives, roughly
// 1.7 million recursive calls sooner.
//
// jsdom uses nwsapi as a library and never calls its `install()`, so nwsapi's
// internal `_matches` never holds a native matcher. Its `matchesNative` then
// falls back to `node.matches` - which IS nwsapi - and `isModal` is
// `matchesNative(node, ':modal') || isFullscreen(node)`, so one query re-enters
// itself until the stack overflows. Its own try/catch swallows that RangeError,
// and every unwinding frame starts the other branch again, so the blowup is
// depth-squared rather than linear. One `matches(':modal')` costs ~1.7M internal
// calls and ~0.6 s; `matches(':hover')` in the same run costs 0.2 ms.
//
// Floating UI's `isTopLayer` asks exactly these on every popper position pass,
// and Radix positions Select, DropdownMenu, Popover and Tooltip with Floating UI
// at `strategy: 'fixed'`, which walks every ancestor on every measurement. So 46
// honest probes become ~6.1e7 calls. Measured on one bare 24-node Select with no
// providers and no page: 30,961 ms and 63,395,097 calls without this shim, 72 ms
// and 46 calls with it, A/B'd OFF/ON/ON/OFF in a single process so it cannot be
// warm-up. Across the suite it was 14 tests in 4 files paying ~644 s.
//
// Fifteen of the sixteen files that open a popper already carried a private copy
// of this; the four slow files were the ones it never reached. One copy, here,
// is the point.
//
// Exactly these three selectors, and deliberately NOT `:open` / `:closed`, which
// several of those private copies also swallowed: nwsapi answers those from a
// real portable branch (`<details>`/`<dialog>` with `open === true`) before it
// ever reaches the recursing path, so returning false for them is a genuine
// behaviour change. `:modal` is what costs; `:fullscreen` is reached from inside
// `isModal`; `:popover-open` is the other selector `isTopLayer` asks and would
// recurse the moment an element carries a `popover` attribute.
//
// nwsapi 2.2.27, reached through jsdom 25.0.1's `"nwsapi": "^2.2.12"`. Delete
// this when nwsapi stops self-recursing. If a test ever needs real top-layer
// semantics, restore `nativeMatches` for that test rather than widening this.
const TOP_LAYER_SELECTORS = new Set([':modal', ':fullscreen', ':popover-open'])
const nativeMatches = window.Element.prototype.matches
window.Element.prototype.matches = function (this: Element, selectors: string): boolean {
  if (typeof selectors === 'string' && TOP_LAYER_SELECTORS.has(selectors.trim())) return false
  return nativeMatches.call(this, selectors)
}

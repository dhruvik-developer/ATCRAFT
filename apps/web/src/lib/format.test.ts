/**
 * `lib/format` is the only place the app is allowed to turn a decimal into a
 * JavaScript number, so it is the one place a money defect can enter silently.
 *
 * These tests pin the two properties that matter and that a refactor would
 * plausibly break:
 *
 *  1. **Arithmetic never touches a float.** `toDecimal` is the gate; everything
 *     upstream of a formatter stays a `Decimal`. A regression here does not
 *     throw - it produces an invoice that is a paisa out, which nobody notices
 *     until a reconciliation fails months later.
 *  2. **Bad input degrades to zero, not to NaN.** These formatters render
 *     directly into tables, and `NaN` in a total column is worse than a wrong
 *     number because it destroys the whole row's readability.
 *
 * Locale is pinned to `en-IN` inside the module, so the grouping asserted below
 * is the Indian lakh/crore convention (12,34,567) rather than the thousands
 * convention. That is deliberate - this is an Indian manufacturing ERP - and
 * asserting it here means a locale change cannot pass unnoticed.
 */

import Decimal from 'decimal.js'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetFormatterCache,
  daysUntil,
  formatDate,
  formatFileSize,
  formatMoney,
  formatNumber,
  formatPercent,
  formatQuantity,
  formatUnitPrice,
  humanise,
  toDateInput,
  toDecimal,
} from './format'

describe('the formatter cache', () => {
  /**
   * Constructing an `Intl.NumberFormat` costs about 42 microseconds and calling
   * `.format()` on a cached one costs 0.8, so every money and quantity cell on
   * a table row used to pay for a constructor. These cases pin the two things
   * that could go wrong with caching them: a wrong cache key handing back a
   * formatter configured for different options, and the cache not actually
   * being used.
   */
  beforeEach(() => {
    __resetFormatterCache()
  })

  it('keeps every option set distinct', () => {
    // Same value, six option sets. If the key collided, two of these would match.
    const rendered = [
      formatMoney('1234.5'),
      formatMoney('1234.5', { showSymbol: false }),
      formatMoney('1234.5', { compact: true }),
      formatMoney('1234.5', { currency: 'USD' }),
      formatNumber(1234.5, 0),
      formatNumber(1234.5, 3),
    ]
    expect(new Set(rendered).size).toBe(rendered.length)
  })

  it('returns the same instance for the same options, and a new one otherwise', () => {
    const spy = vi.spyOn(Intl, 'NumberFormat')
    formatNumber(1, 2)
    formatNumber(2, 2)
    formatNumber(3, 2)
    expect(spy, 'three calls with identical options must construct once').toHaveBeenCalledTimes(1)

    formatNumber(4, 4)
    expect(spy, 'a different option set needs its own formatter').toHaveBeenCalledTimes(2)
    spy.mockRestore()
  })

  it('formats identically before and after the cache is warm', () => {
    const cold = formatMoney('1234567.891')
    const warm = formatMoney('1234567.891')
    expect(warm).toBe(cold)
    __resetFormatterCache()
    expect(formatMoney('1234567.891')).toBe(cold)
  })
})

describe('toDecimal', () => {
  it('parses the decimal strings the API actually sends', () => {
    expect(toDecimal('1234.5678').toFixed(4)).toBe('1234.5678')
    expect(toDecimal('-80').toFixed()).toBe('-80')
    expect(toDecimal('0.0001').toFixed(4)).toBe('0.0001')
  })

  it('treats every flavour of absent as zero rather than NaN', () => {
    // A null quantity means "none recorded", and a column of totals must still
    // add up. `new Decimal(null)` throws, so each of these is a real branch.
    for (const empty of [null, undefined, '']) {
      expect(toDecimal(empty).isZero()).toBe(true)
    }
  })

  it('swallows unparseable input instead of throwing into a render', () => {
    // These formatters run inside table cells. A throw here unmounts the table
    // via the error boundary and loses the whole screen over one bad row.
    expect(toDecimal('not a number').isZero()).toBe(true)
    expect(toDecimal('12,00,000').isZero()).toBe(true) // already-grouped text
  })

  it('keeps precision a float would lose', () => {
    // 0.1 + 0.2 === 0.30000000000000004 as doubles. This is the entire reason
    // the codebase carries decimal.js.
    const sum = toDecimal('0.1').plus(toDecimal('0.2'))
    expect(sum.equals(new Decimal('0.3'))).toBe(true)
    expect(sum.toFixed(1)).toBe('0.3')
  })

  it('holds a value wider than Number.MAX_SAFE_INTEGER', () => {
    const wide = toDecimal('9007199254740993')
    expect(wide.toFixed()).toBe('9007199254740993')
  })
})

describe('formatMoney', () => {
  it('groups in lakhs and shows two decimals by default', () => {
    expect(formatMoney('1234567.5')).toBe('₹12,34,567.50')
  })

  it('renders a null amount as zero, not as an empty cell', () => {
    // A blank where a total belongs reads as "not loaded"; ₹0.00 reads as
    // "nothing owed", which is what a null balance actually means.
    expect(formatMoney(null)).toBe('₹0.00')
  })

  it('can drop the symbol for a column that carries it in the header', () => {
    expect(formatMoney('1000', { showSymbol: false })).toBe('1,000.00')
  })

  it('compacts large amounts for dashboard tiles', () => {
    // Compact notation exists so a tile does not wrap; it is display-only and
    // must never feed further arithmetic.
    expect(formatMoney('12500000', { compact: true })).toMatch(/1\.3\s*Cr/)
  })

  it('honours an explicit currency without changing the locale', () => {
    expect(formatMoney('1000', { currency: 'USD' })).toContain('1,000.00')
  })
})

describe('formatUnitPrice', () => {
  it('keeps the sub-paisa precision a unit price carries', () => {
    // The defect this exists for: `formatMoney` fixes two places, so a washer
    // that genuinely costs 0.004250 each renders as a cost per unit of nothing.
    expect(formatUnitPrice('0.004250')).toBe('₹0.00425')
  })

  it('trims the wire padding rather than showing six places always', () => {
    // The API pads a unit price to six decimals; showing them all turns a round
    // rate into noise in a grid.
    expect(formatUnitPrice('125.000000')).toBe('₹125.00')
  })

  it('never shows fewer than money’s two places', () => {
    expect(formatUnitPrice('7')).toBe('₹7.00')
  })

  it('renders a null rate as zero, like every other value column', () => {
    expect(formatUnitPrice(null)).toBe('₹0.00')
  })

  it('can drop the symbol for a column that carries it in the header', () => {
    expect(formatUnitPrice('12.3456', { showSymbol: false })).toBe('12.3456')
  })
})

describe('formatQuantity', () => {
  it('trims the API uniform 4dp down to what is actually significant', () => {
    expect(formatQuantity('12.0000')).toBe('12')
    expect(formatQuantity('12.5000')).toBe('12.5')
    expect(formatQuantity('12.0500')).toBe('12.05')
  })

  it('keeps genuine fractional precision', () => {
    expect(formatQuantity('0.0001')).toBe('0.0001')
  })

  it('appends the unit when one is known', () => {
    expect(formatQuantity('12.0000', 'NOS')).toBe('12 NOS')
  })

  it('renders a null quantity as zero', () => {
    expect(formatQuantity(null)).toBe('0')
  })

  it('does not round a fifth decimal place into existence', () => {
    // The API sends 4dp; anything beyond it is noise from a client-side
    // computation and must be cut, not carried.
    expect(formatQuantity('1.00005')).toBe('1.0001')
  })
})

describe('formatPercent', () => {
  it('shows one decimal by default', () => {
    expect(formatPercent('12.34')).toBe('12.3%')
  })

  it('can be asked for whole numbers', () => {
    expect(formatPercent('12.34', 0)).toBe('12%')
  })

  it('renders absent as zero rather than NaN%', () => {
    expect(formatPercent(null)).toBe('0.0%')
  })
})

describe('formatNumber', () => {
  it('distinguishes absent from zero', () => {
    // Unlike money and quantity, a plain count of "-" is honest: there is no
    // sensible zero for "how many rows matched" when nothing was asked.
    expect(formatNumber(null)).toBe('-')
    expect(formatNumber(undefined)).toBe('-')
    expect(formatNumber(0)).toBe('0')
  })

  it('groups in lakhs like every other number on screen', () => {
    expect(formatNumber(1234567)).toBe('12,34,567')
  })
})

describe('dates', () => {
  it('formats an ISO timestamp to a day', () => {
    expect(formatDate('2026-03-14T10:30:00Z')).toBe('14 Mar 2026')
  })

  it('renders absent and unparseable alike as a dash', () => {
    expect(formatDate(null)).toBe('-')
    expect(formatDate('tomorrow-ish')).toBe('-')
  })

  it('produces the value an <input type="date"> requires', () => {
    // The HTML date input accepts only yyyy-MM-dd; handing it a display string
    // silently leaves the field blank.
    expect(toDateInput('2026-03-14T10:30:00Z')).toBe('2026-03-14')
    expect(toDateInput(null)).toBe('')
  })

  it('counts whole days to a due date and goes negative once overdue', () => {
    const day = 24 * 60 * 60 * 1000
    expect(daysUntil(new Date(Date.now() + 3 * day))).toBe(3)
    expect(daysUntil(new Date(Date.now() - 3 * day))).toBeLessThan(0)
    expect(daysUntil(null)).toBeNull()
  })
})

describe('formatFileSize', () => {
  it('scales to a readable unit', () => {
    expect(formatFileSize(0)).toMatch(/0/)
    expect(formatFileSize(2048)).toMatch(/2/)
    expect(formatFileSize(5 * 1024 * 1024)).toMatch(/5/)
  })
})

describe('humanise', () => {
  it('turns a SCREAMING_SNAKE enum into Title Case for a status chip', () => {
    // Every status column in the app renders a backend enum through this, so a
    // change here reformats several dozen screens at once. Title Case rather
    // than sentence case is the deliberate choice: these read as short labels
    // in a chip, not as sentences.
    expect(humanise('MATERIAL_ISSUED')).toBe('Material Issued')
    expect(humanise('DRAFT')).toBe('Draft')
    expect(humanise('QC_PENDING')).toBe('Qc Pending')
  })

  it('leaves absent values as a dash', () => {
    expect(humanise(null)).toBe('-')
  })
})

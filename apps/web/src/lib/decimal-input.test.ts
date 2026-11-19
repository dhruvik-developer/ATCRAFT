import { describe, expect, it } from 'vitest'

import {
  CONVERSION_FACTOR,
  MEASURE,
  MONEY_INPUT,
  OPTIONAL_MEASURE_INPUT,
  OPTIONAL_MONEY_INPUT,
  OPTIONAL_PERCENT_INPUT,
  OPTIONAL_QUANTITY_INPUT,
  OPTIONAL_UNIT_PRICE_INPUT,
  PERCENTAGE,
  PERCENT_INPUT,
  QUANTITY,
  QUANTITY_INPUT,
  ROUNDED_MONEY,
  ROUNDED_MONEY_INPUT,
  UNIT_PRICE_INPUT,
  decimalPattern,
  toDecimalInput,
} from './decimal-input'

const accepts = (pattern: RegExp, values: string[]) =>
  values.filter((value) => !pattern.test(value))
const refuses = (pattern: RegExp, values: string[]) => values.filter((value) => pattern.test(value))

describe('decimalPattern', () => {
  it('bounds the whole digits and the places, and nothing else gets through', () => {
    const pattern = decimalPattern({ whole: 3, places: 2 })
    expect(accepts(pattern, ['0', '7', '999', '999.99', '12.5', '0.01'])).toEqual([])
    expect(
      refuses(pattern, ['', '1000', '1.234', '-1', '.5', '5.', '1e3', '1,000', ' 1', '+1', 'x']),
    ).toEqual([])
  })

  it('takes a leading minus only when told the value is signed', () => {
    const signed = decimalPattern(QUANTITY, { signed: true })
    expect(accepts(signed, ['-5', '-0.0001', '5', '-12345678901234.1234'])).toEqual([])
    expect(refuses(signed, ['--5', '- 5', '+5', '-', ''])).toEqual([])
    expect(QUANTITY_INPUT.test('-5')).toBe(false)
  })

  it('takes an empty box only when told the field is optional', () => {
    expect(OPTIONAL_QUANTITY_INPUT.test('')).toBe(true)
    expect(QUANTITY_INPUT.test('')).toBe(false)
    expect(decimalPattern(QUANTITY, { signed: true, optional: true }).test('')).toBe(true)
    // Optional is not "anything goes": a value that is there is still checked.
    expect(OPTIONAL_QUANTITY_INPUT.test('1.23456')).toBe(false)
  })
})

describe('the column presets', () => {
  it('state the columns of apps/api/src/core/types.py', () => {
    // Quantity and Money are Numeric(18, 4), UnitPrice Numeric(18, 6),
    // Percentage Numeric(9, 4) and ConversionFactor Numeric(18, 8).
    expect(QUANTITY_INPUT.test('99999999999999.9999')).toBe(true)
    expect(QUANTITY_INPUT.test('100000000000000')).toBe(false)
    expect(QUANTITY_INPUT.test('1.12345')).toBe(false)
    expect(UNIT_PRICE_INPUT.test('999999999999.999999')).toBe(true)
    expect(UNIT_PRICE_INPUT.test('1000000000000')).toBe(false)
    expect(UNIT_PRICE_INPUT.test('1.1234567')).toBe(false)
    expect(MONEY_INPUT.test('99999999999999.9999')).toBe(true)
    expect(MONEY_INPUT.test('1.12345')).toBe(false)
    expect(decimalPattern(PERCENTAGE).test('99999.9999')).toBe(true)
    expect(decimalPattern(PERCENTAGE).test('100000')).toBe(false)
    expect(decimalPattern(CONVERSION_FACTOR).test('9999999999.12345678')).toBe(true)
    expect(decimalPattern(CONVERSION_FACTOR).test('1.123456789')).toBe(false)
  })

  it('keep a settled amount to the two places to_money keeps', () => {
    expect(ROUNDED_MONEY_INPUT.test('1250.50')).toBe(true)
    expect(ROUNDED_MONEY_INPUT.test('1250.505')).toBe(false)
  })

  it('take a keyed percentage up to 999.9999', () => {
    expect(PERCENT_INPUT.test('999.9999')).toBe(true)
    expect(PERCENT_INPUT.test('1000')).toBe(false)
    expect(OPTIONAL_PERCENT_INPUT.test('')).toBe(true)
  })

  it('offer the optional twin of each required preset', () => {
    for (const pattern of [
      OPTIONAL_QUANTITY_INPUT,
      OPTIONAL_UNIT_PRICE_INPUT,
      OPTIONAL_MONEY_INPUT,
      OPTIONAL_PERCENT_INPUT,
    ]) {
      expect(pattern.test('')).toBe(true)
      expect(pattern.test('12.5')).toBe(true)
      expect(pattern.test('abc')).toBe(false)
    }
  })
})

describe('toDecimalInput', () => {
  it('starts a box on what the API read back, at the places the box keeps', () => {
    // A Money column reads back at four places; a box that keeps two refused
    // the untouched "500.0000", so a saved settlement could not be saved again.
    expect(ROUNDED_MONEY_INPUT.test('500.0000')).toBe(false)
    expect(toDecimalInput('500.0000', ROUNDED_MONEY)).toBe('500.00')
    expect(toDecimalInput('6408.5200', ROUNDED_MONEY)).toBe('6408.52')
    expect(toDecimalInput('0.0000', ROUNDED_MONEY)).toBe('0.00')
    expect(toDecimalInput('125000.5', ROUNDED_MONEY)).toBe('125000.50')
    expect(toDecimalInput('99999999999999.9900', ROUNDED_MONEY)).toBe('99999999999999.99')
    expect(toDecimalInput('112.5000', MEASURE)).toBe('112.500')
    for (const [value, bounds, pattern] of [
      ['500.0000', ROUNDED_MONEY, ROUNDED_MONEY_INPUT],
      ['112.5000', MEASURE, OPTIONAL_MEASURE_INPUT],
    ] as const) {
      expect(pattern.test(toDecimalInput(value, bounds))).toBe(true)
    }
  })

  it('never rounds a figure nobody typed: a value with more places comes back as it was', () => {
    // The box then says so, rather than saving a different number silently.
    expect(toDecimalInput('500.1250', ROUNDED_MONEY)).toBe('500.1250')
    expect(ROUNDED_MONEY_INPUT.test(toDecimalInput('500.1250', ROUNDED_MONEY))).toBe(false)
    expect(toDecimalInput('12.3456', MEASURE)).toBe('12.3456')
  })

  it('hands back anything that is not a decimal untouched, for the box to judge', () => {
    expect(toDecimalInput('', ROUNDED_MONEY)).toBe('')
    expect(toDecimalInput('abc', ROUNDED_MONEY)).toBe('abc')
    expect(toDecimalInput('NaN', ROUNDED_MONEY)).toBe('NaN')
  })
})

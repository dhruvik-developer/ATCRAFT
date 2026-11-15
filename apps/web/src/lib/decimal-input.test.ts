import { describe, expect, it } from 'vitest'

import {
  CONVERSION_FACTOR,
  MONEY_INPUT,
  OPTIONAL_MONEY_INPUT,
  OPTIONAL_PERCENT_INPUT,
  OPTIONAL_QUANTITY_INPUT,
  OPTIONAL_UNIT_PRICE_INPUT,
  PERCENTAGE,
  PERCENT_INPUT,
  QUANTITY,
  QUANTITY_INPUT,
  ROUNDED_MONEY_INPUT,
  UNIT_PRICE_INPUT,
  decimalPattern,
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

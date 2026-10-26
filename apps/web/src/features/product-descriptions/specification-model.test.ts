import { describe, expect, it } from 'vitest'

import { itemSpecificationValue, specificationAttribute } from '@/test/product-description-fixtures'

import {
  specificationDisplay,
  specificationDraft,
  specificationInput,
  specificationReplacement,
} from './specification-model'

describe('typed specification values', () => {
  it('keeps zero and false present in drafts, display and replacement payloads', () => {
    const number = specificationAttribute({ value_type: 'NUMBER', options: [], unit: 'mm' })
    const boolean = specificationAttribute({ value_type: 'BOOLEAN', options: [] })
    const zero = itemSpecificationValue(number, { value_number: '0.0000' })
    const no = itemSpecificationValue(boolean, { value_bool: false })
    expect(specificationDraft(zero)).toBe('0.0000')
    expect(specificationDisplay(zero)).toBe('0.0000 mm')
    expect(specificationInput(number, '0')).toEqual({
      specification_attribute_id: number.id,
      value_number: '0',
    })
    expect(specificationDraft(no)).toBe('false')
    expect(specificationDisplay(no)).toBe('No')
    expect(specificationInput(boolean, 'false')).toEqual({
      specification_attribute_id: boolean.id,
      value_bool: false,
    })
  })

  it('preserves signed decimal strings without a floating-point round trip', () => {
    const number = specificationAttribute({ value_type: 'NUMBER', options: [] })
    expect(specificationInput(number, ' -99999999999999.9999 ')).toEqual({
      specification_attribute_id: number.id,
      value_number: '-99999999999999.9999',
    })
  })

  it.each(['100000000000000', '0.00001', 'NaN', 'Infinity', '1e4', 'not a number'])(
    'rejects an invalid decimal (%s)',
    (value) => {
      expect(() =>
        specificationInput(specificationAttribute({ value_type: 'NUMBER', options: [] }), value),
      ).toThrow()
    },
  )

  it('distinguishes blank, false and invalid boolean input', () => {
    const boolean = specificationAttribute({ value_type: 'BOOLEAN', options: [] })
    expect(specificationInput(boolean, ' ')).toBeNull()
    expect(() => specificationInput(boolean, 'No')).toThrow('choose Yes or No')
  })

  it('only writes an option present in the governed vocabulary', () => {
    const option = specificationAttribute()
    expect(specificationInput(option, 'Brass')).toEqual({
      specification_attribute_id: option.id,
      value_option: 'Brass',
    })
    expect(() => specificationInput(option, 'Steel')).toThrow('choose a permitted option')
  })

  it('trims descriptive text and checks its contract limit', () => {
    const text = specificationAttribute({ value_type: 'TEXT', options: [] })
    expect(specificationInput(text, ' for external use ')).toEqual({
      specification_attribute_id: text.id,
      value_text: 'for external use',
    })
    expect(() => specificationInput(text, 'x'.repeat(2001))).toThrow('2000')
  })

  it('counts false toward the 200-value limit and leaves cleared values out', () => {
    const attributes = Array.from({ length: 201 }, (_, index) =>
      specificationAttribute({
        id: `flag-${index}`,
        value_type: 'BOOLEAN',
        options: [],
      }),
    )
    const drafts = Object.fromEntries(attributes.map((attribute) => [attribute.id, 'false']))
    expect(specificationReplacement(attributes, drafts).errors).toEqual({
      _root: 'Record at most 200 specification values.',
    })
    drafts['flag-0'] = ''
    const replacement = specificationReplacement(attributes, drafts)
    expect(replacement.errors).toEqual({})
    expect(replacement.values).toHaveLength(200)
    expect(replacement.values.every((value) => value.value_bool === false)).toBe(true)
  })

  it('associates invalid values with their attribute while validating the whole replacement', () => {
    const number = specificationAttribute({ id: 'width', value_type: 'NUMBER', options: [] })
    const boolean = specificationAttribute({ id: 'fire', value_type: 'BOOLEAN', options: [] })
    const replacement = specificationReplacement([number, boolean], { width: 'NaN', fire: 'false' })
    expect(replacement.errors.width).toContain('enter a decimal number')
    expect(replacement.values).toEqual([{ specification_attribute_id: 'fire', value_bool: false }])
  })
})

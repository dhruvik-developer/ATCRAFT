import { describe, expect, it } from 'vitest'

import { SELECT_NONE } from './constants'
import { asId, asText, asUpperText, zeroIfBlank } from './form-values'

describe('form values as the API takes them', () => {
  it('sends trimmed text, and a blank as null rather than ""', () => {
    expect(asText('  Bay 4 ')).toBe('Bay 4')
    expect(asText('   ')).toBeNull()
    expect(asText('')).toBeNull()
  })

  it('upper-cases a code and still sends a blank as null', () => {
    expect(asUpperText(' 27aapfu0939f1zv ')).toBe('27AAPFU0939F1ZV')
    expect(asUpperText('  ')).toBeNull()
  })

  it('sends a blank decimal as zero, and keeps the typed one as a string', () => {
    expect(zeroIfBlank('')).toBe('0')
    expect(zeroIfBlank('  ')).toBe('0')
    expect(zeroIfBlank(' 12.5000 ')).toBe('12.5000')
    // Never parsed: the precision typed is the precision sent.
    expect(zeroIfBlank('0.1000')).toBe('0.1000')
  })

  it('reads the "none" sentinel and the empty string as no id', () => {
    expect(asId(SELECT_NONE)).toBeNull()
    expect(asId('')).toBeNull()
    expect(asId('0192f3d4-0000-7000-8000-000000000001')).toBe(
      '0192f3d4-0000-7000-8000-000000000001',
    )
  })
})

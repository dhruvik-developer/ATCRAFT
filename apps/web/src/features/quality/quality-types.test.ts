import { describe, expect, it } from 'vitest'

import { ageInDays } from './quality-types'

const DAY = 86_400_000

describe('ageInDays', () => {
  it('counts whole days on the local calendar since the inspection was raised', () => {
    expect(ageInDays(new Date().toISOString())).toBe(0)
    expect(ageInDays(new Date(Date.now() - 3 * DAY).toISOString())).toBe(3)
  })

  it('never reads a clock running ahead as a negative wait', () => {
    expect(ageInDays(new Date(Date.now() + 2 * DAY).toISOString())).toBe(0)
  })

  it('has no age for a missing or unreadable timestamp', () => {
    expect(ageInDays(null)).toBeNull()
    expect(ageInDays('not a date')).toBeNull()
  })
})

import { describe, expect, it } from 'vitest'

import { isFinalSerialDisposition, serialDispositionQuantities } from './serial-dispositions'

describe('named-unit stock disposition', () => {
  it('accounts for all five outcomes as exact quantity strings', () => {
    expect(
      serialDispositionQuantities({
        a: 'ACCEPTED',
        b: 'REJECTED',
        c: 'REWORK',
        d: 'HOLD',
        e: 'SCRAP',
        f: 'ACCEPTED',
      }),
    ).toEqual({
      accepted_quantity: '2',
      rejected_quantity: '1',
      rework_quantity: '1',
      hold_quantity: '1',
      scrap_quantity: '1',
    })
    expect(serialDispositionQuantities({})).toEqual({
      accepted_quantity: '0',
      rejected_quantity: '0',
      rework_quantity: '0',
      hold_quantity: '0',
      scrap_quantity: '0',
    })
  })

  it('locks released, damaged and scrapped units while permitting held and rework units to be reconsidered', () => {
    for (const disposition of ['ACCEPTED', 'REJECTED', 'SCRAP'])
      expect(isFinalSerialDisposition(disposition)).toBe(true)
    for (const disposition of ['HOLD', 'REWORK', undefined])
      expect(isFinalSerialDisposition(disposition)).toBe(false)
  })
})

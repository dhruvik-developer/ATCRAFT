import { describe, expect, it } from 'vitest'

import { labelSerials, resolveSerialLabels, splitSerialNumbers } from './serial-numbers'

describe('splitSerialNumbers', () => {
  it('reads a scanned, pasted or typed run into unique normalised labels', () => {
    expect(splitSerialNumbers('sn-1\nSN-2\tsn 3, SN-1;SN-4')).toEqual([
      'SN-1',
      'SN-2',
      'SN',
      '3',
      'SN-4',
    ])
  })
})

describe('labelSerials', () => {
  it('pairs each id with the label sent beside it', () => {
    expect(labelSerials(['s-1', 's-2'], ['LK-001', 'LK-002'])).toEqual([
      { id: 's-1', label: 'LK-001' },
      { id: 's-2', label: 'LK-002' },
    ])
  })

  it.each([
    ['absent', undefined],
    ['short', ['LK-001']],
    ['long', ['LK-001', 'LK-002', 'LK-003']],
  ])('labels every unit by its id when the labels are %s', (_case, labels) => {
    // A shifted list would put one unit's label on its neighbour.
    expect(labelSerials(['s-1', 's-2'], labels)).toEqual([
      { id: 's-1', label: 's-1' },
      { id: 's-2', label: 's-2' },
    ])
  })
})

describe('resolveSerialLabels', () => {
  const units = [
    { id: 's-1', label: 'LK-001' },
    { id: 's-2', label: 'LK-002' },
    { id: 's-3', label: 'LK-003' },
  ]

  it('names the units a run of labels points at, in the order typed', () => {
    expect(resolveSerialLabels(' lk-003\nLK-001 ', units)).toEqual({
      ids: ['s-3', 's-1'],
      unknown: [],
      repeated: [],
    })
  })

  it('reports a label the document does not hold, and one named twice', () => {
    expect(resolveSerialLabels('LK-001, LK-009, lk-001, LK-001', units)).toEqual({
      ids: ['s-1'],
      unknown: ['LK-009'],
      repeated: ['LK-001'],
    })
  })

  it('names nothing for an empty run', () => {
    expect(resolveSerialLabels(' ,\n ', units)).toEqual({ ids: [], unknown: [], repeated: [] })
  })
})

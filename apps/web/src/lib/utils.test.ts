import { describe, expect, it } from 'vitest'

import { indexById } from './utils'

describe('indexById', () => {
  it('indexes a readonly option list without copying it first', () => {
    const options: readonly { id: string; code: string }[] = [
      { id: 'a', code: 'MILL-01' },
      { id: 'b', code: 'LATHE-02' },
    ]
    const byId = indexById(options)
    expect(byId.get('b')?.code).toBe('LATHE-02')
    expect(byId.size).toBe(2)
  })

  it('answers an empty map for a list that has not arrived', () => {
    expect(indexById(undefined).size).toBe(0)
  })
})

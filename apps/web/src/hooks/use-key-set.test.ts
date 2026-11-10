import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useKeySet } from './use-key-set'

type Draft = Record<string, string>

function renderKeySet(record: Draft) {
  return renderHook(({ draft }: { draft: Draft }) => useKeySet(draft), {
    initialProps: { draft: record },
  })
}

describe('useKeySet', () => {
  it('keeps the same Set while only the values change', () => {
    const { result, rerender } = renderKeySet({ a: '1.00', b: '2.00' })
    const first = result.current
    expect([...first]).toEqual(['a', 'b'])

    // A keystroke in an amount: a new record, the same keys.
    rerender({ draft: { a: '10.00', b: '2.00' } })
    expect(result.current).toBe(first)
  })

  it('is a new Set when a key joins or leaves', () => {
    const { result, rerender } = renderKeySet({ a: '1.00' })
    const first = result.current

    rerender({ draft: { a: '1.00', b: '2.00' } })
    expect(result.current).not.toBe(first)
    expect([...result.current]).toEqual(['a', 'b'])

    rerender({ draft: { b: '2.00' } })
    expect([...result.current]).toEqual(['b'])
  })

  it('tells an empty record from one keyed by an empty string', () => {
    const { result, rerender } = renderKeySet({})
    expect(result.current.size).toBe(0)

    rerender({ draft: { '': '1.00' } })
    expect(result.current.has('')).toBe(true)
  })
})

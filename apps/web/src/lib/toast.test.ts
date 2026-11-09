import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ApiError } from './api-client'
import { errors } from './messages'
import { toast, useToasts } from './toast'

// src/test/setup.ts empties the store after every test, here as everywhere. These
// two run in order: the first leaves an error toast behind - one never dismisses
// itself - and the second fails if the shared reset stops reaching this store.
describe('the store between tests', () => {
  it('can be left holding an error toast when a test ends', () => {
    toast.error('Left behind')
    const { result } = renderHook(() => useToasts())
    expect(result.current.map((notification) => notification.title)).toEqual(['Left behind'])
  })

  it('is empty again when the next test starts', () => {
    const { result } = renderHook(() => useToasts())
    expect(result.current).toEqual([])
  })
})

describe('direct request error notifications', () => {
  it('hides unexpected technical details from direct request notifications', () => {
    const { result } = renderHook(() => useToasts())
    act(() => toast.failure(new TypeError('Internal token cannot be read')))
    expect(result.current.at(-1)).toMatchObject({
      title: errors.unexpected,
      description: errors.unexpectedDetail,
      variant: 'error',
    })
  })

  it('shares API error headings, correlation details and persistent error duration', () => {
    const { result } = renderHook(() => useToasts())
    act(() => {
      toast.failure(
        new ApiError(503, 'SERVICE_UNAVAILABLE', 'Try again.', undefined, 'abcdef123456'),
      )
    })
    expect(result.current.at(-1)).toMatchObject({
      title: errors.unexpected,
      description: 'Try again. (reference abcdef12)',
      variant: 'error',
      duration: 0,
    })
  })

  it('keeps a meaningful action-specific heading and supplies a missing description', () => {
    const { result } = renderHook(() => useToasts())
    act(() => toast.failure(undefined, 'Could not download the challan'))
    expect(result.current.at(-1)).toMatchObject({
      title: 'Could not download the challan',
      description: errors.unexpectedDetail,
      variant: 'error',
    })
  })
})

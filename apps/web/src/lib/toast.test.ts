import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ApiError } from './api-client'
import { errors } from './messages'
import { toast, useToasts } from './toast'

afterEach(() => {
  const { result, unmount } = renderHook(() => useToasts())
  act(() => result.current.forEach((notification) => toast.dismiss(notification.id)))
  unmount()
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

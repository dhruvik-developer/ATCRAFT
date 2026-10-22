import { describe, expect, it } from 'vitest'

import { ApiError } from './api-client'
import {
  describeError,
  errorDescription,
  errors,
  joinValidationMessages,
  notificationsRead,
} from './messages'

describe('validation sentence formatting', () => {
  it('joins server sentences without doubling punctuation or losing detail', () => {
    expect(joinValidationMessages(['Code is required.', 'Choose an item!', 'Try again?'])).toBe(
      'Code is required. Choose an item! Try again?',
    )
    expect(joinValidationMessages(['  Required  ', '', '  Must be unique. '])).toBe(
      'Required. Must be unique.',
    )
  })
})

describe('error descriptions', () => {
  it('preserves API business messages and adds a reference for server failures', () => {
    const message = 'Only 4 units are available in Main warehouse.'
    expect(errorDescription(new ApiError(422, 'INSUFFICIENT_STOCK', message))).toBe(message)
    expect(
      describeError(
        new ApiError(503, 'SERVICE_UNAVAILABLE', 'Please retry.', undefined, 'abcdef123456'),
      ),
    ).toBe('Please retry. (reference abcdef12)')
    expect(
      describeError(
        new ApiError(200, 'INVALID_RESPONSE', 'Invalid response.', undefined, 'abcdef123456'),
      ),
    ).toBe('Invalid response. (reference abcdef12)')
  })

  it('supplies a useful fallback for missing or unrecognised errors', () => {
    expect(describeError(undefined)).toBe(errors.unexpectedDetail)
    expect(describeError({ message: 'untrusted value' })).toBe(errors.unexpectedDetail)
    expect(describeError(new Error(''))).toBe(errors.unexpectedDetail)
    expect(describeError(null, 'The file could not be downloaded.')).toBe(
      'The file could not be downloaded.',
    )
  })
})

describe('notification read results', () => {
  it('describes a partial update without claiming all notifications are read', () => {
    expect(notificationsRead(50, 12)).toEqual({
      title: 'Notifications updated',
      description: '50 notifications marked as read. 12 still unread.',
    })
  })

  it('uses singular wording for one notification and confirms completion', () => {
    expect(notificationsRead(1, 0)).toEqual({
      title: 'All notifications read',
      description: '1 notification marked as read.',
    })
  })
})

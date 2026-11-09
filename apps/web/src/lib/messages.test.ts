import { describe, expect, it } from 'vitest'

import { ApiError } from './api-client'
import { REASON_MIN_LENGTH } from './constants'
import {
  describeError,
  errorDescription,
  errors,
  fulfilment,
  joinValidationMessages,
  notificationsRead,
  prompts,
  validation,
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
    expect(describeError(new TypeError('Cannot read credentials.token at /internal/auth'))).toBe(
      errors.unexpectedDetail,
    )
    expect(describeError(new Error('internal details'), 'The file could not be downloaded.')).toBe(
      'The file could not be downloaded.',
    )
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

describe('the shared validation vocabulary', () => {
  it('words a length bound the way 165 forms already did, with no full stop', () => {
    expect(validation.maxLength(64)).toBe('At most 64 characters')
  })

  it('words a precision once, in decimal places rather than decimals', () => {
    expect(validation.decimals('a quantity', 4)).toBe(
      'Enter a quantity with up to 4 decimal places',
    )
    expect(validation.decimals('an amount of zero or more', 2)).toBe(
      'Enter an amount of zero or more with up to 2 decimal places',
    )
  })

  it('says a positive quantity the same way on the dispatch dialog as on every form', () => {
    expect(fulfilment.quantityInvalid).toBe(validation.decimals('a quantity above zero', 4))
    expect(validation.positive('Quantity')).toBe('Quantity must be greater than zero.')
  })
})

describe('the reason minimum', () => {
  it('is stated from REASON_MIN_LENGTH, the bound the API holds a reason to', () => {
    // Spelled "three", it would keep saying so after the constant moved, while
    // the API refused on the new bound.
    expect(errors.reasonTooShort).toBe(`Record at least ${REASON_MIN_LENGTH} characters of reason`)
    expect(prompts.reasonRequired).toContain(`at least ${REASON_MIN_LENGTH} characters`)
  })
})

import { describe, expect, it } from 'vitest'

import { ApiError } from '@/lib/api-client'
import { AUTH_RETRY_AFTER_SECONDS } from '@/lib/constants'

import { authenticationRetryAfter } from './auth-retry'

describe('authentication retry timing', () => {
  it.each([45, '45', 1.2])('uses and rounds the server wait %s', (value) => {
    expect(
      authenticationRetryAfter(
        new ApiError(429, 'RATE_LIMITED', 'Please wait.', { retry_after_seconds: value }),
      ),
    ).toBe(Math.ceil(Number(value)))
  })

  it.each([undefined, null, '', 0, -1, 'invalid', Infinity, NaN])(
    'uses a finite fallback for unusable wait %s',
    (value) => {
      expect(
        authenticationRetryAfter(
          new ApiError(429, 'RATE_LIMITED', 'Please wait.', { retry_after_seconds: value }),
        ),
      ).toBe(AUTH_RETRY_AFTER_SECONDS)
    },
  )

  it('does not impose a rate-limit wait on transport or other failures', () => {
    expect(
      authenticationRetryAfter(new ApiError(503, 'SERVICE_UNAVAILABLE', 'Try again.')),
    ).toBeNull()
    expect(authenticationRetryAfter(new TypeError('Private transport failure'))).toBeNull()
  })
})

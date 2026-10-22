import { describe, expect, it, vi } from 'vitest'

import { ApiError } from './api-client'
import {
  applyFieldErrors,
  fieldErrorSummary,
  partitionFieldErrors,
  remapArrayIndices,
} from './form-errors'

/** A 422 as the API shapes it: `details.fields`, keyed by dotted path. */
function validationError(fields: Record<string, string[]>): ApiError {
  return new ApiError(422, 'VALIDATION_ERROR', 'Invalid request', { fields })
}

describe('form error paths', () => {
  it('maps a sparse payload index back to the row shown in the form', () => {
    const map = remapArrayIndices('lines', [1, 4, 8])

    expect(map('lines.0.quantity')).toBe('lines.1.quantity')
    expect(map('lines.2')).toBe('lines.8')
    expect(map('lines.invalid.quantity')).toBe('lines.invalid.quantity')
    expect(map('header.reference')).toBe('header.reference')
  })

  it('separates model and query errors from errors owned by controls', () => {
    const error = validationError({
      'lines.0.quantity': ['Must be positive'],
      _root: ['At least one line is required'],
      'query.page': ['Must be an integer'],
    })

    expect(partitionFieldErrors(error)).toEqual({
      fields: { 'lines.0.quantity': ['Must be positive'] },
      orphans: ['At least one line is required', 'Must be an integer'],
    })
  })

  it('treats bare and nested transport parameters as orphan errors', () => {
    const error = validationError({
      query: ['Choose a bounded date range.'],
      'path.id': ['Enter a valid identifier.'],
      header: ['A required header is missing.'],
      'cookie.session': ['Sign in again.'],
      quantity: ['Enter a positive quantity.'],
    })
    expect(partitionFieldErrors(error)).toEqual({
      fields: { quantity: ['Enter a positive quantity.'] },
      orphans: [
        'Choose a bounded date range.',
        'Enter a valid identifier.',
        'A required header is missing.',
        'Sign in again.',
      ],
    })
  })

  it('ignores empty field messages so they cannot silence the global error notice', () => {
    expect(partitionFieldErrors(validationError({ quantity: [], _root: ['  '] }))).toEqual({
      fields: {},
      orphans: [],
    })
  })
})

describe('applyFieldErrors', () => {
  it('addresses a field array entry by the dotted path the API sends', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ 'lines.0.scrap_percentage': ['Must be at most 100'] }),
      setError,
    )

    expect(setError).toHaveBeenCalledTimes(1)
    expect(setError).toHaveBeenCalledWith('lines.0.scrap_percentage', {
      type: 'server',
      message: 'Must be at most 100',
    })
  })

  it('puts an error on a whole array entry when the path stops at the index', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ 'operations.0': ['Sequence numbers must be unique'] }),
      setError,
    )

    expect(setError).toHaveBeenCalledWith('operations.0', {
      type: 'server',
      message: 'Sequence numbers must be unique',
    })
  })

  it('joins several messages on one control with a full stop', () => {
    const setError = vi.fn()

    applyFieldErrors(validationError({ code: ['Required', 'Must be unique'] }), setError)

    expect(setError).toHaveBeenCalledWith('code', {
      type: 'server',
      message: 'Required. Must be unique',
    })
  })

  it('preserves punctuation when the API already sends complete sentences', () => {
    const setError = vi.fn()
    applyFieldErrors(
      validationError({ code: ['Code is required.', 'Choose a unique code.'] }),
      setError,
    )
    expect(setError).toHaveBeenCalledWith('code', {
      type: 'server',
      message: 'Code is required. Choose a unique code.',
    })
  })

  it('lands a model-level refusal and a query error on root, joined', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({
        _root: ['At least one line is required'],
        'query.page': ['Must be an integer'],
      }),
      setError,
    )

    expect(setError).toHaveBeenCalledTimes(1)
    expect(setError).toHaveBeenCalledWith('root', {
      type: 'server',
      message: 'At least one line is required. Must be an integer',
    })
  })

  it('rewrites the path through mapPath before looking the control up', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ 'lines.0.quantity': ['Must be positive'] }),
      setError,
      remapArrayIndices('lines', [3]),
    )

    expect(setError).toHaveBeenCalledWith('lines.3.quantity', {
      type: 'server',
      message: 'Must be positive',
    })
  })

  it('ignores anything that is not an ApiError', () => {
    const setError = vi.fn()

    applyFieldErrors(new Error('network'), setError)

    expect(setError).not.toHaveBeenCalled()
  })
})

describe('fieldErrorSummary', () => {
  it('joins every message, for a control that is not a form field', () => {
    expect(
      fieldErrorSummary(validationError({ reason: ['Too short'], _root: ['Not allowed'] })),
    ).toBe('Too short. Not allowed')
  })

  it('is null when there is nothing field-level to show', () => {
    expect(fieldErrorSummary(new ApiError(409, 'CONFLICT', 'Changed by someone else'))).toBeNull()
    expect(fieldErrorSummary(new Error('network'))).toBeNull()
  })
})

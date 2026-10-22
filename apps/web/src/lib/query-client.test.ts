/**
 * The global error nets.
 *
 * `MutationCache.onError` and `QueryCache.onError` are the one place a failed
 * request becomes a toast, so the exemptions they carry are the whole contract
 * between "the form shows it" and "the shell says it". A 422 that falls through
 * both is a rejection nobody sees.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from './api-client'
import { errors } from './messages'
import { queryClient, queryKeys } from './query-client'
import { toast } from './toast'

const onMutationError = queryClient.getMutationCache().config.onError
const onQueryError = queryClient.getQueryCache().config.onError

// The handlers read only the error and, for a query, `state.data` and `meta`;
// the remaining arguments are typed off the handlers so a library upgrade that
// adds one fails here, not in the shell.
type MutationErrorArgs = Parameters<NonNullable<typeof onMutationError>>
type QueryErrorArgs = Parameters<NonNullable<typeof onQueryError>>

function failMutation(error: Error): void {
  onMutationError?.(
    error,
    undefined,
    undefined,
    {} as MutationErrorArgs[3],
    {} as MutationErrorArgs[4],
  )
}

/** A first load - no data yet - so the net is allowed to speak. */
function failQuery(error: Error, meta?: Record<string, unknown>): void {
  onQueryError?.(error, { state: { data: undefined }, meta } as unknown as QueryErrorArgs[1])
}

function validationError(fields: Record<string, string[]>): ApiError {
  return new ApiError(422, 'VALIDATION_ERROR', 'Invalid request', { fields })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the mutation error net', () => {
  it('says a model-level refusal aloud, since not every form has a root slot', () => {
    const shown = vi.spyOn(toast, 'error')

    failMutation(
      validationError({ _root: ['A PAYMENT is recorded against a vendor, not a customer.'] }),
    )

    expect(shown).toHaveBeenCalledWith(
      errors.checkTheForm,
      'A PAYMENT is recorded against a vendor, not a customer.',
    )
  })

  it('treats a query-parameter error the same way', () => {
    const shown = vi.spyOn(toast, 'error')

    failMutation(validationError({ 'query.page': ['Must be an integer'] }))

    expect(shown).toHaveBeenCalledWith(errors.checkTheForm, 'Must be an integer')
  })

  it('stays quiet about field errors, which the form renders inline', () => {
    const shown = vi.spyOn(toast, 'error')

    failMutation(validationError({ 'lines.0.quantity': ['Must be positive'] }))

    expect(shown).not.toHaveBeenCalled()
  })

  it('still toasts a 422 that carries no field detail at all', () => {
    const shown = vi.spyOn(toast, 'error')

    failMutation(new ApiError(422, 'INSUFFICIENT_STOCK', 'Only 4 available'))

    expect(shown).toHaveBeenCalledWith(errors.notEnoughStock, 'Only 4 available')
  })

  it('does not suppress a response with empty field-error arrays', () => {
    const shown = vi.spyOn(toast, 'error')
    failMutation(validationError({ quantity: [], _root: [' '] }))
    expect(shown).toHaveBeenCalledWith(errors.checkTheForm, 'Invalid request')
  })

  it('toasts a bare query validation error as one punctuated sentence', () => {
    const shown = vi.spyOn(toast, 'error')
    failMutation(validationError({ query: ['Choose a start date.', 'Choose an end date.'] }))
    expect(shown).toHaveBeenCalledWith(
      errors.checkTheForm,
      'Choose a start date. Choose an end date.',
    )
  })
})

describe('the query error net', () => {
  it('lets a query that renders its own expected rejection opt out', () => {
    const shown = vi.spyOn(toast, 'error')

    failQuery(new ApiError(422, 'VALIDATION_ERROR', 'No conversion path'), {
      suppressErrorToast: true,
    })

    expect(shown).not.toHaveBeenCalled()
  })

  it('toasts a failed first load that did not opt out', () => {
    const shown = vi.spyOn(toast, 'error')

    failQuery(new ApiError(500, 'INTERNAL_ERROR', 'Upstream timed out'))

    expect(shown).toHaveBeenCalledWith(errors.loadFailed, 'Upstream timed out')
  })
})

describe('query keys', () => {
  it('files the approval queue and the report catalogue under their namespaces', () => {
    expect(queryKeys.audit.approvals({ limit: 6 })).toEqual(['audit', 'approvals', { limit: 6 }])
    expect(queryKeys.reports.catalogue).toEqual(['reports', 'catalogue'])
  })
})

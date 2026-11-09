/**
 * The global error nets.
 *
 * `MutationCache.onError` and `QueryCache.onError` are the one place a failed
 * request becomes a toast, so the exemptions they carry are the whole contract
 * between "the form shows it" and "the shell says it". A 422 that falls through
 * both is a rejection nobody sees.
 */

import { partialMatchKey } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { assemblyKeys } from '@/features/assembly/assembly-model'
import { dealerKeys } from '@/features/customers/dealer-model'
import { jobWorkKeys } from '@/features/job-work/job-work-model'
import { qcTemplateKeys } from '@/features/quality/qc-template-model'
import { transporterKeys } from '@/features/transporters/transporter-model'

import * as apiClient from './api-client'
import { ApiError } from './api-client'
import { errors } from './messages'
import * as errorReporting from './error-reporting'
import {
  createQueryClient,
  queryClient,
  queryKeys,
  staleTimes,
  type ErrorNetMeta,
} from './query-client'
import { toast } from './toast'

const onMutationError = queryClient.getMutationCache().config.onError
const onQueryError = queryClient.getQueryCache().config.onError

// The handlers read only the error, its query's `state.data`, and the `meta`
// of the query or mutation; the remaining arguments are typed off the handlers
// so a library upgrade that adds one fails here, not in the shell.
type MutationErrorArgs = Parameters<NonNullable<typeof onMutationError>>
type QueryErrorArgs = Parameters<NonNullable<typeof onQueryError>>

function failMutation(error: Error, meta?: ErrorNetMeta): void {
  onMutationError?.(
    error,
    undefined,
    undefined,
    { meta } as unknown as MutationErrorArgs[3],
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
  it('reports unexpected failures and shows only a safe public description', () => {
    const shown = vi.spyOn(toast, 'error')
    const reported = vi.spyOn(errorReporting, 'reportError').mockImplementation(() => {})
    const error = new TypeError('Internal credentials cannot be read')

    failMutation(error)

    expect(reported).toHaveBeenCalledWith(error, { source: 'mutation' })
    expect(shown).toHaveBeenCalledWith(errors.unexpected, errors.unexpectedDetail)
  })

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
  it("titles the toast with the action a mutation names, keeping the API's reason", () => {
    const shown = vi.spyOn(toast, 'error')

    failMutation(new ApiError(409, 'CONFLICT', 'The shipment has no tracking number.'), {
      errorTitle: 'Could not refresh courier tracking',
    })

    expect(shown).toHaveBeenCalledTimes(1)
    expect(shown).toHaveBeenCalledWith(
      'Could not refresh courier tracking',
      'The shipment has no tracking number.',
    )
  })

  it('uses the named action for a model-level refusal and for a crash as well', () => {
    const shown = vi.spyOn(toast, 'error')
    vi.spyOn(errorReporting, 'reportError').mockImplementation(() => {})
    const meta = { errorTitle: 'Could not save the courier account' }

    failMutation(validationError({ _root: ['Choose a provider.'] }), meta)
    failMutation(new TypeError('Cannot read properties of undefined'), meta)

    expect(shown).toHaveBeenNthCalledWith(1, meta.errorTitle, 'Choose a provider.')
    expect(shown).toHaveBeenNthCalledWith(2, meta.errorTitle, errors.unexpectedDetail)
  })

  it('stays silent for a mutation that renders its own failure', () => {
    const shown = vi.spyOn(toast, 'error')

    failMutation(new ApiError(409, 'CONFLICT', 'Changed by someone else'), {
      suppressErrorToast: true,
    })
    failMutation(validationError({ _root: ['A PAYMENT is recorded against a vendor.'] }), {
      suppressErrorToast: true,
    })

    expect(shown).not.toHaveBeenCalled()
  })

  it('still reports a crash and still ends a lost session when the toast is suppressed', () => {
    const shown = vi.spyOn(toast, 'error')
    const reported = vi.spyOn(errorReporting, 'reportError').mockImplementation(() => {})
    const cleared = vi.spyOn(apiClient, 'clearTokens').mockImplementation(() => {})
    const crash = new TypeError('Cannot read properties of undefined')

    failMutation(crash, { suppressErrorToast: true })
    failMutation(new ApiError(401, 'UNAUTHORIZED', 'Session expired'), { suppressErrorToast: true })

    expect(reported).toHaveBeenCalledWith(crash, { source: 'mutation' })
    expect(cleared).toHaveBeenCalledTimes(1)
    expect(shown).not.toHaveBeenCalled()
  })
})

describe('the query error net', () => {
  it('reports unexpected query failures and gives the first load a safe notification', () => {
    const shown = vi.spyOn(toast, 'error')
    const reported = vi.spyOn(errorReporting, 'reportError').mockImplementation(() => {})
    const error = new TypeError('Internal document metadata could not be read')

    failQuery(error)

    expect(reported).toHaveBeenCalledWith(error, { source: 'query' })
    expect(shown).toHaveBeenCalledWith(errors.loadFailed, errors.unexpectedDetail)
  })

  it('reports an unexpected background failure without interrupting a populated screen', () => {
    const shown = vi.spyOn(toast, 'error')
    const reported = vi.spyOn(errorReporting, 'reportError').mockImplementation(() => {})
    const error = new Error('Internal data transformation failed')

    onQueryError?.(error, { state: { data: [] } } as unknown as QueryErrorArgs[1])

    expect(reported).toHaveBeenCalledWith(error, { source: 'query' })
    expect(shown).not.toHaveBeenCalled()
  })

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

  // A prefix invalidation reaches a key only when the key starts with it, so a
  // cache filed under a root of its own is one its writers' invalidation misses.
  // `partialMatchKey` is the comparison `invalidateQueries` itself makes.
  it.each([
    ['the dealer list, by every customer write', dealerKeys.list({}), queryKeys.customers.all],
    [
      'the raise dialog offer, by every template write',
      qcTemplateKeys.raise('item', 'INCOMING'),
      qcTemplateKeys.all,
    ],
    [
      'the inbound carriers, by every transporter write',
      transporterKeys.inbound({ page: 1 }),
      transporterKeys.all,
    ],
    ['an assembly order, by a QC verdict', assemblyKeys.detail('id'), queryKeys.assembly.all],
    ['a job-work order, by a QC verdict', jobWorkKeys.detail('id'), queryKeys.jobWork.all],
  ] as const)('lets the root reach %s', (_reader, key, root) => {
    expect(partialMatchKey(key, root)).toBe(true)
  })

  it('keeps the dealer list out of the unrestricted customer list it sits beside', () => {
    // The customer list holds rows the dealer screen may not show: neither may
    // answer for the other, so neither key may be a prefix of the other.
    const params = { page: 1, limit: 50 }
    expect(partialMatchKey(dealerKeys.list(params), queryKeys.customers.list(params))).toBe(false)
    expect(partialMatchKey(queryKeys.customers.list(params), dealerKeys.list(params))).toBe(false)
  })
})

describe('createQueryClient', () => {
  it('builds the application policy: both error nets and the per-domain defaults', () => {
    const client = createQueryClient()

    expect(client.getQueryCache().config.onError).toBe(onQueryError)
    expect(client.getMutationCache().config.onError).toBe(onMutationError)
    const { queries, mutations } = client.getDefaultOptions()
    expect(queries?.staleTime).toBe(staleTimes.master)
    expect(queries?.gcTime).toBe(10 * 60 * 1000)
    expect(queries?.refetchOnWindowFocus).toBe(false)
    expect(queries?.retry).toBe(queryClient.getDefaultOptions().queries?.retry)
    expect(mutations?.retry).toBe(false)
  })

  it('merges an override into one kind of default and keeps the rest', () => {
    const client = createQueryClient({ defaultOptions: { queries: { retry: false } } })

    const { queries, mutations } = client.getDefaultOptions()
    expect(queries?.retry).toBe(false)
    expect(queries?.staleTime).toBe(staleTimes.master)
    expect(queries?.refetchOnWindowFocus).toBe(false)
    expect(mutations?.retry).toBe(false)
  })

  it('leaves the nets off only when asked to', () => {
    const client = createQueryClient({ errorNets: false })

    expect(client.getQueryCache().config.onError).toBeUndefined()
    expect(client.getMutationCache().config.onError).toBeUndefined()
    expect(client.getDefaultOptions().queries?.staleTime).toBe(staleTimes.master)
  })

  it('gives every caller a cache of its own', () => {
    expect(createQueryClient().getQueryCache()).not.toBe(queryClient.getQueryCache())
  })
})

describe('query retries', () => {
  it('retries only transient transport failures with a bounded attempt count', () => {
    const retry = queryClient.getDefaultOptions().queries?.retry
    expect(typeof retry).toBe('function')
    if (typeof retry !== 'function') throw new Error('The query retry policy must be callable')

    expect(retry(0, new TypeError('Invalid data transformation'))).toBe(false)
    expect(retry(0, new ApiError(422, 'VALIDATION_ERROR', 'Invalid input'))).toBe(false)
    expect(retry(0, new ApiError(403, 'FORBIDDEN', 'Not permitted'))).toBe(false)
    expect(retry(0, new ApiError(409, 'CONFLICT', 'Changed by someone else'))).toBe(false)
    expect(retry(0, new ApiError(429, 'RATE_LIMITED', 'Wait'))).toBe(true)
    expect(retry(1, new ApiError(429, 'RATE_LIMITED', 'Wait'))).toBe(false)
    expect(retry(1, new ApiError(0, 'NETWORK_ERROR', 'Connection interrupted'))).toBe(true)
    expect(retry(1, new ApiError(503, 'SERVICE_UNAVAILABLE', 'Retry later'))).toBe(true)
    expect(retry(2, new ApiError(503, 'SERVICE_UNAVAILABLE', 'Retry later'))).toBe(false)
  })
})

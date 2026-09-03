/**
 * Map a 422 from the API onto the form control that caused it.
 *
 * The mutation cache suppresses the toast for a 422 that carries field errors
 * (lib/query-client), on the understanding that the form shows them inline.
 * This is the other half of that arrangement, so the two have to stay in step -
 * which is why there is exactly one copy of it, here, next to the api client
 * that shapes `fieldErrors` and the query client that decides to stay quiet.
 */

import type { FieldPath, FieldValues, UseFormSetError } from 'react-hook-form'

import { ApiError } from '@/lib/api-client'

export function applyFieldErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
): void {
  if (!(error instanceof ApiError)) return
  for (const [field, messages] of Object.entries(error.fieldErrors)) {
    // The API sends dotted paths such as `lines.0.quantity`, which is exactly
    // how react-hook-form addresses a field array entry.
    setError(field as FieldPath<T>, { type: 'server', message: messages.join('. ') })
  }
}

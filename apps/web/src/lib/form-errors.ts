/**
 * Map a 422 from the API onto the form control that caused it.
 *
 * The mutation cache suppresses the toast for a 422 that carries field errors
 * (lib/query-client), on the understanding that the form shows them inline.
 * This is the other half of that arrangement, so the two have to stay in step -
 * which is why there is exactly one copy of it, here, next to the api client
 * that shapes `fieldErrors` and the query client that decides to stay quiet.
 *
 * Two keys have no control behind them. `_root` is a model-level refusal whose
 * path is the whole body, and `query.*` names a query parameter. Both land on
 * the form's `root` error, which a dialog renders above its footer as
 * `errors.root?.message`, and the mutation cache toasts them as well - so a
 * form that has no root slot still says something. `partitionFieldErrors` is
 * the one definition of "orphan" both sides read.
 */

import type { FieldPath, FieldValues, UseFormSetError } from 'react-hook-form'

import { ApiError } from '@/lib/api-client'
import { toast } from '@/lib/toast'

/** A 422 key with no form control behind it: the whole body, or a query parameter. */
export function isOrphanField(field: string): boolean {
  return field === '_root' || field.startsWith('query.')
}

export interface PartitionedFieldErrors {
  /** Keys a form control can own, still keyed by the API's dotted path. */
  fields: Record<string, string[]>
  /** Messages under `_root` or `query.*`, in the order the API sent them. */
  orphans: string[]
}

/** Split a 422's field errors into those a control can show and those nothing can. */
export function partitionFieldErrors(error: ApiError): PartitionedFieldErrors {
  const fields: Record<string, string[]> = {}
  const orphans: string[] = []
  for (const [field, messages] of Object.entries(error.fieldErrors)) {
    if (isOrphanField(field)) orphans.push(...messages)
    else fields[field] = messages
  }
  return { fields, orphans }
}

/**
 * Put each field error on its control, and the orphans on `root`.
 *
 * `mapPath` rewrites the API's path before it is looked up on the form, for a
 * form whose payload is not shaped exactly like its fields - see
 * `remapArrayIndices` for the common case.
 */
export function applyFieldErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
  mapPath?: (path: string) => string,
): void {
  if (!(error instanceof ApiError)) return
  const { fields, orphans } = partitionFieldErrors(error)
  for (const [field, messages] of Object.entries(fields)) {
    // The API sends dotted paths such as `lines.0.quantity`, which is exactly
    // how react-hook-form addresses a field array entry.
    const path = mapPath ? mapPath(field) : field
    setError(path as FieldPath<T>, { type: 'server', message: messages.join('. ') })
  }
  if (orphans.length > 0) {
    setError('root', { type: 'server', message: orphans.join('. ') })
  }
}

/**
 * A `mapPath` for a field array the form posts sparsely.
 *
 * When blank rows are dropped before posting, the API's `lines.2.quantity`
 * names the third row *sent*, not the third row on screen. `sent` lists, for
 * each index in the payload, the index of the form row it came from, so the
 * error lands on the row the user can see.
 */
export function remapArrayIndices(name: string, sent: readonly number[]): (path: string) => string {
  const prefix = `${name}.`
  return (path) => {
    if (!path.startsWith(prefix)) return path

    const rest = path.slice(prefix.length)
    const separator = rest.indexOf('.')
    const rawIndex = separator === -1 ? rest : rest.slice(0, separator)
    if (!/^\d+$/.test(rawIndex)) return path

    const suffix = separator === -1 ? '' : rest.slice(separator)
    return `${prefix}${sent[Number(rawIndex)] ?? rawIndex}${suffix}`
  }
}

/**
 * Every message from a 422, joined - for a control that is not a
 * react-hook-form field (the lone reason box in a confirm dialog, say) to show
 * under itself. Null for anything that is not an `ApiError` with field detail,
 * so the caller can leave its slot alone.
 */
export function fieldErrorSummary(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const messages = Object.values(error.fieldErrors).flat()
  return messages.length > 0 ? messages.join('. ') : null
}

/**
 * Surface a 422 that the mutation cache deliberately stays quiet about.
 *
 * The cache suppresses the toast for a 422 carrying field errors on the
 * understanding a form renders them inline. A mutation fired from a confirm
 * dialog has no form, so without this a rejected `reason` or line set produced
 * no message anywhere. Orphans are left out here because the cache already
 * toasts those itself; every other status still toasts globally.
 */
export function reportFieldErrors(error: unknown, title: string): void {
  if (!(error instanceof ApiError) || error.status !== 422) return
  const fields = Object.entries(partitionFieldErrors(error).fields)
  if (fields.length === 0) return
  toast.error(
    title,
    fields.map(([field, messages]) => `${field}: ${messages.join('. ')}`).join(' - '),
  )
}

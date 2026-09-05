/**
 * Where a 422 lands when the payload does not mirror the form.
 *
 * `applyFieldErrors` (lib/form-errors) puts `lines.0.quantity` on form line 0,
 * which is right for every form that sends its field array as it stands. Two
 * procurement dialogs do not: the goods receipt and the purchase return send
 * only the lines a quantity was typed on, renumbered from one, because the API
 * declares those quantities `PositiveQuantity` and refuses a zero. A 422 for
 * payload line 0 then names a form line the storekeeper may never have touched
 * while the offending line shows nothing.
 *
 * The map from payload index to form index is derived from the same predicate
 * the mutation used to filter, so the two cannot drift; `remapArrayIndices`
 * in lib/form-errors turns it into the `mapPath` that `applyFieldErrors`
 * takes, so the single copy of the 422-to-control mapping stays there.
 */

import { ApiError } from '@/lib/api-client'

/** Form indices of the lines the mutation actually sent, in payload order. */
export function sentLineIndices<T>(lines: readonly T[], isSent: (line: T) => boolean): number[] {
  return lines.flatMap((line, index) => (isSent(line) ? [index] : []))
}

/**
 * The server's message for one field of a 422, or null.
 *
 * For the reason dialogs, which are not react-hook-form forms and so have no
 * `setError` to hand to `applyFieldErrors`. The mutation cache stays quiet on a
 * 422 that carries field errors, so a dialog that does not read them shows
 * nothing at all. A reason sent as a query parameter arrives under
 * `query.<field>`, because only the `body` prefix is stripped server-side.
 */
export function serverFieldMessage(error: unknown, field: string): string | null {
  if (!(error instanceof ApiError)) return null
  const fields = error.fieldErrors
  const messages = fields[field] ?? fields[`query.${field}`]
  return messages && messages.length > 0 ? messages.join('. ') : null
}

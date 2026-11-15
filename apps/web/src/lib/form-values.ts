/**
 * Form values as the API takes them.
 *
 * A form holds what was typed - strings, with "" for an empty box and the
 * `SELECT_NONE` sentinel for an emptied select - and a payload holds what the
 * column stores. These are the rules between the two, once, so a screen never
 * restates them: text is trimmed and a blank is null, never ""; an id is null
 * when nothing was chosen; a code is upper-cased; and a decimal whose blank
 * means zero is sent as "0". Decimals stay strings all the way to the wire.
 *
 * Its own module rather than `lib/utils.ts`, which the app shell loads on the
 * first paint: only form screens need these, and each of those is a lazy route.
 */

import { SELECT_NONE } from './constants'

/** A Radix select value as an id: the "not set" sentinel and the empty string both mean null. */
export const asId = (value: string): string | null =>
  value === SELECT_NONE || value === '' ? null : value

/** Trimmed free text as the API stores it: blank becomes null, never "". */
export const asText = (value: string): string | null => {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/**
 * A code as the API stores it - a GSTIN, PAN, IFSC, state or currency code:
 * trimmed and upper-cased, and blank becomes null.
 */
export const asUpperText = (value: string): string | null => asText(value)?.toUpperCase() ?? null

/**
 * A decimal field whose blank means zero, as the API takes it: trimmed, and
 * "0" when empty. It stays a string all the way to the wire - never parsed into
 * a double. Not for a field where blank means "not set" (a threshold that falls
 * back to a default): that is `asText`, because zero there is a real value.
 */
export const zeroIfBlank = (value: string): string => {
  const trimmed = value.trim()
  return trimmed === '' ? '0' : trimmed
}

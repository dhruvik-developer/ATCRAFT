/**
 * Serial label parsing shared by every capture surface.
 *
 * The API normalises labels by removing whitespace and upper-casing them, and
 * refuses a duplicate in one registration. Doing the same here means the count
 * shown under a field is the count the request will carry, rather than a number
 * the operator has to reconcile with a 422 afterwards.
 */

/** Mirrors `MAX_SERIAL_BULK` on the API. */
export const SERIAL_BULK_MAX = 500

/** The canonical label the API will store. */
export function normaliseSerial(value: string): string {
  return value.replace(/\s+/g, '').toUpperCase()
}

/**
 * Split a scanned or pasted run into unique labels in the order they arrived.
 *
 * A scanner commonly emits a newline or a tab after every label, and a pasted
 * spreadsheet column arrives newline separated. Commas and semicolons are
 * accepted too because they are what a person types when no scanner is to hand.
 */
export function splitSerialNumbers(value: string): string[] {
  const seen = new Set<string>()
  const numbers: string[] = []
  for (const entry of value.split(/[\s,;]+/)) {
    const number = normaliseSerial(entry)
    if (!number || seen.has(number)) continue
    seen.add(number)
    numbers.push(number)
  }
  return numbers
}

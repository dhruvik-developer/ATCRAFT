/**
 * Serial label parsing shared by every capture surface.
 *
 * The API normalises labels by removing whitespace and upper-casing them, and
 * refuses a duplicate in one registration. Doing the same here means the count
 * shown under a field is the count the request will carry, rather than a number
 * the operator has to reconcile with a 422 afterwards.
 *
 * The other direction lives here too: a read names units by id with their
 * labels beside them, and a screen shows - and takes - the labels.
 */

/** Mirrors `MAX_SERIAL_BULK` on the API. */
export const SERIAL_BULK_MAX = 500

/**
 * What separates one label from the next in a typed or scanned run.
 *
 * A scanner commonly emits a newline or a tab after every label, and a pasted
 * spreadsheet column arrives newline separated. Commas and semicolons are
 * accepted too because they are what a person types when no scanner is to hand.
 */
const SEPARATORS = /[\s,;]+/

/** The canonical label the API will store. */
export function normaliseSerial(value: string): string {
  return value.replace(/\s+/g, '').toUpperCase()
}

/** Split a scanned or pasted run into unique labels in the order they arrived. */
export function splitSerialNumbers(value: string): string[] {
  const seen = new Set<string>()
  const numbers: string[] = []
  for (const entry of value.split(SEPARATORS)) {
    const number = normaliseSerial(entry)
    if (!number || seen.has(number)) continue
    seen.add(number)
    numbers.push(number)
  }
  return numbers
}

/** One unit a read names: its id, and the label a person reads off the unit. */
export interface SerialUnit {
  id: string
  label: string
}

/**
 * Pair the serial ids a read carries with the labels it sends beside them.
 *
 * A read that lists units by id lists their labels too, in the same order -
 * `serial_numbers` beside `serial_ids`, `consumed_serial_numbers` beside
 * `consumed_serial_ids` - and the API labels an id it cannot resolve by the id
 * itself, so the two never fall out of step. A list that does not line up is
 * not trusted for any unit: each is labelled by its id instead, because a label
 * shifted onto the neighbouring unit is worse than none.
 */
export function labelSerials(
  ids: readonly string[],
  labels: readonly string[] | null | undefined,
): SerialUnit[] {
  if (!labels || labels.length !== ids.length) return ids.map((id) => ({ id, label: id }))
  return ids.map((id, index) => ({ id, label: labels[index] ?? id }))
}

/** What a typed or scanned run names among the units a document already holds. */
export interface NamedSerials {
  /** The named units' ids, in the order their labels arrived. */
  ids: string[]
  /** Labels that no unit on the document carries, normalised. */
  unknown: string[]
  /** Labels that arrived more than once, normalised. */
  repeated: string[]
}

/**
 * Turn typed or scanned labels into the ids of units a document already names.
 *
 * A return names units that left on one dispatch line, and its endpoint takes
 * their ids - which nobody can read off a unit. The run is read the way
 * `splitSerialNumbers` reads it and matched against the document's own units.
 * The API refuses a unit the line did not ship and a unit named twice, so both
 * come back here to be said beside the field, rather than dropped or sent.
 */
export function resolveSerialLabels(value: string, units: readonly SerialUnit[]): NamedSerials {
  const idByLabel = new Map(units.map((unit) => [normaliseSerial(unit.label), unit.id]))
  const seen = new Set<string>()
  const named: NamedSerials = { ids: [], unknown: [], repeated: [] }
  for (const entry of value.split(SEPARATORS)) {
    const label = normaliseSerial(entry)
    if (!label) continue
    if (seen.has(label)) {
      if (!named.repeated.includes(label)) named.repeated.push(label)
      continue
    }
    seen.add(label)
    const id = idByLabel.get(label)
    if (id === undefined) named.unknown.push(label)
    else named.ids.push(id)
  }
  return named
}

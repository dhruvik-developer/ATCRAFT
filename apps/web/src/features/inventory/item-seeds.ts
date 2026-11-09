/**
 * The names a saved document already carries for its lines' items.
 *
 * A BOM, a requisition and a job-work order each read with every line's item
 * code and name, so an edit form can name the item on each line at once. The
 * item picker used to read every one of them again - a request per line, for a
 * name the document had already sent.
 */

/** An item as a document line names it: enough for a picker to show it. */
export interface ItemSeed {
  id: string
  code: string
  name: string
}

/**
 * Index a document's line labels by item id, for `ItemPicker`'s `seed`.
 *
 * An entry the read could not name is left out, so the picker reads that one
 * item as it always did rather than showing a blank.
 */
export function itemSeeds(
  entries: Iterable<{ id?: string | null; code?: string | null; name?: string | null }>,
): Map<string, ItemSeed> {
  const seeds = new Map<string, ItemSeed>()
  for (const { id, code, name } of entries) {
    if (id && code && name) seeds.set(id, { id, code, name })
  }
  return seeds
}

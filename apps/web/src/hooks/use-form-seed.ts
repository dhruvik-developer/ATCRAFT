import { useEffect, useLayoutEffect, useRef } from 'react'

/** One part of what identifies the record a form is seeded from. */
export type FormSeedKeyPart = string | number | boolean | null | undefined

/**
 * What identifies the record a form edits: its id, the id of the payload that
 * arrived, or several of these. A list is compared by its contents, so it can be
 * written inline.
 */
export type FormSeedKey = FormSeedKeyPart | readonly FormSeedKeyPart[]

/**
 * Seed a form - or any editor state - once per opening, and again only when the
 * record it edits changes.
 *
 * `seed` runs after commit when `when` turns true, and when `key` changes while
 * `when` is true. Nothing else runs it: not a re-render, not a new `seed`
 * closure, and not a new identity of the record object - which is what a
 * background refetch hands back, and re-seeding on it would wipe what is being
 * typed. `seed` is read through a ref refreshed on every commit, so when it does
 * run it sees the props of that render, never a stale closure.
 *
 * - `when` is the dialog being open, or the record having arrived for a form
 *   with no opening of its own.
 * - `key` is what identifies the record: its id, or `loadedId` - the id of the
 *   payload that arrived - for a dialog that reads its own record. Such a dialog
 *   seeds blank while the read is in flight (or failed) and seeds again when the
 *   record lands, so the record edited last can never survive into this opening
 *   and be saved onto this one.
 * - Never key it on an option list. A Radix Select clears a value whose option
 *   has not loaded (CLAUDE.md), and re-seeding when options arrive would undo a
 *   choice the person already made.
 */
export function useFormSeed(when: boolean, key: FormSeedKey, seed: () => void): void {
  const latest = useRef(seed)
  useLayoutEffect(() => {
    latest.current = seed
  })
  // A list key is compared by value, so `[id, loadedId]` written inline does not
  // re-seed on every render.
  const token = Array.isArray(key) ? JSON.stringify(key) : key
  useEffect(() => {
    if (when) latest.current()
  }, [when, token])
}

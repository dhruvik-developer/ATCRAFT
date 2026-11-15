import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Merge Tailwind classes with later ones winning.
 *
 * `clsx` alone would leave both `p-2` and `p-4` in the output and let stylesheet
 * order decide; `twMerge` resolves the conflict the way the caller expects.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

/**
 * Index rows by id, for turning a lookup list into labels on a grid.
 *
 * Takes `undefined` because it is nearly always fed a query result that has not
 * arrived yet, and an empty map renders a dash without the caller branching.
 * Takes a readonly list because option lists are typed that way, and indexing
 * never writes to its input.
 */
export function indexById<T extends { id: string }>(
  rows: readonly T[] | undefined,
): Map<string, T> {
  return new Map((rows ?? []).map((row) => [row.id, row]))
}

/** Initials for an avatar fallback. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')
}

import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

import { SELECT_NONE } from './constants'

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
 */
export function indexById<T extends { id: string }>(rows: T[] | undefined): Map<string, T> {
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

/** A Radix select value as an id: the "not set" sentinel and the empty string both mean null. */
export const asId = (value: string): string | null =>
  value === SELECT_NONE || value === '' ? null : value

/** Trimmed free text as the API stores it: blank becomes null, never "". */
export const asText = (value: string): string | null => {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

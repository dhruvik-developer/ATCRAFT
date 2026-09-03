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

/** Delay a rapidly-changing value - used for search-as-you-type. */
export function debounce<T extends (...args: never[]) => void>(fn: T, ms: number): T {
  let timer: ReturnType<typeof setTimeout> | undefined
  return ((...args: Parameters<T>) => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => fn(...args), ms)
  }) as T
}

/**
 * Stable string for a filter object, so React Query treats two structurally
 * identical filter sets as one cache entry regardless of key order.
 */
export function stableKey(value: unknown): string {
  if (value === null || typeof value !== 'object') return String(value)
  if (Array.isArray(value)) return `[${value.map(stableKey).join(',')}]`
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .sort(([a], [b]) => a.localeCompare(b))
  return `{${entries.map(([k, v]) => `${k}:${stableKey(v)}`).join(',')}}`
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

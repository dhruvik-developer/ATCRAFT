/**
 * Minimal toast store.
 *
 * Deliberately a plain observable rather than a React context: the query client
 * and the API layer need to raise a toast from outside the component tree, and
 * threading a hook through those would invert the dependency.
 */

import { useSyncExternalStore } from 'react'

import { ApiError } from '@/lib/api-client'
import { MAX_VISIBLE_TOASTS, TOAST_DURATION_MS } from '@/lib/constants'
import { describeError, errorTitle, errors } from '@/lib/messages'

export type ToastVariant = 'default' | 'success' | 'warning' | 'error'

export interface Toast {
  id: string
  title: string
  description?: string
  variant: ToastVariant
  /** Milliseconds before auto-dismiss. Errors stay until dismissed. */
  duration: number
  action?: { label: string; onClick: () => void }
}

let toasts: Toast[] = []
const listeners = new Set<() => void>()
let counter = 0

function emit(): void {
  listeners.forEach((l) => l())
}

function push(toast: Omit<Toast, 'id'>): string {
  const id = `t${++counter}`
  // Cap the stack: a failed bulk operation can raise dozens of errors at once
  // and burying the screen in toasts helps nobody.
  toasts = [...toasts.slice(-(MAX_VISIBLE_TOASTS - 1)), { ...toast, id }]
  emit()
  if (toast.duration > 0) {
    setTimeout(() => dismiss(id), toast.duration)
  }
  return id
}

export function dismiss(id: string): void {
  toasts = toasts.filter((t) => t.id !== id)
  emit()
}

export const toast = {
  show: (title: string, description?: string) =>
    push({ title, description, variant: 'default', duration: TOAST_DURATION_MS.default }),
  success: (title: string, description?: string) =>
    push({ title, description, variant: 'success', duration: TOAST_DURATION_MS.success }),
  warning: (title: string, description?: string) =>
    push({ title, description, variant: 'warning', duration: TOAST_DURATION_MS.warning }),
  /** Errors do not auto-dismiss: the user may need to read a correlation id. */
  error: (title: string, description?: string) =>
    push({ title, description, variant: 'error', duration: TOAST_DURATION_MS.error }),
  /** Direct request handlers share the global error headlines and correlation formatting. */
  failure: (error: unknown, title?: string) =>
    push({
      title: title ?? (error instanceof ApiError ? errorTitle(error) : errors.unexpected),
      description: describeError(error),
      variant: 'error',
      duration: TOAST_DURATION_MS.error,
    }),
  action: (title: string, label: string, onClick: () => void, description?: string) =>
    push({
      title,
      description,
      variant: 'default',
      duration: TOAST_DURATION_MS.action,
      action: { label, onClick },
    }),
  dismiss,
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => toasts,
    () => toasts,
  )
}

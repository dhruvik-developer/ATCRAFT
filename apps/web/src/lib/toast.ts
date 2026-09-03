/**
 * Minimal toast store.
 *
 * Deliberately a plain observable rather than a React context: the query client
 * and the API layer need to raise a toast from outside the component tree, and
 * threading a hook through those would invert the dependency.
 */

import { useSyncExternalStore } from 'react'

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
  toasts = [...toasts.slice(-4), { ...toast, id }]
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
    push({ title, description, variant: 'default', duration: 5000 }),
  success: (title: string, description?: string) =>
    push({ title, description, variant: 'success', duration: 4000 }),
  warning: (title: string, description?: string) =>
    push({ title, description, variant: 'warning', duration: 7000 }),
  /** Errors do not auto-dismiss: the user may need to read a correlation id. */
  error: (title: string, description?: string) =>
    push({ title, description, variant: 'error', duration: 0 }),
  action: (title: string, label: string, onClick: () => void, description?: string) =>
    push({ title, description, variant: 'default', duration: 10000, action: { label, onClick } }),
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

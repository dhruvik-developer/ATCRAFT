import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { dismiss, toast } from '@/lib/toast'

import { Toaster } from './toaster'

let created: string[] = []

beforeEach(() => {
  vi.useFakeTimers()
  created = []
})

afterEach(() => {
  act(() => created.forEach(dismiss))
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('Toaster', () => {
  it('announces errors as alerts and retains the support reference', () => {
    render(<Toaster />)
    act(() => {
      created.push(toast.error('Could not save', 'Please retry (reference abc123).'))
    })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveAttribute('data-toast-variant', 'error')
    expect(alert).toHaveTextContent('reference abc123')
    act(() => vi.advanceTimersByTime(60_000))
    expect(alert).toBeVisible()
  })

  it('announces success as a status and dismisses it after the configured duration', () => {
    render(<Toaster />)
    act(() => {
      created.push(toast.success('Order created'))
    })
    expect(screen.getByRole('status')).toHaveTextContent('Order created')
    act(() => vi.runAllTimers())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('allows an error to be dismissed without activating another action', () => {
    render(<Toaster />)
    act(() => {
      created.push(toast.error('Could not load'))
    })
    fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('runs a notification action once and dismisses its status', () => {
    const onAction = vi.fn()
    render(<Toaster />)
    act(() => {
      created.push(toast.action('Saved', 'Open order', onAction))
    })
    fireEvent.click(screen.getByRole('button', { name: 'Open order' }))
    expect(onAction).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { reportError } from '@/lib/error-reporting'

import { ErrorBoundary } from './error-boundary'

vi.mock('@/lib/error-reporting', () => ({
  errorReportingEnabled: false,
  reportError: vi.fn(),
}))

function Screen({ fail }: { fail: boolean }) {
  if (fail) throw new Error('Internal credentials.token could not be read')
  return <p>The screen recovered</p>
}

afterEach(() => vi.restoreAllMocks())

describe('render exception recovery', () => {
  it('reports a crash, keeps technical details private and lets the user retry', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { rerender } = render(
      <ErrorBoundary>
        <Screen fail />
      </ErrorBoundary>,
    )

    expect(
      screen.getByRole('heading', { name: 'Something went wrong on this screen' }),
    ).toBeVisible()
    expect(screen.queryByText(/credentials\.token/)).not.toBeInTheDocument()
    expect(screen.queryByText(/error has been reported/)).not.toBeInTheDocument()
    expect(reportError).toHaveBeenCalledWith(expect.any(Error), {
      source: 'render',
      componentStack: expect.any(String),
    })

    rerender(
      <ErrorBoundary>
        <Screen fail={false} />
      </ErrorBoundary>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.getByText('The screen recovered')).toBeVisible()
  })

  it('honors a feature fallback while still reporting the exception', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary fallback={<p>This panel is unavailable. Please try again.</p>}>
        <Screen fail />
      </ErrorBoundary>,
    )
    expect(screen.getByText('This panel is unavailable. Please try again.')).toBeVisible()
    expect(screen.queryByText(/credentials\.token/)).not.toBeInTheDocument()
    expect(reportError).toHaveBeenCalled()
  })
})

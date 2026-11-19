import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { lazyDialog } from './lazy-dialog'

// Eager loading is off under Vitest by default (lib/constants.ts); this file
// turns it on to prove a screen fetches its forms right after it paints.
vi.mock('@/lib/constants', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/constants')>()),
  EAGER_PRELOAD: true,
}))
vi.mock('@/lib/error-reporting', () => ({ reportError: vi.fn(), errorReportingEnabled: false }))

function Form({ open }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return open ? <p>Form body</p> : null
}

describe('forms with eager loading on', () => {
  it('downloads a closed form right after its screen paints, and only once', async () => {
    const loader = vi.fn(() => Promise.resolve({ default: Form }))
    const Deferred = lazyDialog(loader)

    const view = render(
      <>
        <h1>Items</h1>
        <Deferred open={false} onOpenChange={vi.fn()} />
        <Deferred open={false} onOpenChange={vi.fn()} />
      </>,
    )

    expect(screen.getByText('Items')).toBeVisible()
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))

    view.rerender(
      <>
        <h1>Items</h1>
        <Deferred open onOpenChange={vi.fn()} />
        <Deferred open={false} onOpenChange={vi.fn()} />
      </>,
    )
    expect(await screen.findByText('Form body')).toBeVisible()
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('leaves a failed download for the form to retry when it is opened', async () => {
    const loader = vi
      .fn<() => Promise<{ default: typeof Form }>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue({ default: Form })
    const Deferred = lazyDialog(loader)

    const view = render(<Deferred open={false} onOpenChange={vi.fn()} />)
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))

    view.rerender(<Deferred open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Form body')).toBeVisible()
    expect(loader).toHaveBeenCalledTimes(2)
  })
})

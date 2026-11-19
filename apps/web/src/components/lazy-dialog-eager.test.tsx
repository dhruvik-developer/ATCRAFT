import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { lazyDialog } from './lazy-dialog'

// Eager loading is off under Vitest by default (lib/constants.ts); this file
// turns it on to prove a screen's forms are fetched before anyone opens one.
vi.mock('@/lib/constants', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/constants')>()),
  EAGER_PRELOAD: true,
}))
vi.mock('@/lib/error-reporting', () => ({ reportError: vi.fn(), errorReportingEnabled: false }))

function Form({ open }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return open ? <p>Form body</p> : null
}

/** A loader that has not been called yet, and settles only when told to. */
function heldLoader() {
  let finish!: () => void
  const loader = vi.fn(
    () =>
      new Promise<{ default: typeof Form }>((resolve) => {
        finish = () => resolve({ default: Form })
      }),
  )
  return { loader, finish: () => finish() }
}

afterEach(() => {
  Reflect.deleteProperty(navigator, 'connection')
})

describe('forms with eager loading on', () => {
  it('downloads a closed form as soon as its screen code runs, and only once', async () => {
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

  it('downloads a form its screen mounts only once it is opened, before any click', async () => {
    // The customers screen shape: no form exists until a row's Edit is pressed.
    const { loader, finish } = heldLoader()
    const Deferred = lazyDialog(loader)
    function Screen() {
      const [editing, setEditing] = useState(false)
      return (
        <>
          <button onClick={() => setEditing(true)}>Edit</button>
          {editing && <Deferred open onOpenChange={() => setEditing(false)} />}
        </>
      )
    }

    render(<Screen />)
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))
    finish()

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
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

  it('waits for the first opening when the browser asks to save data', async () => {
    Object.defineProperty(navigator, 'connection', {
      value: { saveData: true },
      configurable: true,
    })
    const loader = vi.fn(() => Promise.resolve({ default: Form }))
    const Deferred = lazyDialog(loader)

    const view = render(<Deferred open={false} onOpenChange={vi.fn()} />)
    // Longer than an idle callback takes to run, so a scheduled download would have started.
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(loader).not.toHaveBeenCalled()

    view.rerender(<Deferred open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Form body')).toBeVisible()
    expect(loader).toHaveBeenCalledTimes(1)
  })
})

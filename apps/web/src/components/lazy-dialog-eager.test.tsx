import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { installStaleBundleRecovery } from '@/lib/stale-bundle'
import { asBuiltImport, recoveryHost } from '@/test/preload-failure'
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

let stopRecovery: () => void = () => undefined

afterEach(() => {
  stopRecovery()
  vi.restoreAllMocks()
  Reflect.deleteProperty(navigator, 'connection')
})

/** The stale-bundle recovery main.tsx installs, watching this test's downloads. */
function watchForReloads() {
  const recovery = recoveryHost()
  stopRecovery = installStaleBundleRecovery(window, recovery.host)
  return recovery
}

/** How the browser reports a chunk it could not fetch. */
const chunkFailure = () => new TypeError('Failed to fetch dynamically imported module')

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

  it('never reloads the page for a failed download, and retries it when the form is opened', async () => {
    const recovery = watchForReloads()
    const loader = vi
      .fn<() => Promise<{ default: typeof Form }>>()
      .mockRejectedValueOnce(chunkFailure())
      .mockResolvedValue({ default: Form })
    const Deferred = lazyDialog(asBuiltImport(loader))

    const view = render(<Deferred open={false} onOpenChange={vi.fn()} />)
    await recovery.decided(1)
    expect(recovery.reload).not.toHaveBeenCalled()

    view.rerender(<Deferred open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Form body')).toBeVisible()
    expect(loader).toHaveBeenCalledTimes(2)
    expect(recovery.reload).not.toHaveBeenCalled()
  })

  /** A form opened while its eager download is still running, which then fails. */
  async function openWhileFailing(retry: () => Promise<{ default: typeof Form }>) {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const recovery = watchForReloads()
    let fail!: () => void
    const loader = vi
      .fn<() => Promise<{ default: typeof Form }>>()
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            fail = () => reject(chunkFailure())
          }),
      )
      .mockImplementation(retry)
    const Deferred = lazyDialog(asBuiltImport(loader))

    const view = render(<Deferred open={false} onOpenChange={vi.fn()} />)
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))
    view.rerender(<Deferred open onOpenChange={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: 'Loading form' })).toBeVisible()
    fail()

    // It shares the background download's failure, so it is not reloaded for.
    expect(await screen.findByRole('heading', { name: 'Could not open this form' })).toBeVisible()
    await recovery.decided(1)
    expect(recovery.reload).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    return { recovery, loader }
  }

  it('offers a form opened while its download fails a retry that downloads it afresh', async () => {
    const { recovery, loader } = await openWhileFailing(() => Promise.resolve({ default: Form }))

    expect(await screen.findByText('Form body')).toBeVisible()
    expect(loader).toHaveBeenCalledTimes(2)
    expect(recovery.reload).not.toHaveBeenCalled()
  })

  it('reloads when that retry fails too, since the person is waiting on it', async () => {
    const { recovery, loader } = await openWhileFailing(() => Promise.reject(chunkFailure()))

    await recovery.decided(2)
    expect(loader).toHaveBeenCalledTimes(2)
    expect(recovery.reload).toHaveBeenCalledTimes(1)
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

import { afterEach, describe, expect, it } from 'vitest'

import { CHUNK_RELOAD_LOOP_WINDOW_MS } from '@/lib/constants'
import { noteBackgroundFailure } from '@/lib/preload'
import { installStaleBundleRecovery } from '@/lib/stale-bundle'
import { asBuiltImport, recoveryHost } from '@/test/preload-failure'

/** A chunk the server no longer has, as the browser reports it. */
function missingChunk() {
  return asBuiltImport(() =>
    Promise.reject(new TypeError('Failed to fetch dynamically imported module')),
  )
}

/** A screen or form someone opened: whatever catches it (`lazy`, the boundary) records nothing. */
function waitedOn(load: () => Promise<unknown>): Promise<unknown> {
  return load().catch((error: unknown) => error)
}

let uninstall: () => void = () => undefined
afterEach(() => uninstall())

function install() {
  const recovery = recoveryHost()
  uninstall = installStaleBundleRecovery(window, recovery.host)
  return recovery
}

describe('stale-bundle recovery', () => {
  it('reloads once when a download someone is waiting on fails', async () => {
    const recovery = install()

    await waitedOn(missingChunk())
    await recovery.decided(1)

    expect(recovery.reload).toHaveBeenCalledTimes(1)
    expect(recovery.host.readReloadedAt()).toBe(String(recovery.clock.now))
  })

  it('never reloads for a background download that fails', async () => {
    const recovery = install()

    await missingChunk()().catch(noteBackgroundFailure)
    await recovery.decided(1)

    expect(recovery.reload).not.toHaveBeenCalled()
    // Nor does it spend the loop guard: the next real failure still recovers.
    expect(recovery.host.readReloadedAt()).toBeNull()
    await waitedOn(missingChunk())
    await recovery.decided(2)
    expect(recovery.reload).toHaveBeenCalledTimes(1)
  })

  it('lets the import reject with its own error, which is how a background download sees it', async () => {
    install()
    const failure = new TypeError('Failed to fetch dynamically imported module')

    const caught = await waitedOn(asBuiltImport(() => Promise.reject(failure)))

    expect(caught).toBe(failure)
  })

  it('tells a background failure from a waited-on one in the same burst', async () => {
    const recovery = install()

    await Promise.all([missingChunk()().catch(noteBackgroundFailure), waitedOn(missingChunk())])
    await recovery.decided(2)

    expect(recovery.reload).toHaveBeenCalledTimes(1)
  })

  it('leaves a second failure moments after its reload to the error boundary', async () => {
    const recovery = install()
    await waitedOn(missingChunk())
    await recovery.decided(1)

    recovery.clock.now += CHUNK_RELOAD_LOOP_WINDOW_MS - 1
    await waitedOn(missingChunk())
    await recovery.decided(2)

    expect(recovery.reload).toHaveBeenCalledTimes(1)
  })

  it('recovers again from a later deploy in the same tab', async () => {
    const recovery = install()
    await waitedOn(missingChunk())
    await recovery.decided(1)

    recovery.clock.now += CHUNK_RELOAD_LOOP_WINDOW_MS
    await waitedOn(missingChunk())
    await recovery.decided(2)

    expect(recovery.reload).toHaveBeenCalledTimes(2)
  })

  it('reloads without session storage, reading it as never reloaded', async () => {
    const recovery = recoveryHost()
    uninstall = installStaleBundleRecovery(window, {
      ...recovery.host,
      readReloadedAt: () => null,
      writeReloadedAt: () => undefined,
    })

    await waitedOn(missingChunk())
    await recovery.decided(1)

    expect(recovery.reload).toHaveBeenCalledTimes(1)
  })

  it('stops listening once uninstalled', async () => {
    const recovery = install()
    uninstall()

    await waitedOn(missingChunk())
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(recovery.reload).not.toHaveBeenCalled()
  })
})

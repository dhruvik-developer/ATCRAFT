import { waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { installStaleBundleRecovery } from '@/lib/stale-bundle'
import { renderScreen } from '@/test/harness'
import { asBuiltImport, recoveryHost } from '@/test/preload-failure'

import { PushSessionBridge } from './push-session'
import { syncPushSubscription } from './push'

vi.mock('./push', () => ({ cancelPushSetup: vi.fn(), syncPushSubscription: vi.fn() }))

let stopRecovery: () => void = () => undefined
afterEach(() => stopRecovery())

describe('the push session bridge', () => {
  it('never reloads the page when its background download fails', async () => {
    const recovery = recoveryHost()
    stopRecovery = installStaleBundleRecovery(window, recovery.host)
    // The setup's code failing to arrive, as the production build reports it.
    vi.mocked(syncPushSubscription).mockImplementation(
      asBuiltImport(() =>
        Promise.reject(new TypeError('Failed to fetch dynamically imported module')),
      ),
    )

    renderScreen(<PushSessionBridge />, { permissions: [P.NOTIFICATION_VIEW] })
    await waitFor(() => expect(syncPushSubscription).toHaveBeenCalledTimes(1))
    await recovery.decided(1)

    expect(recovery.reload).not.toHaveBeenCalled()
  })
})

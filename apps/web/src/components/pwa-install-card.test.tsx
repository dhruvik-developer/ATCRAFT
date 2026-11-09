import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PwaInstallCard } from './pwa-install-card'
import { Toaster } from '@/components/ui/toaster'
import * as pwa from '@/lib/pwa-install'

afterEach(() => {
  vi.restoreAllMocks()
  pwa._resetInstallPromptForTests()
})

describe('PwaInstallCard', () => {
  it('hides when the session is already standalone', () => {
    vi.spyOn(pwa, 'isStandaloneDisplay').mockReturnValue(true)
    const { container } = render(<PwaInstallCard audience="vendor" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows Install for Chromium when a deferred prompt exists', () => {
    vi.spyOn(pwa, 'isStandaloneDisplay').mockReturnValue(false)
    vi.spyOn(pwa, 'isIosDevice').mockReturnValue(false)
    vi.spyOn(pwa, 'getDeferredInstallPrompt').mockReturnValue({
      prompt: vi.fn(),
      userChoice: Promise.resolve({ outcome: 'accepted', platform: 'web' }),
    } as unknown as pwa.BeforeInstallPromptEvent)
    render(<PwaInstallCard audience="vendor" />)
    expect(screen.getByRole('heading', { name: 'Install the vendor app' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Install ATCRAFT' })).toBeEnabled()
  })

  it('headlines an install the browser cannot start, and says what to do instead', async () => {
    // The whole sentence used to be the toast's title, full stop included.
    vi.spyOn(pwa, 'isStandaloneDisplay').mockReturnValue(false)
    vi.spyOn(pwa, 'isIosDevice').mockReturnValue(false)
    vi.spyOn(pwa, 'getDeferredInstallPrompt').mockReturnValue({
      prompt: vi.fn(),
      userChoice: Promise.resolve({ outcome: 'accepted', platform: 'web' }),
    } as unknown as pwa.BeforeInstallPromptEvent)
    vi.spyOn(pwa, 'promptPwaInstall').mockResolvedValue('unavailable')
    render(
      <>
        <PwaInstallCard audience="vendor" />
        <Toaster />
      </>,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Install ATCRAFT' }))

    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(await within(toasts).findByText('Could not install the app')).toBeVisible()
    expect(within(toasts).getByText(/Use the browser menu to install the app/)).toBeVisible()
  })

  it('shows Add to Home Screen guidance on iOS', () => {
    vi.spyOn(pwa, 'isStandaloneDisplay').mockReturnValue(false)
    vi.spyOn(pwa, 'isIosDevice').mockReturnValue(true)
    render(<PwaInstallCard audience="dealer" />)
    expect(screen.getByRole('heading', { name: 'Install the dealer app' })).toBeVisible()
    expect(screen.getByText(/Add to Home Screen/i)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Install ATCRAFT' })).toBeNull()
  })
})

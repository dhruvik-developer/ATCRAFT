import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PwaInstallCard } from './pwa-install-card'
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

  it('shows Add to Home Screen guidance on iOS', () => {
    vi.spyOn(pwa, 'isStandaloneDisplay').mockReturnValue(false)
    vi.spyOn(pwa, 'isIosDevice').mockReturnValue(true)
    render(<PwaInstallCard audience="dealer" />)
    expect(screen.getByRole('heading', { name: 'Install the dealer app' })).toBeVisible()
    expect(screen.getByText(/Add to Home Screen/i)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Install ATCRAFT' })).toBeNull()
  })
})

import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ok, renderScreen } from '@/test/harness'
import { pushBrowser } from '@/test/setup'

import PushDeviceCard from './push-device-card'

describe('push device controls', () => {
  it('does not prompt on mount and explains a deployment with push disabled', async () => {
    renderScreen(<PushDeviceCard />, {
      routes: { 'GET /notifications/push/config': ok({ enabled: false, vapid_public_key: null }) },
    })
    expect(
      await screen.findByText(/Push notifications are not enabled for this deployment/),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Enable on this device' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send test' })).toBeDisabled()
    expect(pushBrowser.requestPermission).not.toHaveBeenCalled()
    expect(pushBrowser.register).not.toHaveBeenCalled()
  })

  it('explains a denied site permission without requesting it again', async () => {
    pushBrowser.permission = 'denied'
    renderScreen(<PushDeviceCard />, {
      routes: { 'GET /notifications/push/config': ok({ enabled: true, vapid_public_key: 'AQID' }) },
    })
    expect(await screen.findByText(/Notifications are blocked in this browser/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Enable on this device' })).toBeDisabled()
    expect(pushBrowser.requestPermission).not.toHaveBeenCalled()
  })

  it('shows a safe recoverable error when a browser provider fails', async () => {
    const { user, fetch } = renderScreen(<PushDeviceCard />, {
      routes: { 'GET /notifications/push/config': ok({ enabled: true, vapid_public_key: 'AQID' }) },
    })
    const button = await screen.findByRole('button', { name: 'Enable on this device' })
    // The configuration query enables the control after its initial read.
    await screen.findByText('Notifications have not been enabled on this device.')
    await user.click(button)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Notifications could not be configured on this device. Please try again.',
    )
    expect(screen.queryByText(/must be configured by the test/)).not.toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toEqual([])
  })

  it('explains the iPhone home-screen installation requirement', async () => {
    const browser = vi
      .spyOn(navigator, 'userAgent', 'get')
      .mockReturnValue('Mozilla/5.0 (iPhone) AppleWebKit Safari')
    try {
      renderScreen(<PushDeviceCard />, {
        routes: {
          'GET /notifications/push/config': ok({ enabled: true, vapid_public_key: 'AQID' }),
        },
      })
      expect(await screen.findByText(/Add to Home Screen to receive notifications/)).toBeVisible()
      expect(screen.getByRole('button', { name: 'Enable on this device' })).toBeDisabled()
    } finally {
      browser.mockRestore()
    }
  })
})

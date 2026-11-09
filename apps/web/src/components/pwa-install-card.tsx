import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/primitives'
import {
  ensureInstallPromptListener,
  getDeferredInstallPrompt,
  isIosDevice,
  isStandaloneDisplay,
  promptPwaInstall,
} from '@/lib/pwa-install'
import { toast } from '@/lib/toast'

/**
 * Shown on vendor/dealer home when the session is not already a installed PWA.
 * Chromium gets an Install button; iOS gets Add-to-Home-Screen copy.
 */
export function PwaInstallCard({ audience }: { audience: 'vendor' | 'dealer' }) {
  const [standalone, setStandalone] = useState(isStandaloneDisplay)
  const [canPrompt, setCanPrompt] = useState(() => getDeferredInstallPrompt() !== null)
  const [busy, setBusy] = useState(false)
  const ios = isIosDevice()

  useEffect(() => {
    ensureInstallPromptListener()
    setStandalone(isStandaloneDisplay())
    setCanPrompt(getDeferredInstallPrompt() !== null)
    const onAvailable = () => setCanPrompt(true)
    window.addEventListener('atcraft:pwa-install-available', onAvailable)
    return () => window.removeEventListener('atcraft:pwa-install-available', onAvailable)
  }, [])

  if (standalone) return null

  const title = audience === 'vendor' ? 'Install the vendor app' : 'Install the dealer app'
  const description = ios
    ? 'Add ATCRAFT to your Home Screen for a full-screen app with push notifications.'
    : 'Install ATCRAFT on this device for a full-screen app and faster access to your workspace.'

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {ios ? (
          <p className="text-sm text-muted-foreground">
            In Safari, tap Share, then Add to Home Screen. Open the installed icon, then enable
            notifications from your profile if you want alerts when the browser is closed.
          </p>
        ) : (
          <Button
            type="button"
            className="min-h-11"
            disabled={!canPrompt || busy}
            loading={busy}
            onClick={() => {
              setBusy(true)
              void promptPwaInstall()
                .then((outcome) => {
                  if (outcome === 'accepted') {
                    setStandalone(true)
                    toast.success('ATCRAFT installed on this device')
                  } else if (outcome === 'unavailable') {
                    toast.error(
                      'Could not install the app',
                      'Install is not available in this browser yet. Use the browser menu to install the app, or open the site in Chrome.',
                    )
                  }
                  setCanPrompt(getDeferredInstallPrompt() !== null)
                })
                .finally(() => setBusy(false))
            }}
          >
            Install ATCRAFT
          </Button>
        )}
        {!ios && !canPrompt && (
          <p className="text-sm text-muted-foreground">
            If Install stays disabled, open this site in Chrome or Edge and use the browser&apos;s
            Install app menu after the page has loaded once.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

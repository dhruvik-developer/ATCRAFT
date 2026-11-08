/**
 * Installable-PWA helpers for vendor/dealer home screens.
 *
 * Chromium fires `beforeinstallprompt`; iOS Safari does not — those users get
 * Add-to-Home-Screen instructions instead. The service worker stays push-only
 * (no fetch cache); this module never registers a worker.
 */

export type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>
}

let deferredPrompt: BeforeInstallPromptEvent | null = null
let listening = false

export function isStandaloneDisplay(): boolean {
  if (typeof window === 'undefined') return false
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  )
}

export function isIosDevice(): boolean {
  if (typeof navigator === 'undefined') return false
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  )
}

/** Listen once for Chromium's install prompt; safe to call from React mounts. */
export function ensureInstallPromptListener(): void {
  if (typeof window === 'undefined' || listening) return
  listening = true
  window.addEventListener('beforeinstallprompt', ((event: Event) => {
    event.preventDefault()
    deferredPrompt = event as BeforeInstallPromptEvent
    window.dispatchEvent(new Event('atcraft:pwa-install-available'))
  }) as EventListener)
}

export function getDeferredInstallPrompt(): BeforeInstallPromptEvent | null {
  return deferredPrompt
}

/** For tests — resets module state between cases. */
export function _resetInstallPromptForTests(): void {
  deferredPrompt = null
  listening = false
}

export function _setDeferredInstallPromptForTests(prompt: BeforeInstallPromptEvent | null): void {
  deferredPrompt = prompt
}

export async function promptPwaInstall(): Promise<'accepted' | 'dismissed' | 'unavailable'> {
  const prompt = deferredPrompt
  if (!prompt) return 'unavailable'
  deferredPrompt = null
  await prompt.prompt()
  const { outcome } = await prompt.userChoice
  return outcome
}

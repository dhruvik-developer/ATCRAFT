import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { AppRouter } from '@/app/router'
import { ErrorBoundary } from '@/components/error-boundary'
import { installGlobalErrorReporting } from '@/lib/error-reporting'
import { Toaster } from '@/components/ui/toaster'
import { AuthProvider } from '@/features/auth/auth-context'
import { STORAGE_KEYS } from '@/lib/constants'
import { queryClient } from '@/lib/query-client'
import '@/styles/globals.css'

/**
 * Paint the right theme on the first frame.
 *
 * `useTheme` in AppLayout owns the toggle, but it only runs once that layout
 * mounts - which on a hard reload is after `/auth/me` has returned, and on the
 * login page never. A dark-theme operator therefore got a full white screen for
 * the length of the auth round trip and a light login page on a fresh visit.
 * Setting the class here, before the first render, removes both. Reading
 * `localStorage` can itself throw when site data is blocked, so it is guarded;
 * the media query is the same fallback `useTheme` uses, so the two agree.
 */
function applyStoredTheme(): void {
  let stored: string | null = null
  try {
    stored = localStorage.getItem(STORAGE_KEYS.theme)
  } catch {
    // Storage unavailable (private window, blocked site data). Fall through to
    // the OS preference rather than failing the boot.
  }
  const dark = stored
    ? stored === 'dark'
    : window.matchMedia('(prefers-color-scheme: dark)').matches
  document.documentElement.classList.toggle('dark', dark)
}

applyStoredTheme()

/**
 * Recover from a stale bundle after a deploy.
 *
 * `index.html` is served `no-store` while the hashed chunks it names are
 * immutable and removed on redeploy, so a tab left open across a release holds
 * an index whose next `import('/assets/<page>-<oldhash>.js')` 404s. Vite raises
 * `vite:preloadError` for exactly that; unhandled, it throws, the boundary
 * catches it and reports a crash - and "Try again" can never fix it, because
 * React's `lazy` caches the rejected promise. Reloading picks up the new index
 * and the new hashes.
 *
 * Guarded by *when* the last such reload happened, not by whether one has: a
 * stale bundle is cured by the very next load, so a second failure moments
 * later is a chunk that genuinely is not being served - a broken deploy - and
 * reloading again would spin. Ten seconds is long enough to cover the reload
 * and short enough that a later deploy, in the same long-lived tab, still
 * recovers by itself. Clearing the flag on a successful boot would not work:
 * the entry loads fine in both cases, and it is a lazy route chunk that 404s.
 */
const RELOADED_AT_KEY = 'atcraft.chunk-reloaded-at'
const RELOAD_LOOP_WINDOW_MS = 10_000

window.addEventListener('vite:preloadError', (event) => {
  const now = Date.now()
  let last = 0
  try {
    last = Number(sessionStorage.getItem(RELOADED_AT_KEY)) || 0
  } catch {
    // No session storage (private window, blocked site data). Fall through and
    // reload: a stale bundle is by far the likelier cause of a preload failure.
  }

  // Reloaded for this a moment ago and it failed again - the chunk is really
  // gone. Let it through to the error boundary rather than spinning.
  if (now - last < RELOAD_LOOP_WINDOW_MS) return

  try {
    sessionStorage.setItem(RELOADED_AT_KEY, String(now))
  } catch {
    // Without storage the loop guard cannot arm; the reload below is still the
    // right move for the common case.
  }
  event.preventDefault()
  window.location.reload()
})

const container = document.getElementById('root')
if (!container) throw new Error('Root element #root not found')

// An error boundary catches render errors and nothing else: a failure in an
// event handler, a timer or a promise goes straight past it, and those are most
// of them. Installed before the first render so a crash during mount is caught.
installGlobalErrorReporting()

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          {/* AuthProvider sits inside the router because its restore flow
              navigates, and inside QueryClientProvider because it clears the
              cache on sign-out. */}
          <AuthProvider>
            <AppRouter />
          </AuthProvider>
        </BrowserRouter>
        <Toaster />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)

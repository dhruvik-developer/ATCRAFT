import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { AppRouter } from '@/app/router'
import { ErrorBoundary } from '@/components/error-boundary'
import { installGlobalErrorReporting } from '@/lib/error-reporting'
import { Toaster } from '@/components/ui/toaster'
import { ThemeProvider } from '@/app/theme-context'
import { AuthProvider } from '@/features/auth/auth-context'
import { queryClient } from '@/lib/query-client'
import { applyStoredTheme } from '@/lib/theme'
import '@/styles/globals.css'

/**
 * Paint the right theme on the first frame.
 *
 * The provider owns the theme once React is running, but it only runs once
 * React is running - which on a hard reload is after `/auth/me` has returned.
 * A dark-theme operator therefore got a full white screen for the length of
 * that round trip, and a light login page on a fresh visit. Setting the class
 * here, before the first render, removes both.
 *
 * The logic lives in `@/lib/theme` and not here, because the provider reads the
 * same storage a moment later: two implementations of "which theme is this"
 * that drift apart produce a screen that changes colour just after it loads.
 */
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
      {/* Outermost of the providers: the theme applies to every screen, the
          login page included, and it depends on nothing else. The boundary's
          own fallback renders outside this and reads no context - it is themed
          by the `dark` class `applyStoredTheme` already put on <html>, which is
          what makes a crash screen dark for a dark-theme operator. */}
      <ThemeProvider>
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
      </ThemeProvider>
    </ErrorBoundary>
  </StrictMode>,
)

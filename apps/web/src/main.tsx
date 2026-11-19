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
import { installStaleBundleRecovery } from '@/lib/stale-bundle'
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
 * Recover from a stale bundle after a deploy: a screen or form the person is
 * waiting on that fails to download reloads the page once, while a background
 * download that fails never does (lib/stale-bundle.ts). Installed before the
 * first render, so a screen that fails on the first paint is covered.
 */
installStaleBundleRecovery()

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

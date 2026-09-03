import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { AppRouter } from '@/app/router'
import { ErrorBoundary } from '@/components/error-boundary'
import { installGlobalErrorReporting } from '@/lib/error-reporting'
import { Toaster } from '@/components/ui/toaster'
import { AuthProvider } from '@/features/auth/auth-context'
import { queryClient } from '@/lib/query-client'
import '@/styles/globals.css'

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

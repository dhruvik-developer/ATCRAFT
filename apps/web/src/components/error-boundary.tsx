import { AlertTriangle } from 'lucide-react'
import { Component, type ErrorInfo, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { reportError } from '@/lib/error-reporting'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Last-resort render guard.
 *
 * A crash inside one screen must not leave an operator staring at a blank page
 * with unsaved work and no way back. Error boundaries have to be class
 * components - React has no hook equivalent.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The fallback below tells the user "the error has been logged". That was
    // not true while this only called `console.error` - it logged to a console
    // nobody reads. `reportError` sends it to the configured collector, and
    // still prints it when none is configured.
    reportError(error, { source: 'render', componentStack: info.componentStack ?? undefined })
  }

  render() {
    if (!this.state.error) return this.props.children
    if (this.props.fallback) return this.props.fallback

    return (
      <div className="grid min-h-dvh place-items-center p-6">
        <div className="max-w-md text-center">
          <AlertTriangle className="mx-auto h-10 w-10 text-warning" aria-hidden />
          <h1 className="mt-4 text-lg font-semibold">Something went wrong on this screen</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            The error has been logged. Reloading usually clears it; if it keeps happening, tell your
            administrator what you were doing.
          </p>
          {import.meta.env.DEV && (
            <pre className="mt-4 max-h-48 overflow-auto rounded-md bg-muted p-3 text-left text-xs">
              {this.state.error.message}
            </pre>
          )}
          <div className="mt-6 flex justify-center gap-2">
            <Button onClick={() => window.location.reload()}>Reload</Button>
            <Button variant="outline" onClick={() => this.setState({ error: null })}>
              Try again
            </Button>
          </div>
        </div>
      </div>
    )
  }
}

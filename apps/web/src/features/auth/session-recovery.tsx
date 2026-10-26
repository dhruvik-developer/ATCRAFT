import { AlertCircle } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { authenticationRetryAfter } from '@/features/auth/auth-retry'
import { describeError, errors } from '@/lib/messages'

/** An unverified session can recover without rendering protected pages or losing its route. */
export function SessionRecovery({ error }: { error: Error }) {
  const { retryRestoration, logout } = useAuth()
  const [retryAfter, setRetryAfter] = useState(() => authenticationRetryAfter(error))
  const [isLeaving, setIsLeaving] = useState(false)

  useEffect(() => setRetryAfter(authenticationRetryAfter(error)), [error])
  useEffect(() => {
    if (retryAfter === null) return
    if (retryAfter <= 0) {
      setRetryAfter(null)
      return
    }
    const timer = setTimeout(() => setRetryAfter((seconds) => (seconds ?? 1) - 1), 1000)
    return () => clearTimeout(timer)
  }, [retryAfter])

  return (
    <main className="grid min-h-dvh place-items-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle asChild>
            <h1>{errors.sessionRestoreFailed}</h1>
          </CardTitle>
          <CardDescription>
            Check your connection and try again. You can also sign in with a different account.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Alert variant="destructive">
            <AlertCircle aria-hidden />
            <AlertDescription>{describeError(error, errors.serviceUnavailable)}</AlertDescription>
          </Alert>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              disabled={retryAfter !== null || isLeaving}
              onClick={retryRestoration}
            >
              {retryAfter === null ? 'Try again' : `Try again in ${retryAfter}s`}
            </Button>
            <Button
              type="button"
              variant="outline"
              loading={isLeaving}
              onClick={() => {
                setIsLeaving(true)
                void logout().finally(() => setIsLeaving(false))
              }}
            >
              Sign in instead
            </Button>
          </div>
        </CardContent>
      </Card>
    </main>
  )
}

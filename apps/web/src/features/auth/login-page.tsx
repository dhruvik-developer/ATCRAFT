import { zodResolver } from '@hookform/resolvers/zod'
import { AlertCircle, Eye, EyeOff, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ApiError } from '@/lib/api-client'

/**
 * How long to hold the form when a 429 arrives without a usable wait.
 *
 * `RateLimitedError` and the rate-limit middleware both put
 * `retry_after_seconds` in the error body, so this is defensive - but the
 * fallback has to be a real, finite wait. It used to be zero, which disabled
 * the submit button with no countdown to re-enable it: the form was dead until
 * the page was reloaded, which is a worse outcome than the rate limit itself.
 */
const DEFAULT_RETRY_AFTER_SECONDS = 30

const schema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  // No complexity rules on the *login* form: the policy is enforced when a
  // password is set. Rejecting a legacy password here would lock out a user the
  // API would happily authenticate and then upgrade.
  password: z.string().min(1, 'Password is required'),
})

type LoginForm = z.infer<typeof schema>

export function LoginPage() {
  const { login, isAuthenticated, isRestoring } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [showPassword, setShowPassword] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  /** Seconds still to wait after a 429. Null once the form is usable again. */
  const [retryAfter, setRetryAfter] = useState<number | null>(null)

  // The wait has to run down on its own. Without this the submit button stays
  // disabled for the life of the page - and because a disabled submit button
  // also suppresses implicit submission, pressing Enter in the password field
  // does nothing either, so `onSubmit` never runs to clear the flag. The only
  // recovery was a reload.
  useEffect(() => {
    if (retryAfter === null) return
    if (retryAfter <= 0) {
      setRetryAfter(null)
      setFormError(null)
      return
    }
    const timer = setTimeout(() => setRetryAfter((seconds) => (seconds ?? 1) - 1), 1000)
    return () => clearTimeout(timer)
  }, [retryAfter])

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginForm>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', password: '' },
  })

  if (isRestoring) {
    return (
      <div className="grid min-h-dvh place-items-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (isAuthenticated) {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  const onSubmit = async (values: LoginForm) => {
    setFormError(null)
    setRetryAfter(null)
    try {
      const user = await login(values.email, values.password)
      const from = (location.state as { from?: string } | null)?.from ?? '/'
      navigate(user.must_change_password ? '/profile?change-password=1' : from, { replace: true })
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 429) {
          // The API puts the wait in a `Retry-After` header, which the shared
          // client does not surface, and repeats it in the error body as
          // `details.retry_after_seconds`. Take that when it is usable and fall
          // back to a real, finite wait when it is not - never to a wait that
          // never ends.
          const raw = error.details?.retry_after_seconds
          const seconds = typeof raw === 'number' ? raw : Number(raw)
          setRetryAfter(
            Number.isFinite(seconds) && seconds > 0
              ? Math.ceil(seconds)
              : DEFAULT_RETRY_AFTER_SECONDS,
          )
          // The API's own message first - it is the one that names the limit
          // that was hit; the countdown is appended by the button below.
          setFormError(error.message || 'Too many attempts. Wait before trying again.')
        } else {
          // The API's own message, verbatim. It is what distinguishes bad
          // credentials from a locked, disabled or expired account, and a
          // generic "sign-in failed" throws that away.
          setFormError(error.message)
        }
      } else {
        setFormError('Could not reach the server. Check your connection and try again.')
      }
    }
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-2">
      {/* Brand panel - hidden on small screens where it would just push the
          form below the fold. An <aside>, and its strapline is NOT a heading:
          the panel disappears below `lg`, so an <h1> here exists on a desktop
          and not on a phone, and the page's heading structure would change with
          the viewport. The form is the page; this is decoration beside it. */}
      <aside
        aria-label="About Atcraft Manufacturing ERP"
        className="relative hidden flex-col justify-between bg-sidebar p-10 text-white lg:flex"
      >
        <div className="flex items-center gap-3">
          <div className="grid h-10 w-10 place-items-center rounded-lg bg-sidebar-accent text-lg font-bold">
            A
          </div>
          <span className="text-lg font-semibold">Atcraft Manufacturing ERP</span>
        </div>
        <div className="max-w-md">
          <p className="text-3xl font-semibold leading-tight">
            From sales order to dispatch, with the stock figures that hold up.
          </p>
          <p className="mt-4 text-sm leading-relaxed text-white/70">
            Multi-level BOM, common-part consolidation, MRP, procurement, production, job work,
            quality and dispatch - on one auditable ledger.
          </p>
        </div>
        <p className="text-xs text-white/70">
          Authorised users only. All activity is recorded in the audit trail.
        </p>
      </aside>

      {/* Form panel - the page's <main>. Without a main landmark every element
          here sits outside one, and "skip to content" has nothing to skip to. */}
      <main className="flex items-center justify-center p-6">
        <Card className="w-full max-w-md border-0 shadow-none sm:border sm:shadow-sm">
          <CardHeader>
            <div className="mb-2 flex items-center gap-2.5 lg:hidden">
              <div className="grid h-9 w-9 place-items-center rounded-lg bg-primary font-bold text-primary-foreground">
                A
              </div>
              <span className="font-semibold">Atcraft ERP</span>
            </div>
            {/* h1: the only heading on the page, and the same on every
                viewport. CardTitle renders an h3 by default, which skipped two
                levels from nothing. */}
            <CardTitle asChild className="text-2xl">
              <h1>Sign in</h1>
            </CardTitle>
            <CardDescription>Use your Atcraft account to continue.</CardDescription>
          </CardHeader>

          <CardContent>
            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
              {formError && (
                <Alert variant="destructive">
                  <AlertCircle className="mb-1 text-destructive" />
                  <AlertDescription className="text-foreground">{formError}</AlertDescription>
                </Alert>
              )}

              <div className="space-y-1.5">
                <Label htmlFor="email" required>
                  Email
                </Label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="username"
                  autoFocus
                  placeholder="you@atcraft.example"
                  aria-invalid={Boolean(errors.email)}
                  aria-describedby={errors.email ? 'email-error' : undefined}
                  {...register('email')}
                />
                {errors.email && (
                  <p id="email-error" className="text-xs text-destructive">
                    {errors.email.message}
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="password" required>
                  Password
                </Label>
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    className="pr-10"
                    aria-invalid={Boolean(errors.password)}
                    aria-describedby={errors.password ? 'password-error' : undefined}
                    {...register('password')}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
                {errors.password && (
                  <p id="password-error" className="text-xs text-destructive">
                    {errors.password.message}
                  </p>
                )}
              </div>

              <Button
                type="submit"
                className="w-full"
                loading={isSubmitting}
                disabled={retryAfter !== null}
              >
                {retryAfter === null ? 'Sign in' : `Try again in ${retryAfter}s`}
              </Button>

              <p className="text-center text-sm text-muted-foreground">
                Forgotten your password? Contact your system administrator.
              </p>
            </form>
          </CardContent>
        </Card>
      </main>
    </div>
  )
}

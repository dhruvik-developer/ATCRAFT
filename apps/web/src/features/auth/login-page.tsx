import { AlertCircle, Eye, EyeOff, Loader2 } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'

import { prefetchRoute } from '@/app/route-loaders'
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

/**
 * Two required fields and one shape check, written by hand.
 *
 * Deliberately not react-hook-form + zod, which every other form in the app
 * uses. This page is the only one the router loads eagerly, so those three
 * packages sat in the entry bundle - 26 KB gzipped, about 12% of everything an
 * unauthenticated visitor downloaded - to decide whether two boxes were empty.
 * They stay the right tool for a fifty-field dispatch dialog behind a lazy
 * route; they are not worth a sign-in screen's first paint. The MFA form below
 * has always been a plain controlled form for the same reason.
 *
 * The messages are the ones the schema produced, verbatim.
 */
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

interface LoginErrors {
  email?: string
  password?: string
}

function validate(email: string, password: string): LoginErrors {
  const errors: LoginErrors = {}
  if (email.trim() === '') errors.email = 'Email is required'
  else if (!EMAIL_SHAPE.test(email.trim())) errors.email = 'Enter a valid email address'
  // No complexity rules on the *login* form: the policy is enforced when a
  // password is set. Rejecting a legacy password here would lock out a user the
  // API would happily authenticate and then upgrade.
  if (password === '') errors.password = 'Password is required'
  return errors
}

export function LoginPage() {
  const { login, verifyMfa, isAuthenticated, isRestoring } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [showPassword, setShowPassword] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [mfaChallenge, setMfaChallenge] = useState<string | null>(null)
  const [mfaCode, setMfaCode] = useState('')
  /** Seconds still to wait after a 429. Null once the form is usable again. */
  const [retryAfter, setRetryAfter] = useState<number | null>(null)

  // The dashboard is where a successful sign-in lands. Warm its chunk while
  // the user is typing so the first authenticated paint comes from cache.
  useEffect(() => {
    prefetchRoute('/')
  }, [])

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

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [errors, setErrors] = useState<LoginErrors>({})
  const [isSubmitting, setIsSubmitting] = useState(false)

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

  const showFailure = (error: unknown) => {
    if (error instanceof ApiError) {
      if (error.status === 429) {
        const raw = error.details?.retry_after_seconds
        const seconds = typeof raw === 'number' ? raw : Number(raw)
        setRetryAfter(
          Number.isFinite(seconds) && seconds > 0
            ? Math.ceil(seconds)
            : DEFAULT_RETRY_AFTER_SECONDS,
        )
        setFormError(error.message || 'Too many attempts. Wait before trying again.')
      } else {
        setFormError(error.message)
      }
    } else {
      setFormError('Could not reach the server. Check your connection and try again.')
    }
  }

  const finishLogin = (user: Awaited<ReturnType<typeof verifyMfa>>) => {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    navigate(user.must_change_password ? '/change-password' : from, { replace: true })
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const found = validate(email, password)
    setErrors(found)
    // Validating here rather than at the server so an empty submit does not
    // spend one of the account's rate-limited login attempts.
    if (found.email || found.password) return

    setFormError(null)
    setRetryAfter(null)
    setIsSubmitting(true)
    try {
      const outcome = await login(email.trim(), password)
      if ('mfa_required' in outcome) {
        setMfaChallenge(outcome.challenge_token)
        setMfaCode('')
        return
      }
      finishLogin(outcome)
    } catch (error) {
      showFailure(error)
    } finally {
      setIsSubmitting(false)
    }
  }

  const onMfaSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!mfaChallenge || !/^\d{6}$/.test(mfaCode)) return
    setFormError(null)
    setRetryAfter(null)
    try {
      finishLogin(await verifyMfa(mfaChallenge, mfaCode))
    } catch (error) {
      showFailure(error)
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
      <main className="flex items-center justify-center p-4 sm:p-6">
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
              <h1>{mfaChallenge ? 'Authentication code' : 'Sign in'}</h1>
            </CardTitle>
            <CardDescription>
              {mfaChallenge
                ? 'Enter the six-digit code from your authenticator app.'
                : 'Use your Atcraft account to continue.'}
            </CardDescription>
          </CardHeader>

          <CardContent>
            {mfaChallenge ? (
              <form key="mfa" onSubmit={onMfaSubmit} className="space-y-4" noValidate>
                {formError && (
                  <Alert id="mfa-error" variant="destructive">
                    <AlertCircle className="mb-1 text-destructive" />
                    <AlertDescription className="text-foreground">{formError}</AlertDescription>
                  </Alert>
                )}

                <div className="space-y-1.5">
                  <Label htmlFor="mfa-code" required>
                    Authentication code
                  </Label>
                  <Input
                    id="mfa-code"
                    value={mfaCode}
                    onChange={(event) =>
                      setMfaCode(event.target.value.replace(/\D/g, '').slice(0, 6))
                    }
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    pattern="[0-9]{6}"
                    maxLength={6}
                    autoFocus
                    aria-describedby={formError ? 'mfa-error' : undefined}
                  />
                </div>

                <Button
                  type="submit"
                  className="w-full"
                  disabled={mfaCode.length !== 6 || retryAfter !== null}
                >
                  {retryAfter === null ? 'Verify and sign in' : `Try again in ${retryAfter}s`}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  className="w-full"
                  onClick={() => {
                    setMfaChallenge(null)
                    setMfaCode('')
                    setFormError(null)
                  }}
                >
                  Back to password
                </Button>
              </form>
            ) : (
              <form key="credentials" onSubmit={onSubmit} className="space-y-4" noValidate>
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
                    name="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                  />
                  {errors.email && (
                    <p id="email-error" className="text-xs text-destructive">
                      {errors.email}
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
                      name="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
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
                      {errors.password}
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
                  <Link className="underline-offset-4 hover:underline" to="/forgot-password">
                    Forgotten your password?
                  </Link>
                </p>
              </form>
            )}
          </CardContent>
        </Card>
      </main>
    </div>
  )
}

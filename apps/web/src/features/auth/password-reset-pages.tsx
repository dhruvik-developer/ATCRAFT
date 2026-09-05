import { zodResolver } from '@hookform/resolvers/zod'
import { AlertCircle, CheckCircle2 } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
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
import { ApiError, api } from '@/lib/api-client'

import { passwordField } from './password-policy'

const requestSchema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  company_code: z.string().max(32, 'Company code is too long').optional(),
})

const confirmSchema = z
  .object({
    password: passwordField,
    confirmPassword: z.string().min(1, 'Confirm the new password'),
  })
  .refine((values) => values.password === values.confirmPassword, {
    path: ['confirmPassword'],
    message: 'Passwords do not match',
  })

type RequestForm = z.infer<typeof requestSchema>
type ConfirmForm = z.infer<typeof confirmSchema>

function AuthCard({ children }: { children: ReactNode }) {
  return (
    <main className="grid min-h-dvh place-items-center bg-muted/20 p-4 sm:p-6">
      <Card className="w-full max-w-md">{children}</Card>
    </main>
  )
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Could not reach the server. Check your connection and try again.'
}

export function ForgotPasswordPage() {
  const [submitted, setSubmitted] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RequestForm>({
    resolver: zodResolver(requestSchema),
    defaultValues: { email: '', company_code: '' },
  })

  const submit = async (values: RequestForm) => {
    setFormError(null)
    try {
      await api.post(
        '/auth/password-reset/request',
        {
          email: values.email,
          company_code: values.company_code?.trim() || null,
        },
        { anonymous: true },
      )
      setSubmitted(true)
    } catch (error) {
      setFormError(errorMessage(error))
    }
  }

  if (submitted) {
    return (
      <AuthCard>
        <CardHeader>
          <CheckCircle2 className="mb-2 h-8 w-8 text-primary" aria-hidden="true" />
          <CardTitle asChild>
            <h1>Check your email</h1>
          </CardTitle>
          <CardDescription>
            If an account exists for that address, a password reset link has been sent.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild className="w-full">
            <Link to="/login">Back to sign in</Link>
          </Button>
        </CardContent>
      </AuthCard>
    )
  }

  return (
    <AuthCard>
      <CardHeader>
        <CardTitle asChild>
          <h1>Reset your password</h1>
        </CardTitle>
        <CardDescription>We will send a time-limited link to your account email.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit(submit)} className="space-y-4" noValidate>
          {formError && (
            <Alert variant="destructive">
              <AlertCircle className="mb-1 text-destructive" />
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="reset-email" required>
              Email
            </Label>
            <Input
              id="reset-email"
              type="email"
              autoComplete="email"
              autoFocus
              aria-invalid={Boolean(errors.email)}
              {...register('email')}
            />
            {errors.email && <p className="text-xs text-destructive">{errors.email.message}</p>}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="company-code">Company code (if you use more than one)</Label>
            <Input id="company-code" autoComplete="organization" {...register('company_code')} />
            {errors.company_code && (
              <p className="text-xs text-destructive">{errors.company_code.message}</p>
            )}
          </div>
          <Button type="submit" className="w-full" loading={isSubmitting}>
            Send reset link
          </Button>
          <Button asChild type="button" variant="ghost" className="w-full">
            <Link to="/login">Back to sign in</Link>
          </Button>
        </form>
      </CardContent>
    </AuthCard>
  )
}

export function ResetPasswordPage() {
  const [searchParams] = useSearchParams()
  const { hash } = useLocation()
  // The email link carries the token in the fragment
  // (`/reset-password#token=...`): a fragment never leaves the browser, so it
  // reaches neither the server's access log nor a referrer header. The query
  // form is still read for a link issued before the switch.
  const [token] = useState(
    () =>
      new URLSearchParams(hash.replace(/^#/, '')).get('token') ?? searchParams.get('token') ?? '',
  )
  const [completed, setCompleted] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<ConfirmForm>({
    resolver: zodResolver(confirmSchema),
    defaultValues: { password: '', confirmPassword: '' },
  })

  // Remove the live credential from browser history and referrer data as soon
  // as it has been captured in memory.
  useEffect(() => {
    if (token) window.history.replaceState(window.history.state, '', '/reset-password')
  }, [token])

  const submit = async (values: ConfirmForm) => {
    if (!token) return
    setFormError(null)
    try {
      await api.post(
        '/auth/password-reset/confirm',
        { token, new_password: values.password },
        { anonymous: true },
      )
      setCompleted(true)
    } catch (error) {
      setFormError(errorMessage(error))
    }
  }

  if (!token) {
    return (
      <AuthCard>
        <CardHeader>
          <CardTitle asChild>
            <h1>Reset link unavailable</h1>
          </CardTitle>
          <CardDescription>Request a new password reset link to continue.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild className="w-full">
            <Link to="/forgot-password">Request another link</Link>
          </Button>
        </CardContent>
      </AuthCard>
    )
  }

  if (completed) {
    return (
      <AuthCard>
        <CardHeader>
          <CheckCircle2 className="mb-2 h-8 w-8 text-primary" aria-hidden="true" />
          <CardTitle asChild>
            <h1>Password updated</h1>
          </CardTitle>
          <CardDescription>
            All existing sessions were closed. Sign in with your new password.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild className="w-full">
            <Link to="/login">Sign in</Link>
          </Button>
        </CardContent>
      </AuthCard>
    )
  }

  return (
    <AuthCard>
      <CardHeader>
        <CardTitle asChild>
          <h1>Choose a new password</h1>
        </CardTitle>
        <CardDescription>
          Use at least 12 characters with upper and lower case, a number, and a symbol.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit(submit)} className="space-y-4" noValidate>
          {formError && (
            <Alert variant="destructive">
              <AlertCircle className="mb-1 text-destructive" />
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="new-password" required>
              New password
            </Label>
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              autoFocus
              aria-invalid={Boolean(errors.password)}
              {...register('password')}
            />
            {errors.password && (
              <p className="text-xs text-destructive">{errors.password.message}</p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="confirm-password" required>
              Confirm new password
            </Label>
            <Input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              aria-invalid={Boolean(errors.confirmPassword)}
              {...register('confirmPassword')}
            />
            {errors.confirmPassword && (
              <p className="text-xs text-destructive">{errors.confirmPassword.message}</p>
            )}
          </div>
          <Button type="submit" className="w-full" loading={isSubmitting}>
            Update password
          </Button>
        </form>
      </CardContent>
    </AuthCard>
  )
}

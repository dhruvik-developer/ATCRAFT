/**
 * The signed-in user's own account.
 *
 * Every endpoint here is gated on authentication alone, not on a permission:
 * revoking someone's ability to read their own profile, change their own
 * password or end their own session would lock them into a session they cannot
 * get out of. All three operate strictly on the caller's own rows.
 *
 * `?change-password=1` opens this page with the password form focused - the
 * login flow redirects here when the account is flagged `must_change_password`.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { KeyRound, LogOut, Monitor, ShieldCheck, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useSearchParams } from 'react-router-dom'
import { z } from 'zod'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, DescriptionList, FieldError, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
} from '@/components/ui/primitives'
import { api, ApiError } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDateTime, formatRelative } from '@/lib/format'
import { success } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { useAuth } from './auth-context'
import { PASSWORD_MAX_LENGTH, PASSWORD_RULES, passwordField } from './password-policy'

const SESSION_PAGE_SIZE = 25

/**
 * One row of `GET /auth/sessions` (`SessionRead`).
 *
 * The optional fields are omitted by the API rather than nulled, so every read
 * of them has to tolerate `undefined` as well as `null`.
 */
interface SessionRow {
  id: string
  session_id: string
  created_at: string
  expires_at: string
  last_used_at?: string | null
  ip_address?: string | null
  user_agent?: string | null
  device_label?: string | null
  /** True for the session this request was made with. */
  is_current?: boolean
}

interface ChangePasswordResult {
  revoked_sessions: number
}

interface MfaSetupResult {
  secret: string
  provisioning_uri: string
}

interface MfaChangeResult {
  mfa_enabled: boolean
  revoked_sessions: number
}

/**
 * A user agent string is unreadable in a table cell.
 *
 * This is a display convenience only - the full string stays in the row's
 * title attribute, because "Chrome on Windows" is not enough to recognise which
 * of three shop-floor PCs a session belongs to.
 */
function describeDevice(session: SessionRow): string {
  if (session.device_label) return session.device_label
  const agent = session.user_agent ?? ''
  if (!agent) return 'Unknown device'
  const browser = /Edg\//.test(agent)
    ? 'Edge'
    : /OPR\//.test(agent)
      ? 'Opera'
      : /Chrome\//.test(agent)
        ? 'Chrome'
        : /Firefox\//.test(agent)
          ? 'Firefox'
          : /Safari\//.test(agent)
            ? 'Safari'
            : 'Browser'
  const platform = /Windows/.test(agent)
    ? 'Windows'
    : /Android/.test(agent)
      ? 'Android'
      : /iPhone|iPad/.test(agent)
        ? 'iOS'
        : /Mac OS X/.test(agent)
          ? 'macOS'
          : /Linux/.test(agent)
            ? 'Linux'
            : 'unknown platform'
  return `${browser} on ${platform}`
}

// ------------------------------------------------------- change password ----

const changePasswordSchema = z
  .object({
    current_password: z.string().min(1, 'Enter your current password'),
    new_password: passwordField,
    confirm_password: z.string().min(1, 'Repeat the new password'),
  })
  .refine((values) => values.new_password === values.confirm_password, {
    message: 'The two passwords do not match',
    path: ['confirm_password'],
  })
  .refine((values) => values.new_password !== values.current_password, {
    message: 'The new password must differ from the current one',
    path: ['new_password'],
  })

type ChangePasswordValues = z.infer<typeof changePasswordSchema>

export function ChangePasswordCard({ autoFocus }: { autoFocus: boolean }) {
  const { refreshUser } = useAuth()
  const queryClient = useQueryClient()
  const currentRef = useRef<HTMLInputElement | null>(null)

  const {
    register,
    handleSubmit,
    reset,
    watch,
    setError,
    formState: { errors },
  } = useForm<ChangePasswordValues>({
    resolver: zodResolver(changePasswordSchema),
    defaultValues: { current_password: '', new_password: '', confirm_password: '' },
  })

  useEffect(() => {
    if (autoFocus) currentRef.current?.focus()
  }, [autoFocus])

  const newPassword = watch('new_password') ?? ''

  const mutation = useMutation({
    mutationFn: (values: ChangePasswordValues) =>
      api.post<ChangePasswordResult>('/auth/change-password', {
        current_password: values.current_password,
        new_password: values.new_password,
      }),
    onSuccess: async (result) => {
      reset({ current_password: '', new_password: '', confirm_password: '' })
      void queryClient.invalidateQueries({ queryKey: queryKeys.auth.sessions })
      toast.success(
        success.changed('Password'),
        `${result.revoked_sessions} other session(s) were signed out.`,
      )
      // Clears `must_change_password` so the shell stops nagging.
      await refreshUser()
    },
    // The one shared 422 mapping, so this form stays in step with the mutation
    // cache's rule of staying quiet on a 422 that carries field detail. A
    // policy failure keyed on `new_password` renders once, under the control;
    // one that arrives without field detail is announced by the toast alone.
    // The previous copy here read `details.requirements` as well and put the
    // same sentence on the field beside the toast, so the user saw it twice.
    onError: (error) => applyFieldErrors(error, setError),
  })

  const { ref: registerCurrentRef, ...currentField } = register('current_password')

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-muted-foreground" aria-hidden />
          Change password
        </CardTitle>
        <CardDescription>
          Every other device is signed out when the password changes. This one stays signed in.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="max-w-md space-y-4"
          noValidate
        >
          <div>
            <Label htmlFor="current-password" required>
              Current password
            </Label>
            <Input
              id="current-password"
              type="password"
              autoComplete="current-password"
              aria-invalid={Boolean(errors.current_password)}
              className="mt-1.5"
              {...currentField}
              ref={(element) => {
                registerCurrentRef(element)
                currentRef.current = element
              }}
            />
            <FieldError message={errors.current_password?.message} />
          </div>

          <div>
            <Label htmlFor="new-password" required>
              New password
            </Label>
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              maxLength={PASSWORD_MAX_LENGTH}
              aria-invalid={Boolean(errors.new_password)}
              aria-describedby="password-policy"
              className="mt-1.5"
              {...register('new_password')}
            />
            <FieldError message={errors.new_password?.message} />

            <ul id="password-policy" className="mt-2 space-y-1">
              {PASSWORD_RULES.map((rule) => {
                const met = rule.satisfied(newPassword)
                return (
                  <li
                    key={rule.id}
                    className={
                      met
                        ? 'flex items-center gap-2 text-xs text-success'
                        : 'flex items-center gap-2 text-xs text-muted-foreground'
                    }
                  >
                    {met ? (
                      <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
                    ) : (
                      <X className="h-3.5 w-3.5" aria-hidden />
                    )}
                    <span>{rule.label}</span>
                    <span className="sr-only">{met ? 'met' : 'not met yet'}</span>
                  </li>
                )
              })}
            </ul>
          </div>

          <div>
            <Label htmlFor="confirm-password" required>
              Repeat new password
            </Label>
            <Input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              aria-invalid={Boolean(errors.confirm_password)}
              className="mt-1.5"
              {...register('confirm_password')}
            />
            <FieldError message={errors.confirm_password?.message} />
          </div>

          <Button type="submit" loading={mutation.isPending}>
            Change password
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------- two-factor auth ------

function MfaCard() {
  const { user, refreshUser } = useAuth()
  const [currentPassword, setCurrentPassword] = useState('')
  const [code, setCode] = useState('')
  const [setupResult, setSetupResult] = useState<MfaSetupResult | null>(null)

  const setup = useMutation({
    mutationFn: () =>
      api.post<MfaSetupResult>('/auth/mfa/setup', { current_password: currentPassword }),
    onSuccess: (result) => {
      setSetupResult(result)
      setCode('')
    },
  })

  const confirm = useMutation({
    mutationFn: () => api.post<MfaChangeResult>('/auth/mfa/confirm', { code }),
    onSuccess: async (result) => {
      setCurrentPassword('')
      setCode('')
      setSetupResult(null)
      toast.success(
        'Two-factor authentication enabled',
        `${result.revoked_sessions} other session(s) were signed out.`,
      )
      await refreshUser()
    },
  })

  const disable = useMutation({
    mutationFn: () =>
      api.post<MfaChangeResult>('/auth/mfa/disable', {
        current_password: currentPassword,
        code,
      }),
    onSuccess: async (result) => {
      setCurrentPassword('')
      setCode('')
      toast.success(
        'Two-factor authentication disabled',
        `${result.revoked_sessions} other session(s) were signed out.`,
      )
      await refreshUser()
    },
  })

  const activeMutation = user?.mfa_enabled ? disable : setupResult ? confirm : setup
  const error = activeMutation.error
  const errorMessage =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : error
          ? 'The request could not be completed.'
          : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-muted-foreground" aria-hidden />
          Two-factor authentication
        </CardTitle>
        <CardDescription>
          {user?.mfa_enabled
            ? 'A time-based code is required after your password when you sign in.'
            : 'Add an authenticator app code to protect the account if your password is exposed.'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {errorMessage && (
          <Alert variant="destructive" className="mb-4" role="alert">
            <AlertTitle>Two-factor request failed</AlertTitle>
            <AlertDescription>{errorMessage}</AlertDescription>
          </Alert>
        )}

        {!user?.mfa_enabled && !setupResult && (
          <form
            className="max-w-md space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              setup.mutate()
            }}
          >
            <div>
              <Label htmlFor="mfa-setup-password" required>
                Current password for two-factor setup
              </Label>
              <Input
                id="mfa-setup-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                className="mt-1.5"
                required
              />
            </div>
            <Button type="submit" loading={setup.isPending}>
              Set up two-factor
            </Button>
          </form>
        )}

        {!user?.mfa_enabled && setupResult && (
          <form
            className="max-w-xl space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              confirm.mutate()
            }}
          >
            <Alert>
              <AlertTitle>Enter this secret in your authenticator app</AlertTitle>
              <AlertDescription>
                <code className="mt-2 block break-all rounded bg-muted p-2 font-mono text-sm text-foreground">
                  {setupResult.secret}
                </code>
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs">Show provisioning URI</summary>
                  <code className="mt-1 block break-all text-xs">
                    {setupResult.provisioning_uri}
                  </code>
                </details>
              </AlertDescription>
            </Alert>
            <div className="max-w-xs">
              <Label htmlFor="mfa-confirm-code" required>
                Authenticator code
              </Label>
              <Input
                id="mfa-confirm-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={code}
                onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
                className="mt-1.5 font-mono tracking-widest"
                required
              />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="submit" loading={confirm.isPending} disabled={code.length !== 6}>
                Enable two-factor
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setSetupResult(null)
                  setCode('')
                  setup.reset()
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
        )}

        {user?.mfa_enabled && (
          <form
            className="max-w-md space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              disable.mutate()
            }}
          >
            <Alert variant="warning">
              <AlertTitle>Disabling reduces account protection</AlertTitle>
              <AlertDescription>
                Confirm both your password and a current authenticator code. Other sessions will be
                signed out.
              </AlertDescription>
            </Alert>
            <div>
              <Label htmlFor="mfa-disable-password" required>
                Current password for two-factor removal
              </Label>
              <Input
                id="mfa-disable-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label htmlFor="mfa-disable-code" required>
                Current authenticator code
              </Label>
              <Input
                id="mfa-disable-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={code}
                onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
                className="mt-1.5 font-mono tracking-widest"
                required
              />
            </div>
            <Button
              type="submit"
              variant="destructive"
              loading={disable.isPending}
              disabled={code.length !== 6}
            >
              Disable two-factor
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  )
}

// ------------------------------------------------------------- sessions -----

function SessionsCard() {
  const { logout } = useAuth()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [revoking, setRevoking] = useState<SessionRow | null>(null)
  const [signOutAll, setSignOutAll] = useState(false)

  const sessions = useQuery({
    queryKey: [...queryKeys.auth.sessions, page] as const,
    queryFn: () =>
      api.getPage<SessionRow>('/auth/sessions', { params: { page, limit: SESSION_PAGE_SIZE } }),
    placeholderData: keepPreviousData,
    // A session list that is minutes stale defeats the purpose of showing it.
    staleTime: 0,
  })

  const revoke = useMutation({
    mutationFn: (session: SessionRow) => api.delete(`/auth/sessions/${session.id}`),
    onSuccess: (_result, session) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.auth.sessions })
      setRevoking(null)
      if (session.is_current) {
        toast.show('Signed out', 'You revoked the session you are using.')
        void logout()
        return
      }
      toast.success(success.revoked('Session'), describeDevice(session))
    },
  })

  const revokeAll = useMutation({
    mutationFn: () => api.post<{ revoked_sessions: number }>('/auth/logout-all'),
    onSuccess: () => {
      setSignOutAll(false)
      toast.show('Signed out everywhere', 'Every device, including this one, has been signed out.')
      // The current session is revoked too, so the local state must follow or
      // the shell would keep rendering against a dead token.
      void logout()
    },
  })

  const columns: ColumnDef<SessionRow, unknown>[] = [
    {
      id: 'device',
      header: 'Device',
      enableSorting: false,
      cell: ({ row }) => (
        <span className="flex items-center gap-2" title={row.original.user_agent ?? undefined}>
          <Monitor className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <span className="min-w-0">
            <span className="block truncate font-medium">{describeDevice(row.original)}</span>
            {row.original.is_current && <Badge variant="success">This device</Badge>}
          </span>
        </span>
      ),
    },
    {
      id: 'ip_address',
      header: 'IP address',
      enableSorting: false,
      cell: ({ row }) => (
        <span className="font-mono text-xs">{row.original.ip_address ?? 'Unknown'}</span>
      ),
    },
    {
      id: 'last_used_at',
      header: 'Last used',
      enableSorting: false,
      cell: ({ row }) =>
        row.original.last_used_at ? (
          <time
            dateTime={row.original.last_used_at}
            title={formatDateTime(row.original.last_used_at)}
          >
            {formatRelative(row.original.last_used_at)}
          </time>
        ) : (
          <span className="text-muted-foreground">Not since sign-in</span>
        ),
    },
    {
      id: 'expires_at',
      header: 'Expires',
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-muted-foreground">{formatDateTime(row.original.expires_at)}</span>
      ),
    },
    {
      id: 'actions',
      header: '',
      enableSorting: false,
      enableHiding: false,
      size: 96,
      cell: ({ row }) => (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setRevoking(row.original)}
          aria-label={`Revoke the session on ${describeDevice(row.original)}`}
        >
          Revoke
        </Button>
      ),
    },
  ]

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-3 space-y-0">
        <div>
          <CardTitle>Active sessions</CardTitle>
          <CardDescription>
            Every device currently holding a refresh token for this account.
          </CardDescription>
        </div>
        <Button variant="destructive" size="sm" onClick={() => setSignOutAll(true)}>
          <LogOut />
          Sign out everywhere
        </Button>
      </CardHeader>

      <CardContent>
        <DataTable
          columns={columns}
          data={sessions.data?.data ?? []}
          pagination={sessions.data?.pagination}
          isLoading={sessions.isLoading}
          isFetching={sessions.isFetching}
          error={sessions.error as Error | null}
          onPageChange={setPage}
          onRefresh={() => void sessions.refetch()}
          getRowId={(row) => row.id}
          emptyTitle="No active sessions"
          emptyDescription="Nothing is signed in to this account, not even this tab - which usually means the list could not be read."
        />
      </CardContent>

      <ConfirmDialog
        open={Boolean(revoking)}
        onOpenChange={(open) => {
          if (!open) setRevoking(null)
        }}
        title="Revoke this session?"
        description={
          revoking?.is_current
            ? 'This is the session you are using. Revoking it signs you out of this tab immediately.'
            : 'That device is signed out at once and must sign in again.'
        }
        confirmLabel="Revoke"
        destructive
        loading={revoke.isPending}
        onConfirm={() => revoking && revoke.mutate(revoking)}
      />

      <ConfirmDialog
        open={signOutAll}
        onOpenChange={setSignOutAll}
        title="Sign out of every device?"
        description="All sessions are revoked, including this one. Do this if you think someone else has your password."
        confirmLabel="Sign out everywhere"
        destructive
        loading={revokeAll.isPending}
        onConfirm={() => revokeAll.mutate()}
      />
    </Card>
  )
}

// ------------------------------------------------------------------ page ----

export default function ProfilePage() {
  const { user } = useAuth()
  const [searchParams] = useSearchParams()
  const mustChangePassword = Boolean(user?.must_change_password)
  const focusPassword = searchParams.get('change-password') === '1' || mustChangePassword

  return (
    <div>
      <PageHeader
        title="Your account"
        description="Your details, your password and the devices signed in as you."
      />

      {mustChangePassword && (
        <Alert variant="warning" className="mb-5">
          <AlertTitle>Choose a new password</AlertTitle>
          <AlertDescription>
            This account is still using a password somebody else set. Change it before carrying on.
          </AlertDescription>
        </Alert>
      )}

      <div className="space-y-5">
        <Card>
          <CardHeader>
            <CardTitle>{user?.full_name ?? 'Signed-in user'}</CardTitle>
            <CardDescription>{user?.email}</CardDescription>
          </CardHeader>
          <CardContent>
            <DescriptionList
              columns={3}
              items={[
                // `/auth/me` carries `company_id` but no company name, so the
                // company row would read "-" for everybody. The sign-in facts
                // it does carry are what somebody checking their own account
                // came here for anyway.
                {
                  label: 'Last sign-in',
                  value: user?.last_login_at ? (
                    <time dateTime={user.last_login_at} title={formatDateTime(user.last_login_at)}>
                      {formatRelative(user.last_login_at)}
                    </time>
                  ) : (
                    'This is your first session'
                  ),
                },
                {
                  label: 'Two-factor',
                  value: user?.mfa_enabled ? (
                    <Badge variant="success">Enabled</Badge>
                  ) : (
                    <Badge variant="muted">Not enabled</Badge>
                  ),
                },
                {
                  label: 'Roles',
                  value:
                    user && user.roles.length > 0 ? (
                      <span className="flex flex-wrap gap-1">
                        {user.roles.map((role) => (
                          <Badge key={role} variant="secondary">
                            {role}
                          </Badge>
                        ))}
                      </span>
                    ) : (
                      'No roles'
                    ),
                },
                {
                  label: 'Account type',
                  value: user?.vendor_id ? (
                    <Badge variant="muted">Vendor portal</Badge>
                  ) : user?.is_superuser ? (
                    <Badge variant="warning">Platform administrator</Badge>
                  ) : (
                    'Internal staff'
                  ),
                },
                {
                  label: 'Effective permissions',
                  value: `${user?.permissions.length ?? 0} granted`,
                },
              ]}
            />
            <p className="mt-4 text-xs text-muted-foreground">
              Permissions are resolved by the API on every request, so a change an administrator
              makes takes effect immediately rather than at your next sign-in.
            </p>
          </CardContent>
        </Card>

        <ChangePasswordCard autoFocus={focusPassword} />

        <MfaCard />

        <SessionsCard />
      </div>
    </div>
  )
}

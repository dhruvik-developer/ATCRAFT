import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Copy, Pencil, Plus, RefreshCw, RotateCw } from 'lucide-react'
import { useState } from 'react'

import { PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Checkbox,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDateTime, humanise } from '@/lib/format'
import { reportFieldErrors } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

type CourierProvider = 'SHIPROCKET' | 'GENERIC_WEBHOOK'

interface CourierAccount {
  id: string
  code: string
  provider: CourierProvider
  display_name: string
  is_active: boolean
  auto_confirm_delivery: boolean
  has_credentials: boolean
  has_webhook: boolean
  last_poll_at: string | null
  last_error: string | null
}

interface AccountDraft {
  code: string
  provider: CourierProvider
  display_name: string
  email: string
  password: string
  auto_confirm_delivery: boolean
  is_active: boolean
}

interface EditDraft {
  display_name: string
  email: string
  password: string
  auto_confirm_delivery: boolean
  is_active: boolean
}

interface RotatedWebhook {
  webhook_url: string
  secret: string
  header_name: string
}

const CREATE_FAILED = 'Could not create the courier account'
const UPDATE_FAILED = 'Could not update the courier account'

const EMPTY_DRAFT: AccountDraft = {
  code: '',
  provider: 'SHIPROCKET',
  display_name: '',
  email: '',
  password: '',
  auto_confirm_delivery: true,
  is_active: true,
}

export default function CourierIntegrationPage() {
  const { can } = useAuth()
  const canManage = can(P.SETTINGS_MANAGE)
  const client = useQueryClient()
  const [draft, setDraft] = useState<AccountDraft>(EMPTY_DRAFT)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null)
  const [rotated, setRotated] = useState<RotatedWebhook | null>(null)

  const accounts = useListQuery<CourierAccount>({
    path: '/integrations/courier/accounts',
    queryKey: queryKeys.courierTracking.accounts,
    staleTime: 30_000,
  })
  const accountPagination = accounts.pagination

  const refreshAccounts = () =>
    client.invalidateQueries({ queryKey: queryKeys.courierTracking.all })

  const create = useMutation({
    mutationFn: () =>
      api.post<CourierAccount>('/integrations/courier/accounts', {
        code: draft.code,
        provider: draft.provider,
        display_name: draft.display_name,
        ...(draft.provider === 'SHIPROCKET'
          ? { credentials: { email: draft.email, password: draft.password } }
          : {}),
        auto_confirm_delivery: draft.auto_confirm_delivery,
        is_active: draft.is_active,
      }),
    // The global net says every other refusal under this title. The account
    // fields are plain inputs rather than form fields, so a 422 is named here.
    meta: { errorTitle: CREATE_FAILED },
    onError: (error) => reportFieldErrors(error, CREATE_FAILED),
    onSuccess: async () => {
      await refreshAccounts()
      setDraft(EMPTY_DRAFT)
      toast.success(success.created('Courier account'))
    },
  })

  const update = useMutation({
    mutationFn: ({ id, values }: { id: string; values: EditDraft }) =>
      api.patch<CourierAccount>(`/integrations/courier/accounts/${id}`, {
        display_name: values.display_name,
        is_active: values.is_active,
        auto_confirm_delivery: values.auto_confirm_delivery,
        ...(values.email && values.password
          ? { credentials: { email: values.email, password: values.password } }
          : {}),
      }),
    meta: { errorTitle: UPDATE_FAILED },
    onError: (error) => reportFieldErrors(error, UPDATE_FAILED),
    onSuccess: async () => {
      await refreshAccounts()
      setEditingId(null)
      setEditDraft(null)
      toast.success(success.updated('Courier account'))
    },
  })

  const test = useMutation({
    mutationFn: (id: string) =>
      api.post<{ connected: boolean; provider: CourierProvider }>(
        `/integrations/courier/accounts/${id}/test`,
      ),
    meta: { errorTitle: 'Could not test the courier connection' },
    onSuccess: (result) => toast.success(`${humanise(result.provider)} connection succeeded`),
  })

  const rotate = useMutation({
    mutationFn: (id: string) =>
      api.post<RotatedWebhook>(`/integrations/courier/accounts/${id}/rotate-webhook`),
    meta: { errorTitle: 'Could not rotate the webhook secret' },
    onSuccess: (result) => setRotated(result),
  })

  const startEditing = (account: CourierAccount) => {
    setEditingId(account.id)
    setEditDraft({
      display_name: account.display_name,
      email: '',
      password: '',
      auto_confirm_delivery: account.auto_confirm_delivery,
      is_active: account.is_active,
    })
  }

  const copy = async (label: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      toast.success(`${label} copied`)
    } catch (error) {
      toast.error(`Could not copy ${label.toLowerCase()}`, describeError(error))
    }
  }

  return (
    <>
      <PageHeader
        title="Courier integration"
        description="Configure Shiprocket polling and signed courier webhooks. Credentials are write-only and encrypted before storage."
        breadcrumbs={[{ label: 'Administration' }, { label: 'Courier integration' }]}
      />

      {accounts.error && (
        <Alert variant="destructive" className="mb-5">
          <AlertTitle>Courier accounts could not be loaded</AlertTitle>
          <AlertDescription>{describeError(accounts.error)}</AlertDescription>
        </Alert>
      )}

      {canManage && (
        <Card className="mb-5">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Plus aria-hidden />
              Add courier account
            </CardTitle>
            <CardDescription>
              Shiprocket credentials are sent only when creating or replacing them; they are never
              returned by the API.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form
              className="space-y-4"
              onSubmit={(event) => {
                event.preventDefault()
                create.mutate()
              }}
            >
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <Label htmlFor="courier-code">Account code</Label>
                  <Input
                    id="courier-code"
                    value={draft.code}
                    maxLength={32}
                    required
                    onChange={(event) => setDraft({ ...draft, code: event.target.value })}
                  />
                </div>
                <div>
                  <Label htmlFor="courier-provider">Provider</Label>
                  <select
                    id="courier-provider"
                    className="mt-1.5 h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={draft.provider}
                    onChange={(event) =>
                      setDraft({ ...draft, provider: event.target.value as CourierProvider })
                    }
                  >
                    <option value="SHIPROCKET">Shiprocket</option>
                    <option value="GENERIC_WEBHOOK">Generic webhook</option>
                  </select>
                </div>
                <div>
                  <Label htmlFor="courier-name">Display name</Label>
                  <Input
                    id="courier-name"
                    value={draft.display_name}
                    maxLength={120}
                    required
                    onChange={(event) => setDraft({ ...draft, display_name: event.target.value })}
                  />
                </div>
                {draft.provider === 'SHIPROCKET' && (
                  <>
                    <div>
                      <Label htmlFor="courier-email">Shiprocket email</Label>
                      <Input
                        id="courier-email"
                        type="email"
                        autoComplete="off"
                        value={draft.email}
                        required
                        onChange={(event) => setDraft({ ...draft, email: event.target.value })}
                      />
                    </div>
                    <div>
                      <Label htmlFor="courier-password">Shiprocket password</Label>
                      <Input
                        id="courier-password"
                        type="password"
                        autoComplete="new-password"
                        value={draft.password}
                        required
                        onChange={(event) => setDraft({ ...draft, password: event.target.value })}
                      />
                    </div>
                  </>
                )}
              </div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={draft.auto_confirm_delivery}
                  onCheckedChange={(checked) =>
                    setDraft({ ...draft, auto_confirm_delivery: checked === true })
                  }
                />
                Automatically confirm outbound delivery
              </label>
              <Button type="submit" disabled={!canManage} loading={create.isPending}>
                <Plus />
                Create account
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      {!canManage && (
        <p className="mb-5 text-sm text-muted-foreground">
          You have read-only access. Changing courier settings requires <code>settings.manage</code>
          .
        </p>
      )}

      <section aria-label="Courier accounts" className="space-y-4">
        <h2 className="text-lg font-semibold">Accounts</h2>
        {accounts.isLoading && <p role="status">Loading courier accounts…</p>}
        {!accounts.isLoading && accounts.data.length === 0 && (
          <p className="rounded-lg border p-6 text-sm text-muted-foreground">
            No courier accounts configured.
          </p>
        )}
        {accounts.data.map((account) => (
          <Card key={account.id}>
            <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
              <div>
                <CardTitle>{account.display_name}</CardTitle>
                <CardDescription>
                  {account.code} · {humanise(account.provider)} ·{' '}
                  {account.is_active ? 'Active' : 'Inactive'}
                </CardDescription>
              </div>
              {canManage && (
                <div className="flex flex-wrap justify-end gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={test.isPending}
                    onClick={() => test.mutate(account.id)}
                  >
                    <RefreshCw />
                    Test connection
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={rotate.isPending}
                    onClick={() => rotate.mutate(account.id)}
                  >
                    <RotateCw />
                    Rotate webhook
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => startEditing(account)}>
                    <Pencil />
                    Edit
                  </Button>
                </div>
              )}
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-sm">
                {account.provider === 'SHIPROCKET'
                  ? account.has_credentials
                    ? 'API credentials configured'
                    : 'API credentials not configured'
                  : account.has_webhook
                    ? 'Webhook credentials configured'
                    : 'Webhook credentials not configured'}
              </p>
              <p className="text-sm">
                Auto-confirm delivery: {account.auto_confirm_delivery ? 'On' : 'Off'}
              </p>
              <p className="text-sm text-muted-foreground">
                Last poll: {formatDateTime(account.last_poll_at)}
              </p>
              {account.last_error && (
                <Alert variant="warning">
                  <AlertTitle>Last poll failed</AlertTitle>
                  <AlertDescription>{account.last_error}</AlertDescription>
                </Alert>
              )}
              {editingId === account.id && editDraft && (
                <form
                  className="space-y-4 border-t pt-4"
                  onSubmit={(event) => {
                    event.preventDefault()
                    if (Boolean(editDraft.email) !== Boolean(editDraft.password)) {
                      toast.error('Enter both credential fields to replace the saved credentials')
                      return
                    }
                    update.mutate({ id: account.id, values: editDraft })
                  }}
                >
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <Label htmlFor={`edit-name-${account.id}`}>Display name</Label>
                      <Input
                        id={`edit-name-${account.id}`}
                        value={editDraft.display_name}
                        required
                        maxLength={120}
                        onChange={(event) =>
                          setEditDraft({ ...editDraft, display_name: event.target.value })
                        }
                      />
                    </div>
                    {account.provider === 'SHIPROCKET' && (
                      <>
                        <div>
                          <Label htmlFor={`edit-email-${account.id}`}>New Shiprocket email</Label>
                          <Input
                            id={`edit-email-${account.id}`}
                            type="email"
                            autoComplete="off"
                            value={editDraft.email}
                            onChange={(event) =>
                              setEditDraft({ ...editDraft, email: event.target.value })
                            }
                          />
                        </div>
                        <div>
                          <Label htmlFor={`edit-password-${account.id}`}>
                            New Shiprocket password
                          </Label>
                          <Input
                            id={`edit-password-${account.id}`}
                            type="password"
                            autoComplete="new-password"
                            value={editDraft.password}
                            onChange={(event) =>
                              setEditDraft({ ...editDraft, password: event.target.value })
                            }
                          />
                        </div>
                      </>
                    )}
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={editDraft.is_active}
                      onCheckedChange={(checked) =>
                        setEditDraft({ ...editDraft, is_active: checked === true })
                      }
                    />
                    Active
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={editDraft.auto_confirm_delivery}
                      onCheckedChange={(checked) =>
                        setEditDraft({ ...editDraft, auto_confirm_delivery: checked === true })
                      }
                    />
                    Automatically confirm outbound delivery
                  </label>
                  <div className="flex gap-2">
                    <Button type="submit" loading={update.isPending}>
                      Save changes
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => {
                        setEditingId(null)
                        setEditDraft(null)
                      }}
                    >
                      Cancel
                    </Button>
                  </div>
                </form>
              )}
            </CardContent>
          </Card>
        ))}
        {accountPagination && (
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              Page {accountPagination.page}
              {accountPagination.total_pages ? ` of ${accountPagination.total_pages}` : ''}
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={!accountPagination.has_previous}
                onClick={() => accounts.setPage(accountPagination.page - 1)}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                disabled={!accountPagination.has_next}
                onClick={() => accounts.setPage(accountPagination.page + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </section>

      {rotated && (
        <Card className="mt-5">
          <CardHeader>
            <CardTitle>Webhook secret — copy it now</CardTitle>
            <CardDescription>
              The secret is shown once. Configure the provider to sign requests with the named
              header; rotating it invalidates the previous credentials.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="break-all text-sm">
              <strong>URL:</strong> {rotated.webhook_url}
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void copy('Webhook URL', rotated.webhook_url)}
            >
              <Copy />
              Copy URL
            </Button>
            <p className="break-all text-sm">
              <strong>{rotated.header_name}:</strong> {rotated.secret}
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void copy('Webhook secret', rotated.secret)}
            >
              <Copy />
              Copy secret
            </Button>
            <Button variant="outline" onClick={() => setRotated(null)}>
              Dismiss secret
            </Button>
          </CardContent>
        </Card>
      )}
    </>
  )
}

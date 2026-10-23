import { describeError } from '@/lib/messages'
/**
 * Company settings that change how other screens behave.
 *
 * The first card is WP E1b's sales automation switch. The rule it controls runs
 * inside the confirmation transaction, so the page explains what it will raise
 * and - just as important - what it will not: drafts are the planner's to
 * review, and turning the switch off never withdraws supply that already exists.
 *
 * The stored value is nullable on purpose. NULL means "no company override" and
 * reads as the code default (on); an explicit false is what an administrator
 * chose. The page shows which of the two is in force, because "off" and "never
 * decided" are different answers to an audit question.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw, Save } from 'lucide-react'
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
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import type { SalesSettings } from '@/features/sales/sales-types'
import { api } from '@/lib/api-client'
import { reportFieldErrors } from '@/lib/form-errors'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

export default function SettingsPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const canManage = can(P.SETTINGS_MANAGE)
  const [draft, setDraft] = useState<boolean | null>(null)

  const settings = useQuery({
    queryKey: queryKeys.sales.settings,
    queryFn: () => api.get<SalesSettings>('/sales/settings'),
    staleTime: staleTimes.master,
  })

  const save = useMutation({
    mutationFn: (value: boolean | null) =>
      api.put<SalesSettings>('/sales/settings', { auto_plan_on_confirm: value }),
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.sales.settings, saved)
      setDraft(null)
      toast.success(
        saved.is_default
          ? 'Built-in default restored'
          : saved.auto_plan_on_confirm
            ? 'Automatic shortfall planning is on'
            : 'Automatic shortfall planning is off',
        saved.is_default
          ? 'No company override is stored; the code default (on) is in force.'
          : 'The change applies to the next order confirmed by this company.',
      )
    },
    onError: (error) => reportFieldErrors(error, 'Could not save the setting'),
  })

  const value = draft ?? settings.data?.auto_plan_on_confirm ?? true
  const dirty = draft !== null && draft !== settings.data?.auto_plan_on_confirm

  return (
    <>
      <PageHeader
        title="Settings"
        description="Company-wide defaults. Each one changes what another screen does, so the API re-checks the value on the request that uses it."
        breadcrumbs={[{ label: 'Administration' }, { label: 'Settings' }]}
      />

      <div className="max-w-3xl space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Sales automation</CardTitle>
            <CardDescription>
              What happens after stock has been reserved for a newly confirmed order.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {settings.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not read the settings</AlertTitle>
                <AlertDescription>{describeError(settings.error)}</AlertDescription>
              </Alert>
            )}

            <div className="flex items-start gap-3">
              <Checkbox
                id="auto-plan-on-confirm"
                checked={value}
                disabled={!canManage || settings.isLoading}
                onCheckedChange={(checked) => setDraft(checked === true)}
                aria-describedby="auto-plan-description"
              />
              <div className="space-y-1">
                <Label htmlFor="auto-plan-on-confirm" className="font-medium">
                  Plan the shortfall automatically when an order is confirmed
                </Label>
                <p id="auto-plan-description" className="max-w-2xl text-sm text-muted-foreground">
                  When on, confirming an order with a shortfall raises draft supply in the same
                  transaction: one draft production order for a made part, or one draft purchase
                  requisition for the bought parts. Job-work items are reported to the planner but
                  no document is raised for them. Nothing is released or ordered - a planner reviews
                  and submits every draft.
                </p>
              </div>
            </div>

            {settings.data && (
              <p className="text-xs text-muted-foreground">
                {settings.data.is_default
                  ? 'No company override is stored, so the built-in default (on) is in force.'
                  : `This company stores its own value (${
                      settings.data.auto_plan_on_confirm ? 'on' : 'off'
                    }).`}
              </p>
            )}

            {!canManage && (
              <p className="text-sm text-muted-foreground">
                Reading settings needs <code>settings.view</code>; changing them needs{' '}
                <code>settings.manage</code>.
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() => save.mutate(value)}
                disabled={!canManage || !dirty}
                loading={save.isPending}
              >
                <Save />
                Save
              </Button>
              {settings.data && !settings.data.is_default && (
                <Button
                  variant="outline"
                  onClick={() => save.mutate(null)}
                  disabled={!canManage}
                  loading={save.isPending}
                >
                  <RotateCcw />
                  Restore default
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  )
}

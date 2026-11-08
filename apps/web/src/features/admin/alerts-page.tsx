import { describeError } from '@/lib/messages'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellRing, Save } from 'lucide-react'
import { useState } from 'react'

import { PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
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
import { api } from '@/lib/api-client'
import { reportFieldErrors } from '@/lib/form-errors'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

type AlertRule = SchemaOf<'NotificationAlertRuleRead'>
type AlertRuleUpdate = SchemaOf<'NotificationAlertRuleUpdate'>
type DurationUnit = 'minutes' | 'hours' | 'days'
type DurationDraft = { value: string; unit: DurationUnit }
type AlertRuleDraft = {
  is_enabled: boolean
  threshold: DurationDraft
  reminder: DurationDraft
}

const DURATION_FACTORS: Record<DurationUnit, number> = {
  minutes: 1,
  hours: 60,
  days: 1440,
}

const DURATION_UNITS: readonly { value: DurationUnit; label: string }[] = [
  { value: 'minutes', label: 'minutes' },
  { value: 'hours', label: 'hours' },
  { value: 'days', label: 'days' },
]

function durationFromMinutes(minutes: number): DurationDraft {
  if (minutes > 0 && minutes % DURATION_FACTORS.days === 0) {
    return { value: String(minutes / DURATION_FACTORS.days), unit: 'days' }
  }
  if (minutes > 0 && minutes % DURATION_FACTORS.hours === 0) {
    return { value: String(minutes / DURATION_FACTORS.hours), unit: 'hours' }
  }
  return { value: String(minutes), unit: 'minutes' }
}

/** The server's cap on both durations: `MAX_ALERT_WINDOW_MINUTES` (docs/assumptions.md §B4-2). */
const MAX_ALERT_WINDOW_DAYS = 3650
const MAX_ALERT_WINDOW_MINUTES = MAX_ALERT_WINDOW_DAYS * DURATION_FACTORS.days

function minutesFromDuration(duration: DurationDraft): number | null {
  if (duration.value.trim() === '') return null
  const parsed = Number(duration.value)
  const minutes = parsed * DURATION_FACTORS[duration.unit]
  return Number.isSafeInteger(minutes) && minutes <= MAX_ALERT_WINDOW_MINUTES ? minutes : null
}

function draftFromRule(rule: AlertRule): AlertRuleDraft {
  return {
    is_enabled: rule.is_enabled,
    threshold: durationFromMinutes(rule.threshold_minutes),
    reminder: durationFromMinutes(rule.remind_every_minutes),
  }
}

function formatDuration(minutes: number): string {
  if (minutes === 0) return 'immediately'
  if (minutes % 1440 === 0) return `${minutes / 1440} day${minutes === 1440 ? '' : 's'}`
  if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? '' : 's'}`
  return `${minutes} minutes`
}

export default function AlertsPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const canManage = can(P.SETTINGS_MANAGE)
  const [drafts, setDrafts] = useState<Record<string, AlertRuleDraft>>({})

  const rules = useQuery({
    queryKey: queryKeys.notifications.alertRules,
    queryFn: () => api.get<AlertRule[]>('/notifications/alert-rules'),
    staleTime: staleTimes.master,
  })

  const changedRules = (rules.data ?? []).flatMap((rule): AlertRuleUpdate[] => {
    const draft = drafts[rule.key]
    if (!draft) return []
    const threshold = minutesFromDuration(draft.threshold)
    const reminder = minutesFromDuration(draft.reminder)
    if (
      threshold === null ||
      threshold < 0 ||
      reminder === null ||
      reminder < 1 ||
      (draft.is_enabled === rule.is_enabled &&
        threshold === rule.threshold_minutes &&
        reminder === rule.remind_every_minutes)
    ) {
      return []
    }
    return [
      {
        key: rule.key,
        is_enabled: draft.is_enabled,
        threshold_minutes: threshold,
        remind_every_minutes: reminder,
      },
    ]
  })
  const hasInvalidDraft = Object.values(drafts).some((draft) => {
    const threshold = minutesFromDuration(draft.threshold)
    const reminder = minutesFromDuration(draft.reminder)
    return threshold === null || threshold < 0 || reminder === null || reminder < 1
  })

  const save = useMutation({
    mutationFn: (payload: SchemaOf<'NotificationAlertRulesUpdate'>) =>
      api.put<AlertRule[]>('/notifications/alert-rules', payload),
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.notifications.alertRules, saved)
      setDrafts({})
      toast.success('Alert rules saved', 'The updated thresholds apply to the next alert scan.')
    },
    onError: (error) => reportFieldErrors(error, 'Could not save alert rules'),
  })

  function updateDraft(rule: AlertRule, change: Partial<AlertRuleDraft>) {
    setDrafts((current) => ({
      ...current,
      [rule.key]: { ...draftFromRule(rule), ...current[rule.key], ...change },
    }))
  }

  function updateDurationUnit(
    rule: AlertRule,
    field: 'threshold' | 'reminder',
    unit: DurationUnit,
  ) {
    const current = { ...draftFromRule(rule), ...drafts[rule.key] }
    const duration = current[field]
    const minutes = minutesFromDuration(duration)
    updateDraft(rule, {
      [field]: {
        unit,
        value: minutes === null ? duration.value : String(minutes / DURATION_FACTORS[unit]),
      },
    })
  }

  return (
    <>
      <PageHeader
        title="Smart alerts"
        description="Configure the company-wide conditions and reminder cadence for operational alerts."
        breadcrumbs={[{ label: 'Administration' }, { label: 'Smart alerts' }]}
      />

      <div className="max-w-5xl space-y-4">
        {rules.error && (
          <Alert variant="destructive">
            <AlertTitle>Could not read alert rules</AlertTitle>
            <AlertDescription>{describeError(rules.error)}</AlertDescription>
          </Alert>
        )}

        {!canManage && (
          <Alert>
            <BellRing />
            <AlertTitle>Read-only access</AlertTitle>
            <AlertDescription>
              Reading alert rules needs <code>settings.view</code>; changing them needs{' '}
              <code>settings.manage</code>.
            </AlertDescription>
          </Alert>
        )}

        {rules.data?.map((rule) => {
          const draft = { ...draftFromRule(rule), ...drafts[rule.key] }
          const threshold = minutesFromDuration(draft.threshold)
          const reminder = minutesFromDuration(draft.reminder)
          const thresholdValid = threshold !== null && threshold >= 0
          const reminderValid = reminder !== null && reminder >= 1

          return (
            <Card key={rule.key}>
              <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1">
                    <CardTitle>{rule.name}</CardTitle>
                    <CardDescription>{rule.description}</CardDescription>
                  </div>
                  <span className="rounded-full border px-2.5 py-1 text-xs text-muted-foreground">
                    {rule.severity.toLowerCase()} · {rule.granularity.toLowerCase()}
                  </span>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex items-start gap-3">
                  <Checkbox
                    id={`alert-enabled-${rule.key}`}
                    checked={draft.is_enabled}
                    disabled={!canManage || rules.isLoading}
                    onCheckedChange={(checked) =>
                      updateDraft(rule, { is_enabled: checked === true })
                    }
                  />
                  <Label htmlFor={`alert-enabled-${rule.key}`} className="font-medium">
                    Enable this alert
                  </Label>
                </div>

                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor={`alert-threshold-${rule.key}`}>Alert after</Label>
                    <div className="flex gap-2">
                      <Input
                        id={`alert-threshold-${rule.key}`}
                        // The visible words first, so "click Alert after" works
                        // by voice; the rule's name tells the cards apart.
                        aria-label={`Alert after, ${rule.name}`}
                        aria-invalid={!thresholdValid}
                        type="number"
                        min={0}
                        step="any"
                        value={draft.threshold.value}
                        disabled={!canManage || rules.isLoading}
                        onChange={(event) =>
                          updateDraft(rule, {
                            threshold: { ...draft.threshold, value: event.currentTarget.value },
                          })
                        }
                      />
                      <Select
                        value={draft.threshold.unit}
                        onValueChange={(unit) => {
                          const option = DURATION_UNITS.find((entry) => entry.value === unit)
                          if (option) updateDurationUnit(rule, 'threshold', option.value)
                        }}
                        disabled={!canManage || rules.isLoading}
                      >
                        <SelectTrigger
                          aria-label={`${rule.name} alert threshold unit`}
                          className="w-32 shrink-0"
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {DURATION_UNITS.map((unit) => (
                            <SelectItem key={unit.value} value={unit.value}>
                              {unit.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Current setting: {formatDuration(rule.threshold_minutes)}
                    </p>
                    {!thresholdValid && (
                      <p className="text-sm text-destructive">
                        {`Enter a duration that resolves to whole minutes, up to ${MAX_ALERT_WINDOW_DAYS} days.`}
                      </p>
                    )}
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor={`alert-reminder-${rule.key}`}>Repeat reminder every</Label>
                    <div className="flex gap-2">
                      <Input
                        id={`alert-reminder-${rule.key}`}
                        aria-label={`Repeat reminder every, ${rule.name}`}
                        aria-invalid={!reminderValid}
                        type="number"
                        min={0}
                        step="any"
                        value={draft.reminder.value}
                        disabled={!canManage || rules.isLoading}
                        onChange={(event) =>
                          updateDraft(rule, {
                            reminder: { ...draft.reminder, value: event.currentTarget.value },
                          })
                        }
                      />
                      <Select
                        value={draft.reminder.unit}
                        onValueChange={(unit) => {
                          const option = DURATION_UNITS.find((entry) => entry.value === unit)
                          if (option) updateDurationUnit(rule, 'reminder', option.value)
                        }}
                        disabled={!canManage || rules.isLoading}
                      >
                        <SelectTrigger
                          aria-label={`${rule.name} reminder interval unit`}
                          className="w-32 shrink-0"
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {DURATION_UNITS.map((unit) => (
                            <SelectItem key={unit.value} value={unit.value}>
                              {unit.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Current setting: every {formatDuration(rule.remind_every_minutes)}
                    </p>
                    {!reminderValid && (
                      <p className="text-sm text-destructive">
                        {`Enter a duration of at least one whole minute, up to ${MAX_ALERT_WINDOW_DAYS} days.`}
                      </p>
                    )}
                  </div>
                </div>

                <p className="text-xs text-muted-foreground">
                  {rule.is_customized
                    ? 'A company-specific rule is saved.'
                    : 'Using the built-in default.'}
                </p>
              </CardContent>
            </Card>
          )
        })}

        {rules.data && (
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => save.mutate({ rules: changedRules })}
              disabled={
                !canManage || hasInvalidDraft || changedRules.length === 0 || save.isPending
              }
              loading={save.isPending}
            >
              <Save />
              Save changes{changedRules.length > 0 ? ` (${changedRules.length})` : ''}
            </Button>
            {changedRules.length > 0 && (
              <p className="text-sm text-muted-foreground">Only changed rules will be submitted.</p>
            )}
          </div>
        )}
      </div>
    </>
  )
}

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
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import type { SalesSettings } from '@/features/sales/sales-types'
import { api } from '@/lib/api-client'
import { reportFieldErrors } from '@/lib/form-errors'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

type ApprovalSettings = {
  // The API serialises Decimal as a JSON string; converting through Number
  // would round amounts a company may legitimately have typed to four places.
  po_dual_approval_amount: string
  adjustment_approval_threshold: string
  internal_issue_approval_threshold: string
  requisition_auto_approve_limit: string
  is_default: boolean
}

type ApprovalDraft = {
  po_dual_approval_amount: string
  adjustment_approval_threshold: string
  internal_issue_approval_threshold: string
  requisition_auto_approve_limit: string
}

const approvalFields = [
  ['po_dual_approval_amount', 'PO dual approval amount'],
  ['adjustment_approval_threshold', 'Stock adjustment approval threshold'],
  ['internal_issue_approval_threshold', 'Internal issue approval threshold'],
  ['requisition_auto_approve_limit', 'Requisition auto-approve limit'],
] as const

const approvalDefaults: ApprovalDraft = {
  po_dual_approval_amount: '25000.00',
  adjustment_approval_threshold: '25000.00',
  internal_issue_approval_threshold: '25000.00',
  requisition_auto_approve_limit: '0.00',
}

/** Trim numeric trailing zeros without ever passing the value through Number. */
function decimalText(value: string): string {
  if (!value.includes('.')) return value
  return value.replace(/0+$/, '').replace(/\.$/, '')
}

function toApprovalDraft(settings?: ApprovalSettings | null): ApprovalDraft {
  return {
    po_dual_approval_amount: decimalText(
      settings?.po_dual_approval_amount ?? approvalDefaults.po_dual_approval_amount,
    ),
    adjustment_approval_threshold: decimalText(
      settings?.adjustment_approval_threshold ?? approvalDefaults.adjustment_approval_threshold,
    ),
    internal_issue_approval_threshold: decimalText(
      settings?.internal_issue_approval_threshold ??
        approvalDefaults.internal_issue_approval_threshold,
    ),
    requisition_auto_approve_limit: decimalText(
      settings?.requisition_auto_approve_limit ?? approvalDefaults.requisition_auto_approve_limit,
    ),
  }
}

//: The database stores NUMERIC(18,4): at most four decimal places. The bound is
//: repeated here so an obvious typo is caught before a round trip; the API is
//: still the authority for the digit count and for the non-negative rule.
const approvalAmountPattern = /^\d+(?:\.\d{1,4})?$/

function invalidApprovalField(values: ApprovalDraft): string | null {
  for (const [key, label] of approvalFields) {
    const trimmed = values[key].trim()
    if (trimmed !== '' && !approvalAmountPattern.test(trimmed)) return label
  }
  return null
}

function approvalPayload(values: ApprovalDraft, restoreDefault: boolean) {
  if (restoreDefault) {
    return {
      po_dual_approval_amount: null,
      adjustment_approval_threshold: null,
      internal_issue_approval_threshold: null,
      requisition_auto_approve_limit: null,
    }
  }

  // A blank field is "use the built-in default for this one", which is what the
  // API's explicit null means. Zero is a real threshold the administrator may
  // choose, so an empty box must never be turned into one.
  const asDecimal = (value: string) => {
    const trimmed = value.trim()
    return trimmed === '' ? null : trimmed
  }

  return {
    po_dual_approval_amount: asDecimal(values.po_dual_approval_amount),
    adjustment_approval_threshold: asDecimal(values.adjustment_approval_threshold),
    internal_issue_approval_threshold: asDecimal(values.internal_issue_approval_threshold),
    requisition_auto_approve_limit: asDecimal(values.requisition_auto_approve_limit),
  }
}

export default function SettingsPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const canManage = can(P.SETTINGS_MANAGE)
  const [draft, setDraft] = useState<boolean | null>(null)
  const [approvalDraft, setApprovalDraft] = useState<ApprovalDraft | null>(null)

  const settings = useQuery({
    queryKey: queryKeys.sales.settings,
    queryFn: () => api.get<SalesSettings>('/sales/settings'),
    staleTime: staleTimes.master,
  })

  const approvalSettings = useQuery({
    queryKey: queryKeys.audit.approvalSettings,
    queryFn: () => api.get<ApprovalSettings>('/audit/approval-settings'),
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

  const saveApproval = useMutation({
    mutationFn: ({ values, restoreDefault }: { values: ApprovalDraft; restoreDefault: boolean }) =>
      api.put<ApprovalSettings>(
        '/audit/approval-settings',
        approvalPayload(values, restoreDefault),
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.audit.approvalSettings, saved)
      setApprovalDraft(null)
      toast.success(
        saved.is_default ? 'Approval defaults restored' : 'Approval thresholds updated',
        saved.is_default
          ? 'This company no longer overrides the built-in approval thresholds.'
          : 'The values apply to the next approval-gated action in this company.',
      )
    },
    onError: (error) => reportFieldErrors(error, 'Could not save the approval thresholds'),
  })

  const value = draft ?? settings.data?.auto_plan_on_confirm ?? true
  const dirty = draft !== null && draft !== settings.data?.auto_plan_on_confirm
  const approvalValue = approvalDraft ?? toApprovalDraft(approvalSettings.data)
  const approvalError = invalidApprovalField(approvalValue)
  const approvalDirty =
    approvalDraft !== null &&
    Object.entries(approvalValue).some(
      ([key, value]) =>
        value !== toApprovalDraft(approvalSettings.data)[key as keyof ApprovalDraft],
    )

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

        <Card>
          <CardHeader>
            <CardTitle>Approval thresholds</CardTitle>
            <CardDescription>
              Company overrides for approval-gated actions in procurement and stock control.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {approvalSettings.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not read approval thresholds</AlertTitle>
                <AlertDescription>{describeError(approvalSettings.error)}</AlertDescription>
              </Alert>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              {approvalFields.map(([key, label]) => {
                const fieldKey = key as keyof ApprovalDraft

                return (
                  <div key={key} className="space-y-2">
                    <Label htmlFor={key}>{label}</Label>
                    <Input
                      id={key}
                      type="text"
                      inputMode="decimal"
                      autoComplete="off"
                      value={approvalValue[fieldKey]}
                      disabled={!canManage || approvalSettings.isLoading}
                      aria-invalid={approvalError === label}
                      onChange={(event) => {
                        const nextValue = event.target.value
                        setApprovalDraft((current) => ({
                          ...(current ?? toApprovalDraft(approvalSettings.data)),
                          [fieldKey]: nextValue,
                        }))
                      }}
                    />
                  </div>
                )
              })}
            </div>

            <p className="text-xs text-muted-foreground">
              Amounts may have up to four decimal places. Leave a field blank to restore the
              built-in default for that one; a stored zero is a real threshold.
            </p>

            {approvalError && (
              <Alert variant="destructive">
                <AlertTitle>Check the amount</AlertTitle>
                <AlertDescription>
                  {approvalError} must be a non-negative number with at most four decimal places.
                </AlertDescription>
              </Alert>
            )}

            {approvalSettings.data && (
              <p className="text-xs text-muted-foreground">
                {approvalSettings.data.is_default
                  ? 'No company override is stored, so the built-in defaults are in force.'
                  : `This company stores its own approval thresholds.`}
              </p>
            )}

            {!canManage && (
              <p className="text-sm text-muted-foreground">
                Reading approval thresholds needs <code>settings.view</code>; changing them needs{' '}
                <code>settings.manage</code>.
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() =>
                  saveApproval.mutate({ values: approvalValue, restoreDefault: false })
                }
                disabled={!canManage || !approvalDirty || saveApproval.isPending || !!approvalError}
                loading={saveApproval.isPending}
              >
                <Save />
                Save approval thresholds
              </Button>
              {approvalSettings.data && !approvalSettings.data.is_default && (
                <Button
                  variant="outline"
                  onClick={() =>
                    saveApproval.mutate({ values: approvalValue, restoreDefault: true })
                  }
                  disabled={!canManage || saveApproval.isPending}
                  loading={saveApproval.isPending}
                >
                  <RotateCcw />
                  Restore defaults
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  )
}

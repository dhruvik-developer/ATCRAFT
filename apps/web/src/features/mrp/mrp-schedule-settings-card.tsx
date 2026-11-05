import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw, Save } from 'lucide-react'
import { useState } from 'react'

import { FieldError } from '@/components/shared'
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
import { api } from '@/lib/api-client'
import { reportFieldErrors } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { WarehouseMultiSelect } from './warehouse-multi-select'

type ScheduleSettings = SchemaOf<'MrpScheduleSettingsRead'>
type ScheduleSettingsUpdate = SchemaOf<'MrpScheduleSettingsUpdate'>

interface ScheduleDraft {
  enabled: boolean
  horizonDays: string
  warehouseIds: string[]
  respectSafetyStock: boolean
}

const MAX_HORIZON_DAYS = 1095
const MAX_SCHEDULE_WAREHOUSES = 50

function toDraft(settings: ScheduleSettings): ScheduleDraft {
  return {
    enabled: settings.mrp_scheduled_enabled,
    horizonDays: String(settings.horizon_days),
    warehouseIds: settings.warehouse_ids ?? [],
    respectSafetyStock: settings.respect_safety_stock,
  }
}

function sameDraft(left: ScheduleDraft, right: ScheduleDraft): boolean {
  return (
    left.enabled === right.enabled &&
    left.horizonDays === right.horizonDays &&
    left.respectSafetyStock === right.respectSafetyStock &&
    left.warehouseIds.length === right.warehouseIds.length &&
    left.warehouseIds.every((id, index) => id === right.warehouseIds[index])
  )
}

function horizonError(value: string): string | undefined {
  if (!/^\d{1,4}$/.test(value)) return 'Enter a whole number of days.'
  const days = Number.parseInt(value, 10)
  if (days < 1 || days > MAX_HORIZON_DAYS) {
    return `The planning horizon must be between 1 and ${MAX_HORIZON_DAYS} days.`
  }
  return undefined
}

export function MrpScheduleSettingsCard() {
  const { can } = useAuth()
  const canManage = can(P.MRP_RUN)
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<ScheduleDraft | null>(null)

  const settings = useQuery({
    queryKey: queryKeys.mrp.scheduleSettings,
    queryFn: () => api.get<ScheduleSettings>('/mrp/schedule-settings'),
    staleTime: staleTimes.master,
  })

  const current = settings.data ? toDraft(settings.data) : null
  const value = draft ?? current
  const validationMessage = value ? horizonError(value.horizonDays) : undefined
  const dirty = Boolean(draft && current && !sameDraft(draft, current))

  const save = useMutation({
    mutationFn: (values: ScheduleDraft) => {
      const payload: ScheduleSettingsUpdate = {
        mrp_scheduled_enabled: values.enabled,
        horizon_days: Number.parseInt(values.horizonDays, 10),
        warehouse_ids: values.warehouseIds.length > 0 ? values.warehouseIds : null,
        respect_safety_stock: values.respectSafetyStock,
      }
      return api.put<ScheduleSettings>('/mrp/schedule-settings', payload)
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.mrp.scheduleSettings, saved)
      setDraft(null)
      toast.success(
        success.updated('Nightly MRP settings'),
        'The next scheduled run will use these company settings.',
      )
    },
    onError: (error) => reportFieldErrors(error, 'Could not save nightly MRP settings'),
  })

  const update = (patch: Partial<ScheduleDraft>) => {
    if (!current) return
    setDraft((previous) => ({ ...(previous ?? current), ...patch }))
  }

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle>Nightly MRP schedule</CardTitle>
        <CardDescription>
          Configure this company’s scheduled planning scope. The deployment controls the run time;
          these settings control whether and how this company is planned.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {settings.isLoading && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading schedule settings...
          </p>
        )}
        {settings.error && (
          <Alert variant="destructive">
            <AlertTitle>Could not load schedule settings</AlertTitle>
            <AlertDescription>{describeError(settings.error)}</AlertDescription>
          </Alert>
        )}
        {value && (
          <>
            <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-[minmax(12rem,1fr)_10rem_minmax(14rem,1.3fr)]">
              <div className="flex items-start gap-2">
                <Checkbox
                  id="mrp-scheduled-enabled"
                  checked={value.enabled}
                  disabled={!canManage}
                  onCheckedChange={(checked) => update({ enabled: checked === true })}
                />
                <div>
                  <Label htmlFor="mrp-scheduled-enabled" className="font-normal">
                    Enable nightly planning
                  </Label>
                  <p className="mt-1 text-xs text-muted-foreground">
                    When disabled, the nightly task skips this company.
                  </p>
                </div>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="mrp-scheduled-horizon">Horizon (days)</Label>
                <Input
                  id="mrp-scheduled-horizon"
                  inputMode="numeric"
                  autoComplete="off"
                  value={value.horizonDays}
                  disabled={!canManage}
                  aria-invalid={Boolean(validationMessage)}
                  aria-describedby="mrp-scheduled-horizon-help"
                  onChange={(event) => update({ horizonDays: event.target.value })}
                />
                <p id="mrp-scheduled-horizon-help" className="text-xs text-muted-foreground">
                  Choose 1 to {MAX_HORIZON_DAYS} days.
                </p>
                <FieldError message={validationMessage} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="mrp-scheduled-warehouses">Warehouse scope</Label>
                <WarehouseMultiSelect
                  id="mrp-scheduled-warehouses"
                  value={value.warehouseIds}
                  onChange={(warehouseIds) => update({ warehouseIds })}
                  disabled={!canManage}
                  maxSelections={MAX_SCHEDULE_WAREHOUSES}
                  unavailableMessage="Your role cannot read the warehouse list. The saved scope is preserved, but cannot be edited here."
                />
                <p className="text-xs text-muted-foreground">
                  No selection means all active company warehouses.
                </p>
              </div>

              <div className="flex items-start gap-2 sm:col-span-2 xl:col-span-3">
                <Checkbox
                  id="mrp-scheduled-safety-stock"
                  checked={value.respectSafetyStock}
                  disabled={!canManage}
                  onCheckedChange={(checked) => update({ respectSafetyStock: checked === true })}
                />
                <div>
                  <Label htmlFor="mrp-scheduled-safety-stock" className="font-normal">
                    Protect safety stock
                  </Label>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Keep the safety buffer out of planned available stock.
                  </p>
                </div>
              </div>
            </div>

            {!canManage && (
              <p className="mt-4 text-sm text-muted-foreground">
                Your role can view these settings but cannot change them.
              </p>
            )}

            {canManage && (
              <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                {dirty && (
                  <Button
                    type="button"
                    variant="outline"
                    disabled={save.isPending}
                    onClick={() => setDraft(null)}
                  >
                    <RotateCcw />
                    Discard changes
                  </Button>
                )}
                <Button
                  type="button"
                  loading={save.isPending}
                  disabled={!dirty || Boolean(validationMessage)}
                  onClick={() => value && save.mutate(value)}
                >
                  <Save />
                  Save schedule
                </Button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

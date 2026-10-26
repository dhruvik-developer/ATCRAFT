import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

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
import { api } from '@/lib/api-client'
import { describeError } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import {
  disablePush,
  enablePush,
  getPushState,
  isPushSupported,
  type PushResult,
  type PushState,
} from './push'

const stateLabels: Record<PushState, string> = {
  unsupported: 'Push notifications are not supported by this browser.',
  denied:
    'Notifications are blocked in this browser. Allow notifications in the site settings to enable them.',
  default: 'Notifications have not been enabled on this device.',
  subscribed: 'Notifications are enabled on this device.',
  'not-subscribed': 'This device is not subscribed to notifications.',
}

function needsIosInstall(): boolean {
  const ios =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  const standalone =
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  return ios && !standalone
}

export function PushDeviceCard() {
  const client = useQueryClient()
  const [failure, setFailure] = useState<string | null>(null)
  const state = useQuery({
    queryKey: queryKeys.notifications.pushState,
    queryFn: getPushState,
    staleTime: 0,
    refetchOnWindowFocus: true,
  })
  const config = useQuery({
    queryKey: queryKeys.notifications.pushConfig,
    queryFn: ({ signal }) =>
      api.get<SchemaOf<'PushConfigRead'>>('/notifications/push/config', { signal }),
    enabled: isPushSupported(),
  })
  const operation = useMutation({
    mutationFn: ({ result }: { action: 'enable' | 'disable'; result: Promise<PushResult> }) =>
      result,
    onMutate: () => setFailure(null),
    onSuccess: (result: PushResult, { action }) => {
      client.setQueryData(queryKeys.notifications.pushState, result.state)
      if (result.ok)
        toast.success(
          action === 'enable'
            ? 'Notifications enabled on this device'
            : 'Notifications disabled on this device',
        )
      else setFailure(result.message)
    },
  })
  const test = useMutation({
    mutationFn: () => api.post<SchemaOf<'PushTestRead'>>('/notifications/push/test'),
    onSuccess: (result) =>
      toast.success(
        result.queued > 0
          ? `Test notification queued for ${result.queued} device${result.queued === 1 ? '' : 's'}`
          : 'No subscribed devices are available',
      ),
    onError: (error) => setFailure(describeError(error)),
  })
  const current = state.data ?? 'default'
  const available = config.data?.enabled === true
  const iosHint = needsIosInstall()
  return (
    <Card>
      <CardHeader>
        <CardTitle>This device</CardTitle>
        <CardDescription>
          Choose whether this browser receives notifications when the app is closed.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm" role="status">
          {state.isLoading ? 'Checking device…' : stateLabels[current]}
        </p>
        {config.error && (
          <p role="alert" className="text-sm text-destructive">
            {describeError(config.error)}
          </p>
        )}
        {config.data && !config.data.enabled && (
          <p className="text-sm text-muted-foreground">
            Push notifications are not enabled for this deployment. In-app and email preferences
            remain available below.
          </p>
        )}
        {iosHint && (
          <p className="text-sm text-muted-foreground">
            Add to Home Screen to receive notifications. Open the installed app, then enable
            notifications here.
          </p>
        )}
        {failure && (
          <Alert variant="destructive">
            <AlertDescription>{failure}</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap gap-2">
          {current === 'subscribed' ? (
            <Button
              className="min-h-11"
              variant="outline"
              loading={operation.isPending}
              onClick={() => operation.mutate({ action: 'disable', result: disablePush() })}
            >
              Disable on this device
            </Button>
          ) : (
            <Button
              className="min-h-11"
              disabled={
                state.isLoading ||
                !available ||
                current === 'denied' ||
                current === 'unsupported' ||
                iosHint
              }
              loading={operation.isPending}
              onClick={() => operation.mutate({ action: 'enable', result: enablePush() })}
            >
              Enable on this device
            </Button>
          )}
          <Button
            className="min-h-11"
            variant="outline"
            disabled={!available || current !== 'subscribed' || operation.isPending}
            loading={test.isPending}
            onClick={() => test.mutate()}
          >
            Send test
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
export default PushDeviceCard

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Card,
  CardContent,
  Checkbox,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { MESSAGING_DELIVERIES_PAGE_SIZE } from '@/lib/constants'
import { humanise } from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { NOTIFICATION_TYPES } from '@/features/notifications/notification-types'

type Channel = 'WHATSAPP' | 'SMS'
type TemplateParameter = 'title' | 'body' | 'code' | 'link' | 'amount' | 'date'

const TEMPLATE_PARAMETERS: TemplateParameter[] = ['title', 'body', 'code', 'link', 'amount', 'date']

function parseParameters(value: string): TemplateParameter[] {
  const parsed: TemplateParameter[] = []
  for (const raw of value.split(',')) {
    const name = raw.trim()
    const parameter = TEMPLATE_PARAMETERS.find((allowed) => allowed === name)
    if (parameter) parsed.push(parameter)
  }
  return parsed
}

interface MessagingTemplate {
  channel: Channel
  notification_type: string
  template_name: string | null
  template_id: string | null
  language: string
  parameters: TemplateParameter[]
  sms_text: string | null
  is_active: boolean
}

interface DeliveryRecord {
  id: string
  notification_id: string
  channel: Channel
  outcome: 'DELIVERED' | 'FAILED' | 'RETRY'
  provider_message_id: string | null
  attempted_at: string
  error_code: string | null
}

interface ChannelStatus {
  whatsapp_enabled: boolean
  sms_enabled: boolean
}

const CHANNELS_QUERY_KEY = ['notifications', 'channel-templates'] as const
const DELIVERIES_QUERY_KEY = ['notifications', 'messaging-deliveries'] as const

function updateTemplate(
  templates: MessagingTemplate[],
  index: number,
  patch: Partial<MessagingTemplate>,
): MessagingTemplate[] {
  return templates.map((template, position) =>
    position === index ? { ...template, ...patch } : template,
  )
}

export default function MessagingPage() {
  const { can } = useAuth()
  const canManage = can(P.SETTINGS_MANAGE)
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<MessagingTemplate[]>([])
  const [channel, setChannel] = useState<Channel>('WHATSAPP')
  const [notificationType, setNotificationType] = useState<string>(NOTIFICATION_TYPES[0])

  const status = useQuery({
    queryKey: [...queryKeys.notifications.channels, 'admin'] as const,
    queryFn: () => api.get<ChannelStatus>('/notifications/channel-status'),
  })
  const templates = useQuery({
    queryKey: CHANNELS_QUERY_KEY,
    queryFn: () => api.get<MessagingTemplate[]>('/notifications/channel-templates'),
  })
  const deliveries = useQuery({
    queryKey: DELIVERIES_QUERY_KEY,
    queryFn: () =>
      api.getCursorPage<DeliveryRecord>('/notifications/deliveries', {
        params: { limit: MESSAGING_DELIVERIES_PAGE_SIZE },
      }),
  })

  useEffect(() => {
    if (templates.data) setDraft(templates.data)
  }, [templates.data])

  const save = useMutation({
    mutationFn: () =>
      api.put<MessagingTemplate[]>('/notifications/channel-templates', { templates: draft }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: CHANNELS_QUERY_KEY })
      await queryClient.invalidateQueries({ queryKey: DELIVERIES_QUERY_KEY })
      toast.success(success.saved('Messaging templates'))
    },
  })

  const sendTest = useMutation({
    mutationFn: (template: MessagingTemplate) =>
      api.post<{ sent: boolean }>('/notifications/channel-templates/test', {
        channel: template.channel,
        notification_type: template.notification_type,
      }),
    onSuccess: () => toast.success('Test message sent to your verified number'),
  })

  const addTemplate = () => {
    if (
      draft.some(
        (template) =>
          template.channel === channel && template.notification_type === notificationType,
      )
    ) {
      toast.error('Template already exists', 'Edit the existing channel and notification type.')
      return
    }
    setDraft((current) => [
      ...current,
      {
        channel,
        notification_type: notificationType,
        template_name: channel === 'WHATSAPP' ? '' : null,
        template_id: channel === 'SMS' ? '' : null,
        language: 'en',
        parameters: [],
        sms_text: channel === 'SMS' ? '' : null,
        is_active: true,
      },
    ])
  }

  const enabled = status.data
  const loadError = status.error ?? templates.error

  return (
    <div>
      <PageHeader
        title="Messaging templates"
        description="Configure approved per-company templates and review external delivery attempts."
      />

      {loadError && (
        <Alert variant="warning" className="mb-5">
          <AlertTitle>Messaging settings could not be loaded</AlertTitle>
          <AlertDescription>{describeError(loadError)}</AlertDescription>
        </Alert>
      )}

      {enabled && (
        <Alert className="mb-5">
          <AlertTitle>Provider status</AlertTitle>
          <AlertDescription>
            WhatsApp: {enabled.whatsapp_enabled ? 'enabled' : 'disabled'} · SMS:{' '}
            {enabled.sms_enabled ? 'enabled' : 'disabled'}
          </AlertDescription>
        </Alert>
      )}
      {!canManage && (
        <p className="mb-5 text-sm text-muted-foreground">
          You have read-only access to messaging settings.
        </p>
      )}

      <Card className="mb-5">
        <CardContent className="flex flex-wrap items-end gap-3 pt-5">
          <div>
            <Label htmlFor="template-channel">Channel</Label>
            <select
              id="template-channel"
              className="mt-1.5 h-10 rounded-md border border-input bg-background px-3 text-sm"
              value={channel}
              disabled={!canManage}
              onChange={(event) => setChannel(event.target.value as Channel)}
            >
              <option value="WHATSAPP">WhatsApp</option>
              <option value="SMS">SMS</option>
            </select>
          </div>
          <div>
            <Label htmlFor="template-type">Notification type</Label>
            <select
              id="template-type"
              className="mt-1.5 h-10 max-w-80 rounded-md border border-input bg-background px-3 text-sm"
              value={notificationType}
              disabled={!canManage}
              onChange={(event) => setNotificationType(event.target.value)}
            >
              {NOTIFICATION_TYPES.map((type) => (
                <option key={type} value={type}>
                  {humanise(type)}
                </option>
              ))}
            </select>
          </div>
          <Button type="button" variant="outline" disabled={!canManage} onClick={addTemplate}>
            Add template
          </Button>
          <Button
            type="button"
            className="ml-auto"
            loading={save.isPending}
            disabled={!canManage || draft.length === 0 || templates.isLoading}
            onClick={() => save.mutate()}
          >
            Save templates
          </Button>
        </CardContent>
      </Card>

      <div className="space-y-4">
        {draft.map((template, index) => {
          const parameters = template.parameters.join(', ')
          return (
            <Card key={`${template.channel}:${template.notification_type}`}>
              <CardContent className="space-y-4 pt-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">
                      {template.channel} · {humanise(template.notification_type)}
                    </h2>
                    <p className="text-xs text-muted-foreground">
                      Parameter order must match the provider-approved template.
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Label className="inline-flex items-center gap-2">
                      <Checkbox
                        checked={template.is_active}
                        disabled={!canManage}
                        onCheckedChange={(checked) =>
                          setDraft(updateTemplate(draft, index, { is_active: checked === true }))
                        }
                      />
                      Active
                    </Label>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      loading={sendTest.isPending}
                      disabled={
                        !template.is_active ||
                        !canManage ||
                        (template.channel === 'WHATSAPP'
                          ? !enabled?.whatsapp_enabled
                          : !enabled?.sms_enabled)
                      }
                      onClick={() => sendTest.mutate(template)}
                    >
                      Send test
                    </Button>
                  </div>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  {template.channel === 'WHATSAPP' ? (
                    <>
                      <div>
                        <Label htmlFor={`template-name-${index}`}>Approved template name</Label>
                        <Input
                          id={`template-name-${index}`}
                          value={template.template_name ?? ''}
                          disabled={!canManage}
                          maxLength={128}
                          onChange={(event) =>
                            setDraft(
                              updateTemplate(draft, index, { template_name: event.target.value }),
                            )
                          }
                        />
                      </div>
                      <div>
                        <Label htmlFor={`template-language-${index}`}>Language</Label>
                        <Input
                          id={`template-language-${index}`}
                          value={template.language}
                          disabled={!canManage}
                          maxLength={16}
                          onChange={(event) =>
                            setDraft(updateTemplate(draft, index, { language: event.target.value }))
                          }
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <Label htmlFor={`template-id-${index}`}>DLT template ID</Label>
                        <Input
                          id={`template-id-${index}`}
                          value={template.template_id ?? ''}
                          disabled={!canManage}
                          maxLength={128}
                          onChange={(event) =>
                            setDraft(
                              updateTemplate(draft, index, { template_id: event.target.value }),
                            )
                          }
                        />
                      </div>
                      <div>
                        <Label htmlFor={`template-text-${index}`}>Registered DLT text</Label>
                        <Input
                          id={`template-text-${index}`}
                          value={template.sms_text ?? ''}
                          disabled={!canManage}
                          maxLength={2000}
                          onChange={(event) =>
                            setDraft(updateTemplate(draft, index, { sms_text: event.target.value }))
                          }
                        />
                      </div>
                    </>
                  )}
                  <div className="md:col-span-2">
                    <Label htmlFor={`template-parameters-${index}`}>
                      Ordered parameters (comma-separated)
                    </Label>
                    <Input
                      id={`template-parameters-${index}`}
                      value={parameters}
                      placeholder="title, body, link"
                      disabled={!canManage}
                      onChange={(event) =>
                        setDraft(
                          updateTemplate(draft, index, {
                            parameters: parseParameters(event.target.value),
                          }),
                        )
                      }
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Allowed: {TEMPLATE_PARAMETERS.join(', ')}.
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          )
        })}
        {draft.length === 0 && !templates.isLoading && (
          <p className="rounded-md border border-dashed p-6 text-sm text-muted-foreground">
            No templates configured. Add a channel and notification type to start.
          </p>
        )}
      </div>

      <section className="mt-8">
        <h2 className="mb-3 text-lg font-semibold">Recent delivery attempts</h2>
        {deliveries.isError && (
          <Alert variant="warning">
            <AlertTitle>Delivery history unavailable</AlertTitle>
            <AlertDescription>{describeError(deliveries.error)}</AlertDescription>
          </Alert>
        )}
        {deliveries.data?.data.length === 0 && (
          <p className="text-sm text-muted-foreground">No external delivery attempts yet.</p>
        )}
        {deliveries.data?.data.map((delivery) => (
          <div
            key={delivery.id}
            className="flex flex-wrap justify-between gap-2 border-b py-2 text-sm"
          >
            <span>
              {delivery.channel} · {delivery.outcome}
            </span>
            <time dateTime={delivery.attempted_at}>
              {new Date(delivery.attempted_at).toLocaleString()}
            </time>
            <span className="text-muted-foreground">
              {delivery.error_code ?? delivery.provider_message_id ?? delivery.notification_id}
            </span>
          </div>
        ))}
      </section>
    </div>
  )
}

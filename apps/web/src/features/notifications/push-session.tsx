import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'

import { useAuth } from '@/features/auth/auth-context'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'

/** Mounted once in the active shell, shared by staff and both external portals. */
export function PushSessionBridge() {
  const { user, can } = useAuth()
  const client = useQueryClient()
  const allowed = can(P.NOTIFICATION_VIEW)
  useEffect(() => {
    if (!user?.id || !allowed || !('serviceWorker' in navigator)) return
    let active = true
    let cancelSetup: (() => void) | undefined
    void import('./push')
      .then(async (module) => {
        if (!active) return
        cancelSetup = module.cancelPushSetup
        const result = await module.syncPushSubscription()
        if (active) client.setQueryData(queryKeys.notifications.pushState, result.state)
      })
      .catch(() => undefined)
    const onMessage = (event: MessageEvent) => {
      if (event.data?.type !== 'notification') return
      // The shared prefix includes both the inbox and its unread-count query.
      void client.invalidateQueries({ queryKey: queryKeys.notifications.all })
    }
    navigator.serviceWorker.addEventListener('message', onMessage)
    return () => {
      active = false
      cancelSetup?.()
      navigator.serviceWorker.removeEventListener('message', onMessage)
    }
  }, [user?.id, allowed, client])
  return null
}

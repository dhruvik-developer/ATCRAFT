/** Own-device push lifecycle. Credentials stay in the HTTP client, never the worker. */
import { api } from '@/lib/api-client'
import { SERVICE_WORKER_URL } from '@/lib/constants'
import { describeError } from '@/lib/messages'
import type { SchemaOf } from '@/types/api'

export type PushState = 'unsupported' | 'denied' | 'default' | 'subscribed' | 'not-subscribed'
export type PushResult =
  { ok: true; state: PushState } | { ok: false; state: PushState; message: string }
const REQUEST_TIMEOUT_MS = 10000
const LOGOUT_TIMEOUT_MS = 2000
let lifetime = new AbortController()
let deviceQueue: Promise<unknown> = Promise.resolve()

/** Keep native mutations ordered after a timeout: a late subscription from a
 * signed-out user must be disposed before a new user can reuse the worker.
 * Permission prompts never hold this queue. */
function withDevice(action: () => Promise<PushResult>): Promise<PushResult> {
  const work = deviceQueue.then(action, action)
  deviceQueue = work.catch(() => undefined)
  return work
}

function permissionState(): PushState {
  if (!isPushSupported()) return 'unsupported'
  if (Notification.permission === 'denied') return 'denied'
  return Notification.permission === 'granted' ? 'not-subscribed' : 'default'
}

/** Native permission/provider promises cannot be aborted. Bound the caller's wait. */
function waitFor<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Notification setup was cancelled.', 'AbortError'))
    if (signal.aborted) abort()
    else signal.addEventListener('abort', abort, { once: true })
    work.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
}

async function execute(
  action: (signal: AbortSignal) => Promise<PushResult>,
  timeout = REQUEST_TIMEOUT_MS,
): Promise<PushResult> {
  const parent = lifetime.signal
  const controller = new AbortController()
  const cancel = () => controller.abort()
  parent.addEventListener('abort', cancel, { once: true })
  const timer = setTimeout(cancel, timeout)
  try {
    return await waitFor(action(controller.signal), controller.signal)
  } catch (error) {
    return {
      ok: false,
      state: permissionState(),
      message: controller.signal.aborted
        ? 'Notification setup did not finish. Please try again.'
        : describeError(
            error,
            'Notifications could not be configured on this device. Please try again.',
          ),
    }
  } finally {
    clearTimeout(timer)
    parent.removeEventListener('abort', cancel)
  }
}

export function isPushSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext !== false &&
    'Notification' in window &&
    'PushManager' in window &&
    'serviceWorker' in navigator
  )
}

export async function getPushState(): Promise<PushState> {
  const initial = permissionState()
  if (initial !== 'not-subscribed') return initial
  const result = await execute(async (signal) => {
    const registration = await navigator.serviceWorker.getRegistration(SERVICE_WORKER_URL)
    signal.throwIfAborted()
    const subscription = await registration?.pushManager.getSubscription()
    return { ok: true, state: subscription ? 'subscribed' : 'not-subscribed' }
  })
  return result.state
}

function publicKey(value: string): Uint8Array<ArrayBuffer> {
  const padded =
    value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - (value.length % 4)) % 4)
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0))
}
function sameKey(existing: ArrayBuffer | null, desired: Uint8Array): boolean {
  if (!existing) return false
  const current = new Uint8Array(existing)
  return (
    current.length === desired.length && current.every((value, index) => value === desired[index])
  )
}
async function unregisterEndpoint(endpoint: string, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  await waitFor(
    api.post('/notifications/push/subscriptions/unregister', { endpoint }, { signal }),
    signal,
  )
}

async function registerSubscription(signal: AbortSignal): Promise<PushResult> {
  let subscription: PushSubscription | null = null
  try {
    signal.throwIfAborted()
    const config = await waitFor(
      api.get<SchemaOf<'PushConfigRead'>>('/notifications/push/config', {
        signal,
      }),
      signal,
    )
    signal.throwIfAborted()
    if (!config.enabled || !config.vapid_public_key)
      return {
        ok: false,
        state: permissionState(),
        message: 'Push notifications are not enabled for this deployment.',
      }
    const desired = publicKey(config.vapid_public_key)
    const registration = await navigator.serviceWorker.register(SERVICE_WORKER_URL, {
      scope: '/',
      updateViaCache: 'none',
    })
    signal.throwIfAborted()
    if (!registration.active) await navigator.serviceWorker.ready
    signal.throwIfAborted()
    subscription = await registration.pushManager.getSubscription()
    signal.throwIfAborted()
    if (subscription && !sameKey(subscription.options.applicationServerKey, desired)) {
      await unregisterEndpoint(subscription.endpoint, signal)
      if (!(await subscription.unsubscribe()))
        throw new Error('The previous device subscription could not be replaced.')
      subscription = null
      signal.throwIfAborted()
    }
    subscription ??= await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: desired,
    })
    signal.throwIfAborted()
    const serialized = subscription.toJSON()
    if (!serialized.keys?.p256dh || !serialized.keys.auth)
      throw new Error('The browser did not return a complete push subscription.')
    const payload: SchemaOf<'PushSubscribeRequest'> = {
      endpoint: subscription.endpoint,
      keys: { p256dh: serialized.keys.p256dh, auth: serialized.keys.auth },
      expiration_time: subscription.expirationTime,
      device_label: 'Current browser',
    }
    await waitFor(api.post('/notifications/push/subscriptions', payload, { signal }), signal)
    signal.throwIfAborted()
    return { ok: true, state: 'subscribed' }
  } catch (error) {
    // Cleanup stays inside the device queue, even for a late native result.
    // Never use the next user's credentials to finish an old API registration.
    if (subscription) await subscription.unsubscribe().catch(() => false)
    throw error
  }
}

/** Invoke directly from a click: requestPermission must retain the user gesture. */
export function enablePush(): Promise<PushResult> {
  if (!isPushSupported())
    return Promise.resolve({
      ok: false,
      state: 'unsupported',
      message: 'This browser does not support push notifications.',
    })
  let permission: Promise<NotificationPermission>
  try {
    permission = Notification.requestPermission()
  } catch (error) {
    return execute(async () => {
      throw error
    })
  }
  return execute(async (signal) => {
    const granted = await waitFor(permission, signal)
    signal.throwIfAborted()
    if (granted !== 'granted')
      return {
        ok: false,
        state: granted === 'denied' ? 'denied' : 'default',
        message:
          granted === 'denied'
            ? 'Notifications are blocked in this browser. Change the site permission to enable them.'
            : 'Notifications were not enabled. You can try again when ready.',
      }
    return withDevice(() => registerSubscription(signal))
  })
}

export function syncPushSubscription(): Promise<PushResult> {
  if (!isPushSupported() || Notification.permission !== 'granted')
    return Promise.resolve({ ok: true, state: permissionState() })
  return execute((signal) => withDevice(() => registerSubscription(signal)))
}

async function removeDevice(unregisterWorker: boolean, signal: AbortSignal): Promise<PushResult> {
  if (!isPushSupported()) return { ok: true, state: 'unsupported' }
  const registration = await navigator.serviceWorker.getRegistration(SERVICE_WORKER_URL)
  const subscription = await registration?.pushManager.getSubscription()
  let failure: unknown
  if (subscription) {
    // A queued cleanup may finish after logout. Only native cleanup is safe
    // after the original session's budget ends; never send a late API request.
    if (!signal.aborted) {
      try {
        await unregisterEndpoint(subscription.endpoint, signal)
      } catch (error) {
        failure = error
      }
    }
    try {
      if (!(await subscription.unsubscribe()))
        failure ??= new Error('The browser could not disable this device.')
    } catch (error) {
      failure ??= error
    }
  }
  if (unregisterWorker && registration) {
    try {
      const notifications = await registration.getNotifications()
      notifications.forEach((notification) => notification.close())
    } catch (error) {
      failure ??= error
    }
    try {
      await registration.unregister()
    } catch (error) {
      failure ??= error
    }
  }
  if (failure) throw failure
  return { ok: true, state: permissionState() }
}
function stopPending(): void {
  lifetime.abort()
  lifetime = new AbortController()
}

/** An expired session must not finish registering under a later login. */
export function cancelPushSetup(): void {
  stopPending()
}

export function disablePush(): Promise<PushResult> {
  stopPending()
  return execute((signal) => withDevice(() => removeDevice(false, signal)))
}
/** Logout bypasses permission waits; provider cleanup cannot delay it past 2s. */
export function unregisterOnLogout(): Promise<PushResult> {
  stopPending()
  return execute((signal) => withDevice(() => removeDevice(true, signal)), LOGOUT_TIMEOUT_MS)
}

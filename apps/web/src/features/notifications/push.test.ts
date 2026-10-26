import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api-client'
import { pushBrowser } from '@/test/setup'

vi.mock('@/lib/api-client', async (original) => ({
  ...(await original<typeof import('@/lib/api-client')>()),
  api: { get: vi.fn(), post: vi.fn() },
}))

let push: typeof import('./push')
const key = new Uint8Array([1, 2, 3]).buffer

function subscription(endpoint = 'https://push.example/device', serverKey = key) {
  const value = {
    endpoint,
    expirationTime: null,
    options: { applicationServerKey: serverKey },
    toJSON: () => ({ endpoint, keys: { p256dh: 'public-key', auth: 'auth-key' } }),
    unsubscribe: vi.fn(async () => {
      if (pushBrowser.subscription === value) pushBrowser.subscription = null
      return true
    }),
  } as unknown as PushSubscription
  return value
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

beforeEach(async () => {
  vi.resetModules()
  push = await import('./push')
  vi.mocked(api.get).mockReset().mockResolvedValue({ enabled: true, vapid_public_key: 'AQID' })
  vi.mocked(api.post).mockReset().mockResolvedValue({})
  pushBrowser.subscribe.mockImplementation(async () => {
    pushBrowser.subscription = subscription()
    return pushBrowser.subscription
  })
})
afterEach(() => vi.useRealTimers())

describe('own-device push lifecycle', () => {
  it('requests permission within the click and registers a complete device', async () => {
    const operation = push.enablePush()
    expect(pushBrowser.requestPermission).toHaveBeenCalledOnce()
    expect(await operation).toEqual({ ok: true, state: 'subscribed' })
    expect(pushBrowser.register).toHaveBeenCalledWith('/sw.js', {
      scope: '/',
      updateViaCache: 'none',
    })
    expect(api.post).toHaveBeenCalledWith(
      '/notifications/push/subscriptions',
      {
        endpoint: 'https://push.example/device',
        keys: { p256dh: 'public-key', auth: 'auth-key' },
        expiration_time: null,
        device_label: 'Current browser',
      },
      { signal: expect.any(AbortSignal) },
    )
  })

  it('never prompts or registers automatically without an existing grant', async () => {
    expect(await push.syncPushSubscription()).toEqual({ ok: true, state: 'default' })
    expect(pushBrowser.requestPermission).not.toHaveBeenCalled()
    expect(api.get).not.toHaveBeenCalled()
    expect(pushBrowser.register).not.toHaveBeenCalled()
  })

  it.each(['default', 'denied'] as const)(
    'handles a permission response of %s',
    async (permission) => {
      pushBrowser.requestPermission.mockResolvedValue(permission)
      expect(await push.enablePush()).toMatchObject({ ok: false, state: permission })
      expect(api.get).not.toHaveBeenCalled()
    },
  )

  it('refreshes last-seen without replacing a matching existing subscription', async () => {
    pushBrowser.permission = 'granted'
    const existing = subscription()
    pushBrowser.subscription = existing
    expect(await push.syncPushSubscription()).toEqual({ ok: true, state: 'subscribed' })
    expect(pushBrowser.subscribe).not.toHaveBeenCalled()
    expect(existing.unsubscribe).not.toHaveBeenCalled()
    expect(api.post).toHaveBeenCalledOnce()
  })

  it('unregisters and unsubscribes the old key before registering the replacement', async () => {
    pushBrowser.permission = 'granted'
    const previous = subscription('https://push.example/old', new Uint8Array([9]).buffer)
    pushBrowser.subscription = previous
    expect(await push.syncPushSubscription()).toMatchObject({ ok: true })
    expect(api.post).toHaveBeenNthCalledWith(
      1,
      '/notifications/push/subscriptions/unregister',
      { endpoint: previous.endpoint },
      { signal: expect.any(AbortSignal) },
    )
    expect(previous.unsubscribe).toHaveBeenCalledOnce()
    expect(pushBrowser.subscribe).toHaveBeenCalledOnce()
  })

  it('removes a provider-only device if the API registration fails', async () => {
    vi.mocked(api.post).mockRejectedValueOnce(new Error('Offline'))
    expect(await push.enablePush()).toMatchObject({ ok: false })
    expect(pushBrowser.subscription).toBeNull()
  })

  it('does not create a provider subscription when deployment push is disabled', async () => {
    vi.mocked(api.get).mockResolvedValue({ enabled: false, vapid_public_key: null })
    expect(await push.enablePush()).toMatchObject({ ok: false })
    expect(pushBrowser.subscribe).not.toHaveBeenCalled()
  })

  it('disables the current device without unregistering the worker', async () => {
    await push.enablePush()
    expect(await push.disablePush()).toMatchObject({ ok: true })
    expect(pushBrowser.subscription).toBeNull()
    expect(pushBrowser.unregister).not.toHaveBeenCalled()
  })

  it('unregisters the worker and closes visible notifications on logout', async () => {
    await push.enablePush()
    const close = vi.fn()
    pushBrowser.getNotifications.mockResolvedValue([{ close } as unknown as Notification])
    expect(await push.unregisterOnLogout()).toMatchObject({ ok: true })
    expect(pushBrowser.subscription).toBeNull()
    expect(close).toHaveBeenCalledOnce()
    expect(pushBrowser.unregister).toHaveBeenCalledOnce()
  })

  it('still removes the browser device when authenticated unregistration fails', async () => {
    await push.enablePush()
    vi.mocked(api.post).mockRejectedValueOnce(new Error('Offline'))
    expect(await push.unregisterOnLogout()).toMatchObject({ ok: false })
    expect(pushBrowser.subscription).toBeNull()
    expect(pushBrowser.unregister).toHaveBeenCalledOnce()
  })

  it('logs out while permission is unanswered and ignores its later grant', async () => {
    const permission = deferred<NotificationPermission>()
    pushBrowser.requestPermission.mockReturnValue(permission.promise)
    const enabling = push.enablePush()
    expect(await push.unregisterOnLogout()).toMatchObject({ ok: true })
    expect(await enabling).toMatchObject({ ok: false })
    permission.resolve('granted')
    await Promise.resolve()
    expect(pushBrowser.subscribe).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('unregisters the worker even when clearing visible notifications fails', async () => {
    await push.enablePush()
    pushBrowser.getNotifications.mockRejectedValue(new Error('Browser unavailable'))
    expect(await push.unregisterOnLogout()).toMatchObject({ ok: false })
    expect(pushBrowser.subscription).toBeNull()
    expect(pushBrowser.unregister).toHaveBeenCalledOnce()
  })

  it('cleans the browser after the logout API cleanup budget expires', async () => {
    vi.useFakeTimers()
    await push.enablePush()
    vi.mocked(api.post).mockReturnValueOnce(new Promise(() => undefined))
    const leaving = push.unregisterOnLogout()
    await vi.advanceTimersByTimeAsync(2000)
    expect(await leaving).toMatchObject({ ok: false })
    await vi.waitFor(() => expect(pushBrowser.subscription).toBeNull())
    expect(pushBrowser.unregister).toHaveBeenCalledOnce()
  })

  it('bounds logout during a hung provider, disposes its late result, then permits a new login', async () => {
    vi.useFakeTimers()
    const provider = deferred<PushSubscription>()
    pushBrowser.subscribe.mockReturnValueOnce(provider.promise)
    const enabling = push.enablePush()
    await vi.waitFor(() => expect(pushBrowser.subscribe).toHaveBeenCalledOnce())
    const leaving = push.unregisterOnLogout()
    await vi.advanceTimersByTimeAsync(2000)
    expect(await leaving).toMatchObject({ ok: false })
    expect(await enabling).toMatchObject({ ok: false })
    const newLogin = push.syncPushSubscription()
    expect(pushBrowser.subscribe).toHaveBeenCalledOnce()
    const late = subscription('https://push.example/late')
    pushBrowser.subscription = late
    provider.resolve(late)
    expect(await newLogin).toMatchObject({ ok: true, state: 'subscribed' })
    expect(late.unsubscribe).toHaveBeenCalled()
    expect(
      vi
        .mocked(api.post)
        .mock.calls.some(
          ([, payload]) => (payload as { endpoint?: string })?.endpoint === late.endpoint,
        ),
    ).toBe(false)
    expect(pushBrowser.subscription?.endpoint).toBe('https://push.example/device')
  })

  it('bounds state reads when a browser provider stops responding', async () => {
    vi.useFakeTimers()
    pushBrowser.permission = 'granted'
    pushBrowser.getRegistration.mockReturnValue(new Promise(() => undefined))
    const state = push.getPushState()
    await vi.advanceTimersByTimeAsync(10000)
    expect(await state).toBe('not-subscribed')
  })
})

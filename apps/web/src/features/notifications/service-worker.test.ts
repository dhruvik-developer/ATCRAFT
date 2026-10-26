import { describe, expect, it, vi } from 'vitest'

import source from '../../../public/sw.js?raw'

function worker() {
  const handlers = new Map<string, (event: unknown) => void>()
  const client = {
    url: 'https://erp.example/',
    navigate: vi.fn().mockResolvedValue(null),
    focus: vi.fn(),
    postMessage: vi.fn(),
  }
  const scope = {
    location: { origin: 'https://erp.example' },
    registration: { showNotification: vi.fn().mockResolvedValue(undefined) },
    clients: { matchAll: vi.fn().mockResolvedValue([client]), openWindow: vi.fn() },
    skipWaiting: vi.fn(),
    addEventListener: (name: string, handler: (event: unknown) => void) =>
      handlers.set(name, handler),
  }
  // Execute this repository's plain worker source against a fake browser scope.
  // It has no bundler imports, so this also catches invalid standalone syntax.
  new Function('self', 'URL', source)(scope, URL)
  const dispatch = async (name: string, data: Record<string, unknown>) => {
    let work: Promise<unknown> | undefined
    handlers.get(name)?.({
      ...data,
      waitUntil: (promise: Promise<unknown>) => {
        work = promise
      },
    })
    await work
  }
  return { scope, client, dispatch, handlers }
}

describe('push-only service worker', () => {
  it('registers no fetch handler or offline cache', () => {
    expect([...worker().handlers.keys()].sort()).toEqual([
      'activate',
      'install',
      'notificationclick',
      'push',
    ])
  })

  it('shows a bounded notification and refreshes the open inbox', async () => {
    const { scope, client, dispatch } = worker()
    await dispatch('push', {
      data: {
        json: () => ({
          title: 'x'.repeat(200),
          body: 'b'.repeat(700),
          url: '/dealer/orders?id=1',
          tag: 'order',
        }),
      },
    })
    expect(scope.registration.showNotification).toHaveBeenCalledWith(
      'x'.repeat(120),
      expect.objectContaining({
        body: 'b'.repeat(500),
        tag: 'order',
        data: { url: '/dealer/orders?id=1' },
      }),
    )
    expect(client.postMessage).toHaveBeenCalledWith({ type: 'notification' })
  })

  it('handles malformed payloads with a safe generic notification', async () => {
    const { scope, dispatch } = worker()
    await dispatch('push', {
      data: {
        json: () => {
          throw new Error('Invalid JSON')
        },
      },
    })
    expect(scope.registration.showNotification).toHaveBeenCalledWith(
      'ATCRAFT',
      expect.objectContaining({ data: { url: '/notifications' } }),
    )
  })

  it.each([
    'https://evil.example',
    '//evil.example/path',
    '/\\evil.example/path',
    'javascript:alert(1)',
    null,
  ])('replaces an unsafe URL %s', async (url) => {
    const { scope, dispatch } = worker()
    await dispatch('push', { data: { json: () => ({ url }) } })
    expect(scope.registration.showNotification).toHaveBeenCalledWith(
      'ATCRAFT',
      expect.objectContaining({ data: { url: '/notifications' } }),
    )
  })

  it('closes the notification, navigates and focuses an existing tab', async () => {
    const { client, dispatch } = worker()
    const close = vi.fn()
    await dispatch('notificationclick', {
      notification: { close, data: { url: '/vendor/orders/1' } },
    })
    expect(close).toHaveBeenCalledOnce()
    expect(client.navigate).toHaveBeenCalledWith('/vendor/orders/1')
    expect(client.focus).toHaveBeenCalledOnce()
  })

  it('opens a same-origin window when no eligible tab is available', async () => {
    const { scope, client, dispatch } = worker()
    client.url = 'https://foreign.example/'
    await dispatch('notificationclick', {
      notification: { close: vi.fn(), data: { url: '//foreign.example' } },
    })
    expect(client.navigate).not.toHaveBeenCalled()
    expect(scope.clients.openWindow).toHaveBeenCalledWith('/notifications')
  })
})

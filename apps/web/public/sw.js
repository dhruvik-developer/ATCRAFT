/* Push only. No fetch handler or cache: releases always load fresh application assets. */
const FALLBACK_URL = '/notifications'

function safeUrl(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return FALLBACK_URL
  try {
    const target = new URL(value, self.location.origin)
    return target.origin === self.location.origin ? target.pathname + target.search + target.hash : FALLBACK_URL
  } catch { return FALLBACK_URL }
}

self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()))
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    let data = {}
    try { data = event.data?.json() ?? {} } catch { /* A malformed payload still produces a safe generic notice. */ }
    if (!data || typeof data !== 'object') data = {}
    const title = typeof data.title === 'string' && data.title ? data.title.slice(0, 120) : 'ATCRAFT'
    const body = typeof data.body === 'string' ? data.body.slice(0, 500) : 'There is new activity in ATCRAFT.'
    const tag = typeof data.tag === 'string' && data.tag ? data.tag.slice(0, 160) : 'atcraft-notification'
    await self.registration.showNotification(title, { body, tag, renotify: true, icon: '/icons/icon-192.png', badge: '/icons/icon-192.png', data: { url: safeUrl(data.url) } })
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    clients.forEach((client) => { try { client.postMessage({ type: 'notification' }) } catch { /* A tab may close after matchAll. */ } })
  })())
})
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil((async () => {
    const path = safeUrl(event.notification.data?.url)
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of clients) {
      if (new URL(client.url).origin !== self.location.origin) continue
      try {
        const navigated = await client.navigate(path)
        await (navigated ?? client).focus()
        return
      } catch { /* Try another tab, or open a new one if this tab closed. */ }
    }
    await self.clients.openWindow(path)
  })())
})

# WP C2b: Installable PWA, push opt-in UI, admin inbound tracking board, PO detail additions

Lane C (portals), round 3, step 2. Assumption placeholders continue as `§C2-n`.

Backend is on main: WP C1b (push routes, nginx `sw.js`/manifest locations) and WP C1a (inbound tracking, shipments, vendor committed date, VENDOR_DECLINED, PO PDF).

Background: `reports/notifications-alerts.md` "Proposed design (b)" item 7 and its push-specific traps.

## Part 1: PWA and push (all users: staff, vendors, and later dealers)

1. **`apps/web/public/manifest.webmanifest`**
   - `name` "ATCRAFT ERP", `short_name` "ATCRAFT"
   - `start_url` "/", `scope` "/", `display` "standalone"
   - `background_color` and `theme_color` matching `index.html` and the theme tokens
   - icons: 192 and 512 `purpose: any`, plus a 512 `purpose: maskable`
2. **Icons**
   - Generate the PNG icons once from a simple brand mark: the existing `favicon.svg` motif, or a bold "A" on the brand colour with a maskable safe zone.
   - Use a dev-time script run with the **global** Python + PIL (`python -c ...`). Do not add a project dependency.
   - Commit the PNGs under `public/icons/`. Keep each under 50 KB.
   - Add `apple-touch-icon` (180).
3. **`index.html`**
   - `<link rel="manifest">`, `<meta name="theme-color">`, `apple-touch-icon`
   - `<meta name="apple-mobile-web-app-capable" content="yes">` and the iOS status-bar style
   - Keep the existing pre-React theme paint intact.
4. **`apps/web/public/sw.js`** handles push only:
   - `push`: parse the JSON payload `{title, body, url, tag, severity, count}`, call `showNotification(title, {body, tag, data:{url}, icon, badge, renotify: true})`, and `postMessage({type:'notification'})` to open clients.
   - `notificationclick`: focus an existing same-origin client and navigate it to `url`, or `clients.openWindow(url)`. Only same-origin relative URLs are allowed; anything else falls back to `/notifications`.
   - `install`: `skipWaiting`. `activate`: `clients.claim`.
   - **NO `fetch` handler and NO cache.** This keeps the deploy-recovery reload in `main.tsx` working.
   - The file escapes eslint and tsc, so keep it small, plain, and commented. Add a vitest that loads it in a fake worker scope and exercises both handlers.
5. **`src/features/notifications/push.ts`** (lazy-imported):
   - `isPushSupported()`
   - `getPushState()`: `unsupported | denied | default | subscribed | not-subscribed`
   - `enablePush()`: must be called from a user gesture. Call `Notification.requestPermission`, then `navigator.serviceWorker.register('/sw.js')`, then `pushManager.subscribe({userVisibleOnly:true, applicationServerKey})` using the key from `/notifications/push/config`, then POST the subscription.
   - `disablePush()`: unsubscribe and DELETE or unregister.
   - `syncPushSubscription()`: on load, when permission is granted, re-subscribe if the server key changed, and re-POST the subscription to refresh `last_seen`.
   - `unregisterOnLogout()`
   - All error paths are swallowed into a typed result and never crash the shell.
   - Put the `SERVICE_WORKER_URL` and push query keys in `lib/constants.ts` and `queryKeys`.
6. **UI**
   - Notifications page preferences tab: a "This device" card (state, an Enable/Disable button, "Send test") and a **Push** column next to In-app and Email. Only changed rows are sent, and `push_enabled` is included.
   - The vendor profile and staff profile show the same device card.
   - `app-layout.tsx` and `VendorLayout`: lazy `syncPushSubscription()` after auth, and a service-worker message listener that invalidates `queryKeys.notifications.unreadCount`.
   - `auth-context.tsx` logout: `await unregisterOnLogout()` (errors swallowed) **before** `POST /auth/logout`.
   - iOS hint: if not installed (`display-mode: standalone` false) on iOS Safari, explain "Add to Home Screen to receive notifications".
7. **Tests**
   - Put the `serviceWorker`, `PushManager` and `Notification` stubs in **`src/test/setup.ts`**, not per file (CLAUDE.md "the fix existing in 15 files").
   - Unit-test `push.ts`: enable success, permission denied, key-change re-subscribe, logout unregister.
   - Preferences tab test (body includes `push_enabled`).
   - Service-worker handler test.

## Part 2: Admin inbound tracking and PO additions (staff)

1. **New page `/procurement/inbound-tracking`** ("Inbound tracking", Procurement nav, `P.PURCHASE_ORDER_VIEW`)
   - Board over `GET /procurement/inbound-tracking`.
   - Columns: vendor, PO, category, assignee, status, expected date, vendor committed date, ready flag, latest shipment (courier/transporter, tracking, LR, shipped), days late (red), received %, GRN pending, QC pending.
   - Filter chips in the URL: Overdue, Expected today, In transit, Not acknowledged, plus vendor/category pickers and search.
   - Mobile card layout.
   - Sweeps and handbook.
2. **PO detail page**
   - Shipments panel (from `/procurement/purchase-orders/{id}/shipments`).
   - Vendor committed date, with an "Accept vendor date" action (`P.PURCHASE_ORDER_UPDATE`) shown only when it differs from the expected date.
   - VENDOR_DECLINED banner with the vendor's reason, and actions "Cancel PO" and "Re-send" (re-pick the assignee). These appear only in that status, through the machine's edges.
   - "Download PO PDF" via the job pattern (`P.PURCHASE_ORDER_VIEW`).
   - The PO list gains a "Ready" / "In transit" indicator if the summary carries it. Otherwise add the field in the backend minimally.
3. **Tests**: the inbound board's filters reach the request; PO detail actions are gated and state-dependent; accept-vendor-date body.

## Docs

- Handbook: "Vendor app" chapter (install and push steps for Android and iOS), "Inbound tracking" screen.
- `docs/security.md`: push UI notes, the no-cache SW, and that the SW never reads tokens.
- README: a short "Install as an app" note.

/**
 * Front-end error reporting.
 *
 * The error boundary used to say the error "has been logged" and then call
 * `console.error`, which logs it to a console nobody is watching. A user is told
 * their crash was recorded; it was not. This makes that sentence true.
 *
 * Deliberately transport-agnostic and dependency-free. `@sentry/react` is a
 * substantial dependency and the repository rule is not to add one without
 * justifying it; the backend takes the same line, keeping `sentry-sdk` behind an
 * optional extra and doing nothing at all when no DSN is set. So this posts a
 * small scrubbed payload to whatever endpoint `VITE_ERROR_REPORT_URL` names -
 * a Sentry tunnel, an in-house collector, or nothing, which is the default.
 *
 * What it will not send, matching `setup_sentry` in the API's observability
 * module (`send_default_pii=False`, `max_request_body_size="never"`):
 *
 *   - no request or response bodies. They carry costs, prices and customer data.
 *   - no tokens. The access token lives in memory and the refresh token in
 *     storage, and an error reporter that scooped up either would hand a third
 *     party a live session.
 *   - no query strings. Filters routinely carry item codes and customer ids.
 *   - no user email or name. The user id is enough to correlate a report with a
 *     support call, and is not itself identifying to whoever holds the endpoint.
 *
 * It also refuses to become a problem of its own: reports are rate limited and
 * de-duplicated, the transport failing is swallowed, and `sendBeacon` is used
 * where available so a crash during unload still gets out.
 */

const ENDPOINT = (import.meta.env.VITE_ERROR_REPORT_URL as string | undefined) ?? ''
const RELEASE = (import.meta.env.VITE_APP_RELEASE as string | undefined) ?? 'dev'

/** At most this many reports per session. A render loop must not become a flood. */
const MAX_REPORTS_PER_SESSION = 20
/** The same error, seen again within this window, is not sent twice. */
const DEDUPE_WINDOW_MS = 60_000

export interface ErrorContext {
  /** Where it happened: 'render', 'unhandled', 'rejection', or a feature name. */
  source: string
  /** React's component stack, when the boundary caught it. */
  componentStack?: string
  /** Anything else worth knowing that is safe to send. */
  extra?: Record<string, string | number | boolean | null>
}

let sent = 0
const recent = new Map<string, number>()
let currentUserId: string | null = null

/**
 * Associate reports with a user id - never a name or an email.
 *
 * Called from the auth provider when a session is established or cleared.
 */
export function setErrorReportingUser(userId: string | null): void {
  currentUserId = userId
}

/** Strip the query string: filters carry item codes, customer ids and search terms. */
function safePath(): string {
  try {
    return window.location.pathname
  } catch {
    return 'unknown'
  }
}

/**
 * Reduce a stack to the frames that identify the fault.
 *
 * Bounded rather than complete: a long stack is mostly framework frames, and the
 * top of it is what distinguishes one bug from another.
 */
function safeStack(error: unknown): string {
  if (!(error instanceof Error) || !error.stack) return ''
  return error.stack.split('\n').slice(0, 12).join('\n').slice(0, 4000)
}

function fingerprint(message: string, stack: string): string {
  return `${message}::${stack.split('\n')[1] ?? ''}`
}

/**
 * Report one error, if reporting is configured at all.
 *
 * Never throws and never rejects: an error reporter that can itself fail inside
 * an error handler turns one broken screen into an unrecoverable one.
 */
export function reportError(error: unknown, context: ErrorContext): void {
  // Always visible to a developer, whether or not a collector is configured.
  console.error(`[${context.source}]`, error, context.componentStack ?? '')

  if (!ENDPOINT) return
  if (sent >= MAX_REPORTS_PER_SESSION) return

  const message =
    error instanceof Error ? error.message : typeof error === 'string' ? error : 'Unknown error'
  const stack = safeStack(error)
  const key = fingerprint(message, stack)

  const now = Date.now()
  const last = recent.get(key)
  if (last !== undefined && now - last < DEDUPE_WINDOW_MS) return
  recent.set(key, now)
  sent += 1

  const payload = JSON.stringify({
    release: RELEASE,
    occurred_at: new Date(now).toISOString(),
    source: context.source,
    message: message.slice(0, 500),
    name: error instanceof Error ? error.name : 'Error',
    stack,
    component_stack: context.componentStack?.split('\n').slice(0, 12).join('\n').slice(0, 2000),
    // Path only. See the module docstring for why the query string is dropped.
    path: safePath(),
    user_id: currentUserId,
    user_agent: navigator.userAgent.slice(0, 200),
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    extra: context.extra ?? {},
  })

  try {
    // `sendBeacon` survives the page going away, which is exactly when a crash
    // report is most likely to be lost.
    if (typeof navigator.sendBeacon === 'function') {
      navigator.sendBeacon(ENDPOINT, new Blob([payload], { type: 'application/json' }))
      return
    }
    void fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true,
      // No credentials: the collector is not the API and must never receive a
      // session cookie or an Authorization header.
      credentials: 'omit',
    }).catch(() => {})
  } catch {
    // Reporting failed. That is not the user's problem and must not surface.
  }
}

/**
 * Catch what never reaches a React boundary.
 *
 * An error boundary sees render errors only. A failure in an event handler, a
 * timer or a promise goes straight past it - and those are most of them.
 */
export function installGlobalErrorReporting(): () => void {
  const onError = (event: ErrorEvent) => {
    reportError(event.error ?? event.message, {
      source: 'unhandled',
      extra: { line: event.lineno ?? 0, column: event.colno ?? 0 },
    })
  }
  const onRejection = (event: PromiseRejectionEvent) => {
    reportError(event.reason, { source: 'rejection' })
  }

  window.addEventListener('error', onError)
  window.addEventListener('unhandledrejection', onRejection)
  return () => {
    window.removeEventListener('error', onError)
    window.removeEventListener('unhandledrejection', onRejection)
  }
}

/** Whether a collector is configured. Exposed so the UI can be honest about it. */
export const errorReportingEnabled = Boolean(ENDPOINT)

/** Test seam: reset the session counters. */
export function __resetErrorReporting(): void {
  sent = 0
  recent.clear()
  currentUserId = null
}

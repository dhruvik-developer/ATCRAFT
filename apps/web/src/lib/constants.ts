/**
 * Application-wide constants.
 *
 * One place for the values that used to be repeated across feature folders:
 * a picker's page size, the search debounce, the browser-storage keys, the
 * display locale. A value that lives here is a decision that was made once;
 * a value spelled inline in six dialogs is six chances for them to disagree.
 *
 * Environment-dependent values come from `import.meta.env` and are read here
 * and nowhere else, so the full configuration surface of the bundle is visible
 * in one file (mirrors `src/core/config.py` on the API side).
 */

// ------------------------------------------------------------ environment ---

/** Base URL of the ERP API. Relative in every deployment (the deployment's own nginx routes it). */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) || '/api/v1'

/** Stamped on every crash report so a stack trace can be tied to a deployment. */
export const APP_RELEASE = (import.meta.env.VITE_APP_RELEASE as string | undefined) ?? 'dev'

/** Where crash reports are posted. Empty disables reporting, which is the default. */
export const ERROR_REPORT_URL = (import.meta.env.VITE_ERROR_REPORT_URL as string | undefined) ?? ''

export const APP_NAME = 'Atcraft Manufacturing ERP'

// ------------------------------------------------------- session & storage ---

/** Readable, non-secret cookie set by the API: the CSRF double-submit value. */
export const CSRF_COOKIE_NAME = 'atcraft_csrf'

/** Keys under which the SPA keeps per-browser preferences and markers. */
export const STORAGE_KEYS = {
  theme: 'atcraft.theme',
  sidebar: 'atcraft.sidebar',
  /** Set after sign-out so a failed logout request cannot silently restore the session. */
  sessionSuppressed: 'atcraft.session_suppressed',
} as const

// --------------------------------------------------------------- lists -----

/** Page sizes a data table offers. The API caps `limit` at 200. */
export const PAGE_SIZES = [25, 50, 100, 200] as const

/** Default rows per page on a list screen. */
export const DEFAULT_PAGE_SIZE = 50

/** Rows fetched per keystroke in a search-as-you-type picker. */
export const PICKER_PAGE_SIZE = 20

/**
 * Reference lists loaded whole (units, categories, warehouses, roles, users
 * for a select). This is the API's maximum page size; a reference list that
 * outgrows it needs a picker, not a bigger number.
 */
export const LOOKUP_LIST_LIMIT = 200

/** Delay between the last keystroke and the search request. */
export const SEARCH_DEBOUNCE_MS = 300

// ------------------------------------------------------------- selects -----

/** Radix Select forbids an empty option value, so "no filter" / "not set" need sentinels. */
export const SELECT_ALL = '__all'
export const SELECT_NONE = '__none'

// ------------------------------------------------------------- reasons -----

/** The bound every audited `reason` carries on the API: `Field(min_length=3, max_length=255)`. */
export const REASON_MIN_LENGTH = 3
export const REASON_MAX_LENGTH = 255

// ------------------------------------------------------------ polling ------

/** How often the unread-notifications badge is refreshed. */
export const NOTIFICATION_POLL_MS = 60_000

// ------------------------------------------------------------- toasts ------

/** Auto-dismiss delays. Errors do not auto-dismiss: the user may need to read a correlation id. */
export const TOAST_DURATION_MS = {
  default: 5_000,
  success: 4_000,
  warning: 7_000,
  error: 0,
  action: 10_000,
} as const

/** A failed bulk operation can raise dozens of errors; the stack is capped. */
export const MAX_VISIBLE_TOASTS = 5

// ---------------------------------------------------------- formatting -----

export const DEFAULT_LOCALE = 'en-IN'
export const DEFAULT_CURRENCY = 'INR'

/** date-fns patterns used for display and for `<input type="date">`. */
export const DATE_FORMAT = 'dd MMM yyyy'
export const DATE_TIME_FORMAT = 'dd MMM yyyy, HH:mm'
export const DATE_INPUT_FORMAT = 'yyyy-MM-dd'

// ----------------------------------------------------- error reporting -----

/** At most this many crash reports per session. A render loop must not become a flood. */
export const ERROR_REPORT_MAX_PER_SESSION = 20

/** The same error, seen again within this window, is not reported twice. */
export const ERROR_REPORT_DEDUPE_WINDOW_MS = 60_000

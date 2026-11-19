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

/** The product's full name, on the sign-in page's brand panel. */
export const APP_NAME = 'Atcraft Manufacturing ERP'
/** The name where there is room for two words: the shell, the portals, the small sign-in mark. */
export const APP_SHORT_NAME = 'Atcraft ERP'

// ------------------------------------------------------- session & storage ---

/** Readable, non-secret cookie set by the API: the CSRF double-submit value. */
export const CSRF_COOKIE_NAME = 'atcraft_csrf'

/** Keys under which the SPA keeps per-browser preferences and markers. */
export const STORAGE_KEYS = {
  theme: 'atcraft.theme',
  sidebar: 'atcraft.sidebar',
  /** Set after sign-out so a failed logout request cannot silently restore the session. */
  sessionSuppressed: 'atcraft.session_suppressed',
  /** Session storage: when a stale-chunk reload last ran, so a broken deploy cannot loop. */
  chunkReloadedAt: 'atcraft.chunk-reloaded-at',
} as const

/**
 * A failed download this soon after a stale-chunk reload is a chunk that really
 * is not being served, and is left to the error boundary rather than reloaded
 * for again (lib/stale-bundle.ts).
 */
export const CHUNK_RELOAD_LOOP_WINDOW_MS = 10_000

// --------------------------------------------------------------- lists -----

/** Maximum `limit` accepted and returned by the API's pagination contract. */
export const MAX_PAGE_SIZE = 200

/** Page sizes a data table offers. */
export const PAGE_SIZES = [25, 50, 100, MAX_PAGE_SIZE] as const

/** Default rows per page on a list screen. */
export const DEFAULT_PAGE_SIZE = 50
export const AUTH_RETRY_AFTER_SECONDS = 30

/** Recent scan-history window shown on the packing screen. */
export const CARTON_SCAN_HISTORY_DAYS = 30

/** Rows fetched per keystroke in a search-as-you-type picker. */
export const PICKER_PAGE_SIZE = 20

/** Rows fetched per cursor-paginated activity page. */
export const CURSOR_PAGE_SIZE = 20

/** Delivery attempts shown at a time on the messaging administration screen. */
export const MESSAGING_DELIVERIES_PAGE_SIZE = 20

/**
 * Reference lists loaded whole (units, categories, warehouses, roles, users
 * for a select). This is the API's maximum page size; a reference list that
 * outgrows it needs a picker, not a bigger number.
 */
export const LOOKUP_LIST_LIMIT = MAX_PAGE_SIZE

/** Delay between the last keystroke and the search request. */
export const SEARCH_DEBOUNCE_MS = 300

/**
 * List endpoints cap the `search` query parameter at 64 characters. The field
 * stops at the same limit, so a pasted paragraph cannot become a 422 and a
 * persistent error toast.
 */
export const SEARCH_MAX_LENGTH = 64

// ------------------------------------------------------------- selects -----

/** Radix Select forbids an empty option value, so "no filter" / "not set" need sentinels. */
export const SELECT_ALL = '__all'
export const SELECT_NONE = '__none'

// ------------------------------------------------------------- reasons -----

/** The bound every audited `reason` carries on the API: `Field(min_length=3, max_length=255)`. */
export const REASON_MIN_LENGTH = 3
export const REASON_MAX_LENGTH = 255

// --------------------------------------------------- product descriptions ---

/** Bounds mirrored from the D5 descriptive-product request contracts. */
export const SPECIFICATION_VALUE_MAX = 200
export const SPECIFICATION_OPTION_MAX = 100
export const SPECIFICATION_FILTER_MAX = 20
export const PRODUCT_DESCRIPTION_POSITION_MAX = 9999
export const PRODUCT_DESCRIPTION_TEXT_MAX = 2000

// ------------------------------------------------------------ assembly -----

/**
 * Bounds mirroring the assembly schemas. A priority of 100 is what a new order
 * is stored with, and what its component reservations inherit, when the
 * planner does not choose one.
 */
export const DEFAULT_ASSEMBLY_PRIORITY = 100
export const ASSEMBLY_PRIORITY_MAX = 9999
export const ASSEMBLY_LINE_MAX_LENGTH = 64
export const ASSEMBLY_NOTES_MAX_LENGTH = 2000

// ------------------------------------------------------------ polling ------

/** How often the unread-notifications badge is refreshed. */
export const NOTIFICATION_POLL_MS = 60_000

// ------------------------------------------------------- eager loading ------

/**
 * Eager loading. Right after the app shell paints, every screen the person can
 * open is downloaded in the background, and every screen downloads its forms as
 * soon as its own code has run, so no click waits on the network (lib/preload.ts).
 * Off under Vitest, where it would compile every page for each test that renders
 * the shell; a browser that asks to save data opts out at run time.
 */
export const EAGER_PRELOAD = import.meta.env.MODE !== 'test'

/** How many screen downloads the eager preload keeps in flight at once. */
export const ROUTE_PRELOAD_CONCURRENCY = 4

/** Longest an eager download waits for the browser to be idle before it starts anyway. */
export const PRELOAD_IDLE_TIMEOUT_MS = 1_000

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
/**
 * `<input type="datetime-local">`, in the browser's own zone: to the minute, or
 * to the millisecond for an input with `step="0.001"`.
 */
export const DATE_TIME_INPUT_FORMAT = "yyyy-MM-dd'T'HH:mm"
export const DATE_TIME_INPUT_PRECISE_FORMAT = "yyyy-MM-dd'T'HH:mm:ss.SSS"
/** A short weekday ("Mon"), for a column of days. */
export const WEEKDAY_FORMAT = 'EEE'

// ----------------------------------------------------- error reporting -----

/** At most this many crash reports per session. A render loop must not become a flood. */
export const ERROR_REPORT_MAX_PER_SESSION = 20

/** The same error, seen again within this window, is not reported twice. */
export const ERROR_REPORT_DEDUPE_WINDOW_MS = 60_000

/** The worker handles push only; authenticated API responses are never cached. */
export const SERVICE_WORKER_URL = '/sw.js'

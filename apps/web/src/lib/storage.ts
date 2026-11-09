/**
 * Browser storage that cannot take a screen down.
 *
 * `localStorage` and `sessionStorage` throw wherever site data is blocked - a
 * browser policy, a locked-down kiosk, some private windows - and in some
 * browsers merely reading the property throws, before any method is called.
 * AppLayout read the sidebar preference straight off `localStorage` while
 * rendering, so on such a machine every signed-in route showed the crash
 * fallback instead of just forgetting whether the sidebar was collapsed.
 *
 * Every access goes through here; `eslint.config.js` refuses the bare globals
 * anywhere else. A read that cannot happen answers like an unset key, and a
 * write that cannot happen answers `false`, for the one caller that has to
 * remember its decision another way (api-client's sign-out suppression). What
 * lives in browser storage is a per-viewer convenience: nothing here may be the
 * only record of something that must persist.
 */

export type StorageArea = 'local' | 'session'

function storageArea(area: StorageArea): Storage {
  // Inside the callers' try: the property read is what throws in some browsers.
  return area === 'local' ? window.localStorage : window.sessionStorage
}

/** The stored value, or null when the key is unset or storage is unavailable. */
export function readStorage(key: string, area: StorageArea = 'local'): string | null {
  try {
    return storageArea(area).getItem(key)
  } catch {
    return null
  }
}

/** Store a value; false when storage is unavailable or full. */
export function writeStorage(key: string, value: string, area: StorageArea = 'local'): boolean {
  try {
    storageArea(area).setItem(key, value)
    return true
  } catch {
    return false
  }
}

/** Remove a value; false when storage is unavailable. */
export function removeStorage(key: string, area: StorageArea = 'local'): boolean {
  try {
    storageArea(area).removeItem(key)
    return true
  } catch {
    return false
  }
}

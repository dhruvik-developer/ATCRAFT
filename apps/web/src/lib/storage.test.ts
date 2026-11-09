/**
 * Browser storage that answers instead of throwing.
 *
 * Where site data is blocked every Storage method throws a SecurityError, and in
 * some browsers so does reading `window.localStorage` itself. The helpers turn
 * both into the answer an unset key would give, so no caller can be taken down
 * by them.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { readStorage, removeStorage, writeStorage } from './storage'

const denied = () => {
  throw new DOMException('The operation is insecure.', 'SecurityError')
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
})

describe('browser storage', () => {
  it('reads, writes and removes in either area', () => {
    expect(writeStorage('atcraft.sidebar', 'collapsed')).toBe(true)
    expect(readStorage('atcraft.sidebar')).toBe('collapsed')
    expect(readStorage('atcraft.sidebar', 'session')).toBeNull()

    expect(writeStorage('atcraft.chunk-reloaded-at', '42', 'session')).toBe(true)
    expect(readStorage('atcraft.chunk-reloaded-at', 'session')).toBe('42')

    expect(removeStorage('atcraft.sidebar')).toBe(true)
    expect(readStorage('atcraft.sidebar')).toBeNull()
  })

  it('answers like an unset key when every method is refused', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(denied)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(denied)
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(denied)

    expect(readStorage('atcraft.sidebar')).toBeNull()
    expect(writeStorage('atcraft.sidebar', 'collapsed')).toBe(false)
    expect(removeStorage('atcraft.sidebar', 'session')).toBe(false)
  })

  it('survives a browser that refuses the property read itself', () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(denied)

    expect(readStorage('atcraft.sidebar')).toBeNull()
    expect(writeStorage('atcraft.sidebar', 'collapsed')).toBe(false)
    expect(removeStorage('atcraft.sidebar')).toBe(false)
  })
})

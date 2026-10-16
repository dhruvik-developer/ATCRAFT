import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SEARCH_DEBOUNCE_MS } from '@/lib/constants'

import { DataTable } from './data-table'

afterEach(() => vi.useRealTimers())

describe('server-controlled table search', () => {
  it('follows navigation without submitting the previous search draft again', () => {
    vi.useFakeTimers()
    const onSearchChange = vi.fn()
    const columns = [{ accessorKey: 'code', header: 'Code' }]
    const props = { columns, data: [{ code: 'HINGE' }], onSearchChange }
    const { rerender } = render(<DataTable {...props} searchValue="hinge" />)
    const search = screen.getByRole('textbox', { name: 'Search...' })

    // A pending edit must not overwrite a filter supplied by the shell or a
    // navigation to the same mounted list.
    fireEvent.change(search, { target: { value: 'hinge draft' } })
    rerender(<DataTable {...props} searchValue="lock" />)
    expect(search).toHaveValue('lock')
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS))
    expect(onSearchChange).not.toHaveBeenCalled()

    // The next deliberate edit still reaches the server after the debounce.
    fireEvent.change(search, { target: { value: 'lock body' } })
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS))
    expect(onSearchChange).toHaveBeenCalledExactlyOnceWith('lock body')
  })
})

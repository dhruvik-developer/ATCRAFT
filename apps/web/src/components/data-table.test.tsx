import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SEARCH_DEBOUNCE_MS } from '@/lib/constants'
import { ApiError } from '@/lib/api-client'
import { errors } from '@/lib/messages'

import { DataTable } from './data-table'

afterEach(() => vi.useRealTimers())

describe('keyboard table navigation', () => {
  it('lets keyboard users focus the scroll area even when rows have no actions', async () => {
    const user = userEvent.setup()
    render(
      <DataTable
        columns={[{ accessorKey: 'code', header: 'Code' }]}
        data={[{ code: 'SALES-REVENUE' }]}
      />,
    )
    const region = screen.getByRole('group', { name: 'Table results' })
    expect(region).toContainElement(screen.getByRole('table'))
    await user.tab()
    expect(screen.getByRole('button', { name: 'Columns' })).toHaveFocus()
    await user.tab()
    expect(region).toHaveFocus()
  })
})

describe('shared table errors', () => {
  const columns = [{ accessorKey: 'code', header: 'Code' }]

  it('offers a retry with safe text for an unexpected error', () => {
    const onRefresh = vi.fn()
    render(
      <DataTable
        columns={columns}
        data={[]}
        error={new TypeError('Internal credentials.token cannot be read')}
        onRefresh={onRefresh}
      />,
    )
    expect(screen.getByText(errors.unexpectedDetail)).toBeVisible()
    expect(screen.queryByText(/credentials\.token/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(onRefresh).toHaveBeenCalledOnce()
  })

  it('keeps the API message and support reference visible', () => {
    render(
      <DataTable
        columns={columns}
        data={[]}
        error={new ApiError(503, 'SERVICE_UNAVAILABLE', 'Please retry.', undefined, 'abcdef123456')}
      />,
    )
    expect(screen.getByText('Please retry. (reference abcdef12)')).toBeVisible()
  })
})

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

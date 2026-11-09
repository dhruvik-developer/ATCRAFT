import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SEARCH_DEBOUNCE_MS, SEARCH_MAX_LENGTH } from '@/lib/constants'
import { ApiError } from '@/lib/api-client'
import { empty, errors } from '@/lib/messages'

import { CellButton, DataTable } from './data-table'

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

describe('the empty table', () => {
  const columns = [{ accessorKey: 'code', header: 'Code' }]

  it('says the shared filtered sentence when a screen gives none of its own', () => {
    render(<DataTable columns={columns} data={[]} />)
    expect(screen.getByText(empty.title)).toBeVisible()
    // The wording thirty list screens already used, so a list that leaves the
    // sentence to the table reads the same as its neighbours.
    expect(screen.getByText('Nothing matches the current filters.')).toBeVisible()
    expect(empty.filtered).toBe('Nothing matches the current filters.')
  })

  it('composes a screen hint after the shared sentence, and names the control to widen', () => {
    const { rerender } = render(
      <DataTable
        columns={columns}
        data={[]}
        emptyDescription={`${empty.filtered} Clear a filter, or add the supplier.`}
      />,
    )
    expect(
      screen.getByText('Nothing matches the current filters. Clear a filter, or add the supplier.'),
    ).toBeVisible()

    rerender(
      <DataTable
        columns={columns}
        data={[]}
        emptyDescription={empty.filteredWiden('date window')}
      />,
    )
    expect(
      screen.getByText(
        'Nothing matches the current filters. Widen the date window or clear a filter.',
      ),
    ).toBeVisible()
  })
})

describe('server-controlled table search', () => {
  it('caps the term at the API query limit', () => {
    const props = {
      columns: [{ accessorKey: 'code', header: 'Code' }],
      data: [{ code: 'HINGE' }],
      onSearchChange: vi.fn(),
    }
    render(<DataTable {...props} />)

    expect(screen.getByRole('textbox', { name: 'Search...' })).toHaveAttribute(
      'maxLength',
      String(SEARCH_MAX_LENGTH),
    )
  })

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

  it('offers a clear control only while there is a term, and clearing reaches the server', () => {
    vi.useFakeTimers()
    const onSearchChange = vi.fn()
    render(
      <DataTable
        columns={[{ accessorKey: 'code', header: 'Code' }]}
        data={[{ code: 'HINGE' }]}
        onSearchChange={onSearchChange}
        searchValue="hinge"
      />,
    )
    const search = screen.getByRole('textbox', { name: 'Search...' })

    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(search).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Clear search' })).not.toBeInTheDocument()
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS))
    expect(onSearchChange).toHaveBeenCalledExactlyOnceWith('')
  })
})

describe('the pagination summary', () => {
  it('counts the total in the application locale, as the cells are', () => {
    render(
      <DataTable
        columns={[{ accessorKey: 'code', header: 'Code' }]}
        data={[{ code: 'HINGE' }]}
        pagination={{
          page: 1,
          limit: 50,
          total: 100000,
          total_pages: 2000,
          has_next: true,
          has_previous: false,
        }}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('1-1 of 1,00,000')
  })
})

describe('CellButton', () => {
  it('opens the row from the keyboard and never submits a form around the table', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => event.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        {/* A caller asking for submit still gets a plain button: a code is not a form action. */}
        <CellButton type="submit" onClick={() => onOpen('INV-0001')}>
          INV-0001
        </CellButton>
      </form>,
    )

    const code = screen.getByRole('button', { name: 'INV-0001' })
    expect(code).toHaveAttribute('type', 'button')
    expect(code).toHaveClass('font-medium', 'hover:underline', 'focus-visible:ring-2')
    code.focus()
    await user.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledWith('INV-0001')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('opens the record once when its handler stops the row click', async () => {
    const user = userEvent.setup()
    const onRowClick = vi.fn()
    const onOpen = vi.fn()
    render(
      <DataTable
        columns={[
          {
            accessorKey: 'code',
            header: 'Code',
            cell: ({ row }) => (
              <CellButton
                onClick={(event) => {
                  event.stopPropagation()
                  onOpen(row.original.code)
                }}
              >
                {row.original.code}
              </CellButton>
            ),
          },
        ]}
        data={[{ code: 'PO-0001' }]}
        onRowClick={onRowClick}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'PO-0001' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onRowClick).not.toHaveBeenCalled()
  })
})

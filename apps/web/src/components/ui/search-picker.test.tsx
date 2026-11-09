/**
 * The one search picker every server-searched field is built on.
 *
 * These pin the keyboard model (a combobox opening a search box over a listbox
 * that is a single Tab stop), where the status text is said (a live region
 * beside the listbox, never inside it), and that the read is debounced, made
 * only while open, and never mistaken for an empty answer when it fails.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { queryKeys } from '@/lib/query-client'
import { fail, page, renderScreen, type RouteBody, type RouteRequest } from '@/test/harness'

import { SearchPicker } from './search-picker'

interface Part {
  id: string
  code: string
  status: 'ACTIVE' | 'RETIRED'
}

const PARTS: Part[] = [
  { id: 'p1', code: 'HINGE-01', status: 'ACTIVE' },
  { id: 'p2', code: 'HINGE-02', status: 'RETIRED' },
  { id: 'p3', code: 'HINGE-03', status: 'ACTIVE' },
]

function PartPicker({
  initial = null,
  disabled = false,
  onTermChange,
}: {
  initial?: string | null
  disabled?: boolean
  onTermChange?: (term: string) => void
}) {
  const [value, setValue] = useState<string | null>(initial)
  return (
    <SearchPicker<Part>
      id="part"
      label="Part"
      display={PARTS.find((part) => part.id === value)?.code ?? null}
      placeholder="Any part"
      disabled={disabled}
      searchLabel="Search parts"
      searchPlaceholder="Search by code"
      query={(term) => {
        const params = {
          limit: PICKER_PAGE_SIZE,
          with_total: false,
          ...(term ? { search: term } : {}),
        }
        return {
          queryKey: queryKeys.items.list(params),
          queryFn: ({ signal }) => api.getPage<Part>('/products/items', { params, signal }),
          staleTime: 0,
        }
      }}
      getKey={(part) => part.id}
      selectedKey={value}
      isDisabled={(part) => part.status === 'RETIRED'}
      renderOption={(part) => (
        <span>
          {part.code}
          {part.status === 'RETIRED' ? ' - retired, cannot be used' : ''}
        </span>
      )}
      onSelect={(part) => setValue(part.id)}
      emptyText={(term) => (term ? 'No part matches that search.' : 'No part is on record.')}
      clear={value ? { label: 'Clear part', onClear: () => setValue(null) } : undefined}
      onTermChange={onTermChange}
    />
  )
}

const field = () => screen.getByRole('combobox', { name: 'Part' })
const search = () => screen.getByRole('textbox', { name: 'Search parts' })
const option = (code: RegExp) => screen.getByRole('option', { name: code })

function partsRoute(rows: Part[] = PARTS, more = false) {
  return { 'GET /products/items': page(rows, { has_next: more }) }
}

describe('the search picker by keyboard', () => {
  it('opens on ArrowDown, walks the list with the arrows, Home and End, and chooses with Enter', async () => {
    const { user } = renderScreen(<PartPicker />, { routes: partsRoute() })

    await user.tab()
    expect(field()).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    await waitFor(() => expect(search()).toHaveFocus())
    await screen.findByRole('option', { name: /HINGE-01/ })

    await user.keyboard('{ArrowDown}')
    expect(option(/HINGE-01/)).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(option(/HINGE-02/)).toHaveFocus()
    await user.keyboard('{End}')
    expect(option(/HINGE-03/)).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(option(/HINGE-03/), 'the last row is the end of the list').toHaveFocus()
    await user.keyboard('{Home}')
    expect(option(/HINGE-01/)).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(search(), 'ArrowUp from the first row goes back to the search').toHaveFocus()

    await user.keyboard('{ArrowUp}')
    expect(option(/HINGE-03/), 'ArrowUp from the search enters at the end').toHaveFocus()
    await user.keyboard('{Enter}')

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(field()).toHaveTextContent('HINGE-03')
    await waitFor(() => expect(field()).toHaveFocus())
  })

  it('is one Tab stop, entered at the current choice', async () => {
    const { user } = renderScreen(<PartPicker initial="p3" />, { routes: partsRoute() })

    await user.click(field())
    await screen.findByRole('option', { name: /HINGE-03/ })
    const listbox = screen.getByRole('listbox', { name: 'Part' })
    const stops = within(listbox)
      .getAllByRole('option')
      .filter((node) => node.tabIndex === 0)
    expect(stops).toEqual([option(/HINGE-03/)])
    expect(option(/HINGE-03/)).toHaveAttribute('aria-selected', 'true')
    expect(option(/HINGE-01/)).toHaveAttribute('aria-selected', 'false')

    await user.tab()
    expect(option(/HINGE-03/), 'Tab from the search lands on the choice').toHaveFocus()
    await user.click(search())
    await user.keyboard('{ArrowDown}')
    expect(option(/HINGE-03/), 'and so does ArrowDown').toHaveFocus()
  })

  it('chooses with Space, and Escape closes without choosing and gives focus back', async () => {
    const { user } = renderScreen(<PartPicker />, { routes: partsRoute() })

    await user.click(field())
    await screen.findByRole('option', { name: /HINGE-01/ })
    await user.keyboard('{ArrowDown}[Space]')
    expect(field()).toHaveTextContent('HINGE-01')

    await user.click(field())
    await screen.findByRole('option', { name: /HINGE-03/ })
    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}{Escape}')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(field(), 'Escape chose nothing').toHaveTextContent('HINGE-01')
    await waitFor(() => expect(field()).toHaveFocus())
  })

  it('reaches a row the API would refuse, says why, and does not choose it', async () => {
    const { user } = renderScreen(<PartPicker />, { routes: partsRoute() })

    await user.click(field())
    await screen.findByRole('option', { name: /HINGE-02/ })
    await user.keyboard('{ArrowDown}{ArrowDown}')
    const retired = option(/HINGE-02 - retired, cannot be used/)
    expect(retired).toHaveFocus()
    expect(retired).toHaveAttribute('aria-disabled', 'true')

    await user.keyboard('{Enter}')
    await user.click(retired)
    expect(screen.getByRole('listbox'), 'the picker stayed open').toBeVisible()
    expect(field()).toHaveTextContent('Any part')
  })

  it('sends a character typed in the list on into the search', async () => {
    const searched: (string | null)[] = []
    const terms: string[] = []
    const { user } = renderScreen(<PartPicker onTermChange={(term) => terms.push(term)} />, {
      routes: {
        'GET /products/items': ({ url }: RouteRequest) => {
          searched.push(url.searchParams.get('search'))
          return page(PARTS)
        },
      },
    })

    await user.click(field())
    await user.type(search(), 'HIN')
    // The rows answering the settled term, not the ones it replaced.
    await waitFor(() => expect(searched).toContain('HIN'))
    await screen.findByRole('option', { name: /HINGE-01/ })
    await user.keyboard('{ArrowDown}G')
    expect(search()).toHaveFocus()
    expect(search()).toHaveValue('HING')

    await waitFor(() => expect(searched).toContain('HING'))
    await screen.findByRole('option', { name: /HINGE-01/ })
    await user.keyboard('{ArrowDown}')
    expect(option(/HINGE-01/)).toHaveFocus()
    await user.keyboard('{Backspace}')
    expect(search()).toHaveFocus()
    expect(search()).toHaveValue('HIN')
    expect(terms.at(-1), 'the caller hears every keystroke').toBe('HIN')
  })
})

describe('the search picker status', () => {
  it('says it is searching in a status region, with no listbox until rows arrive', async () => {
    const { user } = renderScreen(<PartPicker />, {
      routes: { 'GET /products/items': () => new Promise<RouteBody>(() => {}) },
    })

    await user.click(field())
    expect(await screen.findByRole('status')).toHaveTextContent('Searching...')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('says the empty sentence for no rows, and the search one once a term is typed', async () => {
    const { user } = renderScreen(<PartPicker />, { routes: partsRoute([]) })

    await user.click(field())
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('No part is on record.'),
    )
    await user.type(search(), 'zz')
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('No part matches that search.'),
    )
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('says a page is not every match, beside the listbox rather than inside it', async () => {
    const { user } = renderScreen(<PartPicker />, { routes: partsRoute(PARTS, true) })

    await user.click(field())
    const status = screen.getByRole('status')
    await waitFor(() =>
      expect(status).toHaveTextContent('Showing the first 3. Type more to narrow the search.'),
    )
    expect(screen.getByRole('listbox').contains(status)).toBe(false)
    expect(within(screen.getByRole('listbox')).getAllByRole('option')).toHaveLength(3)
  })

  it('tells a screen reader how many rows answered when the page is complete', async () => {
    const { user } = renderScreen(<PartPicker />, { routes: partsRoute() })

    await user.click(field())
    await screen.findByRole('option', { name: /HINGE-01/ })
    expect(screen.getByRole('status')).toHaveTextContent('3 results')
    expect(screen.getByRole('status')).toHaveClass('sr-only')
  })

  it('shows a failed search as an alert with the API reason, never as no match', async () => {
    const { user } = renderScreen(<PartPicker />, {
      routes: { 'GET /products/items': fail(503, 'SERVICE_UNAVAILABLE', 'Item search is down') },
    })

    await user.click(field())
    expect(await screen.findByRole('alert')).toHaveTextContent('Item search is down')
    expect(screen.queryByText('No part is on record.')).not.toBeInTheDocument()
    expect(screen.getByRole('status').textContent).toBe('')
  })
})

describe('the search picker read', () => {
  it('reads nothing until it is opened, and then one debounced search per settled term', async () => {
    const searched: (string | null)[] = []
    const { user } = renderScreen(<PartPicker />, {
      routes: {
        'GET /products/items': ({ url }: RouteRequest) => {
          searched.push(url.searchParams.get('search'))
          expect(url.searchParams.get('limit')).toBe(String(PICKER_PAGE_SIZE))
          return page(PARTS)
        },
      },
    })

    await screen.findByRole('combobox', { name: 'Part' })
    expect(searched, 'a closed picker reads nothing').toEqual([])

    await user.click(field())
    await screen.findByRole('option', { name: /HINGE-01/ })
    await user.type(search(), 'HINGE-0')
    await waitFor(() => expect(searched.at(-1)).toBe('HINGE-0'))
    expect(searched[0], 'opening reads the unsearched first page').toBeNull()
    // Seven keystrokes; a picker reading per keystroke would have made eight reads.
    expect(searched.length).toBeLessThan(4)
  })

  it('cannot be opened, so reads nothing, while disabled', async () => {
    const { user, fetch } = renderScreen(<PartPicker disabled />, { routes: partsRoute() })

    expect(field()).toBeDisabled()
    await user.click(field())
    expect(screen.queryByRole('textbox', { name: 'Search parts' })).not.toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.path === '/products/items')).toEqual([])
  })

  it('clears through its own control', async () => {
    const { user } = renderScreen(<PartPicker initial="p1" />, { routes: partsRoute() })

    expect(field()).toHaveTextContent('HINGE-01')
    await user.click(screen.getByRole('button', { name: 'Clear part' }))
    expect(field()).toHaveTextContent('Any part')
    expect(screen.queryByRole('button', { name: 'Clear part' })).not.toBeInTheDocument()
  })
})

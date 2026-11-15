import { screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { page, renderScreen } from '@/test/harness'

import { cleared, useFilterWriter, useListQuery } from './use-list-query'

function IndependentLists() {
  const [params] = useSearchParams()
  const register = useListQuery({ path: '/register', queryKey: (p) => ['register', p] })
  const lines = useListQuery({
    path: '/lines',
    queryKey: (p) => ['lines', p],
    namespace: 'lines',
  })
  const orders = useListQuery({
    path: '/orders',
    queryKey: (p) => ['orders', p],
    namespace: 'orders',
  })
  return (
    <>
      <p>Register page {register.page}</p>
      <p>Lines page {lines.page}</p>
      <p>Orders page {orders.page}</p>
      <p>Page keys {[register.pageParam, lines.pageParam, orders.pageParam].join(' ')}</p>
      <output aria-label="URL parameters">{params.toString()}</output>
      <button onClick={() => lines.setPage(lines.page + 1)}>Next lines</button>
      <button onClick={() => orders.setSearch('late')}>Find orders</button>
      <button onClick={() => orders.setSort('code', 'desc')}>Sort orders</button>
      <button onClick={() => orders.setParam('status', 'RELEASED')}>Filter orders</button>
      <button onClick={() => orders.setParams({ status: undefined, q: undefined })}>
        Clear orders
      </button>
    </>
  )
}

describe('independent list URL state', () => {
  it.each([
    ['page=NaN&limit=Infinity&dir=sideways', '1', '50', 'asc'],
    ['page=0&limit=0&dir=', '1', '50', 'asc'],
    ['page=-2&limit=-5', '1', '50', 'asc'],
    ['page=1.5&limit=2.5', '1', '50', 'asc'],
    ['page=1e2&limit=2e1', '1', '50', 'asc'],
    ['page=9007199254740992&limit=201', '1', '50', 'asc'],
    ['page=&limit=', '1', '50', 'asc'],
    ['page=3&limit=20&dir=desc', '3', '20', 'desc'],
    ['page=3&limit=200&dir=asc', '3', '200', 'asc'],
  ])(
    'normalizes list URL parameters: %s',
    async (query, expectedPage, expectedLimit, expectedDir) => {
      const requests: URLSearchParams[] = []
      renderScreen(<IndependentLists />, {
        route: `/?sort=code&${query}`,
        routes: {
          'GET /register': ({ url }) => {
            requests.push(url.searchParams)
            return page([])
          },
          'GET /lines': page([]),
          'GET /orders': page([]),
        },
      })
      await waitFor(() => expect(requests).toHaveLength(1))
      expect(requests[0]?.get('page')).toBe(expectedPage)
      expect(requests[0]?.get('limit')).toBe(expectedLimit)
      expect(requests[0]?.get('sort_dir')).toBe(expectedDir)
    },
  )

  it('names the URL key each list keeps its page under', async () => {
    renderScreen(<IndependentLists />, {
      routes: { 'GET /register': page([]), 'GET /lines': page([]), 'GET /orders': page([]) },
    })
    expect(await screen.findByText('Page keys page lines_page orders_page')).toBeVisible()
  })

  it('cancels a pending list request when its screen unmounts', async () => {
    const { unmount } = renderScreen(<IndependentLists />, {
      routes: {
        'GET /register': () => new Promise(() => {}),
        'GET /lines': page([]),
        'GET /orders': page([]),
      },
    })
    await waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/register')),
      ).toBe(true)
    })
    const request = vi
      .mocked(fetch)
      .mock.calls.find(([input]) => String(input).includes('/register'))
    const signal = request?.[1]?.signal
    expect(signal).toBeDefined()
    expect(signal?.aborted).toBe(false)
    unmount()
    expect(signal?.aborted).toBe(true)
  })

  it('isolates paging, search, sorting and filters while preserving the default register', async () => {
    const requests: Record<'register' | 'lines' | 'orders', URLSearchParams[]> = {
      register: [],
      lines: [],
      orders: [],
    }
    const { user } = renderScreen(<IndependentLists />, {
      route: '/?page=3&q=register&sort=name&dir=asc&lines_page=2&orders_page=4&status=DRAFT',
      routes: Object.fromEntries(
        Object.entries(requests).map(([name, urls]) => [
          `GET /${name}`,
          ({ url }: { url: URL }) => {
            urls.push(url.searchParams)
            return page([])
          },
        ]),
      ),
    })

    await waitFor(() => {
      expect(requests.register).toHaveLength(1)
      expect(requests.lines).toHaveLength(1)
      expect(requests.orders).toHaveLength(1)
    })
    expect(requests.register[0]?.get('page')).toBe('3')
    expect(requests.register[0]?.get('search')).toBe('register')
    expect(requests.lines[0]?.get('page')).toBe('2')
    expect(requests.lines[0]?.has('search')).toBe(false)
    expect(requests.lines[0]?.has('sort_by')).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Next lines' }))
    await waitFor(() => expect(requests.lines.at(-1)?.get('page')).toBe('3'))
    expect(screen.getByText('Register page 3')).toBeVisible()
    expect(screen.getByText('Orders page 4')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Find orders' }))
    await waitFor(() => expect(requests.orders.at(-1)?.get('search')).toBe('late'))
    expect(requests.orders.at(-1)?.get('page')).toBe('1')
    await user.click(screen.getByRole('button', { name: 'Sort orders' }))
    await waitFor(() => expect(requests.orders.at(-1)?.get('sort_by')).toBe('code'))
    expect(requests.orders.at(-1)?.get('sort_dir')).toBe('desc')
    await user.click(screen.getByRole('button', { name: 'Filter orders' }))
    expect(screen.getByLabelText('URL parameters')).toHaveTextContent('orders_status=RELEASED')
    await user.click(screen.getByRole('button', { name: 'Clear orders' }))
    const finalParams = new URLSearchParams(
      screen.getByLabelText('URL parameters').textContent ?? '',
    )
    expect(finalParams.has('orders_status')).toBe(false)
    expect(finalParams.has('orders_q')).toBe(false)
    expect(finalParams.get('status')).toBe('DRAFT')
    expect(finalParams.get('q')).toBe('register')
    expect(finalParams.get('page')).toBe('3')
    expect(finalParams.get('lines_page')).toBe('3')
  })
})

describe('the rows before the first page', () => {
  it('are one array across renders, so nothing derived from them recomputes', async () => {
    // `data ?? []` handed every render a new empty array, which is a new
    // dependency for every memo and effect that reads it.
    const seen: unknown[][] = []
    function Waiting() {
      const [renders, setRenders] = useState(0)
      const list = useListQuery({
        path: '/waiting',
        queryKey: (p) => ['waiting', p],
        enabled: false,
      })
      seen.push(list.data)
      return <button onClick={() => setRenders(renders + 1)}>Render again {renders}</button>
    }
    const { user } = renderScreen(<Waiting />)

    await user.click(await screen.findByRole('button', { name: 'Render again 0' }))
    expect(await screen.findByRole('button', { name: 'Render again 1' })).toBeVisible()

    expect(seen.length).toBeGreaterThan(1)
    expect(seen[0]).toEqual([])
    for (const rows of seen) expect(rows).toBe(seen[0])
    // Shared by every list that has none, so it refuses to be filled.
    expect(Object.isFrozen(seen[0])).toBe(true)
  })
})

function Filters({ pageParam }: { pageParam?: string | null }) {
  const [params] = useSearchParams()
  const write = useFilterWriter(pageParam)
  return (
    <>
      <output aria-label="URL parameters">{params.toString()}</output>
      <button onClick={() => write({ status: 'OPEN', vendor_id: 'v-2' })}>Narrow</button>
      <button onClick={() => write(cleared(['status', 'vendor_id', 'q']))}>Clear</button>
      <button onClick={() => write({ spec: ['finish=PVD', 'size=96'] })}>Specifications</button>
      <button onClick={() => write({ spec: [] })}>No specifications</button>
      <button onClick={() => write({ status: '', page: 3 })}>Blank and number</button>
    </>
  )
}

const url = () => screen.getByLabelText('URL parameters').textContent

describe('useFilterWriter', () => {
  it('writes every key in one navigation and sends the list back to its first page', async () => {
    const { user } = renderScreen(<Filters />, { route: '/?status=DRAFT&page=4&tab=orders' })
    await user.click(screen.getByRole('button', { name: 'Narrow' }))
    expect(url()).toBe('status=OPEN&tab=orders&vendor_id=v-2')
  })

  it('clears a set of filters at once, leaving the rest of the URL alone', async () => {
    const { user } = renderScreen(<Filters />, {
      route: '/?status=OPEN&vendor_id=v-2&q=late&page=2&tab=orders',
    })
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(url()).toBe('tab=orders')
  })

  it("resets a namespaced list's own page, not another list's", async () => {
    const { user } = renderScreen(<Filters pageParam="followups_page" />, {
      route: '/?page=3&followups_page=5',
    })
    await user.click(screen.getByRole('button', { name: 'Narrow' }))
    expect(url()).toBe('page=3&status=OPEN&vendor_id=v-2')
  })

  it('keeps every page where it is when told the write narrows nothing', async () => {
    const { user } = renderScreen(<Filters pageParam={null} />, { route: '/?page=3' })
    await user.click(screen.getByRole('button', { name: 'Narrow' }))
    expect(url()).toBe('page=3&status=OPEN&vendor_id=v-2')
  })

  it('writes a multi-valued key as one entry per value, and an empty list as none', async () => {
    const { user } = renderScreen(<Filters />, { route: '/?spec=colour=BLACK&page=2' })
    await user.click(screen.getByRole('button', { name: 'Specifications' }))
    expect(url()).toBe('spec=finish%3DPVD&spec=size%3D96')
    await user.click(screen.getByRole('button', { name: 'No specifications' }))
    expect(url()).toBe('')
  })

  it('removes a blank value and writes a number as text', async () => {
    const { user } = renderScreen(<Filters pageParam={null} />, { route: '/?status=OPEN' })
    await user.click(screen.getByRole('button', { name: 'Blank and number' }))
    expect(url()).toBe('page=3')
  })
})

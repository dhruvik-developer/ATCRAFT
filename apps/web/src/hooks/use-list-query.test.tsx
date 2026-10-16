import { screen, waitFor } from '@testing-library/react'
import { useSearchParams } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { page, renderScreen } from '@/test/harness'

import { useListQuery } from './use-list-query'

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

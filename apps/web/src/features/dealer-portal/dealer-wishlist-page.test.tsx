import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { page, renderScreen } from '@/test/harness'
import type { SchemaOf } from '@/types/api'

import { DealerWishlistPage } from './dealer-order-pages'

function saved(index: number): SchemaOf<'DealerWishlistRead'> {
  return {
    id: `saved-${index}`,
    item_id: `item-${index}`,
    item_code: `HANDLE-${index}`,
    item_name: `Brass handle ${index}`,
    item_variant_id: null,
    price: '90',
    price_on_request: false,
    availability_band: 'IN_STOCK',
  }
}

describe('dealer wishlist', () => {
  // The list is paginated on the server: a dealer who has saved more than a
  // page sees the first page and a pager, never one unbounded read.
  it('reads the saved items a page at a time', async () => {
    const asked: URLSearchParams[] = []
    const firstPage = Array.from({ length: 12 }, (_, index) => saved(index + 1))
    const { user } = renderScreen(<DealerWishlistPage />, {
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/wishlist': ({ url }) => {
          asked.push(url.searchParams)
          return url.searchParams.get('page') === '2'
            ? page([saved(13)], {
                page: 2,
                limit: 12,
                total: 13,
                total_pages: 2,
                has_previous: true,
              })
            : page(firstPage, { limit: 12, total: 13, total_pages: 2, has_next: true })
        },
      },
    })

    await screen.findByText('Brass handle 1')
    expect(screen.queryByText('Brass handle 13')).toBeNull()
    expect(asked[0]?.get('limit')).toBe('12')

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await screen.findByText('Brass handle 13')
    expect(asked.at(-1)?.get('page')).toBe('2')
  })

  it('offers no pager when every saved item fits on one page', async () => {
    renderScreen(<DealerWishlistPage />, {
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: { 'GET /dealer-portal/wishlist': page([saved(1)]) },
    })

    await screen.findByText('Brass handle 1')
    expect(screen.queryByRole('navigation', { name: 'Pagination' })).toBeNull()
  })

  it('says the wishlist is empty when nothing is saved', async () => {
    renderScreen(<DealerWishlistPage />, {
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: { 'GET /dealer-portal/wishlist': page([]) },
    })

    await screen.findByText('Your wishlist is empty')
    expect(screen.queryByRole('navigation', { name: 'Pagination' })).toBeNull()
  })
})

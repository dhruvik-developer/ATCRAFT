import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'
import type { SchemaOf } from '@/types/api'

import { DealerCatalogPage, DealerItemPage } from './dealer-catalog-pages'

const item: SchemaOf<'DealerCatalogItemDetail'> = {
  id: 'item-1',
  code: 'HANDLE',
  name: 'Brass handle',
  category_id: null,
  brand_id: null,
  price: '20',
  price_on_request: false,
  orderable: true,
  availability_band: 'IN_STOCK',
  best_offer: null,
  in_wishlist: false,
  in_cart_quantity: '0',
  primary_image_id: null,
  description: 'Exterior lever handle',
  hsn_code: '8302',
  gst_rate: '18',
  can_return: true,
  variants: [],
  slabs: [],
  offers: [],
  image_ids: [],
  specifications: [
    { name: 'Fire rated', value: 'No', unit: null },
    { name: 'Backset', value: '0', unit: 'mm' },
  ],
  accessories: [
    {
      accessory_item_id: 'screws',
      code: 'SCREW-KIT',
      name: 'Fixing screw kit',
      relation: 'INCLUDED',
      quantity: '4',
      price: null,
      price_on_request: false,
      in_the_box: true,
    },
    {
      accessory_item_id: 'rose',
      code: 'ROSE',
      name: 'Matching rose',
      relation: 'OPTIONAL',
      quantity: '2.5',
      price: '10',
      price_on_request: false,
      in_the_box: false,
    },
  ],
}
const price: SchemaOf<'DealerPriceQuote'> = {
  item_id: item.id,
  variant_id: null,
  quantity: '1',
  gross_unit: '20',
  offer: null,
  discount_percentage: '0',
  net_unit: '20',
  tax_rate: '18',
  tax_estimate: '3.6',
  price_on_request: false,
}

describe('dealer product descriptions', () => {
  it.each([true, false])(
    'keeps included accessories informational and gates optional ordering (%s)',
    async (priceOnRequest) => {
      const { user, fetch } = renderScreen(<DealerItemPage />, {
        route: '/dealer/catalog/item-1',
        path: '/dealer/catalog/:itemId',
        permissions: [P.DEALER_PORTAL_BROWSE, P.DEALER_PORTAL_ORDER],
        routes: {
          'GET /dealer-portal/catalog/items/item-1': ok({
            ...item,
            accessories: item.accessories.map((row) =>
              row.in_the_box
                ? row
                : { ...row, price: priceOnRequest ? null : '10', price_on_request: priceOnRequest },
            ),
          }),
          'GET /dealer-portal/catalog/items/item-1/price': ok(price),
          'POST /dealer-portal/cart/lines': ok({}),
        },
      })
      const included = await screen.findByRole('group', { name: 'Fixing screw kit' })
      expect(within(included).getByText(/In the box/)).toBeVisible()
      expect(within(included).queryByRole('link')).toBeNull()
      expect(within(included).queryByRole('button', { name: 'Add to cart' })).toBeNull()
      expect(within(included).queryByText('Price on request')).toBeNull()
      expect(within(included).queryByText(/₹/)).toBeNull()
      expect(screen.getByText('No')).toBeVisible()
      expect(screen.getByText('0 mm')).toBeVisible()
      const optional = screen.getByRole('group', { name: 'Matching rose' })
      const add = within(optional).getByRole('button', { name: 'Add to cart' })
      expect(within(optional).getByLabelText('Quantity')).toHaveValue('2.5')
      if (priceOnRequest) {
        expect(add).toBeDisabled()
        expect(within(optional).getByText('Price on request')).toBeVisible()
      } else {
        await user.click(add)
        await waitFor(() =>
          expect(fetch.requestFor('POST', '/dealer-portal/cart/lines')).toEqual({
            item_id: 'rose',
            item_variant_id: null,
            quantity: '2.5',
          }),
        )
      }
      expect(fetch.calls.some((call) => call.path.startsWith('/products/'))).toBe(false)
    },
  )

  it('keeps optional descriptions visible without an order permission', async () => {
    renderScreen(<DealerItemPage />, {
      route: '/dealer/catalog/item-1',
      path: '/dealer/catalog/:itemId',
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/catalog/items/item-1': ok(item),
        'GET /dealer-portal/catalog/items/item-1/price': ok(price),
      },
    })
    const optional = await screen.findByRole('group', { name: 'Matching rose' })
    expect(within(optional).getByRole('link', { name: 'Matching rose' })).toHaveAttribute(
      'href',
      '/dealer/catalog/rose',
    )
    expect(within(optional).queryByRole('button', { name: 'Add to cart' })).toBeNull()
  })
})

describe('dealer specification filters', () => {
  const facets: SchemaOf<'DealerSpecificationFilterRead'>[] = [
    {
      id: 'material',
      code: 'MATERIAL',
      name: 'Material',
      value_type: 'OPTION',
      unit: null,
      options: ['Brass', 'Zinc Alloy'],
    },
    {
      id: 'fire',
      code: 'FIRE',
      name: 'Fire rated',
      value_type: 'BOOLEAN',
      unit: null,
      options: [],
    },
  ]

  it('sends repeated typed filters to the server and preserves false while resetting the product page', async () => {
    const requests: URL[] = []
    const { user, fetch } = renderScreen(<DealerCatalogPage />, {
      route: '/dealer/catalog?page=3&spec=MATERIAL%3DBrass&spec=FIRE%3Dfalse&has_accessories=true',
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/items': ({ url }) => {
          requests.push(url)
          return page([item])
        },
        'GET /dealer-portal/catalog/specifications': page(facets),
      },
    })
    const fire = await screen.findByRole('combobox', { name: 'Fire rated' })
    expect(fire).toHaveValue('false')
    expect(requests.at(-1)?.searchParams.getAll('spec')).toEqual(['MATERIAL=Brass', 'FIRE=false'])
    expect(requests.at(-1)?.searchParams.get('has_accessories')).toBe('true')
    await user.selectOptions(fire, 'true')
    await waitFor(() =>
      expect(requests.at(-1)?.searchParams.getAll('spec')).toEqual(['MATERIAL=Brass', 'FIRE=true']),
    )
    expect(requests.at(-1)?.searchParams.get('page')).toBe('1')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Material' }), '')
    await waitFor(() => expect(requests.at(-1)?.searchParams.getAll('spec')).toEqual(['FIRE=true']))
    expect(fetch.calls.some((call) => call.path.startsWith('/products/'))).toBe(false)
  })

  it('does not drop an explicitly empty filter value from a shared URL', async () => {
    let requested: URL | undefined
    renderScreen(<DealerCatalogPage />, {
      route: '/dealer/catalog?spec=MATERIAL%3D',
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/specifications': page(facets),
        'GET /dealer-portal/catalog/items': ({ url }) => {
          requested = url
          return page([])
        },
      },
    })
    await screen.findByText('No matching products')
    expect(requested?.searchParams.getAll('spec')).toEqual(['MATERIAL='])
  })

  it('pages metadata separately and can clear a retired or unavailable filter', async () => {
    let requested: URL | undefined
    const { user } = renderScreen(<DealerCatalogPage />, {
      route: '/dealer/catalog?spec=RETIRED%3Dold',
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/items': ({ url }) => {
          requested = url
          return page([])
        },
        'GET /dealer-portal/catalog/specifications': ({ url }) =>
          page(url.searchParams.get('page') === '2' ? [facets[1]!] : [facets[0]!], {
            has_next: url.searchParams.get('page') !== '2',
          }),
      },
    })
    await user.click(await screen.findByRole('button', { name: 'More filters' }))
    expect(await screen.findByRole('combobox', { name: 'Fire rated' })).toBeVisible()
    expect(requested?.searchParams.getAll('spec')).toEqual(['RETIRED=old'])
    await user.click(screen.getByRole('button', { name: 'Clear specification filters' }))
    await waitFor(() => expect(requested?.searchParams.getAll('spec')).toEqual([]))
  })

  it('refuses a twenty-first active filter without dropping the existing restrictions', async () => {
    const restrictions = Array.from({ length: 20 }, (_, index) => `spec=CODE${index}%3Dvalue`).join(
      '&',
    )
    const requests: URL[] = []
    const { user } = renderScreen(<DealerCatalogPage />, {
      route: `/dealer/catalog?${restrictions}`,
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/specifications': page(facets),
        'GET /dealer-portal/catalog/items': ({ url }) => {
          requests.push(url)
          return page([])
        },
      },
    })
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Fire rated' }), 'true')
    expect(await screen.findByText('Use at most 20 specification filters.')).toBeVisible()
    expect(requests.at(-1)?.searchParams.getAll('spec')).toHaveLength(20)
    expect(requests).toHaveLength(1)
  })
})

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'
import type { SchemaOf } from '@/types/api'

import { DealerCatalogPage } from './dealer-catalog-pages'
import { DealerAccountPage, DealerPdf, DealerReturnsPage } from './dealer-commerce-pages'
import { DealerCartPage, DealerOrderPage } from './dealer-order-pages'
import PromotionsPage from './promotions-page'

const credit: SchemaOf<'DealerCreditPosition'> = {
  credit_limit: '10000',
  credit_days: 30,
  outstanding_amount: '0',
  order_value: '0',
  exposure: '0',
  headroom: '10000',
  is_unlimited: false,
  within_limit: true,
  is_blocked: false,
  failure_reason: null,
}
const item: SchemaOf<'DealerCatalogItem'> = {
  id: 'item',
  code: 'HANDLE',
  name: 'Brass handle',
  category_id: null,
  brand_id: null,
  price: null,
  price_on_request: true,
  orderable: false,
  availability_band: 'IN_STOCK',
  best_offer: null,
  in_wishlist: false,
  in_cart_quantity: '0',
  primary_image_id: null,
}
const dealer: SchemaOf<'DealerPortalMe'> = {
  customer_id: 'customer',
  customer_code: 'D001',
  customer_name: 'Dealer One',
  customer_type: 'DEALER',
  price_list_type: 'DEALER',
  credit,
  addresses: [],
  primary_contact: null,
  sales_person: null,
}

describe('dealer credit note amounts', () => {
  it.each(['0.00', '0.01'])(
    'explains a nonzero credit rounding residual through scoped account data (%s)',
    async (roundOff) => {
      const { fetch } = renderScreen(<DealerAccountPage />, {
        permissions: [P.DEALER_PORTAL_ACCOUNT],
        routes: {
          'GET /dealer-portal/me': ok(dealer),
          'GET /dealer-portal/outstanding': ok({
            currency: 'INR',
            overdue_amount: '0',
            ageing_not_due: '0',
            ageing_total_outstanding: '0',
            overdue_invoices: [],
          }),
          'GET /dealer-portal/statement': ok({
            opening_balance: '0',
            closing_balance: '0',
            rows: [],
          }),
          'GET /dealer-portal/credit-notes': page([
            {
              id: 'credit-1',
              code: 'CN-001',
              note_date: '2026-10-25',
              status: 'ISSUED',
              currency: 'INR',
              taxable_amount: '0.98',
              tax_amount: '0.18',
              round_off_amount: roundOff,
              total_amount: '1.17',
            },
          ]),
        },
      })
      await screen.findByText('CN-001')
      if (roundOff === '0.00') expect(screen.queryByText('Round off')).toBeNull()
      else {
        expect(screen.getByText('Round off').nextElementSibling).toHaveTextContent('0.01')
        expect(screen.getByText('Tax').nextElementSibling).toHaveTextContent('0.18')
      }
      expect(fetch.calls.some((call) => call.path.startsWith('/finance/'))).toBe(false)
    },
  )
})
const cart: SchemaOf<'DealerCartRead'> = {
  cart_id: 'cart',
  cart_version: 7,
  lines: [
    {
      id: 'line',
      item_id: 'item',
      item_code: 'HANDLE',
      item_name: 'Brass handle',
      item_variant_id: null,
      quantity: '2',
      unit_price: '10',
      discount_percentage: '0',
      offer: null,
      net_amount: '20',
      tax_amount: '3.6',
      line_total: '23.6',
      price_on_request: false,
      warnings: [],
    },
  ],
  subtotal: '20',
  discount_amount: '0',
  tax_amount: '3.6',
  total_amount: '23.6',
  credit,
  warnings: [],
}
const order: SchemaOf<'DealerOrderDetail'> = {
  id: 'order',
  code: 'SO/1',
  status: 'DRAFT',
  order_date: '2026-10-24',
  required_date: '2026-11-01',
  currency: 'INR',
  total_amount: '23.6',
  source: 'DEALER_PORTAL',
  line_count: 0,
  created_at: '2026-10-24T00:00:00Z',
  customer_po_number: null,
  subtotal: '20',
  discount_amount: '0',
  tax_amount: '3.6',
  notes: null,
  lines: [],
}
const offer: SchemaOf<'DealerOfferRead'> = {
  id: 'offer',
  code: 'AUTUMN',
  title: 'Autumn offer',
  description: null,
  scope_kind: 'ALL',
  category_id: null,
  brand_id: null,
  item_id: null,
  customer_id: null,
  min_quantity: '1',
  discount_percentage: '5',
  is_active: false,
  audience_price_type: null,
  sort_order: 0,
  valid_from: null,
  valid_to: null,
  status: 'INACTIVE',
}

describe('dealer shopping and orders', () => {
  it('keeps catalog filters on the server and respects price-on-request items', async () => {
    let requested: URL | undefined
    renderScreen(<DealerCatalogPage />, {
      route:
        '/dealer/catalog?category_id=category&brand_id=brand&in_stock_only=true&on_offer_only=true&q=handle',
      permissions: [P.DEALER_PORTAL_BROWSE, P.DEALER_PORTAL_ORDER],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/items': ({ url }) => {
          requested = url
          return page([item])
        },
      },
    })
    expect(await screen.findByText('Brass handle')).toBeVisible()
    expect(screen.getByText('Price on request')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Add to cart' })).toBeDisabled()
    expect(requested?.searchParams.get('category_id')).toBe('category')
    expect(requested?.searchParams.get('brand_id')).toBe('brand')
    expect(requested?.searchParams.get('search')).toBe('handle')
    expect(requested?.searchParams.get('in_stock_only')).toBe('true')
    expect(requested?.searchParams.get('on_offer_only')).toBe('true')
  })

  it('adds an item with decimal quantity and no client pricing', async () => {
    const view = renderScreen(<DealerCatalogPage />, {
      permissions: [P.DEALER_PORTAL_BROWSE, P.DEALER_PORTAL_ORDER],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/items': page([
          { ...item, orderable: true, price_on_request: false, price: '10' },
        ]),
        'POST /dealer-portal/cart/lines': ok(cart),
      },
    })
    const quantity = await screen.findByLabelText('Quantity')
    await view.user.clear(quantity)
    await view.user.type(quantity, '1.25')
    await view.user.click(screen.getByRole('button', { name: 'Add to cart' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/dealer-portal/cart/lines')).toEqual({
        item_id: 'item',
        item_variant_id: null,
        quantity: '1.25',
      }),
    )
  })

  it('does not offer ordering to a browse-only dealer', async () => {
    renderScreen(<DealerCatalogPage />, {
      permissions: [P.DEALER_PORTAL_BROWSE],
      routes: {
        'GET /dealer-portal/catalog/categories': ok([]),
        'GET /dealer-portal/catalog/brands': ok([]),
        'GET /dealer-portal/catalog/items': page([{ ...item, orderable: true }]),
      },
    })
    await screen.findByText('Brass handle')
    expect(screen.queryByRole('button', { name: 'Add to cart' })).not.toBeInTheDocument()
  })

  it('submits checkout with the cart version and leaves prices to the API', async () => {
    const view = renderScreen(<DealerCartPage />, {
      permissions: [P.DEALER_PORTAL_ORDER],
      routes: {
        'GET /dealer-portal/cart': ok(cart),
        'GET /dealer-portal/me': ok(dealer),
        'POST /dealer-portal/checkout': ok(order),
      },
    })
    await view.user.click(await screen.findByRole('button', { name: 'Place order for review' }))
    const dialog = screen.getByRole('dialog')
    await view.user.type(within(dialog).getByLabelText('Required date'), toDateInput(new Date()))
    await view.user.click(within(dialog).getByRole('button', { name: 'Place order' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/dealer-portal/checkout')).toEqual({
        cart_version: 7,
        required_date: toDateInput(new Date()),
        shipping_address_id: null,
        billing_address_id: null,
        customer_po_number: null,
        notes: null,
      }),
    )
  })

  it('refreshes a conflicting cart and requires review before retrying checkout', async () => {
    const view = renderScreen(<DealerCartPage />, {
      permissions: [P.DEALER_PORTAL_ORDER],
      routes: {
        'GET /dealer-portal/cart': ok(cart),
        'GET /dealer-portal/me': ok(dealer),
        'POST /dealer-portal/checkout': fail(409, 'VERSION_CONFLICT', 'Cart changed'),
      },
    })
    await view.user.click(await screen.findByRole('button', { name: 'Place order for review' }))
    const dialog = screen.getByRole('dialog')
    await view.user.type(within(dialog).getByLabelText('Required date'), toDateInput(new Date()))
    await view.user.click(within(dialog).getByRole('button', { name: 'Place order' }))
    expect(await within(dialog).findByText(/Your cart changed/)).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Place order' })).toBeDisabled()
    await waitFor(() =>
      expect(
        view.fetch.calls.filter(
          (call) => call.method === 'GET' && call.path === '/dealer-portal/cart',
        ).length,
      ).toBeGreaterThan(1),
    )
    expect(
      view.fetch.calls.filter(
        (call) => call.method === 'POST' && call.path === '/dealer-portal/checkout',
      ),
    ).toHaveLength(1)
  })

  it.each(['DRAFT', 'CONFIRMED'] as const)(
    'only a DRAFT order exposes cancellation (%s)',
    async (status) => {
      renderScreen(<DealerOrderPage />, {
        route: '/dealer/orders/order',
        path: '/dealer/orders/:orderId',
        permissions: [P.DEALER_PORTAL_ORDER],
        routes: {
          'GET /dealer-portal/orders/order': ok({ ...order, status }),
          'GET /dealer-portal/orders/order/dispatches': ok([]),
        },
      })
      await screen.findByRole('heading', { name: 'SO/1' })
      expect(Boolean(screen.queryByRole('button', { name: 'Cancel order' }))).toBe(
        status === 'DRAFT',
      )
    },
  )

  it('queues a portal PDF and watches the customer-scoped job URL', async () => {
    const view = renderScreen(<DealerPdf resource="invoices/invoice" label="invoice PDF" />, {
      routes: {
        'POST /dealer-portal/invoices/invoice/pdf': ok({
          job_id: 'job',
          status: 'queued',
          status_url: '/api/v1/dealer-portal/jobs/job',
        }),
        'GET /dealer-portal/jobs/job': ok({
          id: 'job',
          report_key: 'SALES_INVOICE',
          status: 'COMPLETED',
          row_count: 1,
          download_url: '/api/v1/dealer-portal/documents/document/download',
          document_id: 'document',
        }),
      },
    })
    await view.user.click(screen.getByRole('button', { name: 'Generate invoice PDF' }))
    expect(await screen.findByText('invoice PDF is ready')).toBeVisible()
    expect(view.fetch.calls.some((call) => call.path === '/dealer-portal/jobs/job')).toBe(true)
    expect(view.fetch.calls.some((call) => call.path.startsWith('/reports/'))).toBe(false)
  })

  it('requests a return using eligible dispatch line identifiers', async () => {
    const view = renderScreen(<DealerReturnsPage />, {
      permissions: [P.DEALER_PORTAL_RETURN],
      routes: {
        'GET /dealer-portal/returns': page([]),
        'GET /dealer-portal/returns/eligible-dispatches': page([
          {
            id: 'dispatch',
            code: 'DN/1',
            dispatched_at: null,
            lines: [
              {
                dispatch_line_id: 'line',
                item_id: 'item',
                item_code: 'HANDLE',
                item_name: 'Handle',
                returnable_quantity: '2',
              },
            ],
          },
        ]),
        'POST /dealer-portal/returns': ok({ id: 'return' }),
      },
    })
    await view.user.click(screen.getByRole('button', { name: 'Request return' }))
    await view.user.click(await screen.findByRole('button', { name: /DN\/1/ }))
    await view.user.type(screen.getByLabelText('Return quantity'), '1.25')
    await view.user.type(screen.getByLabelText('Reason', { exact: true }), 'DAMAGED')
    await view.user.click(screen.getByRole('button', { name: 'Submit request' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/dealer-portal/returns')).toEqual({
        dispatch_id: 'dispatch',
        lines: [{ dispatch_line_id: 'line', quantity: '1.25', reason_code: 'DAMAGED', note: null }],
        notes: null,
      }),
    )
  })
})

describe('staff promotions', () => {
  it('keeps mutation actions hidden for a reader', async () => {
    renderScreen(<PromotionsPage />, {
      route: '/promotions?tab=offers',
      permissions: [P.PROMOTION_VIEW],
      routes: { 'GET /promotions/offers': page([offer]) },
    })
    await screen.findByText('Autumn offer')
    expect(screen.queryByRole('button', { name: 'New offer' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument()
  })

  it('saves an inactive offer with the selected audience and decimal rules', async () => {
    const view = renderScreen(<PromotionsPage />, {
      route: '/promotions?tab=offers',
      permissions: [P.PROMOTION_VIEW, P.PROMOTION_MANAGE],
      routes: { 'GET /promotions/offers': page([]), 'POST /promotions/offers': ok(offer) },
    })
    await view.user.click(await screen.findByRole('button', { name: 'New offer' }))
    await view.user.type(screen.getByLabelText('Code'), 'AUTUMN')
    await view.user.type(screen.getByLabelText('Title'), 'Autumn offer')
    await view.user.type(screen.getByLabelText('Discount %'), '5.125')
    await view.user.selectOptions(screen.getByLabelText('Audience'), 'DEALER')
    await view.user.click(screen.getByRole('button', { name: 'Save offer' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/promotions/offers')).toMatchObject({
        code: 'AUTUMN',
        title: 'Autumn offer',
        scope_kind: 'ALL',
        min_quantity: '1',
        discount_percentage: '5.125',
        audience_price_type: 'DEALER',
        item_id: null,
        category_id: null,
        brand_id: null,
        is_active: false,
      }),
    )
  })

  it('requires a reason and uses the explicit activation endpoint', async () => {
    const view = renderScreen(<PromotionsPage />, {
      route: '/promotions?tab=offers',
      permissions: [P.PROMOTION_VIEW, P.PROMOTION_MANAGE],
      routes: {
        'GET /promotions/offers': page([offer]),
        'POST /promotions/offers/offer/activate': ok({
          ...offer,
          status: 'ACTIVE',
          is_active: true,
        }),
      },
    })
    await view.user.click(await screen.findByRole('button', { name: 'Activate' }))
    const dialog = screen.getByRole('dialog')
    await view.user.click(within(dialog).getByRole('button', { name: 'Activate' }))
    expect(await within(dialog).findByText(/at least 3 characters/)).toBeVisible()
    expect(view.fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    await view.user.type(within(dialog).getByLabelText('Reason'), 'Approved campaign')
    await view.user.click(within(dialog).getByRole('button', { name: 'Activate' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/promotions/offers/offer/activate')).toEqual({
        reason: 'Approved campaign',
      }),
    )
  })
})

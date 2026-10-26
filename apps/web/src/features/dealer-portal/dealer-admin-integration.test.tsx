import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { UserFormDialog } from '@/features/admin/user-dialogs'
import UsersPage from '@/features/admin/users-page'
import SalesOrdersPage from '@/features/sales/sales-orders-page'
import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import { ItemImagesPanel } from './item-images-panel'
import PromotionsPage from './promotions-page'

const role = {
  id: 'dealer-role',
  code: 'DEALER_USER',
  name: 'Dealer buyer',
  description: null,
  is_system: false,
  is_vendor_role: false,
  is_dealer_role: true,
  is_active: true,
  permission_count: 4,
  created_at: '2026-10-24T00:00:00Z',
}

describe('dealer account administration', () => {
  it('creates a dealer-bound user with only a dealer role and searches dealer channels on the server', async () => {
    let customerPath = ''
    const view = renderScreen(<UserFormDialog open onOpenChange={vi.fn()} userId={null} />, {
      permissions: [P.USER_CREATE, P.CUSTOMER_VIEW, P.ROLE_VIEW],
      routes: {
        'GET /organization/branches': page([]),
        'GET /organization/plants': page([]),
        'GET /organization/departments': page([]),
        'GET /roles': page([
          role,
          {
            ...role,
            id: 'staff-role',
            code: 'STAFF',
            name: 'Internal staff',
            is_dealer_role: false,
          },
        ]),
        'GET /customers/dealers': ({ url }) => {
          customerPath = url.pathname
          return page([{ id: 'customer', code: 'D001', name: 'Dealer One', is_blocked: false }])
        },
        'GET /customers/customer': ok({
          id: 'customer',
          code: 'D001',
          name: 'Dealer One',
          is_blocked: false,
        }),
        'POST /users': ok({ full_name: 'Dealer Buyer', email: 'buyer@atcraft.example' }),
      },
    })
    await view.user.type(screen.getByRole('textbox', { name: 'Full name' }), 'Dealer Buyer')
    await view.user.type(screen.getByRole('textbox', { name: 'Email' }), 'buyer@atcraft.example')
    await view.user.type(screen.getByLabelText(/First password/), 'DealerPass!2026')
    await view.user.selectOptions(screen.getByLabelText('Account kind'), 'dealer')
    expect(await screen.findByText('Dealer buyer')).toBeVisible()
    expect(
      screen.queryByText('Internal staff', { selector: 'span.font-medium' }),
    ).not.toBeInTheDocument()
    await view.user.click(screen.getByRole('combobox', { name: 'Dealer portal account' }))
    await view.user.click(await screen.findByRole('option', { name: /D001.*Dealer One/ }))
    await view.user.click(screen.getByRole('checkbox', { name: /Dealer buyer/ }))
    await view.user.click(screen.getByRole('button', { name: 'Create user' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/users')).toMatchObject({
        customer_id: 'customer',
        vendor_id: null,
        role_ids: ['dealer-role'],
        allowed_category_ids: [],
      }),
    )
    expect(customerPath).toContain('/customers/dealers')
  })

  it('filters dealer portal accounts by customer rather than filtering a downloaded user page', async () => {
    let filters: URLSearchParams | undefined
    renderScreen(<UsersPage />, {
      route: '/admin/users?account_type=dealer&customer_id=customer',
      permissions: [P.USER_VIEW],
      routes: {
        'GET /organization/branches': page([]),
        'GET /organization/plants': page([]),
        'GET /users': ({ url }) => {
          filters = url.searchParams
          return page([])
        },
      },
    })
    await screen.findByText('No users')
    expect(filters?.get('is_dealer_user')).toBe('true')
    expect(filters?.get('customer_id')).toBe('customer')
  })

  it('passes the sales source filter to the API', async () => {
    let filters: URLSearchParams | undefined
    renderScreen(<SalesOrdersPage />, {
      route: '/sales-orders?source=DEALER_PORTAL',
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        'GET /sales': ({ url }) => {
          filters = url.searchParams
          return page([])
        },
      },
    })
    await screen.findByText('No sales orders found')
    expect(filters?.get('source')).toBe('DEALER_PORTAL')
  })
})

describe('catalog images and promotion scope', () => {
  it('uploads an item-bound attachment before assigning the primary image', async () => {
    const view = renderScreen(<ItemImagesPanel itemId="item" />, {
      permissions: [P.ITEM_UPDATE, P.DOCUMENT_UPLOAD, P.DOCUMENT_VIEW],
      routes: {
        'GET /documents': page([]),
        'POST /documents': ok({ id: 'image', mime_type: 'image/png' }),
        'PUT /products/items/item/image': ok(null),
      },
    })
    await view.user.upload(
      await screen.findByLabelText('Upload image'),
      new File(['png'], 'handle.png', { type: 'image/png' }),
    )
    await view.user.click(screen.getByRole('button', { name: 'Upload and set primary' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('PUT', '/products/items/item/image')).toEqual({
        document_id: 'image',
      }),
    )
    const upload = vi
      .mocked(globalThis.fetch)
      .mock.calls.find(([, options]) => options?.method === 'POST')?.[1]?.body as FormData
    expect(upload.get('entity_type')).toBe('item')
    expect(upload.get('entity_id')).toBe('item')
    expect(upload.get('category')).toBe('ATTACHMENT')
    expect(upload.get('file')).toBeInstanceOf(File)
  })

  it('removes only the primary association while preserving the uploaded document', async () => {
    const view = renderScreen(<ItemImagesPanel itemId="item" primaryImageId="image" />, {
      permissions: [P.ITEM_UPDATE],
      routes: { 'PUT /products/items/item/image': ok(null) },
    })
    await view.user.click(await screen.findByRole('button', { name: 'Remove primary image' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('PUT', '/products/items/item/image')).toEqual({
        document_id: null,
      }),
    )
    expect(view.fetch.calls.some((call) => call.method === 'DELETE')).toBe(false)
  })

  it('refuses a nonimage upload before making a request', async () => {
    const view = renderScreen(<ItemImagesPanel itemId="item" />, {
      permissions: [P.ITEM_UPDATE, P.DOCUMENT_UPLOAD],
      routes: {},
    })
    await view.user.click(await screen.findByRole('button', { name: 'Upload and set primary' }))
    expect(await screen.findByText('Choose an image to upload.')).toBeVisible()
    expect(view.fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('previews an offer through its bounded server projection', async () => {
    const view = renderScreen(<PromotionsPage />, {
      route: '/promotions?tab=offers',
      permissions: [P.PROMOTION_VIEW],
      routes: {
        'GET /promotions/offers': page([
          {
            ...role,
            id: 'offer',
            code: 'AUTUMN',
            title: 'Autumn offer',
            status: 'INACTIVE',
            discount_percentage: '5',
            valid_from: null,
            valid_to: null,
          },
        ]),
        'GET /promotions/offers/offer/preview': ok({ item_count: 17 }),
      },
    })
    await view.user.click(await screen.findByRole('button', { name: 'Preview scope' }))
    expect(await screen.findByText('Applies to 17 products')).toBeVisible()
  })

  it('uploads a banner before saving its inactive content', async () => {
    const view = renderScreen(<PromotionsPage />, {
      permissions: [P.PROMOTION_VIEW, P.PROMOTION_MANAGE, P.DOCUMENT_UPLOAD],
      routes: {
        'GET /promotions/banners': page([]),
        'POST /documents': ok({ id: 'image' }),
        'POST /promotions/banners': ok({ id: 'banner' }),
      },
    })
    await view.user.click(await screen.findByRole('button', { name: 'New banner' }))
    const dialog = screen.getByRole('dialog')
    await view.user.type(within(dialog).getByLabelText('Code'), 'BANNER-1')
    await view.user.type(within(dialog).getByLabelText('Title'), 'New collection')
    await view.user.upload(
      within(dialog).getByLabelText('Banner image'),
      new File(['png'], 'banner.png', { type: 'image/png' }),
    )
    await view.user.click(within(dialog).getByRole('button', { name: 'Save banner' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/promotions/banners')).toMatchObject({
        image_document_id: 'image',
        link_kind: 'NONE',
        link_id: null,
        status: 'INACTIVE',
      }),
    )
    const posts = view.fetch.calls.filter((call) => call.method === 'POST').map((call) => call.path)
    expect(posts).toEqual(['/documents', '/promotions/banners'])
  })
})

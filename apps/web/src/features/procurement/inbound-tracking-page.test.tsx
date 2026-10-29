import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteRequest } from '@/test/harness'
import type { SchemaOf } from '@/types/api'

import InboundTrackingPage from './inbound-tracking-page'
import { InboundDocumentDialog } from './inbound-document-dialog'
import { InboundShipmentDialog } from './inbound-shipment-dialog'
import { PurchaseOrderInboundPanel } from './purchase-order-inbound-panel'

const ID = '01a00000-0000-7000-8000-000000000111'
const SHIPMENT = '01a00000-0000-7000-8000-000000000222'
const BASE = `/procurement/purchase-orders/${ID}`
const order = {
  id: ID,
  code: 'PO-0042',
  status: 'ACKNOWLEDGED' as const,
  currency: 'INR',
  expected_delivery_date: '2026-10-25',
  vendor_committed_date: '2026-10-27',
  acknowledged_at: '2026-10-20T09:00:00Z',
  ready_for_dispatch_at: null,
  ready_packages: null,
  vendor_decline_reason: null,
}
const shipment: SchemaOf<'InboundShipmentRead'> = {
  id: SHIPMENT,
  purchase_order_id: ID,
  shipment_number: 2,
  transporter_id: null,
  courier_name: 'Safe Transport',
  tracking_reference: 'TRACK-42',
  lr_number: 'LR-42',
  vehicle_number: 'MH12AB1234',
  driver_name: 'Driver',
  driver_mobile: '1234567890',
  shipped_at: '2026-10-20T08:30:31.125Z',
  expected_delivery_date: '2026-10-25',
  delivered_at: null,
  packages: 2,
  weight_kg: '9.5000',
  freight_amount: '125.50',
  notes: 'Keep upright',
  created_at: '2026-10-20T08:30:00Z',
}
const related = { [`GET ${BASE}/shipments`]: page([shipment]), [`GET ${BASE}/documents`]: page([]) }

describe('staff inbound tracking', () => {
  it('sends bookmarked flags, category, vendor, search and paging to the server', async () => {
    const calls: URL[] = []
    const { user } = renderScreen(<InboundTrackingPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW],
      route:
        '/procurement/inbound-tracking?overdue=true&category_id=cat1&vendor_id=vendor1&q=PO&limit=25',
      routes: {
        'GET /procurement/inbound-tracking': ({ url }) => {
          calls.push(url)
          return page([], {
            page: Number(url.searchParams.get('page')),
            limit: 25,
            has_next: true,
            total: 60,
            total_pages: 3,
          })
        },
      },
    })
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    expect(calls[0]?.searchParams.get('category_id')).toBe('cat1')
    expect(calls[0]?.searchParams.get('vendor_id')).toBe('vendor1')
    expect(calls[0]?.searchParams.get('overdue')).toBe('true')
    expect(calls[0]?.searchParams.get('search')).toBe('PO')
    const nextPage = await screen.findByRole('button', { name: 'Next page' })
    await waitFor(() => expect(nextPage).toBeEnabled())
    await user.click(nextPage)
    await waitFor(() => expect(calls.at(-1)?.searchParams.get('page')).toBe('2'))
    await user.click(screen.getByRole('checkbox', { name: 'Include completed' }))
    await waitFor(() => expect(calls.at(-1)?.searchParams.get('include_completed')).toBe('true'))
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(calls.at(-1)?.searchParams.has('category_id')).toBe(false))
    expect(calls.at(-1)?.searchParams.has('overdue')).toBe(false)
    expect(calls.at(-1)?.searchParams.get('search')).toBe('PO')
    expect(screen.queryByRole('combobox', { name: /category/i })).not.toBeInTheDocument()
  })

  it('shows API failures with retry rather than reporting an empty board', async () => {
    renderScreen(<InboundTrackingPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW],
      routes: {
        'GET /procurement/inbound-tracking': fail(
          422,
          'VALIDATION_ERROR',
          'Inbound service unavailable',
        ),
      },
    })
    expect(await screen.findByText('Inbound service unavailable')).toBeVisible()
    expect(screen.queryByText('No inbound orders found')).not.toBeInTheDocument()
  })
})

describe('purchase order inbound controls', () => {
  it('requires update/receiving/upload permissions for mutations', async () => {
    renderScreen(<PurchaseOrderInboundPanel order={order} />, {
      permissions: [P.PURCHASE_ORDER_VIEW],
      routes: related,
    })
    expect(await screen.findByText('Safe Transport')).toBeVisible()
    for (const name of [
      'Adopt vendor date',
      'Add shipment',
      'Upload inbound document',
      'Edit shipment 2',
      'Mark shipment 2 delivered',
    ])
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate purchase order PDF' })).toBeVisible()
  })

  it('confirms vendor date adoption and gate arrival through distinct APIs', async () => {
    const adopt = vi.fn(() => ok(order))
    const deliver = vi.fn(() => ok({ ...shipment, delivered_at: '2026-10-24T09:00:00Z' }))
    const { user } = renderScreen(<PurchaseOrderInboundPanel order={order} />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_UPDATE, P.GRN_CREATE],
      routes: {
        ...related,
        [`POST ${BASE}/accept-vendor-date`]: adopt,
        [`POST ${BASE}/shipments/${SHIPMENT}/delivered`]: deliver,
      },
    })
    await user.click(await screen.findByRole('button', { name: 'Adopt vendor date' }))
    expect(adopt).not.toHaveBeenCalled()
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Adopt date' }))
    await waitFor(() => expect(adopt).toHaveBeenCalledOnce())
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Mark shipment 2 delivered' }))
    expect(deliver).not.toHaveBeenCalled()
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Mark delivered' }),
    )
    await waitFor(() => expect(deliver).toHaveBeenCalledOnce())
  })

  it('explains a vendor decline while shipment creation stays unavailable', async () => {
    renderScreen(
      <PurchaseOrderInboundPanel
        order={{
          ...order,
          status: 'VENDOR_DECLINED',
          vendor_decline_reason: 'Capacity unavailable',
        }}
      />,
      { permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_UPDATE], routes: related },
    )
    expect(await screen.findByText(/Vendor declined: Capacity unavailable/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add shipment' })).not.toBeInTheDocument()
  })

  it('keeps all consignment details and timestamp when correcting a shipment', async () => {
    const patch = vi.fn((_request: RouteRequest) => ok(shipment))
    const { user } = renderScreen(
      <InboundShipmentDialog order={order} shipment={shipment} scope="staff" onClose={vi.fn()} />,
      {
        permissions: [P.PURCHASE_ORDER_UPDATE],
        routes: { [`PATCH ${BASE}/shipments/${SHIPMENT}`]: patch },
      },
    )
    const courier = await screen.findByLabelText('Courier name')
    fireEvent.change(courier, { target: { value: 'Corrected Transport' } })
    await user.click(screen.getByRole('button', { name: 'Save shipment' }))
    await waitFor(() => expect(patch).toHaveBeenCalledOnce())
    expect(patch.mock.calls[0]?.[0]).toMatchObject({
      body: {
        courier_name: 'Corrected Transport',
        shipped_at: new Date(shipment.shipped_at).toISOString(),
        freight_amount: '125.50',
        weight_kg: '9.5000',
        packages: 2,
        lr_number: 'LR-42',
        notes: 'Keep upright',
      },
    })
  })

  it('validates missing dispatch details without posting an invalid shipment', async () => {
    const create = vi.fn(() => ok(shipment))
    const { user } = renderScreen(
      <InboundShipmentDialog order={order} scope="staff" onClose={vi.fn()} />,
      { permissions: [P.PURCHASE_ORDER_UPDATE], routes: { [`POST ${BASE}/shipments`]: create } },
    )
    await user.click(await screen.findByRole('button', { name: 'Record shipment' }))
    expect(await screen.findByText('Choose a transporter or enter the courier name')).toBeVisible()
    expect(create).not.toHaveBeenCalled()
  })

  it('loads shipment pages for typed staff attachments without relying on the visible order page', async () => {
    const requests: URL[] = []
    const { user } = renderScreen(
      <InboundDocumentDialog
        scope="staff"
        order={{
          id: ID,
          code: order.code,
          shipments: [],
          shipments_pagination: { limit: 50, has_next: false, has_previous: false },
        }}
        canShip
        canInvoice
        onClose={vi.fn()}
      />,
      {
        permissions: [P.DOCUMENT_UPLOAD, P.PURCHASE_ORDER_VIEW],
        routes: {
          [`GET ${BASE}/shipments`]: ({ url }) => {
            requests.push(url)
            return page([{ ...shipment, shipment_number: Number(url.searchParams.get('page')) }], {
              has_next: url.searchParams.get('page') === '1',
              has_previous: url.searchParams.get('page') !== '1',
            })
          },
        },
      },
    )
    expect(await screen.findByRole('option', { name: 'Shipment 1' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'More shipments' }))
    expect(await screen.findByRole('option', { name: 'Shipment 2' })).toBeVisible()
    expect(requests.map((url) => url.searchParams.get('page'))).toEqual(['1', '2'])
    expect(screen.getByRole('option', { name: 'INVOICE' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'LR COPY' })).toBeInTheDocument()
  })

  it('queues the existing PDF job and reports a failed generation', async () => {
    const queued = vi.fn(() =>
      ok({ job_id: SHIPMENT, status: 'QUEUED', status_url: `/api/v1/reports/jobs/${SHIPMENT}` }),
    )
    const { user } = renderScreen(<PurchaseOrderInboundPanel order={order} />, {
      permissions: [P.PURCHASE_ORDER_VIEW],
      routes: {
        ...related,
        [`POST ${BASE}/pdf`]: queued,
        [`GET /reports/jobs/${SHIPMENT}`]: ok({
          id: SHIPMENT,
          report_key: 'purchase-order',
          status: 'FAILED',
          parameters: null,
          row_count: 0,
          generated_at: null,
          expires_at: null,
          document_id: null,
          download_url: null,
          error_message: 'PDF generation could not complete',
          created_at: '2026-10-24T00:00:00Z',
        }),
      },
    })
    await user.click(await screen.findByRole('button', { name: 'Generate purchase order PDF' }))
    expect(await screen.findByText('PDF generation could not complete')).toBeVisible()
    expect(queued).toHaveBeenCalledOnce()
  })
})

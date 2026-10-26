import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import { PackingOrderCreateDialog } from './packing-order-create-dialog'
import type { PackingOrderDetail } from './packing-types'
import type { SalesOrderDetail } from '@/features/sales/sales-types'

const ITEM_ID = '01a00000-0000-7000-8000-0000000000i1'
const CUSTOMER_ID = '01a00000-0000-7000-8000-0000000000c1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-0000000000w1'
const UOM_ID = '01a00000-0000-7000-8000-0000000000u1'
const SALES_ORDER_ID = '01a00000-0000-7000-8000-0000000000s1'
const SALES_LINE_ID = '01a00000-0000-7000-8000-0000000000l1'
const PERMISSIONS = [
  P.PACKING_MANAGE,
  P.PACKING_VIEW,
  P.ITEM_VIEW,
  P.CUSTOMER_VIEW,
  P.WAREHOUSE_VIEW,
]

const customer = { id: CUSTOMER_ID, code: 'C-0001', name: 'Acme Hardware' }
const warehouse = { id: WAREHOUSE_ID, code: 'MAIN', name: 'Main warehouse' }
const item = {
  id: ITEM_ID,
  code: 'LOCK-01',
  name: 'Mortise lock',
  base_uom_id: UOM_ID,
}

function createdOrder(salesOrderId: string | null = null): PackingOrderDetail {
  return {
    id: '01a00000-0000-7000-8000-0000000000p1',
    code: 'PK-0001',
    sales_order_id: salesOrderId,
    customer_id: CUSTOMER_ID,
    warehouse_id: WAREHOUSE_ID,
    status: 'DRAFT',
    planned_date: '2026-10-24',
    packed_date: null,
    packed_by_id: null,
    total_cartons: 0,
    total_gross_weight: '0.0000',
    total_net_weight: '0.0000',
    dispatch_id: null,
    handed_over_at: null,
    notes: null,
    version: 1,
    lines: [
      {
        id: '01a00000-0000-7000-8000-0000000000l2',
        packing_order_id: '01a00000-0000-7000-8000-0000000000p1',
        line_number: 1,
        sales_order_line_id: salesOrderId ? SALES_LINE_ID : null,
        item_id: ITEM_ID,
        quantity_to_pack: '2.0000',
        packed_quantity: '0.0000',
        uom_id: UOM_ID,
        is_batch_tracked: false,
        is_serial_tracked: false,
        requires_final_qc: false,
      },
    ],
  }
}

const salesOrder: SalesOrderDetail = {
  id: SALES_ORDER_ID,
  code: 'SO-0001',
  status: 'CONFIRMED',
  customer_id: CUSTOMER_ID,
  warehouse_id: WAREHOUSE_ID,
  source: 'MANUAL',
  order_date: '2026-10-01',
  required_date: '2026-10-30',
  promised_date: null,
  customer_po_number: null,
  branch_id: null,
  shipping_address_id: null,
  billing_address_id: null,
  transporter_id: null,
  place_of_supply_state_code: null,
  currency: 'INR',
  is_interstate: false,
  priority: 5,
  is_urgent: false,
  requires_approval: false,
  approved_by_id: null,
  approved_at: null,
  cancellation_reason: null,
  notes: null,
  subtotal: '100.0000',
  discount_amount: '0.0000',
  tax_amount: '18.0000',
  total_amount: '118.0000',
  version: 1,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  lines: [
    {
      id: SALES_LINE_ID,
      sales_order_id: SALES_ORDER_ID,
      line_number: 1,
      item_id: ITEM_ID,
      item_variant_id: null,
      description: 'Mortise lock',
      quantity: '10.0000',
      uom_id: UOM_ID,
      unit_price: '10.000000',
      discount_percentage: '0.0000',
      discount_amount: '0.0000',
      tax_rate: '18.0000',
      cgst_amount: '9.0000',
      sgst_amount: '9.0000',
      igst_amount: '0.0000',
      tax_amount: '18.0000',
      line_total: '118.0000',
      allocated_quantity: '7.0000',
      dispatched_quantity: '3.0000',
      invoiced_quantity: '0.0000',
      reservation_id: null,
      offer_id: null,
      required_date: null,
      notes: null,
      status: 'PARTIALLY_DISPATCHED',
    },
  ],
}

function commonRoutes() {
  return {
    'GET /customers': page([customer]),
    [`GET /customers/${CUSTOMER_ID}`]: ok(customer),
    'GET /warehouses': page([warehouse]),
    'GET /products/items': page([item]),
    [`GET /products/items/${ITEM_ID}`]: ok(item),
  }
}

async function chooseItem(user: ReturnType<typeof renderScreen>['user'], dialog: HTMLElement) {
  await user.click(within(dialog).getByRole('combobox', { name: 'Item 1' }))
  await screen.findByRole('option', { name: /LOCK-01/ })
  await user.click(screen.getByRole('option', { name: /LOCK-01/ }))
}

describe('raising packing orders', () => {
  it('creates a standalone packing order with the selected item base unit', async () => {
    const { user, fetch } = renderScreen(
      <PackingOrderCreateDialog open onOpenChange={vi.fn()} onCreated={vi.fn()} />,
      {
        permissions: PERMISSIONS,
        routes: {
          ...commonRoutes(),
          'POST /packing': ok(createdOrder()),
        },
      },
    )
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Customer' }))
    await screen.findByRole('option', { name: /C-0001/ })
    await user.click(screen.getByRole('option', { name: /C-0001/ }))

    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN - Main warehouse/ }))
    await chooseItem(user, dialog)
    await user.type(within(dialog).getByLabelText('Quantity to pack'), '2')
    await waitFor(() =>
      expect(fetch.calls.some((call) => call.path === `/products/items/${ITEM_ID}`)).toBe(true),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Raise packing order' }))

    await waitFor(() => {
      const body = fetch.requestFor('POST', '/packing') as {
        customer_id: string
        warehouse_id: string
        planned_date: string
        lines: { item_id: string; quantity_to_pack: string; uom_id: string }[]
      }
      expect(body.customer_id).toBe(CUSTOMER_ID)
      expect(body.warehouse_id).toBe(WAREHOUSE_ID)
      expect(body.planned_date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
      expect(body.lines).toEqual([{ item_id: ITEM_ID, quantity_to_pack: '2', uom_id: UOM_ID }])
    })
  })

  it('creates from a sales order using only undelivered quantities and preserves line links', async () => {
    const { user, fetch } = renderScreen(
      <PackingOrderCreateDialog
        open
        salesOrderId={SALES_ORDER_ID}
        onOpenChange={vi.fn()}
        onCreated={vi.fn()}
      />,
      {
        permissions: PERMISSIONS,
        routes: {
          ...commonRoutes(),
          [`GET /sales/${SALES_ORDER_ID}`]: ok(salesOrder),
          'POST /packing': ok(createdOrder(SALES_ORDER_ID)),
        },
      },
    )
    const dialog = await screen.findByRole('dialog')
    expect(await screen.findByText(/SO-0001: only lines/)).toBeVisible()
    expect(await within(dialog).findByLabelText('Quantity to pack')).toHaveValue('7')

    await user.click(within(dialog).getByRole('button', { name: 'Raise packing order' }))

    await waitFor(() => {
      const body = fetch.requestFor('POST', '/packing') as {
        sales_order_id: string
        customer_id: string
        warehouse_id: string
        lines: {
          item_id: string
          quantity_to_pack: string
          uom_id: string
          sales_order_line_id: string
        }[]
      }
      expect(body.sales_order_id).toBe(SALES_ORDER_ID)
      expect(body.customer_id).toBe(CUSTOMER_ID)
      expect(body.warehouse_id).toBe(WAREHOUSE_ID)
      expect(body.lines).toEqual([
        {
          item_id: ITEM_ID,
          quantity_to_pack: '7',
          uom_id: UOM_ID,
          sales_order_line_id: SALES_LINE_ID,
        },
      ])
    })
  })

  it('rejects quantities above the undelivered sales-order balance before posting', async () => {
    const { user, fetch } = renderScreen(
      <PackingOrderCreateDialog
        open
        salesOrderId={SALES_ORDER_ID}
        onOpenChange={vi.fn()}
        onCreated={vi.fn()}
      />,
      {
        permissions: PERMISSIONS,
        routes: {
          ...commonRoutes(),
          [`GET /sales/${SALES_ORDER_ID}`]: ok(salesOrder),
          'POST /packing': ok(createdOrder(SALES_ORDER_ID)),
        },
      },
    )
    const dialog = await screen.findByRole('dialog')
    const quantity = await within(dialog).findByLabelText('Quantity to pack')
    await user.clear(quantity)
    await user.type(quantity, '8')
    await user.click(within(dialog).getByRole('button', { name: 'Raise packing order' }))

    expect(await screen.findByText('Cannot exceed 7 still undelivered.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST' && call.path === '/packing')).toBe(
      false,
    )
  })

  it('shows API errors instead of closing as if creation succeeded', async () => {
    const { user } = renderScreen(
      <PackingOrderCreateDialog open onOpenChange={vi.fn()} onCreated={vi.fn()} />,
      {
        permissions: PERMISSIONS,
        routes: {
          ...commonRoutes(),
          'POST /packing': fail(409, 'conflict', 'The customer is blocked.'),
        },
      },
    )
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Customer' }))
    await screen.findByRole('option', { name: /C-0001/ })
    await user.click(screen.getByRole('option', { name: /C-0001/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN - Main warehouse/ }))
    await chooseItem(user, dialog)
    await user.type(within(dialog).getByLabelText('Quantity to pack'), '2')
    await screen.findByText('Base unit')
    await user.click(within(dialog).getByRole('button', { name: 'Raise packing order' }))

    expect(await within(dialog).findByText('The customer is blocked.')).toBeVisible()
    expect(dialog).toBeVisible()
  })
})

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SalesOrderDetail } from '@/features/sales/sales-types'
import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'
import { CreateDispatchDialog } from './dispatch-dialogs'

const order: SalesOrderDetail = {
  id: 'order',
  code: 'SO-0001',
  customer_id: 'customer',
  warehouse_id: 'warehouse',
  branch_id: null,
  status: 'CONFIRMED',
  source: 'ERP',
  order_date: '2026-10-12',
  required_date: '2026-11-01',
  promised_date: null,
  priority: 5,
  is_urgent: false,
  currency: 'INR',
  subtotal: '5000',
  discount_amount: '0',
  tax_amount: '900',
  total_amount: '5900',
  customer_po_number: null,
  place_of_supply_state_code: '27',
  is_interstate: false,
  requires_approval: false,
  approved_at: null,
  approved_by_id: null,
  version: 1,
  created_at: '2026-10-12T06:00:00Z',
  updated_at: '2026-10-12T06:00:00Z',
  billing_address_id: null,
  shipping_address_id: null,
  transporter_id: null,
  cancellation_reason: null,
  notes: null,
  lines: [],
}

describe('reserved order dispatch linkage', () => {
  it('prefills dispatchable order lines and sends sales_order_line_id', async () => {
    const { user, fetch } = renderScreen(
      <CreateDispatchDialog open onOpenChange={() => {}} order={order} />,
      {
        permissions: [P.DISPATCH_CREATE, P.SALES_ORDER_VIEW, P.CUSTOMER_VIEW, P.WAREHOUSE_VIEW],
        routes: {
          'GET /sales': page([order]),
          'GET /sales/order/allocation-status': ok({
            order_id: 'order',
            code: 'SO-0001',
            status: 'CONFIRMED',
            warehouse_id: 'warehouse',
            fully_allocated: false,
            total_shortfall_lines: 1,
            lines: [
              {
                line_id: 'order-line',
                line_number: 1,
                item_id: 'item',
                item_code: 'FG-HINGE',
                ordered_quantity: '500',
                allocated_quantity: '200',
                reserved_quantity: '200',
                dispatchable_quantity: '200',
                dispatched_quantity: '0',
                shortfall_quantity: '300',
                status: 'PARTIALLY_ALLOCATED',
                reservation_id: 'reservation',
              },
            ],
          }),
          'GET /customers': page([
            { id: 'customer', name: 'Hardware Dealer', code: 'C1', is_active: true },
          ]),
          'GET /customers/customer': ok({
            id: 'customer',
            name: 'Hardware Dealer',
            code: 'C1',
            is_active: true,
          }),
          'GET /warehouses': page([
            { id: 'warehouse', code: 'WH-MAIN', name: 'Main store', is_active: true },
          ]),
          'POST /dispatch': ok({ id: 'dispatch', code: 'DISP/2026-27/00001' }),
        },
      },
    )
    await screen.findByText(/FG-HINGE/)
    expect(screen.queryByRole('button', { name: 'Add line' })).not.toBeInTheDocument()
    const quantity = screen.getByLabelText('Quantity for line 1')
    await user.clear(quantity)
    await user.type(quantity, '150')
    await user.type(screen.getByLabelText('Vehicle number'), 'MH12AB1234')
    await user.click(screen.getByRole('button', { name: 'Create dispatch' }))
    await expect
      .poll(() => fetch.requestFor('POST', '/dispatch'))
      .toMatchObject({
        sales_order_id: 'order',
        warehouse_id: 'warehouse',
        lines: [
          {
            item_id: 'item',
            sales_order_line_id: 'order-line',
            quantity: '150',
            serial_ids: [],
            carton_ids: [],
          },
        ],
      })
  })
})

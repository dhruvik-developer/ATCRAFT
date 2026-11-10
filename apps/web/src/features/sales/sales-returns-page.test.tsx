import type { QueryClient, QueryKey } from '@tanstack/react-query'
import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { messages } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import type { DispatchDetail, DispatchRow } from '@/features/dispatch/dispatch-types'
import { financeKeys } from '@/features/finance/finance-types'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'
import type { SchemaOf } from '@/types/api'

import SalesReturnsPage, { type SalesReturnRow } from './sales-returns-page'

type SalesReturnLine = SchemaOf<'SalesReturnLineRead'>

function salesReturnRow(overrides: Partial<SalesReturnRow> = {}): SalesReturnRow {
  return {
    id: 'sr-1',
    code: 'SR/2026-001',
    dispatch_id: 'dispatch-1',
    dispatch_code: 'DSP-0001',
    customer_id: 'customer-1',
    customer_code: 'C-0042',
    customer_name: 'Acme Hardware',
    status: 'DRAFT',
    source: 'ERP',
    notes: null,
    return_warehouse_id: null,
    received_at: null,
    cancelled_at: null,
    cancellation_reason: null,
    ...overrides,
  }
}

function returnLine(overrides: Partial<SalesReturnLine> = {}): SalesReturnLine {
  return {
    id: 'line-1',
    dispatch_line_id: 'dispatch-line-1',
    item_id: 'item-1',
    item_code: 'LOCK-01',
    item_name: 'Mortise lock',
    quantity: '1.0000',
    serial_ids: ['serial-1'],
    serial_numbers: ['LK-0001'],
    accepted_serial_ids: [],
    damaged_serial_ids: [],
    scrapped_serial_ids: [],
    credited_quantity: '0.0000',
    reason_code: 'CUSTOMER_RETURN',
    requires_qc: true,
    qc_inspection_id: null,
    note: null,
    ...overrides,
  }
}

/** The register with one return in it, and that return's own read. */
function returnRoutes(row: SalesReturnRow, lines: SalesReturnLine[] = [returnLine()]): RouteTable {
  return {
    'GET /sales-returns': page([row]),
    [`GET /sales-returns/${row.id}`]: ok({ ...row, lines }),
  }
}

const OLD_DELIVERY: DispatchRow = {
  sales_invoice_id: null,
  id: 'dispatch-101',
  code: 'DSP-0101',
  dispatch_type: 'SALES',
  status: 'DELIVERED',
  customer_id: 'customer-1',
  vendor_id: null,
  warehouse_id: 'warehouse-1',
  sales_order_id: 'order-1',
  dispatch_date: '2026-01-05',
  expected_delivery_date: null,
  actual_delivery_date: '2026-01-07',
  transporter_id: null,
  courier_name: null,
  tracking_id: null,
  total_boxes: 1,
  total_weight_kg: '2.0000',
  created_at: '2026-01-05T09:00:00Z',
}

const OLD_DELIVERY_DETAIL = {
  ...OLD_DELIVERY,
  lines: [
    {
      id: 'dispatch-line-1',
      line_number: 1,
      sales_order_line_id: 'order-line-1',
      item_id: 'item-1',
      item_code: 'HINGE-04',
      item_name: 'Butt hinge 4 inch',
      quantity: '4.0000',
      batch_id: null,
      serial_ids: [],
      serial_numbers: [],
      carton_ids: [],
      reservation_id: null,
      inventory_transaction_id: null,
      notes: null,
    },
  ],
} as unknown as DispatchDetail

/** A delivered dispatch of two serial-tracked locks. */
const SERIAL_DELIVERY_DETAIL = {
  ...OLD_DELIVERY,
  lines: [
    {
      id: 'dispatch-line-1',
      line_number: 1,
      sales_order_line_id: 'order-line-1',
      item_id: 'item-1',
      item_code: 'LOCK-01',
      item_name: 'Mortise lock',
      quantity: '2.0000',
      batch_id: null,
      serial_ids: ['ser-1', 'ser-2'],
      serial_numbers: ['LK-0001', 'LK-0002'],
      carton_ids: [],
      reservation_id: null,
      inventory_transaction_id: null,
      notes: null,
    },
  ],
} as unknown as DispatchDetail

async function openReturn(
  user: ReturnType<typeof renderScreen>['user'],
  code = 'SR/2026-001',
): Promise<HTMLElement> {
  await user.click(await screen.findByRole('button', { name: code }))
  return screen.findByRole('dialog', { name: code })
}

describe('SalesReturnsPage', () => {
  it('names the dispatch and the customer in the register, and offers no sort it ignores', async () => {
    // Both columns printed the raw UUID. The endpoint lists newest first and
    // takes no sort, so a heading that offered one changed nothing.
    renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW],
      routes: { 'GET /sales-returns': page([salesReturnRow()]) },
    })

    const row = (await screen.findByRole('button', { name: 'SR/2026-001' })).closest('tr')!
    expect(within(row).getByText('DSP-0001')).toBeVisible()
    expect(within(row).getByText('C-0042')).toBeVisible()
    expect(within(row).getByText('Acme Hardware')).toBeVisible()
    expect(within(row).getByText('ERP')).toBeVisible()
    expect(within(row).queryByText(/dispatch-1|customer-1/)).toBeNull()
    for (const header of screen.getAllByRole('columnheader')) {
      expect(within(header).queryByRole('button')).toBeNull()
    }
  })

  it('opens a return over the list and names its lines, units and their verdicts', async () => {
    const row = salesReturnRow({ status: 'RECEIVED', received_at: '2026-02-03T10:00:00Z' })
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW],
      routes: returnRoutes(row, [
        returnLine({
          quantity: '2.0000',
          serial_ids: ['serial-1', 'serial-2'],
          serial_numbers: ['LK-0001', 'LK-0002'],
          accepted_serial_ids: ['serial-1'],
          credited_quantity: '1.0000',
          reason_code: 'Wrong finish',
        }),
      ]),
    })

    const dialog = await openReturn(user)
    expect(within(dialog).getByText('C-0042 - Acme Hardware')).toBeVisible()
    expect(within(dialog).getByText('DSP-0001')).toBeVisible()
    const lines = await within(dialog).findByRole('region', { name: 'Returned lines' })
    const cells = within(within(lines).getAllByRole('row')[1]!).getAllByRole('cell')
    expect(cells[0]).toHaveTextContent('LOCK-01')
    expect(cells[0]).toHaveTextContent('Mortise lock')
    expect(cells[1]).toHaveTextContent(/^2$/)
    expect(cells[2]).toHaveTextContent('LK-0001Accepted')
    expect(cells[2]).toHaveTextContent('LK-0002')
    expect(cells[2]).not.toHaveTextContent('LK-0002Accepted')
    expect(cells[3]).toHaveTextContent(/^Wrong finish$/)
    expect(cells[4]).toHaveTextContent(/^1$/)
    expect(within(dialog).queryByText(/serial-1|item-1/)).toBeNull()
    // A reader may look, not act.
    expect(within(dialog).queryByRole('button', { name: /return$/ })).toBeNull()
  })

  it('shows only valid actions for a draft return', async () => {
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: returnRoutes(salesReturnRow()),
    })

    const dialog = await openReturn(user)

    expect(within(dialog).getByRole('button', { name: 'Receive return' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Cancel return' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Close return' })).not.toBeInTheDocument()
  })

  it.each([
    ['DRAFT', 'receive', 'Receive return', 'Sales return marked received'],
    ['RECEIVED', 'close', 'Close return', 'Sales return closed'],
  ] as const)('says a %s return was taken through %s', async (status, action, button, title) => {
    // Before, the panel only closed - indistinguishable from dismissing it.
    const row = salesReturnRow({ status })
    const { user } = renderScreen(
      <>
        <SalesReturnsPage />
        <Toaster />
      </>,
      {
        permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
        routes: {
          ...returnRoutes(row, []),
          [`POST /sales-returns/sr-1/${action}`]: ok(row),
        },
      },
    )

    const dialog = await openReturn(user)
    await user.click(within(dialog).getByRole('button', { name: button }))

    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(await within(toasts).findByText(title)).toBeVisible()
    expect(within(toasts).getByText('SR/2026-001')).toBeVisible()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('shows close and cancel for a received return', async () => {
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: returnRoutes(salesReturnRow({ status: 'RECEIVED' }), []),
    })

    const dialog = await openReturn(user)

    expect(within(dialog).getByRole('button', { name: 'Close return' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Cancel return' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Receive return' })).not.toBeInTheDocument()
  })

  it('collects the cancellation reason in a dialog that opens empty', async () => {
    const { user, fetch } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        ...returnRoutes(salesReturnRow()),
        'POST /sales-returns/sr-1/cancel': ok(salesReturnRow({ status: 'CANCELLED' })),
      },
    })

    const detail = await openReturn(user)
    const trigger = within(detail).getByRole('button', { name: 'Cancel return' })
    expect(trigger).toHaveClass('text-destructive')
    await user.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: 'Cancel SR/2026-001?' })
    const box = within(dialog).getByRole('textbox', { name: 'Reason' })
    const confirm = within(dialog).getByRole('button', { name: 'Cancel return' })
    // Nothing is suggested: the native prompt arrived pre-filled with
    // "Customer requested cancellation", and one OK audited it as the cause.
    expect(box).toHaveValue('')
    expect(confirm).toHaveClass('bg-destructive')

    await user.click(confirm)
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    await user.type(box, 'ab')
    await user.click(confirm)
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)

    await user.clear(box)
    await user.type(box, '  Raised against the wrong dispatch  ')
    await user.click(confirm)
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/sales-returns/sr-1/cancel')).toEqual({
        reason: 'Raised against the wrong dispatch',
      }),
    )
    // Both close: the reason, and the return it cancelled.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps a refused cancellation open with the reason the API gave', async () => {
    const { user } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        ...returnRoutes(salesReturnRow()),
        'POST /sales-returns/sr-1/cancel': fail(
          422,
          'VALIDATION_ERROR',
          'Request validation failed',
          {
            fields: { reason: ['The reason must name what changed'] },
          },
        ),
      },
    })

    const detail = await openReturn(user)
    await user.click(within(detail).getByRole('button', { name: 'Cancel return' }))
    const dialog = await screen.findByRole('dialog', { name: 'Cancel SR/2026-001?' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 'Changed mind')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel return' }))

    expect(await within(dialog).findByText('The reason must name what changed')).toBeVisible()
    expect(screen.getByRole('dialog', { name: 'Cancel SR/2026-001?' })).toBeVisible()
    expect(within(dialog).getByRole('textbox', { name: 'Reason' })).toHaveValue('Changed mind')
  })

  it('sends nothing when the cancellation is dismissed, and keeps the return open', async () => {
    const { user, fetch } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: returnRoutes(salesReturnRow()),
    })

    const detail = await openReturn(user)
    await user.click(within(detail).getByRole('button', { name: 'Cancel return' }))
    const dialog = await screen.findByRole('dialog', { name: 'Cancel SR/2026-001?' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 'Not after all')
    await user.keyboard('{Escape}')

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Cancel SR/2026-001?' })).not.toBeInTheDocument(),
    )
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    const still = screen.getByRole('dialog', { name: 'SR/2026-001' })
    expect(within(still).getByRole('button', { name: 'Cancel return' })).toBeVisible()
  })

  it('saves per-serial QC dispositions for a received return, by label', async () => {
    const row = salesReturnRow({ status: 'RECEIVED' })
    const { user, fetch } = renderScreen(
      <>
        <SalesReturnsPage />
        <Toaster />
      </>,
      {
        permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
        routes: {
          ...returnRoutes(row),
          'POST /sales-returns/sr-1/units': ok({
            ...row,
            lines: [returnLine({ accepted_serial_ids: ['serial-1'] })],
          }),
        },
      },
    )

    const detail = await openReturn(user)
    await user.click(await within(detail).findByRole('button', { name: 'Dispose serial units' }))
    const dialog = await screen.findByRole('dialog', { name: 'Disposition returned serial units' })
    // The label on the unit, not its id: the UUID was all this dialog showed.
    expect(within(dialog).getByText('LK-0001')).toBeVisible()
    expect(within(dialog).queryByText('serial-1')).toBeNull()
    await user.click(within(dialog).getByRole('combobox', { name: 'Disposition for LK-0001' }))
    await user.click(await screen.findByRole('option', { name: 'Accepted' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save dispositions' }))

    await waitFor(() => fetch.requestFor('POST', '/sales-returns/sr-1/units'))
    expect(fetch.requestFor('POST', '/sales-returns/sr-1/units')).toEqual({
      accepted: ['serial-1'],
      damaged: [],
      scrapped: [],
    })
    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(await within(toasts).findByText('Serial dispositions recorded')).toBeVisible()
  })

  it("names a refused disposition from the 422's field detail, once", async () => {
    const row = salesReturnRow({ status: 'RECEIVED' })
    const { user } = renderScreen(
      <>
        <SalesReturnsPage />
        <Toaster />
      </>,
      {
        permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
        errorNets: true,
        routes: {
          ...returnRoutes(row),
          'POST /sales-returns/sr-1/units': fail(
            422,
            'VALIDATION_ERROR',
            'Some fields need attention.',
            { fields: { accepted: ['LK-0001 was not received on this return'] } },
          ),
        },
      },
    )

    const detail = await openReturn(user)
    await user.click(await within(detail).findByRole('button', { name: 'Dispose serial units' }))
    await user.click(screen.getByRole('combobox', { name: 'Disposition for LK-0001' }))
    await user.click(await screen.findByRole('option', { name: 'Accepted' }))
    await user.click(screen.getByRole('button', { name: 'Save dispositions' }))

    expect(await screen.findByText('LK-0001 was not received on this return')).toBeVisible()
    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(within(toasts).queryAllByRole('alert')).toHaveLength(0)
  })

  it('finds an older delivery by searching the server, and returns against it', async () => {
    // Not on the first page: only a search that names it brings it back. A fixed
    // first page used to be the only way in, so this return could not be raised.
    const searches: URL[] = []
    const { user, fetch } = renderScreen(
      <>
        <SalesReturnsPage />
        <Toaster />
      </>,
      {
        permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
        routes: {
          'GET /sales-returns': page([]),
          'GET /warehouses/options': ok([]),
          'GET /dispatch': ({ url }) => {
            searches.push(url)
            return url.searchParams.get('search') === 'DSP-0101'
              ? page([OLD_DELIVERY])
              : page([], { has_next: false })
          },
          'GET /dispatch/dispatch-101': ok(OLD_DELIVERY_DETAIL),
          'POST /sales-returns': ok(salesReturnRow({ dispatch_id: 'dispatch-101' })),
        },
      },
    )

    await user.click(await screen.findByRole('button', { name: 'New return' }))
    const dialog = await screen.findByRole('dialog', { name: 'New sales return' })
    await user.click(within(dialog).getByRole('combobox', { name: 'Sales dispatch' }))
    await user.type(await screen.findByRole('textbox', { name: 'Search dispatches' }), 'DSP-0101')
    await user.click(await screen.findByRole('option', { name: /DSP-0101/ }))

    await user.type(
      await within(dialog).findByRole('spinbutton', {
        name: 'Return quantity for line 1 (HINGE-04)',
      }),
      '1',
    )
    // The line names its item, and a quantity, not the API's four-place string.
    expect(within(dialog).getByText('Butt hinge 4 inch')).toBeInTheDocument()
    expect(within(dialog).getByText('Dispatched: 4')).toBeInTheDocument()
    expect(within(dialog).queryByText('item-1')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Create return' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', '/sales-returns')).toMatchObject({
        dispatch_id: 'dispatch-101',
        lines: [{ dispatch_line_id: 'dispatch-line-1', quantity: '1', serial_ids: [] }],
      }),
    )
    const named = searches.find((url) => url.searchParams.get('search') === 'DSP-0101')
    expect(named?.searchParams.get('dispatch_type')).toBe('SALES')
    expect(named?.searchParams.get('status')).toBe('DELIVERED')
    const toasts = screen.getByRole('region', { name: 'Notifications' })
    expect(await within(toasts).findByText('Sales return created')).toBeVisible()
    expect(within(toasts).getByText('SR/2026-001')).toBeVisible()
  })
})

describe('what a return write leaves stale elsewhere', () => {
  // Receiving posts the units into stock, holds the inspected ones in QC with an
  // inspection each and counts them back off the dispatch; settling held units
  // moves them on; any of the three, closing too, may write a credit note. Only
  // the return's own keys were dropped, so on the production cache the stock,
  // the inspection queue and the credit read as before the write.
  const STOCK = queryKeys.inventory.balances({ page: 1 })
  const AVAILABLE = queryKeys.allocation.atp('item-1')
  const INSPECTIONS = queryKeys.quality.inspections({ page: 1 })
  const DISPATCHES = queryKeys.dispatch.list({ page: 1 })
  const CREDIT_NOTES = financeKeys.creditNotes({ page: 1 })
  const CUSTOMER = queryKeys.customers.detail('customer-1')
  const ALL = [STOCK, AVAILABLE, INSPECTIONS, DISPATCHES, CREDIT_NOTES, CUSTOMER]

  function seed(client: QueryClient): void {
    for (const queryKey of ALL) client.setQueryData(queryKey, { data: [] })
  }

  function staleness(client: QueryClient): (key: QueryKey) => boolean | undefined {
    return (key) => client.getQueryState(key)?.isInvalidated
  }

  it.each([
    ['receiving', 'DRAFT', 'receive', 'Receive return', ALL, []],
    [
      'closing',
      'RECEIVED',
      'close',
      'Close return',
      [CREDIT_NOTES, CUSTOMER],
      [STOCK, AVAILABLE, INSPECTIONS, DISPATCHES],
    ],
  ] as const)(
    'drops what %s changed, and nothing it did not',
    async (_, status, action, button, dropped, kept) => {
      const row = salesReturnRow({ status })
      const { user, fetch, queryClient } = renderScreen(<SalesReturnsPage />, {
        permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
        cachePolicy: 'production',
        routes: { ...returnRoutes(row, []), [`POST /sales-returns/sr-1/${action}`]: ok(row) },
      })
      seed(queryClient)
      const isStale = staleness(queryClient)

      const dialog = await openReturn(user)
      await user.click(within(dialog).getByRole('button', { name: button }))

      await waitFor(() => fetch.requestFor('POST', `/sales-returns/sr-1/${action}`))
      await waitFor(() => expect(dropped.map(isStale)).toEqual(dropped.map(() => true)))
      expect(kept.map(isStale)).toEqual(kept.map(() => false))
    },
  )

  it('drops the stock, availability and credit once held units are settled', async () => {
    const row = salesReturnRow({ status: 'RECEIVED' })
    const { user, fetch, queryClient } = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      cachePolicy: 'production',
      routes: {
        ...returnRoutes(row),
        'POST /sales-returns/sr-1/units': ok({
          ...row,
          lines: [returnLine({ accepted_serial_ids: ['serial-1'] })],
        }),
      },
    })
    seed(queryClient)
    const isStale = staleness(queryClient)

    const detail = await openReturn(user)
    await user.click(await within(detail).findByRole('button', { name: 'Dispose serial units' }))
    const dialog = await screen.findByRole('dialog', { name: 'Disposition returned serial units' })
    await user.click(within(dialog).getByRole('combobox', { name: 'Disposition for LK-0001' }))
    await user.click(await screen.findByRole('option', { name: 'Accepted' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save dispositions' }))

    await waitFor(() => fetch.requestFor('POST', '/sales-returns/sr-1/units'))
    await waitFor(() =>
      expect([STOCK, AVAILABLE, CREDIT_NOTES, CUSTOMER].map(isStale)).toEqual([
        true,
        true,
        true,
        true,
      ]),
    )
    // Settling a unit decides nothing in quality and moves nothing on the dispatch.
    expect([INSPECTIONS, DISPATCHES].map(isStale)).toEqual([false, false])
  })
})

describe('naming the returned units', () => {
  /** Open the form on the serial-tracked delivery and enter a quantity of one. */
  async function startSerialReturn() {
    const rendered = renderScreen(<SalesReturnsPage />, {
      permissions: [P.SALES_RETURN_VIEW, P.SALES_RETURN_CREATE],
      routes: {
        'GET /sales-returns': page([]),
        'GET /warehouses/options': ok([]),
        'GET /dispatch': page([OLD_DELIVERY]),
        'GET /dispatch/dispatch-101': ok(SERIAL_DELIVERY_DETAIL),
        'POST /sales-returns': ok(salesReturnRow({ dispatch_id: 'dispatch-101' })),
      },
    })
    const { user } = rendered
    await user.click(await screen.findByRole('button', { name: 'New return' }))
    const dialog = await screen.findByRole('dialog', { name: 'New sales return' })
    await user.click(within(dialog).getByRole('combobox', { name: 'Sales dispatch' }))
    await user.click(await screen.findByRole('option', { name: /DSP-0101/ }))
    await user.type(
      await within(dialog).findByRole('spinbutton', {
        name: 'Return quantity for line 1 (LOCK-01)',
      }),
      '1',
    )
    const serials = within(dialog).getByRole('textbox', {
      name: 'Returned serial numbers for line 1 (LOCK-01)',
    })
    return { ...rendered, dialog, serials }
  }

  it('takes the label on the unit and sends the id the API wants', async () => {
    // The form asked for "Serial IDs, comma separated" and listed UUIDs to copy.
    const { user, fetch, dialog, serials } = await startSerialReturn()
    expect(within(dialog).getByText('Dispatched serials: LK-0001, LK-0002')).toBeVisible()
    expect(within(dialog).queryByText(/ser-1|ser-2/)).toBeNull()

    // Typed in lower case, as a person does; the API stores labels upper-cased.
    await user.type(serials, ' lk-0002 ')
    await user.click(within(dialog).getByRole('button', { name: 'Create return' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', '/sales-returns')).toMatchObject({
        lines: [{ dispatch_line_id: 'dispatch-line-1', quantity: '1', serial_ids: ['ser-2'] }],
      }),
    )
  })

  it('refuses a label this line did not ship, beside the field, and sends nothing', async () => {
    const { user, fetch, dialog, serials } = await startSerialReturn()

    await user.type(serials, 'LK-0009')
    await user.click(within(dialog).getByRole('button', { name: 'Create return' }))

    expect(within(dialog).getByText('Not dispatched on this line: LK-0009.')).toBeVisible()
    expect(serials).toHaveAttribute('aria-invalid', 'true')
    expect(serials).toHaveAccessibleDescription('Not dispatched on this line: LK-0009.')
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)

    // Corrected, the refusal goes and the return is raised.
    await user.clear(serials)
    await user.type(serials, 'LK-0001')
    expect(within(dialog).queryByText(/Not dispatched on this line/)).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Create return' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/sales-returns')).toMatchObject({
        lines: [{ serial_ids: ['ser-1'] }],
      }),
    )
  })

  it('refuses a unit named twice once the field is left, and sends nothing', async () => {
    const { user, fetch, dialog, serials } = await startSerialReturn()

    await user.type(serials, 'LK-0001, lk-0001')
    // Nothing is said while the label is still being typed.
    expect(within(dialog).queryByText(/Named more than once/)).toBeNull()
    await user.tab()

    expect(within(dialog).getByText('Named more than once: LK-0001.')).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Create return' }))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })
})

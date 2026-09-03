/**
 * Stock movements - transfers, adjustments and physical counts.
 *
 * Everything on this screen ends as a signed row in the inventory ledger, and a
 * ledger row is never deleted. That makes two things worth holding still.
 *
 * **The permission behind each button.** Four rights are in play and they are
 * deliberately not the same right:
 *
 *   P.INVENTORY_TRANSFER        raise and move a transfer
 *   P.INVENTORY_ADJUST          raise, submit and post an adjustment
 *   P.INVENTORY_ADJUST_APPROVE  approve one somebody else raised
 *   P.INVENTORY_PHYSICAL_COUNT  open a sheet and count it
 *   P.INVENTORY_RECONCILE       accept what was counted and post the variance
 *
 * The last pair is the one to watch: whoever walks the aisles is not
 * automatically the person who accepts what they wrote down, and the confirm
 * action used to sit behind an early `if (!canCount) return null` that hid it
 * from a reconciler who had no counting right at all.
 *
 * **The transition behind each button.** These state machines have no edge from
 * most statuses, and a button offered on a status the machine will refuse puts
 * the operator through a confirmation for nothing - an invalid transition reads
 * exactly like a real failure. Confirm in particular was once offered on every
 * unsettled sheet, including ones nobody had counted.
 */

import { screen, within } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import MovementsPage from './movements-page'
import type { PhysicalCountRow, StockAdjustmentRow, StockTransferRow } from './types'

/**
 * Two shims this environment needs before a real dropdown can be operated,
 * neither of which changes what the screen does.
 *
 * jsdom implements neither pointer capture nor `scrollIntoView`, and Radix's
 * select and popover primitives call both while opening.
 *
 * The third line is about speed, and it is the difference between a 20-second
 * file and a 20-minute one. Floating UI asks `element.matches(':modal')` to
 * detect the top layer; nwsapi 2.2.27, the selector engine inside jsdom 25,
 * answers `:modal` by calling `element.matches()` again - itself - and recurses
 * until the stack overflows and the throw is swallowed, at 20-30 seconds per
 * popper mount. False is also a browser's answer for a Radix div: it is not a
 * native `<dialog>` and not the fullscreen element.
 */
beforeAll(() => {
  const proto = window.Element.prototype as unknown as Record<string, unknown>
  if (!proto.hasPointerCapture) proto.hasPointerCapture = () => false
  if (!proto.setPointerCapture) proto.setPointerCapture = () => {}
  if (!proto.releasePointerCapture) proto.releasePointerCapture = () => {}
  if (!proto.scrollIntoView) proto.scrollIntoView = () => {}

  const nativeMatches = window.Element.prototype.matches
  const TOP_LAYER = /^:(modal|fullscreen|popover-open|open|closed)$/
  window.Element.prototype.matches = function (this: Element, selector: string) {
    if (typeof selector === 'string' && TOP_LAYER.test(selector.trim())) return false
    return nativeMatches.call(this, selector)
  }
})

const ITEM = {
  id: 'it000000-0000-7000-8000-000000000001',
  code: 'BRASS-ROD-12',
  name: 'Brass rod 12mm',
}
const FROM = { id: 'wh000000-0000-7000-8000-000000000001', code: 'MAIN', name: 'Main store' }
const TO = { id: 'wh000000-0000-7000-8000-000000000002', code: 'FG', name: 'Finished goods' }

function transfer(overrides: Partial<StockTransferRow> = {}): StockTransferRow {
  return {
    id: 'tr000000-0000-7000-8000-000000000001',
    code: 'TRF/2026-27/00011',
    status: 'DRAFT',
    from_warehouse_id: FROM.id,
    from_warehouse_code: FROM.code,
    to_warehouse_id: TO.id,
    to_warehouse_code: TO.code,
    transfer_date: '2026-09-01',
    expected_date: '2026-09-03',
    dispatched_at: null,
    received_at: null,
    approved_at: null,
    reason: 'Rebalancing ahead of the assembly plan',
    notes: null,
    line_count: 2,
    total_quantity: '80.0000',
    created_at: '2026-09-01T09:00:00Z',
    updated_at: '2026-09-01T09:00:00Z',
    ...overrides,
  }
}

function adjustment(overrides: Partial<StockAdjustmentRow> = {}): StockAdjustmentRow {
  return {
    id: 'ad000000-0000-7000-8000-000000000001',
    code: 'ADJ/2026-27/00007',
    status: 'DRAFT',
    warehouse_id: FROM.id,
    warehouse_code: FROM.code,
    adjustment_date: '2026-09-01',
    reason_code: 'DAMAGE',
    reason: 'Pallet damaged by a forklift in aisle B',
    notes: null,
    total_value: '4500.00',
    requires_approval: false,
    approved_at: null,
    posted_at: null,
    decision_note: null,
    line_count: 1,
    created_at: '2026-09-01T09:00:00Z',
    updated_at: '2026-09-01T09:00:00Z',
    created_by_id: null,
    ...overrides,
  }
}

function count(overrides: Partial<PhysicalCountRow> = {}): PhysicalCountRow {
  return {
    id: 'pc000000-0000-7000-8000-000000000001',
    code: 'PC/2026-27/00003',
    status: 'COUNTED',
    warehouse_id: FROM.id,
    warehouse_code: FROM.code,
    location_id: null,
    count_date: '2026-09-01',
    started_at: '2026-09-01T06:00:00Z',
    counted_at: '2026-09-01T11:00:00Z',
    confirmed_at: null,
    confirmed_by_id: null,
    line_count: 40,
    variance_line_count: 2,
    variance_value: '-1250.00',
    reason: 'Quarterly cycle count',
    notes: null,
    created_at: '2026-09-01T05:00:00Z',
    updated_at: '2026-09-01T11:00:00Z',
    ...overrides,
  }
}

const TRANSFERS_TAB = '/inventory/movements?tab=transfers'
const ADJUSTMENTS_TAB = '/inventory/movements?tab=adjustments'
const COUNTS_TAB = '/inventory/movements?tab=counts'

/** The row a button lives in, so a multi-row assertion cannot cross rows. */
function rowFor(code: string): HTMLElement {
  const cell = screen.getByText(code)
  const row = cell.closest('tr')
  expect(row, `no row for ${code}`).not.toBeNull()
  return row as HTMLElement
}

// ----------------------------------------------------------- transfers -----

describe('transfers', () => {
  it('offers no transitions at all without INVENTORY_TRANSFER', async () => {
    renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW],
      routes: { 'GET /inventory/transfers': page([transfer()]) },
    })

    expect(await screen.findByText('TRF/2026-27/00011')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new transfer/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^approve$/i })).not.toBeInTheDocument()
  })

  it('offers exactly the one transition each status has an edge for', async () => {
    // TRANSFER_FLOW: DRAFT -> APPROVED -> IN_TRANSIT -> RECEIVED. RECEIVED and
    // CANCELLED are terminal; offering anything there is a guaranteed refusal.
    renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_TRANSFER],
      routes: {
        'GET /inventory/transfers': page([
          transfer({ id: 't1', code: 'TRF-DRAFT', status: 'DRAFT' }),
          transfer({ id: 't2', code: 'TRF-APPROVED', status: 'APPROVED' }),
          transfer({ id: 't3', code: 'TRF-TRANSIT', status: 'IN_TRANSIT' }),
          transfer({ id: 't4', code: 'TRF-RECEIVED', status: 'RECEIVED' }),
          transfer({ id: 't5', code: 'TRF-CANCELLED', status: 'CANCELLED' }),
        ]),
      },
    })

    await screen.findByText('TRF-DRAFT')
    expect(
      within(rowFor('TRF-DRAFT')).getByRole('button', { name: /approve/i }),
    ).toBeInTheDocument()
    expect(
      within(rowFor('TRF-APPROVED')).getByRole('button', { name: /dispatch/i }),
    ).toBeInTheDocument()
    expect(
      within(rowFor('TRF-TRANSIT')).getByRole('button', { name: /receive/i }),
    ).toBeInTheDocument()
    expect(within(rowFor('TRF-RECEIVED')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(rowFor('TRF-CANCELLED')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('confirms before dispatching, and says stock leaves now', async () => {
    // Dispatch is the step that actually takes stock out of the source
    // warehouse and puts it in transit, so it is confirmed with the quantity and
    // the warehouse named rather than fired straight off a grid button.
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_TRANSFER],
      routes: {
        'GET /inventory/transfers': page([transfer({ status: 'APPROVED' })]),
        'POST /inventory/transfers': ok({ id: 'tr000000-0000-7000-8000-000000000001' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /dispatch/i }))
    const dialog = await screen.findByRole('dialog')
    expect(
      within(dialog).getByText(/80 leaves MAIN now and counts as in transit/i),
    ).toBeInTheDocument()
    // Nothing has been sent while the question is still on screen.
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)

    await user.click(within(dialog).getByRole('button', { name: 'Dispatch transfer' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const call = fetch.calls.find((c) => c.method === 'POST')!
    expect(call.path).toBe('/inventory/transfers/tr000000-0000-7000-8000-000000000001/dispatch')
    // A transition carries no payload; the document already says what moves.
    expect(call.body).toEqual({})
  })

  it('sends line quantities as decimal strings and narrows the item search', async () => {
    // Two things at once because they are one screen's contract: quantities stay
    // strings, and the picker asks the API for non-serial items only.
    // docs/assumptions.md 25 - a transfer line has nowhere to name units, so the
    // API refuses a serial-tracked item outright. Narrowing server-side means
    // the refusal is never reached; filtering in the browser would silently drop
    // matches past the first page.
    const itemSearches: URL[] = []
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_TRANSFER],
      routes: {
        'GET /inventory/transfers': page([]),
        'GET /warehouses': page([FROM, TO]),
        'GET /products/items': ({ url }) => {
          itemSearches.push(url)
          return page([ITEM])
        },
        'POST /inventory/transfers': ok({ code: 'TRF/2026-27/00012' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /new transfer/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'From warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'To warehouse' }))
    await user.click(await screen.findByRole('option', { name: /FG/ }))
    await user.type(
      within(dialog).getByRole('textbox', { name: /reason/i }),
      'Rebalancing ahead of the September assembly plan',
    )
    await user.click(within(dialog).getByRole('combobox', { name: /item, line 1/i }))
    await user.click(await screen.findByRole('option', { name: /BRASS-ROD-12/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /^quantity$/i }), '12.5000')

    await user.click(within(dialog).getByRole('button', { name: /create transfer/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/inventory/transfers') as {
      from_warehouse_id: string
      to_warehouse_id: string
      reason: string
      lines: { item_id: string; quantity: unknown }[]
    }
    expect(body.from_warehouse_id).toBe(FROM.id)
    expect(body.to_warehouse_id).toBe(TO.id)
    expect(body.lines).toEqual([{ item_id: ITEM.id, quantity: '12.5000' }])
    expect(typeof body.lines[0]!.quantity).toBe('string')

    expect(itemSearches.length).toBeGreaterThan(0)
    expect(itemSearches[0]!.searchParams.get('is_serial_tracked')).toBe('false')
  })

  it('will not send a transfer from a warehouse to itself', async () => {
    // Same source and destination is a document the API refuses, and the round
    // trip to learn that is a round trip that could have been a message.
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_TRANSFER],
      routes: {
        'GET /inventory/transfers': page([]),
        'GET /warehouses': page([FROM, TO]),
        'GET /products/items': page([ITEM]),
      },
    })

    await user.click(await screen.findByRole('button', { name: /new transfer/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'From warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'To warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /reason/i }), 'Moving stock around')
    await user.click(within(dialog).getByRole('button', { name: /create transfer/i }))

    expect(
      await screen.findByText('The source and destination warehouses must differ.'),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
})

// --------------------------------------------------------- adjustments -----

describe('adjustments', () => {
  it('loads the adjustment list only when its tab is opened', async () => {
    // The tabs unmount what they are not showing, which is why a tab switch has
    // to clear page, sort and search too: page 4 of transfers is meaningless
    // once the table below it is adjustments.
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/transfers': page([transfer()]),
        'GET /inventory/adjustments': page([adjustment()]),
      },
    })
    await screen.findByText('TRF/2026-27/00011')
    expect(fetch.calls.map((call) => call.path)).not.toContain('/inventory/adjustments')

    await user.click(screen.getByRole('tab', { name: /adjustments/i }))
    expect(await screen.findByText('ADJ/2026-27/00007')).toBeInTheDocument()
  })

  it('offers the one edge a draft actually has, which depends on requires_approval', async () => {
    // A draft under the threshold posts straight to the ledger; over it, the
    // same edge is refused and the document has to go round the approval loop.
    // `requires_approval` is decided by the service and carried on the row, so
    // the screen never guesses which of the two DRAFT edges is live.
    renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_ADJUST],
      routes: {
        'GET /inventory/adjustments': page([
          adjustment({ id: 'a1', code: 'ADJ-SMALL', status: 'DRAFT', requires_approval: false }),
          adjustment({ id: 'a2', code: 'ADJ-BIG', status: 'DRAFT', requires_approval: true }),
          adjustment({ id: 'a3', code: 'ADJ-APPROVED', status: 'APPROVED' }),
          adjustment({ id: 'a4', code: 'ADJ-POSTED', status: 'POSTED' }),
        ]),
      },
    })

    await screen.findByText('ADJ-SMALL')
    expect(within(rowFor('ADJ-SMALL')).getByRole('button', { name: /post/i })).toBeInTheDocument()
    expect(within(rowFor('ADJ-BIG')).getByRole('button', { name: /submit/i })).toBeInTheDocument()
    expect(
      within(rowFor('ADJ-APPROVED')).getByRole('button', { name: /post/i }),
    ).toBeInTheDocument()
    // POSTED is terminal: the ledger row exists and cannot be unwritten.
    expect(within(rowFor('ADJ-POSTED')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('keeps approving separate from raising and posting', async () => {
    // The whole point of the approval loop is that the second pair of eyes is a
    // different pair. Someone who may raise and post an adjustment must not be
    // able to approve one waiting for a decision.
    renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_ADJUST],
      routes: {
        'GET /inventory/adjustments': page([
          adjustment({ code: 'ADJ-WAITING', status: 'PENDING_APPROVAL', requires_approval: true }),
        ]),
      },
    })

    await screen.findByText('ADJ-WAITING')
    expect(within(rowFor('ADJ-WAITING')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('gives the approver the approve button and nothing else', async () => {
    renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_ADJUST_APPROVE],
      routes: {
        'GET /inventory/adjustments': page([
          adjustment({
            id: 'a1',
            code: 'ADJ-WAITING',
            status: 'PENDING_APPROVAL',
            requires_approval: true,
          }),
          adjustment({ id: 'a2', code: 'ADJ-DRAFT', status: 'DRAFT', requires_approval: false }),
        ]),
      },
    })

    await screen.findByText('ADJ-WAITING')
    expect(
      within(rowFor('ADJ-WAITING')).getByRole('button', { name: /approve/i }),
    ).toBeInTheDocument()
    // Approving is not posting: an approver with no `inventory.adjust` cannot
    // put the movement on the ledger.
    expect(within(rowFor('ADJ-DRAFT')).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new adjustment/i })).not.toBeInTheDocument()
  })

  it('warns that a posted adjustment cannot be deleted, then posts it', async () => {
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_ADJUST],
      routes: {
        'GET /inventory/adjustments': page([
          adjustment({ status: 'DRAFT', requires_approval: false }),
        ]),
        'POST /inventory/adjustments': ok({ id: 'ad000000-0000-7000-8000-000000000001' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /post/i }))
    const dialog = await screen.findByRole('dialog')
    expect(
      within(dialog).getByText(/ledger entry cannot be deleted afterwards/i),
    ).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Post adjustment' }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.calls.find((c) => c.method === 'POST')!.path).toBe(
      '/inventory/adjustments/ad000000-0000-7000-8000-000000000001/post',
    )
  })

  it('renders the document value as a magnitude, with no sign', async () => {
    // Every line is costed as `abs(quantity) * unit_cost` and the table carries
    // a `total_value >= 0` CHECK, so a write-off and a write-on of the same size
    // are the same number here. Rendering it signed made both read as gains.
    renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/adjustments': page([adjustment({ total_value: '4500.00' })]),
      },
    })

    await screen.findByText('ADJ/2026-27/00007')
    const value = screen.getByText(/4,500\.00/)
    expect(value.textContent).not.toMatch(/[+-]/)
  })

  it('sends a signed quantity as a string, with the cause classified', async () => {
    // Master rules 83: no stock change without a recorded reason. The code says
    // what kind of loss it was, the sentence says what happened, and the sign on
    // the quantity is the direction - which is why the field is text and not a
    // number input.
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_ADJUST],
      routes: {
        'GET /inventory/adjustments': page([]),
        'GET /warehouses': page([FROM, TO]),
        'GET /products/items': page([ITEM]),
        'POST /inventory/adjustments': ok({ code: 'ADJ/2026-27/00008' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /new adjustment/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Reason code' }))
    await user.click(await screen.findByRole('option', { name: 'Damage' }))
    await user.type(
      within(dialog).getByRole('textbox', { name: /^reason/i }),
      'Pallet damaged by a forklift in aisle B on 28 Aug',
    )
    await user.click(within(dialog).getByRole('combobox', { name: /item, line 1/i }))
    await user.click(await screen.findByRole('option', { name: /BRASS-ROD-12/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /^quantity$/i }), '-12.5000')

    await user.click(within(dialog).getByRole('button', { name: /raise adjustment/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/inventory/adjustments') as {
      warehouse_id: string
      reason_code: string
      reason: string
      lines: { item_id: string; quantity: unknown }[]
    }
    expect(body.warehouse_id).toBe(FROM.id)
    expect(body.reason_code).toBe('DAMAGE')
    expect(body.lines).toEqual([{ item_id: ITEM.id, quantity: '-12.5000' }])
    expect(typeof body.lines[0]!.quantity).toBe('string')
  })

  it('shows a 422 from the server on the field it names', async () => {
    const { user } = renderScreen(<MovementsPage />, {
      route: ADJUSTMENTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_ADJUST],
      routes: {
        'GET /inventory/adjustments': page([]),
        'GET /warehouses': page([FROM, TO]),
        'GET /products/items': page([ITEM]),
        'POST /inventory/adjustments': fail(422, 'VALIDATION_ERROR', 'Invalid adjustment', {
          fields: { 'lines.0.quantity': ['Item BRASS-ROD-12 has only 4 on hand at MAIN.'] },
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /new adjustment/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Reason code' }))
    await user.click(await screen.findByRole('option', { name: 'Damage' }))
    await user.type(
      within(dialog).getByRole('textbox', { name: /^reason/i }),
      'Pallet damaged by a forklift in aisle B on 28 Aug',
    )
    await user.click(within(dialog).getByRole('combobox', { name: /item, line 1/i }))
    await user.click(await screen.findByRole('option', { name: /BRASS-ROD-12/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /^quantity$/i }), '-12')
    await user.click(within(dialog).getByRole('button', { name: /raise adjustment/i }))

    // The API addresses a field array entry the way react-hook-form does, so
    // `lines.0.quantity` lands on the line the operator typed rather than
    // nowhere at all.
    expect(
      await screen.findByText('Item BRASS-ROD-12 has only 4 on hand at MAIN.'),
    ).toBeInTheDocument()
  })
})

// ----------------------------------------------------- physical counts -----

describe('physical counts', () => {
  it('does not offer Confirm to a counter who cannot reconcile', async () => {
    // The count endpoints enforce `inventory.physical_count`; `/confirm` - the
    // step that posts the variance to the ledger - enforces
    // `inventory.reconcile`. Whoever walks the aisles is not automatically the
    // person who accepts what they wrote down.
    renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_PHYSICAL_COUNT],
      routes: {
        'GET /inventory/physical-counts': page([
          count({ id: 'c1', code: 'PC-DRAFT', status: 'DRAFT' }),
          count({ id: 'c2', code: 'PC-COUNTED', status: 'COUNTED' }),
        ]),
      },
    })

    await screen.findByText('PC-DRAFT')
    expect(within(rowFor('PC-DRAFT')).getByRole('button', { name: /start/i })).toBeInTheDocument()
    expect(within(rowFor('PC-COUNTED')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('offers Confirm to a reconciler who cannot count', async () => {
    // The other half, and the one that regressed: the action column used to
    // return early on the counting right, which hid Confirm from exactly the
    // person the endpoint is for.
    renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_RECONCILE],
      routes: {
        'GET /inventory/physical-counts': page([
          count({ id: 'c1', code: 'PC-DRAFT', status: 'DRAFT' }),
          count({ id: 'c2', code: 'PC-COUNTED', status: 'COUNTED' }),
        ]),
      },
    })

    await screen.findByText('PC-COUNTED')
    expect(
      within(rowFor('PC-COUNTED')).getByRole('button', { name: /confirm/i }),
    ).toBeInTheDocument()
    // Opening and starting a sheet is the counter's job, not the reconciler's.
    expect(within(rowFor('PC-DRAFT')).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new count/i })).not.toBeInTheDocument()
  })

  it('offers Confirm only on a sheet that has been counted', async () => {
    // COUNT_FLOW: DRAFT -> IN_PROGRESS -> COUNTED -> CONFIRMED. Confirm was once
    // offered on every unsettled status, so it appeared on sheets nobody had
    // finished counting - which would post the snapshot back over itself.
    renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_PHYSICAL_COUNT, P.INVENTORY_RECONCILE],
      routes: {
        'GET /inventory/physical-counts': page([
          count({ id: 'c1', code: 'PC-DRAFT', status: 'DRAFT' }),
          count({ id: 'c2', code: 'PC-PROGRESS', status: 'IN_PROGRESS' }),
          count({ id: 'c3', code: 'PC-COUNTED', status: 'COUNTED' }),
          count({ id: 'c4', code: 'PC-CONFIRMED', status: 'CONFIRMED' }),
          count({ id: 'c5', code: 'PC-CANCELLED', status: 'CANCELLED' }),
        ]),
      },
    })

    await screen.findByText('PC-DRAFT')
    expect(within(rowFor('PC-DRAFT')).getByRole('button', { name: /start/i })).toBeInTheDocument()
    // IN_PROGRESS moves on by keying entries, which this screen does not do.
    expect(within(rowFor('PC-PROGRESS')).queryByRole('button')).not.toBeInTheDocument()
    expect(
      within(rowFor('PC-COUNTED')).getByRole('button', { name: /confirm/i }),
    ).toBeInTheDocument()
    expect(within(rowFor('PC-CONFIRMED')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(rowFor('PC-CANCELLED')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows the variance before asking for an irreversible posting', async () => {
    // Confirmation writes ledger rows, and a ledger row is never deleted - so
    // the figures a person is agreeing to are put in front of them first, rather
    // than described afterwards in a toast.
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_RECONCILE],
      routes: {
        'GET /inventory/physical-counts': page([count()]),
        'GET /inventory/physical-counts/pc000000-0000-7000-8000-000000000001/lines': page([
          {
            id: 'ln1',
            line_number: 1,
            item_id: ITEM.id,
            item_code: ITEM.code,
            item_name: ITEM.name,
            location_id: null,
            batch_id: null,
            system_quantity: '100.0000',
            counted_quantity: '90.0000',
            variance_quantity: '-10.0000',
            unit_cost: '125.00',
            variance_value: '-1250.00',
            counted_at: '2026-09-01T11:00:00Z',
            counted_by_id: null,
            notes: null,
          },
          {
            // Nobody counted this one. The service skips it rather than writing
            // it to zero, so it must not appear in the preview either.
            id: 'ln2',
            line_number: 2,
            item_id: 'it000000-0000-7000-8000-000000000002',
            item_code: 'HINGE-4',
            item_name: 'Butt hinge 4 inch',
            location_id: null,
            batch_id: null,
            system_quantity: '40.0000',
            counted_quantity: null,
            variance_quantity: null,
            unit_cost: '30.00',
            variance_value: null,
            counted_at: null,
            counted_by_id: null,
            notes: null,
          },
        ]),
        'POST /inventory/physical-counts': ok(
          count({ status: 'CONFIRMED', variance_value: '-1250.00', variance_line_count: 1 }),
        ),
      },
    })

    await user.click(await screen.findByRole('button', { name: /confirm/i }))
    const dialog = await screen.findByRole('dialog')

    expect(await within(dialog).findByText('BRASS-ROD-12')).toBeInTheDocument()
    expect(within(dialog).getByText(/book 100, counted 90/i)).toBeInTheDocument()
    // An uncounted line is left alone, not treated as zero stock.
    expect(within(dialog).queryByText('HINGE-4')).not.toBeInTheDocument()
    expect(within(dialog).getByText(/a line nobody counted is left alone/i)).toBeInTheDocument()
    // And the caveat that makes the preview honest: the posting re-reads under a
    // lock, so these are not quite the settled numbers.
    expect(within(dialog).getByText(/posts counted minus current/i)).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: /confirm and post/i }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.calls.find((c) => c.method === 'POST')!.path).toBe(
      '/inventory/physical-counts/pc000000-0000-7000-8000-000000000001/confirm',
    )
  })

  it('says so when the breakdown is only a sample of the sheet', async () => {
    // The line endpoint caps a page at 200 and a count sheet can be longer, so
    // the per-line list is a sample while the headline figures stay the sheet's
    // own server-side totals. A total summed from whatever fitted on one page
    // would understate a write-off, and understating a write-off is the one
    // direction this dialog must not err in - so the reader is told which of the
    // two they are looking at.
    const { user } = renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_RECONCILE],
      routes: {
        'GET /inventory/physical-counts': page([
          count({ variance_line_count: 40, variance_value: '-98000.00' }),
        ]),
        'GET /inventory/physical-counts/pc000000-0000-7000-8000-000000000001/lines': page(
          [
            {
              id: 'ln1',
              line_number: 1,
              item_id: ITEM.id,
              item_code: ITEM.code,
              item_name: ITEM.name,
              location_id: null,
              batch_id: null,
              system_quantity: '100.0000',
              counted_quantity: '90.0000',
              variance_quantity: '-10.0000',
              unit_cost: '125.00',
              variance_value: '-1250.00',
              counted_at: '2026-09-01T11:00:00Z',
              counted_by_id: null,
              notes: null,
            },
          ],
          { has_next: true, total: 200, pages: 2 },
        ),
      },
    })

    await user.click(await screen.findByRole('button', { name: /confirm/i }))
    const dialog = await screen.findByRole('dialog')

    // One line listed, of the forty the server says differ.
    expect(await within(dialog).findByText(/what the posting moves \(1 of 40/i)).toBeInTheDocument()
    expect(within(dialog).getByText(/across the lines listed below/i)).toBeInTheDocument()
    // The sheet's own total, not the one line that happened to fit on the page.
    expect(within(dialog).getByText('-₹98,000.00')).toBeInTheDocument()
  })

  it('will not let the posting be agreed to while the figures are still loading', async () => {
    // A handler that resolves late, so the dialog is caught in the state a real
    // network gives it. Agreeing to an irreversible posting before the variance
    // is on screen is the one thing this dialog exists to prevent.
    const { user } = renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_RECONCILE],
      routes: {
        'GET /inventory/physical-counts': page([count()]),
        'GET /inventory/physical-counts/pc000000-0000-7000-8000-000000000001/lines': () =>
          new Promise((resolve) => setTimeout(() => resolve(page([])), 400)),
      },
    })

    await user.click(await screen.findByRole('button', { name: /confirm/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('button', { name: /confirm and post/i })).toBeDisabled()
    expect(within(dialog).getByText(/reading the counted lines/i)).toBeInTheDocument()

    // Once the lines are in, the same button is live.
    expect(
      await within(dialog).findByText(/nothing on the sheet differs from the book/i),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /confirm and post/i })).toBeEnabled()
  })

  it('keeps the sheet totals when the line breakdown cannot be read', async () => {
    // The totals come from the count itself and are still true; only the detail
    // behind them is missing. Hiding the whole dialog would strand a reconciler
    // who has to close the sheet.
    const { user } = renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_RECONCILE],
      routes: {
        'GET /inventory/physical-counts': page([count()]),
        'GET /inventory/physical-counts/pc000000-0000-7000-8000-000000000001/lines': fail(
          500,
          'INTERNAL_ERROR',
          'The line query timed out.',
        ),
      },
    })

    await user.click(await screen.findByRole('button', { name: /confirm/i }))
    const dialog = await screen.findByRole('dialog')
    expect(
      await within(dialog).findByText(/the line-by-line breakdown could not be read/i),
    ).toBeInTheDocument()
    // The sheet's own net variance, negative, still on screen.
    expect(within(dialog).getByText('-₹1,250.00')).toBeInTheDocument()
  })

  it('sends the count sheet the API actually asks for', async () => {
    // `reason` was missing from this form entirely, so every attempt to open a
    // sheet came back a validation error naming a field the dialog did not have.
    const { user, fetch } = renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_PHYSICAL_COUNT],
      routes: {
        'GET /inventory/physical-counts': page([]),
        'GET /warehouses': page([FROM, TO]),
        'POST /inventory/physical-counts': ok({ code: 'PC/2026-27/00004' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /new count/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.type(
      within(dialog).getByRole('textbox', { name: /^reason/i }),
      'Quarterly cycle count for the finished-goods store',
    )
    await user.click(within(dialog).getByRole('button', { name: /open count sheet/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/inventory/physical-counts') as Record<string, unknown>
    expect(body.warehouse_id).toBe(FROM.id)
    expect(body.reason).toBe('Quarterly cycle count for the finished-goods store')
    // The optional scope note was left blank. Null is what the contract declares
    // for it; an empty string would be stored as a note that says nothing.
    expect(body.notes).toBeNull()
  })

  it('renders the count variance signed, unlike an adjustment total', async () => {
    // Deliberately the opposite of the adjustment column above: a count variance
    // has a direction that means something - stock found or stock missing - and
    // an adjustment document total does not.
    renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/physical-counts': page([count({ variance_value: '1250.00' })]),
      },
    })

    await screen.findByText('PC/2026-27/00003')
    expect(screen.getByText(/^\+/)).toBeInTheDocument()
  })
})

describe('states a person can act on', () => {
  it('explains an empty transfer list', async () => {
    renderScreen(<MovementsPage />, {
      route: TRANSFERS_TAB,
      permissions: [P.INVENTORY_VIEW],
      routes: { 'GET /inventory/transfers': page([]) },
    })

    expect(await screen.findByText('No transfers yet')).toBeInTheDocument()
  })

  it('reports a refused list rather than showing an empty one', async () => {
    renderScreen(<MovementsPage />, {
      route: COUNTS_TAB,
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/physical-counts': fail(
          403,
          'PERMISSION_DENIED',
          'You may not view counts.',
        ),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeInTheDocument()
    expect(screen.getByText('You may not view counts.')).toBeInTheDocument()
  })
})

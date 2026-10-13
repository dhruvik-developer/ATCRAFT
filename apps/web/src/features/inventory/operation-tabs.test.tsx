import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import OperationTab from './operation-tabs'

const WAREHOUSE = { id: '019a0000-0000-7000-8000-000000000001', code: 'MAIN', name: 'Main store' }
const ITEM = {
  id: '019a0000-0000-7000-8000-000000000002',
  code: 'BRASS',
  name: 'Brass sheet',
  is_batch_tracked: false,
  default_warehouse_id: null,
  default_location_id: null,
}
const BIN = {
  id: '019a0000-0000-7000-8000-000000000003',
  code: 'BIN-B',
  name: 'Receiving rack',
  warehouse_id: WAREHOUSE.id,
}
const issue = (overrides = {}) => ({
  id: '019a0000-0000-7000-8000-000000000004',
  code: 'II-0001',
  warehouse_id: WAREHOUSE.id,
  reason: 'Issue material to maintenance',
  issue_date: '2026-10-12',
  status: 'DRAFT',
  requires_approval: false,
  total_value: '1200',
  lines: [],
  ...overrides,
})

describe('inventory operation documents', () => {
  it('does not offer disposal to a stock viewer without the disposal right', async () => {
    renderScreen(<OperationTab kind="scrap-disposals" />, {
      permissions: [P.INVENTORY_VIEW],
      routes: { 'GET /inventory/scrap-disposals': page([]) },
    })
    await screen.findByText('No scrap disposal documents')
    expect(screen.queryByRole('button', { name: /new scrap disposal/i })).not.toBeInTheDocument()
  })

  it('submits a high-value draft without moving stock', async () => {
    const { user, fetch } = renderScreen(<OperationTab kind="internal-issues" />, {
      permissions: [P.INVENTORY_VIEW, P.INTERNAL_ISSUE_CREATE],
      routes: {
        'GET /inventory/internal-issues': page([issue({ requires_approval: true })]),
        [`POST /inventory/internal-issues/${issue().id}/submit`]: ok(
          issue({ status: 'PENDING_APPROVAL' }),
        ),
      },
    })
    await user.click(await screen.findByRole('button', { name: /^submit$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^submit$/i }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `/inventory/internal-issues/${issue().id}/submit`)).toEqual({})
    expect(fetch.calls.some((call) => call.path.endsWith('/post'))).toBe(false)
  })

  it('gates approval independently from issue creation', async () => {
    renderScreen(<OperationTab kind="internal-issues" />, {
      permissions: [P.INVENTORY_VIEW, P.INTERNAL_ISSUE_APPROVE],
      routes: { 'GET /inventory/internal-issues': page([issue({ status: 'PENDING_APPROVAL' })]) },
    })
    expect(await screen.findByRole('button', { name: /^approve$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^reject$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new internal issue/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^cancel$/i })).not.toBeInTheDocument()
  })

  it('rejects a pending issue only with a recorded reason', async () => {
    const pending = issue({
      status: 'PENDING_APPROVAL',
      requires_approval: true,
      created_by_id: '019a0000-0000-7000-8000-000000000099',
    })
    const { user, fetch } = renderScreen(<OperationTab kind="internal-issues" />, {
      permissions: [P.INVENTORY_VIEW, P.INTERNAL_ISSUE_APPROVE],
      routes: {
        'GET /inventory/internal-issues': page([pending]),
        [`POST /inventory/internal-issues/${pending.id}/reject`]: ok(
          issue({ status: 'CANCELLED' }),
        ),
      },
    })
    await user.click(await screen.findByRole('button', { name: /^reject$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^reject issue$/i }))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Use the stock already in the cage',
    )
    await user.click(within(dialog).getByRole('button', { name: /^reject issue$/i }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `/inventory/internal-issues/${pending.id}/reject`)).toEqual({
      reason: 'Use the stock already in the cage',
    })
  })

  it('opens the recorded quantities and bins before an issue is posted', async () => {
    const document = issue({
      lines: [
        {
          id: 'line-1',
          item_code: 'BRASS',
          item_name: 'Brass sheet',
          quantity: '2.5000',
          batch_id: null,
          location_id: BIN.id,
          location_code: 'BIN-B',
        },
      ],
    })
    const { user } = renderScreen(<OperationTab kind="internal-issues" />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/internal-issues': page([document]),
        [`GET /inventory/internal-issues/${document.id}`]: ok(document),
      },
    })
    await user.click(await screen.findByRole('button', { name: document.code }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('BRASS')).toBeVisible()
    expect(within(dialog).getByText('BIN-B')).toBeVisible()
    expect(within(dialog).getByText('2.5')).toBeVisible()
    expect(within(dialog).queryByText(/FIFO value/)).not.toBeInTheDocument()
  })

  it('cancels only with a recorded reason', async () => {
    const { user, fetch } = renderScreen(<OperationTab kind="internal-issues" />, {
      permissions: [P.INVENTORY_VIEW, P.INTERNAL_ISSUE_CREATE],
      routes: {
        'GET /inventory/internal-issues': page([issue()]),
        [`POST /inventory/internal-issues/${issue().id}/cancel`]: ok(
          issue({ status: 'CANCELLED' }),
        ),
      },
    })
    await user.click(await screen.findByRole('button', { name: /^cancel$/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^cancel issue$/i }))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Maintenance job was cancelled',
    )
    await user.click(within(dialog).getByRole('button', { name: /^cancel issue$/i }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `/inventory/internal-issues/${issue().id}/cancel`)).toEqual({
      reason: 'Maintenance job was cancelled',
    })
  })

  it('posts a write-off disposal with no realised amount', async () => {
    const { user, fetch } = renderScreen(<OperationTab kind="scrap-disposals" />, {
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_SCRAP_DISPOSE, P.LOCATION_VIEW, P.ITEM_VIEW],
      routes: {
        'GET /inventory/scrap-disposals': page([]),
        'GET /warehouses': page([WAREHOUSE]),
        'GET /products/items': page([ITEM]),
        [`GET /products/items/${ITEM.id}`]: ok(ITEM),
        'GET /warehouses/locations': page([BIN]),
        'POST /inventory/scrap-disposals': ok({ code: 'SD-0001' }),
      },
    })
    await user.click(await screen.findByRole('button', { name: /new scrap disposal/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Write off the damaged brass',
    )
    await user.click(within(dialog).getByRole('combobox', { name: /item, line 1/i }))
    await user.click(await screen.findByRole('option', { name: /BRASS/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Quantity' }), '1.0000')
    await user.click(within(dialog).getByRole('button', { name: /post scrap disposal/i }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', '/inventory/scrap-disposals')).toEqual({
      warehouse_id: WAREHOUSE.id,
      reason: 'Write off the damaged brass',
      disposal_kind: 'WRITE_OFF',
      buyer_reference: null,
      realised_amount: '0',
      lines: [
        {
          item_id: ITEM.id,
          quantity: '1.0000',
          batch_id: null,
          location_id: null,
        },
      ],
    })
  })

  it('posts a bin move with its exact source and destination positions', async () => {
    const { user, fetch } = renderScreen(<OperationTab kind="bin-moves" />, {
      permissions: [P.INVENTORY_VIEW, P.INVENTORY_TRANSFER, P.LOCATION_VIEW, P.ITEM_VIEW],
      routes: {
        'GET /inventory/bin-moves': page([]),
        'GET /warehouses': page([WAREHOUSE]),
        'GET /products/items': page([ITEM]),
        [`GET /products/items/${ITEM.id}`]: ok(ITEM),
        'GET /warehouses/locations': page([BIN]),
        [`GET /warehouses/locations/${BIN.id}`]: ok(BIN),
        'POST /inventory/bin-moves': ok({ code: 'BM-0001' }),
      },
    })
    await user.click(await screen.findByRole('button', { name: /new bin move/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Put received goods on the rack',
    )
    await user.click(within(dialog).getByRole('combobox', { name: /item, line 1/i }))
    await user.click(await screen.findByRole('option', { name: /BRASS/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Quantity' }), '2.5000')
    await user.click(within(dialog).getByRole('combobox', { name: 'To bin' }))
    await user.click(await screen.findByRole('option', { name: /BIN-B/ }))
    await user.click(within(dialog).getByRole('button', { name: /post bin move/i }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', '/inventory/bin-moves')).toEqual({
      warehouse_id: WAREHOUSE.id,
      reason: 'Put received goods on the rack',
      lines: [
        {
          item_id: ITEM.id,
          quantity: '2.5000',
          batch_id: null,
          from_location_id: null,
          to_location_id: BIN.id,
        },
      ],
    })
  })
})

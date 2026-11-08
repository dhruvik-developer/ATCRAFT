/**
 * The routings register, reached from its list.
 *
 * The row opens a routing on a pointer click, but a `<tr onClick>` is not in
 * the tab order, and the row's only other control is Edit - offered on a draft,
 * and only to `routing.manage`. So the code is a real button for the same
 * panel, or an approved routing's operations could not be read from the
 * keyboard at all.
 */

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { RoutingDetail } from './routing-model'
import RoutingPage from './routing-page'

const STAMP = '2026-09-01T09:00:00Z'

const ROUTING: RoutingDetail = {
  id: 'rt-1',
  code: 'RT-001',
  name: 'Hinge body',
  item_id: 'item-1',
  version: 2,
  status: 'APPROVED',
  is_default: true,
  effective_from: '2026-09-01',
  effective_to: null,
  approved_by_id: null,
  approved_at: null,
  created_at: STAMP,
  updated_at: STAMP,
  notes: null,
  operations: [],
}

/** The list, plus everything the detail panel reads once it opens. */
const ROUTES = {
  'GET /routing': page([ROUTING]),
  'GET /routing/rt-1': ok(ROUTING),
  'GET /routing/rt-1/cost': ok({ operations: [] }),
  'GET /routing/work-centers': page([]),
  'GET /routing/machines': page([]),
}

describe('opening a routing from the list', () => {
  it('opens the routing from its code with the keyboard', async () => {
    const { user } = renderScreen(<RoutingPage />, {
      permissions: [P.ROUTING_VIEW],
      routes: ROUTES,
    })

    const code = await screen.findByRole('button', { name: 'RT-001' })
    code.focus()
    await user.keyboard('{Enter}')

    expect(await screen.findByRole('dialog', { name: 'RT-001 - Hinge body' })).toBeVisible()
  })

  it('still opens the routing from a click anywhere on the row', async () => {
    const { user } = renderScreen(<RoutingPage />, {
      permissions: [P.ROUTING_VIEW],
      routes: ROUTES,
    })

    await user.click(await screen.findByText('Hinge body'))

    expect(await screen.findByRole('dialog', { name: 'RT-001 - Hinge body' })).toBeVisible()
  })
})

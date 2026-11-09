/**
 * Switching a warehouse off, and back on, with an audited reason.
 *
 * The status change collected its reason in a hand-rolled box with no length
 * cap. The API stores 255 characters and refuses more with a field error on
 * `reason`, which the global handler leaves to the screen - and this screen
 * rendered nothing, so the button spun, stopped, and the warehouse stayed as it
 * was with no word why. It now goes through the shared ReasonDialog.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { REASON_MAX_LENGTH } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import type { Warehouse } from './warehouse-model'
import WarehousesPage from './warehouses-page'

const WAREHOUSE: Warehouse = {
  id: 'wh000000-0000-7000-8000-000000000001',
  code: 'WH-MAIN',
  name: 'Main store',
  warehouse_type: 'FINISHED_GOODS',
  branch_id: null,
  plant_id: null,
  parent_warehouse_id: null,
  manager_user_id: null,
  address_line1: null,
  address_line2: null,
  city: null,
  state: null,
  state_code: null,
  postal_code: null,
  country: 'IN',
  phone: null,
  email: null,
  is_active: true,
  is_default: false,
  allow_negative_stock: false,
  notes: null,
  location_count: 0,
  created_at: '2026-01-01T09:00:00Z',
  updated_at: '2026-01-01T09:00:00Z',
  deleted_at: null,
}

const DEACTIVATE = `POST /warehouses/${WAREHOUSE.id}/deactivate`

function renderPage(routes: RouteTable) {
  return renderScreen(<WarehousesPage />, {
    permissions: [P.WAREHOUSE_VIEW, P.WAREHOUSE_MANAGE],
    routes: { 'GET /warehouses': page([WAREHOUSE]), ...routes },
  })
}

async function openDeactivate(user: ReturnType<typeof renderPage>['user']): Promise<HTMLElement> {
  await user.click(await screen.findByRole('button', { name: 'Actions for WH-MAIN' }))
  await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))
  return screen.findByRole('dialog', { name: 'Deactivate WH-MAIN?' })
}

describe('changing a warehouse status', () => {
  it('holds the reason to what the API stores and sends it trimmed', async () => {
    const { user, fetch } = renderPage({ [DEACTIVATE]: ok({ ...WAREHOUSE, is_active: false }) })

    const dialog = await openDeactivate(user)
    const reason = within(dialog).getByRole('textbox', { name: 'Reason' })
    expect(reason).toHaveAttribute('maxlength', String(REASON_MAX_LENGTH))

    await user.type(reason, '  Moved to the new plant  ')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', `/warehouses/${WAREHOUSE.id}/deactivate`)).toEqual({
        reason: 'Moved to the new plant',
      }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('shows a refused reason inside the dialog and keeps it open', async () => {
    const refusal = 'String should have at most 255 characters'
    const { user } = renderPage({
      [DEACTIVATE]: fail(422, 'VALIDATION_ERROR', 'Some fields need attention.', {
        fields: { reason: [refusal] },
      }),
    })

    const dialog = await openDeactivate(user)
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason' }), 'Closing the bay')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate' }))

    expect(await within(dialog).findByText(refusal)).toBeVisible()
    expect(screen.getByRole('dialog', { name: 'Deactivate WH-MAIN?' })).toBeVisible()
  })
})

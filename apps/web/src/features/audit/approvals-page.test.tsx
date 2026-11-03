import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import type { SchemaOf } from '@/types/api'
import { page, renderScreen } from '@/test/harness'

import ApprovalsPage from './approvals-page'

type ApprovalRequest = SchemaOf<'ApprovalRequestRead'>

const request: ApprovalRequest = {
  id: '01a00000-0000-7000-8000-000000000101',
  entity_type: 'purchase_order',
  entity_id: '01a00000-0000-7000-8000-000000000102',
  entity_code: 'PO-0001',
  requested_action: 'purchase_order.approve',
  reason: 'Value requires approval',
  payload: null,
  status: 'PENDING',
  decided_by_id: null,
  decision_note: null,
  created_by_id: '01a00000-0000-7000-8000-000000000103',
  created_at: '2026-10-25T10:00:00Z',
  required_permission: 'purchase_order.approve',
  can_decide: true,
}

describe('approvals inbox', () => {
  it('lists permitted requests with a deep link to the owning document', async () => {
    renderScreen(<ApprovalsPage />, {
      permissions: [P.PURCHASE_ORDER_APPROVE],
      route: '/approvals',
      routes: { 'GET /audit/approvals': page([request]) },
    })

    expect(await screen.findByRole('link', { name: 'PO-0001' })).toHaveAttribute(
      'href',
      `/procurement/purchase-orders/${request.entity_id}`,
    )
    expect(screen.getByText('Purchase Order Approve')).toBeVisible()
    expect(screen.getByText('Open on the document')).toBeVisible()
  })

  it('does not expose the queue without any approval permission', () => {
    renderScreen(<ApprovalsPage />, { permissions: [], route: '/approvals' })
    expect(screen.getByRole('heading', { name: 'Approval access required' })).toBeVisible()
  })
})

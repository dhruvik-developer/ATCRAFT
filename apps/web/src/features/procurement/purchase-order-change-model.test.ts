import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'

import {
  MAX_PURCHASE_ORDER_LINES,
  buildPurchaseOrderAmendment,
  canResolveDuplicateCandidate,
  duplicateReasonLimit,
  duplicateResolutionPermission,
  isPoAmendable,
  type AmendmentLineDraft,
} from './purchase-order-change-model'
import type { DuplicateCandidate } from './procurement-types'

function line(overrides: Partial<AmendmentLineDraft> = {}): AmendmentLineDraft {
  return {
    key: 'existing-line',
    lineId: 'po-line-id',
    itemId: 'item-id',
    label: 'Brass handle',
    quantity: '10.0000',
    unitPrice: '125.123456',
    allocations: [],
    ...overrides,
  }
}

function candidate(overrides: Partial<DuplicateCandidate> = {}): DuplicateCandidate {
  return {
    purchase_order_id: 'po-id',
    purchase_order_code: 'PO-0001',
    purchase_order_line_id: 'po-line-id',
    purchase_order_version: 4,
    item_id: 'item-id',
    status: 'DRAFT',
    has_receipt_history: false,
    open_quantity: '10.0000',
    required_date: '2026-11-01',
    confidence: 'EXACT',
    match_reasons: ['Same pending requisition line'],
    recommended_action: 'MERGE_WITH_EXISTING',
    available_actions: ['MERGE_WITH_EXISTING', 'INCREASE_EXISTING_QUANTITY'],
    ...overrides,
  }
}

describe('purchase order amendment boundaries', () => {
  it('retains line identity, Decimal precision and the source document version', () => {
    const result = buildPurchaseOrderAmendment(' Additional production demand ', [line()], 4)
    expect(result.valid).toBe(true)
    expect(result.payload).toEqual({
      expected_version: 4,
      reason: 'Additional production demand',
      lines: [
        { line_id: 'po-line-id', line_number: 1, quantity: '10.0000', unit_price: '125.123456' },
      ],
    })
  })

  it('identifies newly added items without replacing retained line identities', () => {
    const result = buildPurchaseOrderAmendment(
      'Add the matching fasteners',
      [line(), line({ key: 'new', lineId: null, itemId: 'fasteners' })],
      4,
    )
    expect(result.valid).toBe(true)
    expect(result.payload.lines[0]).toHaveProperty('line_id', 'po-line-id')
    expect(result.payload.lines[1]).toMatchObject({ item_id: 'fasteners', line_number: 2 })
    expect(result.payload.lines[1]).not.toHaveProperty('line_id')
  })

  it('requires the operator to choose which requisition quantity is returned', () => {
    const draft = line({
      quantity: '8',
      allocations: [
        {
          requisitionLineId: 'pr-a',
          label: 'PR-0001 line 1',
          quantity: '6',
          originalQuantity: '6',
        },
        {
          requisitionLineId: 'pr-b',
          label: 'PR-0002 line 1',
          quantity: '4',
          originalQuantity: '4',
        },
      ],
    })
    const unresolved = buildPurchaseOrderAmendment('Reduce the requirement', [draft], 4)
    expect(unresolved.valid).toBe(false)
    expect(unresolved.errors['lines.0.source_allocations']).toMatch(
      /choose the requisition quantities/i,
    )

    const decided = buildPurchaseOrderAmendment(
      'Reduce the requirement',
      [
        {
          ...draft,
          allocations: draft.allocations.map((allocation) =>
            allocation.requisitionLineId === 'pr-b' ? { ...allocation, quantity: '2' } : allocation,
          ),
        },
      ],
      4,
    )
    expect(decided.valid).toBe(true)
    expect(decided.payload.lines[0]?.source_allocations).toEqual([
      { requisition_line_id: 'pr-a', quantity: '6' },
      { requisition_line_id: 'pr-b', quantity: '2' },
    ])
  })

  it('explicitly removes a zeroed source allocation while retaining other demand', () => {
    const result = buildPurchaseOrderAmendment(
      'Cancel demand from PR-0002',
      [
        line({
          quantity: '6',
          allocations: [
            {
              requisitionLineId: 'pr-a',
              label: 'PR-0001 line 1',
              quantity: '6',
              originalQuantity: '6',
            },
            {
              requisitionLineId: 'pr-b',
              label: 'PR-0002 line 1',
              quantity: '0',
              originalQuantity: '4',
            },
          ],
        }),
      ],
      4,
    )
    expect(result.valid).toBe(true)
    expect(result.payload.lines[0]?.source_allocations).toEqual([
      { requisition_line_id: 'pr-a', quantity: '6' },
    ])
  })

  it('does not rewrite allocations when only their numeric formatting differs', () => {
    const result = buildPurchaseOrderAmendment(
      'Change the negotiated price',
      [
        line({
          allocations: [
            {
              requisitionLineId: 'pr-a',
              label: 'PR-0001 line 1',
              quantity: '6.0000',
              originalQuantity: '6',
            },
          ],
        }),
      ],
      4,
    )
    expect(result.valid).toBe(true)
    expect(result.payload.lines[0]).not.toHaveProperty('source_allocations')
  })

  it.each(['0', '-1', '1.00001', '100000000000000', 'NaN', 'Infinity', '1e4'])(
    'refuses an invalid order quantity %s',
    (quantity) => {
      const result = buildPurchaseOrderAmendment('Fix planned quantities', [line({ quantity })], 4)
      expect(result.valid).toBe(false)
      expect(result.errors['lines.0.quantity']).toBeTruthy()
    },
  )

  it.each(['-0.01', '1.1234567', '1000000000000', 'NaN', 'Infinity'])(
    'refuses an invalid unit price %s',
    (unitPrice) => {
      const result = buildPurchaseOrderAmendment('Fix the agreed rate', [line({ unitPrice })], 4)
      expect(result.valid).toBe(false)
      expect(result.errors['lines.0.unit_price']).toBeTruthy()
    },
  )

  it('refuses invalid allocations instead of silently dropping an unparseable source', () => {
    const result = buildPurchaseOrderAmendment(
      'Reduce source quantities',
      [
        line({
          allocations: [
            {
              requisitionLineId: 'pr-a',
              label: 'PR-0001 line 1',
              quantity: '-1',
              originalQuantity: '6',
            },
          ],
        }),
      ],
      4,
    )
    expect(result.valid).toBe(false)
    expect(result.errors['lines.0.source_allocations.0.quantity']).toBeTruthy()
  })

  it('bounds reasons and the full resulting line set', () => {
    expect(buildPurchaseOrderAmendment('  ', [line()], 4).errors.reason).toBeTruthy()
    expect(buildPurchaseOrderAmendment('x'.repeat(256), [line()], 4).errors.reason).toBeTruthy()
    expect(buildPurchaseOrderAmendment('Remove every line', [], 4).errors.lines).toBeTruthy()
    expect(
      buildPurchaseOrderAmendment(
        'Too many incoming lines',
        Array.from({ length: MAX_PURCHASE_ORDER_LINES + 1 }, (_, index) =>
          line({ key: String(index) }),
        ),
        4,
      ).errors.lines,
    ).toBeTruthy()
  })
})

describe('backend-authorized duplicate actions', () => {
  it.each(['APPROVED', 'SENT', 'ACKNOWLEDGED'])(
    'allows amendment only with explicit absence of receipt history in %s',
    (status) => {
      expect(isPoAmendable({ status, has_receipt_history: false })).toBe(true)
      expect(isPoAmendable({ status, has_receipt_history: true })).toBe(false)
      expect(isPoAmendable({ status })).toBe(false)
    },
  )

  it.each(['DRAFT', 'PENDING_APPROVAL', 'RECEIVED', 'CANCELLED', 'UNKNOWN'])(
    'does not offer the amendment command in %s',
    (status) => {
      expect(isPoAmendable({ status, has_receipt_history: false })).toBe(false)
    },
  )

  it('requires status, current version, receipt proof and the matcher action hint', () => {
    expect(canResolveDuplicateCandidate(candidate(), 'MERGE')).toBe(true)
    expect(canResolveDuplicateCandidate(candidate({ has_receipt_history: true }), 'MERGE')).toBe(
      false,
    )
    expect(canResolveDuplicateCandidate(candidate({ status: 'UNKNOWN' }), 'MERGE')).toBe(false)
    expect(canResolveDuplicateCandidate(candidate({ purchase_order_version: 0 }), 'MERGE')).toBe(
      false,
    )
    expect(
      canResolveDuplicateCandidate(
        candidate({ available_actions: ['MERGE_WITH_EXISTING'] }),
        'INCREASE',
      ),
    ).toBe(false)
  })

  it('withholds increase when the incoming draft also contains a different item', () => {
    expect(canResolveDuplicateCandidate(candidate(), 'INCREASE', ['item-id'])).toBe(true)
    expect(canResolveDuplicateCandidate(candidate(), 'INCREASE', ['item-id', 'other-item'])).toBe(
      false,
    )
    expect(canResolveDuplicateCandidate(candidate(), 'INCREASE', [])).toBe(false)
    expect(canResolveDuplicateCandidate(candidate(), 'INCREASE')).toBe(false)
  })

  it('uses the action-specific permission and reason bound', () => {
    expect(duplicateResolutionPermission('MERGE')).toBe(P.PURCHASE_ORDER_UPDATE)
    expect(duplicateResolutionPermission('INCREASE')).toBe(P.PURCHASE_ORDER_UPDATE)
    expect(duplicateResolutionPermission('CREATE_WITH_APPROVAL')).toBe(P.PURCHASE_ORDER_CREATE)
    expect(duplicateResolutionPermission('CANCEL_REQUEST')).toBe(P.PURCHASE_REQUISITION_CREATE)
    expect(duplicateReasonLimit('CREATE_WITH_APPROVAL')).toBe(1000)
    expect(duplicateReasonLimit('MERGE')).toBe(255)
    expect(duplicateReasonLimit('INCREASE')).toBe(255)
    expect(duplicateReasonLimit('CANCEL_REQUEST')).toBe(255)
  })
})

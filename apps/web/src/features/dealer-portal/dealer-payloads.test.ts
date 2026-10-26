import { describe, expect, it } from 'vitest'

import type { SchemaOf } from '@/types/api'

import {
  dealerCheckoutPayload,
  dealerQuantity,
  dealerReturnPayload,
  isIsoCalendarDate,
} from './dealer-payloads'
import { bannerPayload, offerPayload } from './promotion-payloads'

function fields(values: Record<string, string>) {
  const result = new FormData()
  Object.entries(values).forEach(([name, value]) => result.set(name, value))
  return result
}

const dispatch: SchemaOf<'DealerEligibleDispatch'> = {
  id: 'dispatch',
  code: 'DN/1',
  dispatched_at: null,
  lines: [
    {
      dispatch_line_id: 'line',
      item_id: 'item',
      item_code: 'HANDLE',
      item_name: 'Handle',
      returnable_quantity: '2.5',
    },
  ],
}

describe('dealer input contracts', () => {
  it.each(['1', '0.0001', '99999999999999.9999'])('preserves exact decimal quantity %s', (value) =>
    expect(dealerQuantity(value)).toBe(value),
  )
  it.each(['', '0', '-1', '1.12345', '1e3', 'Infinity', 'NaN', '100000000000000', ' 1 ', '0x10'])(
    'refuses unsafe quantity %s',
    (value) => expect(() => dealerQuantity(value)).toThrow(/positive quantity/),
  )
  it.each(['2026-02-29', '2026-13-01', '2026-02-31', '0000-01-01', 'tomorrow', '2026-1-01'])(
    'refuses invalid calendar date %s',
    (value) => expect(isIsoCalendarDate(value)).toBe(false),
  )
  it('accepts a leap day in a leap year', () => expect(isIsoCalendarDate('2028-02-29')).toBe(true))
  it('submits the cart version and addresses without client prices or customer identity', () => {
    expect(
      dealerCheckoutPayload(
        7,
        fields({
          required_date: '2026-11-01',
          shipping_address_id: 'ship',
          notes: '  urgent  ',
          total_amount: '0.01',
          customer_id: 'another',
          discount_percentage: '100',
        }),
        '2026-10-24',
      ),
    ).toEqual({
      cart_version: 7,
      required_date: '2026-11-01',
      shipping_address_id: 'ship',
      billing_address_id: null,
      customer_po_number: null,
      notes: 'urgent',
    })
  })
  it('requires a current cart version and a present or future required date', () => {
    expect(() =>
      dealerCheckoutPayload(null, fields({ required_date: '2026-11-01' }), '2026-10-24'),
    ).toThrow(/Refresh/)
    expect(() =>
      dealerCheckoutPayload(1, fields({ required_date: '2026-10-23' }), '2026-10-24'),
    ).toThrow(/today or later/)
    expect(() =>
      dealerCheckoutPayload(1, fields({ required_date: '2026-02-31' }), '2026-01-01'),
    ).toThrow(/today or later/)
  })
  it('builds return lines solely from the eligible dispatch', () => {
    expect(
      dealerReturnPayload(
        dispatch,
        fields({
          'quantity-line': '1.25',
          'reason-line': 'DAMAGED',
          'note-line': 'Bent',
          'quantity-forged': '100',
          notes: '  Please inspect  ',
        }),
      ),
    ).toEqual({
      dispatch_id: 'dispatch',
      lines: [{ dispatch_line_id: 'line', quantity: '1.25', reason_code: 'DAMAGED', note: 'Bent' }],
      notes: 'Please inspect',
    })
  })
  it('refuses empty returns, excessive quantities and absent line reasons', () => {
    expect(() => dealerReturnPayload(dispatch, fields({}))).toThrow(/at least one line/)
    expect(() =>
      dealerReturnPayload(
        dispatch,
        fields({ 'quantity-line': '2.5001', 'reason-line': 'DAMAGED' }),
      ),
    ).toThrow(/exceeds/)
    expect(() => dealerReturnPayload(dispatch, fields({ 'quantity-line': '1' }))).toThrow(/reason/)
  })
})

const promotion = {
  code: 'OFFER-1',
  title: 'Autumn offer',
  sort_order: '2',
  valid_from: '2026-10-24',
  valid_to: '2026-11-24',
  audience_price_type: 'DEALER',
}

describe('promotion payloads', () => {
  it.each(['ALL', 'ITEM', 'CATEGORY', 'BRAND'])(
    'includes exactly the target for %s scope',
    (scope) => {
      const payload = offerPayload(
        fields({
          ...promotion,
          scope_kind: scope,
          min_quantity: '2.25',
          discount_percentage: '5.125',
        }),
        'target',
        'customer',
      )
      expect(payload).toMatchObject({
        scope_kind: scope,
        customer_id: 'customer',
        min_quantity: '2.25',
        discount_percentage: '5.125',
        is_active: false,
      })
      for (const kind of ['ITEM', 'CATEGORY', 'BRAND'] as const)
        expect(payload[`${kind.toLowerCase()}_id` as 'item_id' | 'category_id' | 'brand_id']).toBe(
          kind === scope ? 'target' : null,
        )
    },
  )
  it.each(['0', '-1', '100.0001', '1e2', 'NaN'])('rejects invalid discount %s', (discount) =>
    expect(() =>
      offerPayload(
        fields({
          ...promotion,
          scope_kind: 'ALL',
          min_quantity: '1',
          discount_percentage: discount,
        }),
        null,
        null,
      ),
    ).toThrow(/Discount/),
  )
  it('requires a scope target, valid dates and a bounded sort order', () => {
    expect(() =>
      offerPayload(
        fields({ ...promotion, scope_kind: 'ITEM', min_quantity: '1', discount_percentage: '5' }),
        null,
        null,
      ),
    ).toThrow(/scope target/)
    expect(() =>
      bannerPayload(fields({ ...promotion, valid_to: '2026-10-23' }), 'image', null),
    ).toThrow(/end date/)
    expect(() =>
      bannerPayload(fields({ ...promotion, valid_to: '2026-02-31' }), 'image', null),
    ).toThrow(/valid promotion date/)
    expect(() => bannerPayload(fields({ ...promotion, sort_order: '1.5' }), 'image', null)).toThrow(
      /whole number/,
    )
  })
  it('defaults a new banner to inactive and requires its image and link target', () => {
    expect(
      bannerPayload(fields({ ...promotion, link_kind: 'NONE' }), 'image', 'ignored'),
    ).toMatchObject({
      image_document_id: 'image',
      link_kind: 'NONE',
      link_id: null,
      status: 'INACTIVE',
    })
    expect(() => bannerPayload(fields(promotion), '', null)).toThrow(/Upload/)
    expect(() => bannerPayload(fields({ ...promotion, link_kind: 'ITEM' }), 'image', null)).toThrow(
      /link target/,
    )
  })
})

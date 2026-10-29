import { describe, expect, it } from 'vitest'

import {
  isPoCancellable,
  isPoClosable,
  isPoSendable,
  poStageIndex,
  PO_LIFECYCLE,
} from './procurement-types'

describe('a purchase order the vendor declined', () => {
  it('can be sent again or cancelled by the buyer', () => {
    // The machine's VENDOR_DECLINED -> SENT and VENDOR_DECLINED -> CANCELLED edges.
    expect(isPoSendable('VENDOR_DECLINED')).toBe(true)
    expect(isPoCancellable('VENDOR_DECLINED')).toBe(true)
  })

  it('stays at the Sent stage of the lifecycle', () => {
    expect(PO_LIFECYCLE[poStageIndex('VENDOR_DECLINED')]?.label).toBe('Sent')
  })

  it('leaves the other send and cancel rules as they were', () => {
    expect(isPoSendable('APPROVED')).toBe(true)
    expect(isPoSendable('SENT')).toBe(false)
    expect(isPoCancellable('PARTIALLY_RECEIVED')).toBe(false)
    expect(isPoCancellable('ACKNOWLEDGED')).toBe(true)
  })

  it('closes only partially received, GRN-completed, or paid orders', () => {
    expect(isPoClosable('PARTIALLY_RECEIVED')).toBe(true)
    expect(isPoClosable('GRN_COMPLETED')).toBe(true)
    expect(isPoClosable('PAID')).toBe(true)
    expect(isPoClosable('INVOICED')).toBe(false)
    expect(isPoClosable('CANCELLED')).toBe(false)
  })
})

/**
 * The allocation arithmetic every settlement editor shares.
 *
 * Three editors - vendor payments, customer receipts and vendor debit notes -
 * send their whole allocation set with `replace: true`, and the server bounds
 * each invoice by what it owes *plus* what this document already put against
 * it, because the replace releases that amount first. The editors used to work
 * this out three times and had drifted: the vendor-payment and debit-note
 * copies left the add-back out, so an invoice taken out of the draft and added
 * again could not be given its own amount back.
 */

import Decimal from 'decimal.js'
import { describe, expect, it } from 'vitest'

import { allocatedByInvoice, allocationCeiling, proposeAllocation } from './payment-model'

describe('allocatedByInvoice', () => {
  it('keys each allocation by the invoice it names, vendor or sales', () => {
    const byInvoice = allocatedByInvoice([
      { amount: '600.00', vendor_invoice_id: 'vi-1', sales_invoice_id: null },
      { amount: '50.00', vendor_invoice_id: null, sales_invoice_id: 'si-1' },
      { amount: '25.00', vendor_invoice_id: 'vi-2' },
    ])
    expect([...byInvoice]).toEqual([
      ['vi-1', '600.00'],
      ['si-1', '50.00'],
      ['vi-2', '25.00'],
    ])
  })

  it('skips a row that names no invoice, and is empty for a document with none', () => {
    expect(
      allocatedByInvoice([{ amount: '9.00', vendor_invoice_id: null, sales_invoice_id: null }])
        .size,
    ).toBe(0)
    expect(allocatedByInvoice([]).size).toBe(0)
    expect(allocatedByInvoice(null).size).toBe(0)
    expect(allocatedByInvoice(undefined).size).toBe(0)
  })
})

describe('allocationCeiling', () => {
  it('adds back what this document already puts against the invoice', () => {
    expect(allocationCeiling('400.00', '600.00').toFixed(2)).toBe('1000.00')
  })

  it('is the outstanding alone for an invoice this document never settled', () => {
    expect(allocationCeiling('400.00', null).toFixed(2)).toBe('400.00')
    expect(allocationCeiling('400.00', undefined).toFixed(2)).toBe('400.00')
  })

  it('adds in decimal, so the paisa it reports are the paisa owed', () => {
    // 0.1 + 0.2 as doubles is 0.30000000000000004.
    expect(allocationCeiling('0.10', '0.20').toString()).toBe('0.3')
  })
})

describe('proposeAllocation', () => {
  it('fills the invoice to its ceiling when the document has room for it', () => {
    expect(proposeAllocation(new Decimal('1000.00'), '5000.00', new Decimal('0'))).toBe('1000.00')
  })

  it('caps the line at what the document has left after the lines already drafted', () => {
    expect(proposeAllocation(new Decimal('1000.00'), '1500.00', new Decimal('900.00'))).toBe(
      '600.00',
    )
  })

  it('never proposes less than nothing when the draft already settles too much', () => {
    expect(proposeAllocation(new Decimal('1000.00'), '1000.00', new Decimal('1200.00'))).toBe(
      '0.00',
    )
  })

  it('gives a released invoice its own amount back when it is added again', () => {
    // A 600 settlement against an invoice that now owes 400: the replace frees
    // the 600, so it can be restored even though it is above the outstanding.
    expect(
      proposeAllocation(allocationCeiling('400.00', '600.00'), '2000.00', new Decimal('1400.00')),
    ).toBe('600.00')
  })

  it('answers in two places, as the wire stores money', () => {
    expect(proposeAllocation(new Decimal('118'), '200', new Decimal('0'))).toBe('118.00')
  })
})

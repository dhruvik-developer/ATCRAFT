/**
 * The payables queue: every supplier bill, and the three-way match behind it.
 *
 * What this suite is for, in the order it matters.
 *
 * **Keying a bill and clearing it for payment are different rights.**
 * `vendor_invoice.create` is what the API declares on POST, PATCH, DELETE and
 * submit alike; `vendor_invoice.approve` is what clears the bill for payment,
 * and it is deliberately a different person's job. A control on the wrong side
 * of that line lets whoever entered an invoice also authorise paying it, which
 * is the entire reason the two rights exist.
 *
 * **The entry form is where money is defined.** Every line is priced with
 * decimal.js in a mirror of `service.price_invoice_line`, rounding at exactly
 * the four points the server rounds at - and the CGST half is rounded while
 * SGST is the *remainder*, so that on an odd number of paise the two halves
 * still add up to the tax the `tax_amount = cgst + sgst + igst` CHECK
 * constraint requires. The fixture below is chosen so that the tax lands on an
 * odd paisa: a second independent halving gives 22.51 + 22.51 = 45.02 against a
 * tax of 45.01, and the row would be refused on insert.
 *
 * **What reaches the wire stays a string.** Quantities, prices, discounts, tax
 * rates and the round-off are decimal strings; a field the API defaults to zero
 * is sent as "0" rather than blank; and a field where *absent* means something
 * - the vendor's agreed credit days, the company's own denomination - is
 * omitted from the request rather than sent as null.
 *
 * A note on where the arithmetic is asserted. The pricing preview is pinned on
 * an *amendment*, where the form is seeded from a saved bill, rather than by
 * typing lines into a new one. That is not a preference: the preview does not
 * currently refresh as line figures are typed (reported as a finding with this
 * suite), so asserting it on the entry path would pin the stale figures instead
 * of the arithmetic. The entry path is tested for what it *sends*, which is
 * correct, and the amendment path for what it *shows*.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import type {
  MatchResult,
  VendorInvoiceDetail,
  VendorInvoiceLine,
  VendorInvoiceSummary,
} from './finance-types'
import VendorInvoicesPage from './vendor-invoices-page'

const INVOICE_ID = 'vi000000-0000-7000-8000-000000000001'
const INVOICE_PATH = `/finance/vendor-invoices/${INVOICE_ID}`
const NUMBER = 'SBW/4419'

const VENDOR = {
  id: 'vn000000-0000-7000-8000-000000000001',
  code: 'VEN-0007',
  name: 'Sharma Brass Works',
  is_approved: true,
  lead_time_days: 14,
}

function summary(overrides: Partial<VendorInvoiceSummary> = {}): VendorInvoiceSummary {
  return {
    id: INVOICE_ID,
    code: 'VINV/2026-27/00031',
    vendor_id: VENDOR.id,
    vendor_invoice_number: NUMBER,
    invoice_date: '2026-09-01',
    due_date: '2026-10-01',
    status: 'DRAFT',
    match_status: 'MATCHED',
    currency: 'INR',
    subtotal: '1200.06',
    tax_amount: '159.01',
    total_amount: '1359.00',
    paid_amount: '0.00',
    outstanding_amount: '1359.00',
    purchase_order_id: null,
    goods_receipt_id: null,
    invoice_type: 'PURCHASE',
    job_work_order_id: null,
    cost_centre_id: null,
    created_at: '2026-09-01T09:00:00Z',
    ...overrides,
  }
}

function line(overrides: Partial<VendorInvoiceLine> = {}): VendorInvoiceLine {
  return {
    id: 'vl000000-0000-7000-8000-000000000001',
    line_number: 1,
    item_id: null,
    purchase_order_line_id: null,
    goods_receipt_line_id: null,
    cost_centre_id: null,
    description: 'Brass rod 12mm',
    hsn_code: null,
    quantity: '2.0000',
    uom_id: null,
    unit_price: '125.030000',
    discount_amount: '0.0000',
    taxable_amount: '250.06',
    tax_rate: '18.0000',
    cgst_amount: '22.51',
    sgst_amount: '22.50',
    igst_amount: '0.00',
    tax_amount: '45.01',
    line_total: '295.07',
    job_work_receipt_line_id: null,
    recovery_amount: '0.0000',
    notes: null,
    ...overrides,
  }
}

function invoice(overrides: Partial<VendorInvoiceDetail> = {}): VendorInvoiceDetail {
  return {
    ...summary(),
    round_off_amount: '0.00',
    match_variance_amount: '0.00',
    matched_at: '2026-09-01T09:05:00Z',
    variance_override_reason: null,
    approved_by_id: null,
    approved_at: null,
    dispute_reason: null,
    cancellation_reason: null,
    recovered_amount: '0.0000',
    notes: 'Received with the September consignment',
    version: 1,
    lines: [line()],
    ...overrides,
  }
}

function matchResult(overrides: Partial<MatchResult> = {}): MatchResult {
  return {
    invoice_id: INVOICE_ID,
    invoice_code: 'VINV/2026-27/00031',
    vendor_id: VENDOR.id,
    purchase_order_id: null,
    goods_receipt_id: null,
    job_work_order_id: null,
    matched: true,
    match_status: 'MATCHED',
    price_tolerance: '0.02',
    quantity_tolerance: '0.02',
    total_invoiced: '1359.00',
    total_expected: '1359.00',
    total_variance: '0.00',
    worst_variance: '0.00',
    blocking_reasons: [],
    has_overbilling: false,
    lines: [],
    ...overrides,
  }
}

/** The whole match dialog, opened from the row it belongs to. */
async function openMatchDialog(
  user: ReturnType<typeof renderScreen>['user'],
): Promise<HTMLElement> {
  await user.click(await screen.findByRole('button', { name: NUMBER }))
  return screen.findByRole('dialog')
}

/**
 * A line's inputs, disambiguated by position.
 *
 * Every line repeats the same accessible names - "Quantity", "Unit price",
 * "Discount", "GST rate %" - so the only thing separating line 2 from line 1 is
 * document order. That is also an accessibility gap, reported with this suite:
 * a screen reader hears "Quantity" twice with nothing to say which line it
 * belongs to.
 */
function lineInput(dialog: HTMLElement, name: string | RegExp, index: number): HTMLElement {
  const fields = within(dialog).getAllByRole('textbox', { name })
  expect(fields[index], `no field "${String(name)}" on line ${index + 1}`).toBeDefined()
  return fields[index] as HTMLElement
}

// -------------------------------------------------------- permission gating --

describe('recording a bill and clearing it for payment', () => {
  it('offers entry to vendor_invoice.create and not to an approver', async () => {
    // Whoever keys a bill must not be the person who authorises paying it. The
    // API declares `vendor_invoice.create` on POST, PATCH, DELETE and submit
    // alike, so the entry point follows that right - not the approval right the
    // page sits next to.
    const approver = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE],
      routes: { 'GET /finance/vendor-invoices': page([summary()]) },
    })
    expect(await screen.findByText('VINV/2026-27/00031')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /record invoice/i })).not.toBeInTheDocument()
    approver.unmount()

    renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: { 'GET /finance/vendor-invoices': page([summary()]) },
    })
    expect(await screen.findByText('VINV/2026-27/00031')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /record invoice/i })).toBeInTheDocument()
  })

  it('gives the recorder the document commands and never the approval', async () => {
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: {
        'GET /finance/vendor-invoices': page([summary()]),
        [`GET ${INVOICE_PATH}`]: ok(invoice()),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
      },
    })

    const dialog = await openMatchDialog(user)
    expect(await within(dialog).findByRole('button', { name: /amend/i })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /submit for approval/i })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /delete draft/i })).toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: /approve invoice/i }),
    ).not.toBeInTheDocument()
  })

  it('gives the approver the approval and none of the document commands', async () => {
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ status: 'PENDING_APPROVAL' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ status: 'PENDING_APPROVAL' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
      },
    })

    const dialog = await openMatchDialog(user)
    expect(
      await within(dialog).findByRole('button', { name: /approve invoice/i }),
    ).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /amend/i })).not.toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: /submit for approval/i }),
    ).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /delete draft/i })).not.toBeInTheDocument()
  })
})

// ------------------------------------------------------- lifecycle gating ----

describe('a command is offered only in the status its state machine permits', () => {
  it('drops submit and delete once the bill has left draft, and keeps amend', async () => {
    // `EDITABLE_INVOICE_STATUSES` deliberately includes PENDING_APPROVAL: an
    // approver who sends a bill back for correction does not move it out of the
    // queue. Submitting and deleting are draft-only, because one re-runs the
    // match on something already queued and the other hard-deletes a row that
    // somebody is reviewing.
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ status: 'PENDING_APPROVAL' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ status: 'PENDING_APPROVAL' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
      },
    })

    const dialog = await openMatchDialog(user)
    expect(await within(dialog).findByRole('button', { name: /amend/i })).toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: /submit for approval/i }),
    ).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /delete draft/i })).not.toBeInTheDocument()
  })

  it('offers nothing once the bill has reached the ledger', async () => {
    // An APPROVED invoice has posted. There is no edge from here that any of
    // these four endpoints would accept, so a user holding both rights is
    // offered none of them rather than walked through a form for a refusal.
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE, P.VENDOR_INVOICE_APPROVE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ status: 'APPROVED' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ status: 'APPROVED' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
      },
    })

    const dialog = await openMatchDialog(user)
    // The dialog really did load - the assertions below are absences, and an
    // absence proves nothing against a dialog that never painted.
    expect(await within(dialog).findByText('VINV/2026-27/00031')).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /amend/i })).not.toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: /submit for approval/i }),
    ).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /delete draft/i })).not.toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: /approve invoice/i }),
    ).not.toBeInTheDocument()
  })

  it('will not let an approval be sent while the match verdict is unreadable', async () => {
    // The endpoint decides on the match; approving without having read it is a
    // guess, and the variance it would silently override is exactly what the
    // override reason exists to record.
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ status: 'PENDING_APPROVAL' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ status: 'PENDING_APPROVAL' })),
        [`GET ${INVOICE_PATH}/match`]: fail(
          503,
          'SERVICE_UNAVAILABLE',
          'The match service is unavailable',
        ),
      },
    })

    const dialog = await openMatchDialog(user)
    expect(await within(dialog).findByText('The match could not be read')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /approve invoice/i })).toBeDisabled()
  })
})

// ---------------------------------------------------------- approval body ----

describe('approving a bill', () => {
  it('sends a null override reason rather than an empty string on a clean match', async () => {
    // `override_reason` is bounded at three characters whenever it is present,
    // so "" is a 422 and null is the spelling that means "nothing to override".
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ status: 'PENDING_APPROVAL' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ status: 'PENDING_APPROVAL' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
        [`POST ${INVOICE_PATH}/approve`]: ok(invoice({ status: 'APPROVED' })),
      },
    })

    const dialog = await openMatchDialog(user)
    await user.click(await within(dialog).findByRole('button', { name: /approve invoice/i }))

    const confirm = await screen.findByRole('dialog', { name: /approve this invoice/i })
    await user.click(within(confirm).getByRole('button', { name: /approve/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `${INVOICE_PATH}/approve`)).toEqual({ override_reason: null })
  })

  it('refuses a one-word note on a clean approval rather than losing it to a silent 422', async () => {
    // On a clean match the box is an optional note, but it still travels as
    // `override_reason`, which is 3..500 whenever present. Sent, a two-letter
    // note comes back a 422 with a field error that the mutation cache stays
    // quiet about - and this dialog has no form to show it - so the spinner
    // would stop and nothing at all would be said.
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ status: 'PENDING_APPROVAL' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ status: 'PENDING_APPROVAL' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
        [`POST ${INVOICE_PATH}/approve`]: ok(invoice({ status: 'APPROVED' })),
      },
    })

    const dialog = await openMatchDialog(user)
    await user.click(await within(dialog).findByRole('button', { name: /approve invoice/i }))

    const confirm = await screen.findByRole('dialog', { name: /approve this invoice/i })
    await user.type(within(confirm).getByRole('textbox', { name: 'Note' }), 'ok')
    await user.click(within(confirm).getByRole('button', { name: /approve/i }))

    expect(await within(confirm).findByRole('alert')).toHaveTextContent(
      'A note is optional, but one that is written must run to at least 3 characters',
    )
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('will not send an override until a real sentence explains it', async () => {
    // Approving over a variance concedes the price or quantity difference to
    // the vendor. The reason is what an auditor reads afterwards, so a two-word
    // note is refused before the request rather than after it.
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE],
      routes: {
        'GET /finance/vendor-invoices': page([
          summary({ status: 'PENDING_APPROVAL', match_status: 'VARIANCE' }),
        ]),
        [`GET ${INVOICE_PATH}`]: ok(
          invoice({ status: 'PENDING_APPROVAL', match_status: 'VARIANCE' }),
        ),
        [`GET ${INVOICE_PATH}/match`]: ok(
          matchResult({
            matched: false,
            match_status: 'VARIANCE',
            blocking_reasons: ['Line 1 is billed above the ordered price'],
            total_variance: '112.00',
          }),
        ),
        [`POST ${INVOICE_PATH}/approve`]: ok(invoice({ status: 'APPROVED' })),
      },
    })

    const dialog = await openMatchDialog(user)
    expect(
      await within(dialog).findByText('Line 1 is billed above the ordered price'),
    ).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /approve invoice/i }))

    // The confirmation renames itself when the match is out of tolerance: this
    // is an override, and the box below it stops being optional.
    const confirm = await screen.findByRole('dialog', { name: /approve over a match variance/i })
    await user.click(within(confirm).getByRole('button', { name: /approve/i }))
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(within(confirm).getByRole('alert')).toHaveTextContent(
      'Record at least 10 characters explaining the override',
    )

    await user.type(
      within(confirm).getByRole('textbox', { name: 'Override reason' }),
      'Freight surcharge agreed with the buyer by email',
    )
    await user.click(within(confirm).getByRole('button', { name: /approve/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `${INVOICE_PATH}/approve`)).toEqual({
      override_reason: 'Freight surcharge agreed with the buyer by email',
    })
  })
})

// ------------------------------------------------------------ the entry form -

describe('recording a multi-line bill', () => {
  /**
   * Two lines chosen so the arithmetic has somewhere to go wrong.
   *
   * Line 1: 2 x 125.03 at 18% intrastate. Taxable 250.06, tax 45.0108 which
   * rounds to 45.01 - an ODD number of paise, so CGST is 22.51 and SGST is the
   * remaining 22.50. Halving twice would give 22.51 + 22.51 = 45.02 and break
   * the `tax_amount = cgst + sgst + igst` CHECK on the row.
   *
   * Line 2: 10 x 99.99 less a 49.90 discount at 12% interstate. Taxable 950.00,
   * IGST 114.00.
   *
   * Header: 1200.06 taxable, 159.01 tax, less a 0.07 round-off, so the bill
   * comes to a clean 1359.00 - which is what round-off is for.
   */
  const routes = {
    'GET /finance/vendor-invoices': page([summary()]),
    'GET /vendors': page([VENDOR]),
    'POST /finance/vendor-invoices': ok(invoice()),
  }

  async function fillTwoLines(
    user: ReturnType<typeof renderScreen>['user'],
    dialog: HTMLElement,
  ): Promise<void> {
    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /invoice number/i }), NUMBER)

    await user.click(within(dialog).getByRole('button', { name: /add line/i }))
    await user.type(lineInput(dialog, 'Description', 0), 'Brass rod 12mm')
    await user.type(lineInput(dialog, 'Quantity', 0), '2')
    await user.type(lineInput(dialog, 'Unit price', 0), '125.03')
    await user.type(lineInput(dialog, 'GST rate %', 0), '18')

    // The second line inherits the rate and the place of supply from the first,
    // which is what a bill taxed at one slab throughout actually looks like -
    // so both are cleared here rather than assumed.
    await user.click(within(dialog).getByRole('button', { name: /add line/i }))
    await user.type(lineInput(dialog, 'Description', 1), 'Handle castings')
    await user.type(lineInput(dialog, 'Quantity', 1), '10')
    await user.type(lineInput(dialog, 'Unit price', 1), '99.99')
    await user.type(lineInput(dialog, 'Discount', 1), '49.90')
    await user.clear(lineInput(dialog, 'GST rate %', 1))
    await user.type(lineInput(dialog, 'GST rate %', 1), '12')
    await user.click(within(dialog).getAllByRole('checkbox', { name: /interstate/i })[1]!)

    await user.type(within(dialog).getByRole('textbox', { name: /round off/i }), '-0.07')
  }

  it('sends every figure as a string, zero for a blank default, and nothing for an absent one', async () => {
    // A float anywhere near a monetary value is a defect. Beyond that: a blank
    // discount or rate is "0" because the API defaults those to zero, while a
    // blank currency or due date is OMITTED - absent means the company's own
    // denomination and the vendor's agreed credit days, and null would say
    // something different.
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE, P.VENDOR_VIEW],
      routes,
    })

    await screen.findByText('VINV/2026-27/00031')
    await user.click(screen.getByRole('button', { name: /record invoice/i }))
    const dialog = await screen.findByRole('dialog')
    await fillTwoLines(user, dialog)
    await user.click(within(dialog).getByRole('button', { name: /record the invoice/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/finance/vendor-invoices') as Record<string, unknown>

    expect(Object.keys(body)).not.toContain('currency')
    expect(Object.keys(body)).not.toContain('due_date')
    expect(body.vendor_id).toBe(VENDOR.id)
    expect(body.round_off_amount).toBe('-0.07')
    expect(typeof body.round_off_amount).toBe('string')

    expect(body.lines).toEqual([
      {
        item_id: null,
        uom_id: null,
        description: 'Brass rod 12mm',
        hsn_code: null,
        quantity: '2',
        unit_price: '125.03',
        // Blank, and the API defaults it to zero - so "0", not null and not "".
        discount_amount: '0',
        tax_rate: '18',
        is_interstate: false,
        purchase_order_line_id: null,
        goods_receipt_line_id: null,
        // "As the invoice": no account here may read cost centres, so none is
        // offered and none is named.
        cost_centre_id: null,
        notes: null,
      },
      {
        item_id: null,
        uom_id: null,
        description: 'Handle castings',
        hsn_code: null,
        quantity: '10',
        unit_price: '99.99',
        discount_amount: '49.90',
        tax_rate: '12',
        is_interstate: true,
        purchase_order_line_id: null,
        goods_receipt_line_id: null,
        cost_centre_id: null,
        notes: null,
      },
    ])

    // Nothing derived travels: the server prices each line itself, and
    // `VendorInvoiceLineIn` forbids unknown keys, so a taxable amount or a
    // CGST split computed here would be a 422 rather than a helpful hint.
    for (const sent of body.lines as Record<string, unknown>[]) {
      expect(Object.keys(sent)).not.toContain('taxable_amount')
      expect(Object.keys(sent)).not.toContain('cgst_amount')
      expect(typeof sent.quantity).toBe('string')
      expect(typeof sent.unit_price).toBe('string')
      expect(typeof sent.tax_rate).toBe('string')
    }
  })

  it('refuses a discount larger than the line it comes off, and names the line', async () => {
    // `price_invoice_line` raises on this, and a document-level refusal would
    // leave a clerk hunting through twenty lines for the one at fault.
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE, P.VENDOR_VIEW],
      routes,
    })

    await screen.findByText('VINV/2026-27/00031')
    await user.click(screen.getByRole('button', { name: /record invoice/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /invoice number/i }), NUMBER)
    await user.click(within(dialog).getByRole('button', { name: /add line/i }))
    await user.type(lineInput(dialog, 'Quantity', 0), '2')
    await user.type(lineInput(dialog, 'Unit price', 0), '100.00')
    await user.type(lineInput(dialog, 'Discount', 0), '250.00')
    await user.click(within(dialog).getByRole('button', { name: /record the invoice/i }))

    expect(
      await within(dialog).findByText("A discount cannot exceed the line's own value"),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('refuses a bill that fell due before it was raised', async () => {
    // `update_invoice` refuses it outright, and it is a keying slip in either
    // direction: the bill would sit at the top of the overdue queue from the
    // moment it was entered.
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE, P.VENDOR_VIEW],
      routes,
    })

    await screen.findByText('VINV/2026-27/00031')
    await user.click(screen.getByRole('button', { name: /record invoice/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /invoice number/i }), NUMBER)
    await user.click(within(dialog).getByRole('button', { name: /add line/i }))
    await user.type(lineInput(dialog, 'Quantity', 0), '1')
    await user.type(lineInput(dialog, 'Unit price', 0), '10.00')
    await user.type(within(dialog).getByLabelText('Due date'), '2020-01-01')
    await user.click(within(dialog).getByRole('button', { name: /record the invoice/i }))

    expect(
      await within(dialog).findByText('The due date cannot fall before the invoice date'),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('lands a server field error on the line it belongs to', async () => {
    // The API addresses a field-array entry as `lines.0.quantity`, which is
    // exactly how react-hook-form addresses it. If that mapping breaks, a
    // rejected bill shows nothing at all and the clerk re-submits it unchanged.
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE, P.VENDOR_VIEW],
      routes: {
        ...routes,
        'POST /finance/vendor-invoices': fail(422, 'VALIDATION_ERROR', 'Invalid invoice', {
          fields: {
            vendor_invoice_number: ['This vendor has already billed that number'],
            'lines.0.quantity': ['exceeds the quantity accepted at the gate'],
          },
        }),
      },
    })

    await screen.findByText('VINV/2026-27/00031')
    await user.click(screen.getByRole('button', { name: /record invoice/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /invoice number/i }), NUMBER)
    await user.click(within(dialog).getByRole('button', { name: /add line/i }))
    await user.type(lineInput(dialog, 'Quantity', 0), '5')
    await user.type(lineInput(dialog, 'Unit price', 0), '10.00')
    await user.click(within(dialog).getByRole('button', { name: /record the invoice/i }))

    expect(
      await within(dialog).findByText('This vendor has already billed that number'),
    ).toBeInTheDocument()
    expect(
      within(dialog).getByText('exceeds the quantity accepted at the gate'),
    ).toBeInTheDocument()
  })

  it('says so rather than failing on a vendor field it never showed', async () => {
    // A bill has to name the vendor who sent it, and the vendor master is its
    // own right. Without it the required field is not on screen at all, so the
    // form would refuse on something the user cannot see or fix.
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: { 'GET /finance/vendor-invoices': page([summary()]) },
    })

    await screen.findByText('VINV/2026-27/00031')
    await user.click(screen.getByRole('button', { name: /record invoice/i }))
    const dialog = await screen.findByRole('dialog')

    expect(within(dialog).getByText('You cannot record a bill from here')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /record the invoice/i })).toBeDisabled()
  })
})

// ------------------------------------------------------------- amendment -----

describe('amending a bill', () => {
  /** The two lines from the file header, as the server would have stored them. */
  const TWO_LINES = [
    line(),
    line({
      id: 'vl000000-0000-7000-8000-000000000002',
      line_number: 2,
      description: 'Handle castings',
      quantity: '10.0000',
      unit_price: '99.990000',
      discount_amount: '49.9000',
      taxable_amount: '950.00',
      tax_rate: '12.0000',
      cgst_amount: '0.00',
      sgst_amount: '0.00',
      igst_amount: '114.00',
      tax_amount: '114.00',
      line_total: '1064.00',
    }),
  ]

  it('prices a saved bill on screen exactly as the server priced it', async () => {
    // The reconciliation preview, and the only reason to key a bill line by
    // line rather than take the vendor's word for the total.
    //
    // Line 1 is 2 x 125.03 at 18%: taxable 250.06, tax 45.0108 which rounds to
    // an ODD 45.01 - so CGST is the rounded half, 22.51, and SGST is the
    // REMAINDER, 22.50. Halving twice would give 22.51 twice, a tax of 45.02,
    // and a row the `tax_amount = cgst + sgst + igst` CHECK refuses on insert.
    //
    // Line 2 crosses a state border, so the whole 114.00 is IGST and neither
    // half exists. `is_interstate` is input-only on the wire, so the form has
    // to recover it from the split it produced - a non-zero IGST.
    //
    // The header then adds to 1,200.06 + 159.01, and the seven-paisa round-off
    // brings the bill to the clean 1,359.00 printed on the paper.
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: {
        'GET /finance/vendor-invoices': page([summary()]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ lines: TWO_LINES, round_off_amount: '-0.0700' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
      },
    })

    const matchDialog = await openMatchDialog(user)
    await user.click(await within(matchDialog).findByRole('button', { name: /amend/i }))
    const form = await screen.findByRole('dialog', { name: /amend VINV/i })

    expect(await within(form).findByText('₹250.06')).toBeInTheDocument()
    expect(within(form).getByText('₹45.01')).toBeInTheDocument()
    expect(within(form).getByText(/CGST ₹22\.51 \+ SGST ₹22\.50/)).toBeInTheDocument()
    expect(within(form).getByText('₹295.07')).toBeInTheDocument()

    expect(within(form).getByText('₹950.00')).toBeInTheDocument()
    expect(within(form).getByText('₹114.00')).toBeInTheDocument()
    expect(within(form).getByText('₹1,064.00')).toBeInTheDocument()

    expect(within(form).getByText('₹1,200.06')).toBeInTheDocument()
    expect(within(form).getByText('₹159.01')).toBeInTheDocument()
    expect(within(form).getByText('-₹0.07')).toBeInTheDocument()
    expect(within(form).getByText('₹1,359.00')).toBeInTheDocument()
  })

  it('replaces the whole line set and clears a note with an empty string', async () => {
    // `VendorInvoiceUpdate` replaces the lines wholesale - a bill is a document,
    // and patching one line of something about to be approved invites an
    // approval of other than what was reviewed. `notes` is the one field where
    // the empty string means something: the service writes whatever is not
    // null, so null leaves the old note standing and only "" clears it.
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: {
        'GET /finance/vendor-invoices': page([summary()]),
        [`GET ${INVOICE_PATH}`]: ok(invoice()),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
        [`PATCH ${INVOICE_PATH}`]: ok(invoice()),
      },
    })

    const matchDialog = await openMatchDialog(user)
    await user.click(await within(matchDialog).findByRole('button', { name: /amend/i }))

    const form = await screen.findByRole('dialog', { name: /amend VINV/i })
    // The saved bill is read back into the same form, trailing zeros trimmed.
    expect(await within(form).findByDisplayValue('125.03')).toBeInTheDocument()
    await user.clear(within(form).getByRole('textbox', { name: /notes/i }))
    await user.click(within(form).getByRole('button', { name: /save the amendment/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'PATCH')).toBe(true)
    const body = fetch.requestFor('PATCH', INVOICE_PATH) as Record<string, unknown>

    expect(body.notes).toBe('')
    // The vendor and the currency were fixed when the bill was recorded, so an
    // amendment cannot carry them even though the form still holds both.
    expect(Object.keys(body)).not.toContain('vendor_id')
    expect(Object.keys(body)).not.toContain('currency')
    expect(body.lines).toHaveLength(1)
    expect((body.lines as Record<string, unknown>[])[0]).toMatchObject({
      quantity: '2',
      unit_price: '125.03',
      tax_rate: '18',
      discount_amount: '0',
      is_interstate: false,
    })
  })
})

// ---------------------------------------------------------- cost centres -----

describe('where a bill is charged', () => {
  /**
   * Approval posts one purchase-expense leg per effective centre - a line's
   * own, else the header's, else none - so both levels have to reach the wire,
   * and an amendment has to carry both back even for an account that is shown
   * neither picker: the line set is replaced wholesale on every save.
   */
  const STAMP = '2026-09-01T09:00:00Z'
  const centre = (id: string, code: string, name: string, parent_id: string | null = null) => ({
    id,
    code,
    name,
    cost_centre_type: 'PRODUCTION',
    parent_id,
    plant_id: null,
    head_user_id: null,
    description: null,
    is_active: true,
    created_at: STAMP,
    updated_at: STAMP,
  })
  const CENTRES = [
    centre('cc-prod', 'CC-PROD', 'Production'),
    centre('cc-mach', 'CC-MACH', 'Machining', 'cc-prod'),
    centre('cc-pack', 'CC-PACK', 'Packing', 'cc-prod'),
  ]

  it('sends the header centre and a line of its own, to an account that may read centres', async () => {
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [
        P.VENDOR_INVOICE_VIEW,
        P.VENDOR_INVOICE_CREATE,
        P.VENDOR_VIEW,
        P.COST_CENTRE_VIEW,
      ],
      routes: {
        'GET /finance/vendor-invoices': page([summary()]),
        'GET /vendors': page([VENDOR]),
        'GET /organization/cost-centres': page(CENTRES),
        'POST /finance/vendor-invoices': ok(invoice()),
      },
    })

    await screen.findByText('VINV/2026-27/00031')
    await user.click(screen.getByRole('button', { name: /record invoice/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: /invoice number/i }), NUMBER)

    await user.click(within(dialog).getByRole('combobox', { name: 'Cost centre' }))
    await user.click(await screen.findByRole('option', { name: 'CC-MACH - Machining' }))

    await user.click(within(dialog).getByRole('button', { name: /add line/i }))
    await user.type(lineInput(dialog, 'Quantity', 0), '2')
    await user.type(lineInput(dialog, 'Unit price', 0), '125.03')
    await user.click(within(dialog).getByRole('combobox', { name: 'Line 1 cost centre' }))
    await user.click(await screen.findByRole('option', { name: 'CC-PACK - Packing' }))

    await user.click(within(dialog).getByRole('button', { name: /record the invoice/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/finance/vendor-invoices') as {
      cost_centre_id: string | null
      lines: { cost_centre_id: string | null }[]
    }
    expect(body.cost_centre_id).toBe('cc-mach')
    expect(body.lines[0]!.cost_centre_id).toBe('cc-pack')
  })

  it('carries every centre an amended bill holds, from an account shown no picker', async () => {
    const { user, fetch } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ cost_centre_id: 'cc-mach' })]),
        [`GET ${INVOICE_PATH}`]: ok(
          invoice({ cost_centre_id: 'cc-mach', lines: [line({ cost_centre_id: 'cc-pack' })] }),
        ),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
        [`PATCH ${INVOICE_PATH}`]: ok(invoice()),
      },
    })

    const matchDialog = await openMatchDialog(user)
    // Without the right the detail does not try to name the centre either.
    expect(within(matchDialog).queryByText('Cost centre')).not.toBeInTheDocument()
    await user.click(await within(matchDialog).findByRole('button', { name: /amend/i }))

    const form = await screen.findByRole('dialog', { name: /amend VINV/i })
    expect(await within(form).findByDisplayValue('125.03')).toBeInTheDocument()
    expect(within(form).queryByRole('combobox', { name: /cost centre/i })).not.toBeInTheDocument()
    await user.click(within(form).getByRole('button', { name: /save the amendment/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'PATCH')).toBe(true)
    const body = fetch.requestFor('PATCH', INVOICE_PATH) as {
      cost_centre_id: string | null
      lines: { cost_centre_id: string | null }[]
    }
    // Not null: on the header null clears the centre, and a line sent without
    // its own would silently fall back to the header's at approval.
    expect(body.cost_centre_id).toBe('cc-mach')
    expect(body.lines[0]!.cost_centre_id).toBe('cc-pack')
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })

  it("names the bill's centre on its detail, to an account that may read centres", async () => {
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW, P.COST_CENTRE_VIEW],
      routes: {
        'GET /finance/vendor-invoices': page([summary({ cost_centre_id: 'cc-mach' })]),
        [`GET ${INVOICE_PATH}`]: ok(invoice({ cost_centre_id: 'cc-mach' })),
        [`GET ${INVOICE_PATH}/match`]: ok(matchResult()),
        'GET /organization/cost-centres': page(CENTRES),
      },
    })

    const matchDialog = await openMatchDialog(user)
    expect(await within(matchDialog).findByText('CC-MACH - Machining')).toBeVisible()
  })
})

// ------------------------------------------------------ empty and broken -----

describe('the invoice type column and filter', () => {
  it('shows the type on every row and narrows the queue by it', async () => {
    const seen: { url?: URL } = {}
    const { user } = renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW],
      routes: {
        'GET /finance/vendor-invoices': ({ url }: { url?: URL }) => {
          seen.url = url
          return page([
            summary(),
            summary({
              id: 'vi000000-0000-7000-8000-000000000002',
              code: 'VINV/2026-27/00032',
              vendor_invoice_number: 'CHR/4420',
              invoice_type: 'JOB_WORK',
              job_work_order_id: 'jw000000-0000-7000-8000-000000000001',
            }),
          ])
        },
      },
    })

    expect(await screen.findByText('VINV/2026-27/00031')).toBeInTheDocument()
    expect(screen.getByText('Purchase')).toBeInTheDocument()
    expect(screen.getByText('Job Work')).toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Invoice type' }))
    await user.click(await screen.findByRole('option', { name: 'Job Work' }))
    await waitFor(() => expect(seen.url?.searchParams.get('invoice_type')).toBe('JOB_WORK'))
  }, 120_000)
})

describe('the states with no rows in them', () => {
  it('says what an empty queue holds without demanding a purchase order', async () => {
    // The entry form on this same page records a bill without an order when the
    // account cannot read orders, so the empty state must not tell a clerk that
    // a freight or service bill has no place here.
    renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW],
      routes: { 'GET /finance/vendor-invoices': page([]) },
    })

    expect(await screen.findByText('No vendor invoices')).toBeInTheDocument()
    expect(screen.getByText(/with or without a purchase order/i)).toBeInTheDocument()
  })

  it('shows the server message when the queue cannot be read', async () => {
    // A silent empty table reads as "no bills to pay", which on a payables
    // queue is the most expensive wrong answer available.
    renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_INVOICE_VIEW],
      routes: {
        'GET /finance/vendor-invoices': fail(
          403,
          'PERMISSION_DENIED',
          'You may not read vendor invoices',
        ),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeInTheDocument()
    expect(screen.getByText('You may not read vendor invoices')).toBeInTheDocument()
  })
})

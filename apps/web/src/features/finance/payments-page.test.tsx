/**
 * Payments and receipts - the register where money actually leaves the bank.
 *
 * Three properties of this screen are worth holding still, and all three are
 * the kind a render-only test sails straight past.
 *
 * **Recording and approving are different rights, on purpose.** Whoever keys a
 * payment in must not be the person who releases the money, so `payment.record`
 * and `payment.approve` gate different controls - and settling an approved
 * payment against invoices is `payment.record`, not an approval, because
 * deciding *which bills the money paid* never changes how much money moved.
 * Gating settlement on the approval right would lock the clerk out of matching
 * a payment they keyed themselves; gating approval on the record right would
 * hand the release of funds to the person who asked for it. Either is one
 * character's difference in a `can()` call, and neither is visible to `tsc`.
 *
 * **Allocation is payables-only.** `PaymentAllocationIn` names a
 * `vendor_invoice_id` and this contract has no receivable counterpart, so
 * `POST /allocate` refuses a RECEIPT outright. A settlement affordance on a
 * receipt walks a clerk through a table of invoices for a guaranteed refusal.
 *
 * **Money is decimal all the way to the wire.** The settlement dialog subtracts
 * what a draft settles from what the payment is worth and refuses to save when
 * that goes negative. Under IEEE-754 that subtraction goes negative on sums
 * that are exact in decimal - which would block a correct, fully matched
 * settlement with a message saying it exceeds the payment. The test below uses
 * three real invoice amounts that add up exactly and drift negative as doubles.
 *
 * One note on how the request-shape tests reach the dialogs. Selecting a row
 * action from the register's own menu currently opens the read-only detail
 * dialog on top of the dialog that was chosen (see the finding reported with
 * this suite: the Radix menu portal bubbles its click through the React tree
 * into the row's `onClick`). Rather than assert that behaviour is correct, the
 * register tests below assert only what the menu *offers* - which is the
 * permission and lifecycle gating, the highest-value thing here - and the
 * dialogs that build a request are driven directly, so the request shapes stay
 * pinned while the entry path is broken.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import { PaymentCommandDialog, PaymentFormDialog } from './payment-dialogs'
import type { PaymentDetail, PaymentRow } from './payment-model'
import PaymentsPage from './payments-page'

const PAYMENT_ID = 'pm000000-0000-7000-8000-000000000001'
const DETAIL_PATH = `/finance/payments/${PAYMENT_ID}`
const CODE = 'PAY/2026-27/00042'

const VENDOR = {
  id: 'vn000000-0000-7000-8000-000000000001',
  code: 'VEN-0007',
  name: 'Sharma Brass Works',
  is_approved: true,
  lead_time_days: 14,
}

const CUSTOMER = {
  id: 'cu000000-0000-7000-8000-000000000001',
  code: 'CUS-0003',
  name: 'Meridian Interiors',
  is_blocked: false,
}

function payment(overrides: Partial<PaymentRow> = {}): PaymentRow {
  return {
    id: PAYMENT_ID,
    code: CODE,
    payment_type: 'PAYMENT',
    party_type: 'VENDOR',
    party_id: VENDOR.id,
    payment_date: '2026-09-01',
    amount: '125000.50',
    allocated_amount: '0.00',
    unallocated_amount: '125000.50',
    currency: 'INR',
    payment_method: 'BANK_TRANSFER',
    reference_number: 'UTR-99001',
    status: 'DRAFT',
    created_at: '2026-09-01T09:00:00Z',
    ...overrides,
  }
}

function detail(overrides: Partial<PaymentDetail> = {}): PaymentDetail {
  return {
    ...payment(),
    bank_name: 'HDFC Bank',
    instrument_date: '2026-08-30',
    approved_by_id: null,
    approved_at: null,
    cancellation_reason: null,
    notes: null,
    version: 1,
    allocations: [],
    ...overrides,
  }
}

/** The row a control lives in, so a multi-row assertion cannot cross rows. */
function rowFor(code: string): HTMLElement {
  const cell = screen.getByText(code)
  const row = cell.closest('tr')
  expect(row, `no row for ${code}`).not.toBeNull()
  return row as HTMLElement
}

/** Open a row's action menu and hand back the menu itself to assert against. */
async function openRowMenu(
  user: ReturnType<typeof renderScreen>['user'],
  code = CODE,
): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: `Actions for ${code}` }))
  return screen.findByRole('menu')
}

// -------------------------------------------------------- permission gating --

describe('the two rights this screen spans', () => {
  it('gives someone who may record, and not approve, exactly the recording controls', async () => {
    // The register is a segregation-of-duties boundary. A clerk who may key a
    // payment must never see the control that releases the money, and the
    // reverse - a recorder locked out of amending their own draft - wastes the
    // right they do hold.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: { 'GET /finance/payments': page([payment()]) },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Record payment' })).toBeInTheDocument()

    const menu = await openRowMenu(user)
    expect(within(menu).getByRole('menuitem', { name: 'Amend draft' })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: 'Delete draft' })).toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: /approve/i })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: /cancel/i })).not.toBeInTheDocument()
  })

  it('gives someone who may approve, and not record, exactly the approval controls', async () => {
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
      routes: { 'GET /finance/payments': page([payment()]) },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    // No way in to the entry form: this account may release money, not key it.
    expect(screen.queryByRole('button', { name: 'Record payment' })).not.toBeInTheDocument()

    const menu = await openRowMenu(user)
    expect(
      within(menu).getByRole('menuitem', { name: 'Approve - post the money' }),
    ).toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Amend draft' })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Delete draft' })).not.toBeInTheDocument()
  })

  it('offers settlement to a recorder and withholds it from an approver', async () => {
    // Settling decides which bills the money paid; it never changes the amount
    // that left the bank, so it belongs to `payment.record`. This is the exact
    // shape of the authorisation defects that have shipped here - a control
    // gated on the neighbouring right - and a stubbed `can()` passes it either
    // way round.
    const approved = payment({ status: 'APPROVED' })

    const recorder = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: { 'GET /finance/payments': page([approved]) },
    })
    expect(await screen.findByText(CODE)).toBeInTheDocument()
    const recorderMenu = await openRowMenu(recorder.user)
    expect(
      within(recorderMenu).getByRole('menuitem', { name: 'Settle invoices' }),
    ).toBeInTheDocument()
    recorder.unmount()

    const approver = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
      routes: { 'GET /finance/payments': page([approved]) },
    })
    expect(await screen.findByText(CODE)).toBeInTheDocument()
    // The approver's menu is not empty - cancelling is theirs - so this is a
    // genuine absence of the settle entry, not an absence of the whole menu.
    const approverMenu = await openRowMenu(approver.user)
    expect(
      within(approverMenu).getByRole('menuitem', { name: 'Cancel and reverse' }),
    ).toBeInTheDocument()
    expect(
      within(approverMenu).queryByRole('menuitem', { name: 'Settle invoices' }),
    ).not.toBeInTheDocument()
  })

  it('shows a plain viewer the register and no row actions at all', async () => {
    // The whole actions column is dropped rather than rendered empty, so the
    // read-only case is genuinely read-only and not one disabled button away
    // from a 403.
    renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW],
      routes: { 'GET /finance/payments': page([payment()]) },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: `Actions for ${CODE}` })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Record payment' })).not.toBeInTheDocument()
  })
})

// --------------------------------------------------- allocation is payables --

describe('a receipt is never settled', () => {
  const RECEIPT_CODE = 'RCT/2026-27/00009'
  const receipt = {
    code: RECEIPT_CODE,
    payment_type: 'RECEIPT' as const,
    status: 'APPROVED' as const,
    allocated_amount: '0.00',
  }

  it('offers no settlement affordance on a receipt row, whatever its status', async () => {
    // An approved payment in this state would offer "Settle invoices". The
    // receipt's menu is otherwise identical, which is what makes the absence
    // meaningful rather than an artefact of a menu that never rendered.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.PAYMENT_APPROVE],
      routes: { 'GET /finance/payments': page([payment(receipt)]) },
    })

    expect(await screen.findByText(RECEIPT_CODE)).toBeInTheDocument()
    const menu = await openRowMenu(user, RECEIPT_CODE)
    expect(within(menu).getByRole('menuitem', { name: 'Cancel and reverse' })).toBeInTheDocument()
    expect(
      within(menu).queryByRole('menuitem', { name: 'Settle invoices' }),
    ).not.toBeInTheDocument()
  })

  it('reads a receipt as not-applicable rather than as an unsettled balance', async () => {
    // A zero in the Settled column would read as an outstanding matching job
    // that somebody has to go and do. "n/a" says the column does not apply,
    // which is the truth for a receipt.
    renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW],
      routes: { 'GET /finance/payments': page([payment(receipt)]) },
    })

    await screen.findByText(RECEIPT_CODE)
    expect(within(rowFor(RECEIPT_CODE)).getAllByText('n/a')).toHaveLength(2)
  })

  it('closes the door out of the reader for a receipt as well', async () => {
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: {
        'GET /finance/payments': page([payment(receipt)]),
        [`GET ${DETAIL_PATH}`]: ok(detail(receipt)),
      },
    })

    await user.click(await screen.findByText(RECEIPT_CODE))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('Not applicable to a receipt')).toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: 'Settle invoices' }),
    ).not.toBeInTheDocument()
  })
})

// ------------------------------------------------------- lifecycle gating ----

describe('a command is offered only where the endpoint would accept it', () => {
  it('withholds cancellation while an allocation still stands', async () => {
    // `cancel_payment` refuses a payment with allocations: un-settling the
    // invoices is a deliberate act, not a side effect of reversing the money.
    // The menu entry therefore has to read `allocated_amount`, not the status.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.PAYMENT_APPROVE],
      routes: {
        'GET /finance/payments': page([
          payment({
            status: 'ALLOCATED',
            allocated_amount: '125000.50',
            unallocated_amount: '0.00',
          }),
        ]),
      },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    const menu = await openRowMenu(user)
    expect(within(menu).getByRole('menuitem', { name: 'Settle invoices' })).toBeInTheDocument()
    expect(
      within(menu).queryByRole('menuitem', { name: 'Cancel and reverse' }),
    ).not.toBeInTheDocument()
  })

  it('offers cancellation on an approved payment nothing has been settled from', async () => {
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
      routes: {
        'GET /finance/payments': page([payment({ status: 'APPROVED', allocated_amount: '0.00' })]),
      },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    const menu = await openRowMenu(user)
    expect(within(menu).getByRole('menuitem', { name: 'Cancel and reverse' })).toBeInTheDocument()
  })

  it('offers nothing at all on a cancelled payment', async () => {
    // Cancelling a cancelled payment would run the reversal a second time, and
    // there is no other edge out of the terminal state. An empty menu is not
    // rendered at all, so the trigger itself has to be gone.
    renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE, P.PAYMENT_RECORD],
      routes: {
        'GET /finance/payments': page([payment({ status: 'CANCELLED', allocated_amount: '0.00' })]),
      },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: `Actions for ${CODE}` })).not.toBeInTheDocument()
  })

  it('will not offer settlement on money that has not moved yet', async () => {
    // Only an approved payment settles a bill: a draft has posted nothing, so
    // there is no money to apply to anything.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: { 'GET /finance/payments': page([payment({ status: 'DRAFT' })]) },
    })

    expect(await screen.findByText(CODE)).toBeInTheDocument()
    const menu = await openRowMenu(user)
    expect(
      within(menu).queryByRole('menuitem', { name: 'Settle invoices' }),
    ).not.toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: 'Amend draft' })).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------- money ------

describe('money on the register', () => {
  it('formats every row in its own currency and never in the company default', async () => {
    // Each row states its own denomination and nothing on this screen adds two
    // together. The Indian grouping is the visible half of that: a payment of
    // one and a quarter lakh reads 1,25,000.50, not 125,000.50, and a dollar
    // payment must not pick up a rupee sign from a default that leaked past the
    // row it belongs to.
    renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW],
      routes: {
        'GET /finance/payments': page([
          payment({
            id: 'p1',
            code: 'PAY-INR',
            amount: '125000.50',
            allocated_amount: '100000.00',
            unallocated_amount: '25000.50',
            currency: 'INR',
          }),
          payment({
            id: 'p2',
            code: 'PAY-USD',
            amount: '1000.00',
            allocated_amount: '250.00',
            unallocated_amount: '750.00',
            currency: 'USD',
          }),
        ]),
      },
    })

    await screen.findByText('PAY-INR')
    const inr = rowFor('PAY-INR')
    expect(within(inr).getByText('₹1,25,000.50')).toBeInTheDocument()
    // The unsettled figure is the server's own derived column, printed rather
    // than recomputed here: the payables register, the ageing report and this
    // row all read it, and a browser-side subtraction is how the three come to
    // disagree.
    expect(within(inr).getByText('₹25,000.50')).toBeInTheDocument()
    expect(within(rowFor('PAY-USD')).getByText('$1,000.00')).toBeInTheDocument()
  })
})

// -------------------------------------------------------- what it sends ------

describe('recording a payment', () => {
  it('offers actual money methods and withholds unsupported noncash adjustments', async () => {
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: { 'GET /finance/payments': page([payment()]) },
    })
    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Method' }))
    expect(await screen.findByRole('option', { name: 'Bank transfer' })).toBeVisible()
    expect(screen.queryByRole('option', { name: 'Adjustment' })).toBeNull()
  })

  it('sends the amount as a decimal string and omitted fields as null', async () => {
    // The whole point of the register. `amount` reaching the API as the JSON
    // number 125000.5 would be a double the moment it was parsed, and every
    // figure downstream - the allocation ceiling, the ageing bucket, the ledger
    // leg - is derived from it. A blank optional field is spelled `null`, which
    // is what `PaymentCreate` declares; an empty string fails its pattern.
    const { user, fetch } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.VENDOR_VIEW],
      routes: {
        'GET /finance/payments': page([payment()]),
        'GET /vendors': page([VENDOR]),
        'POST /finance/payments': ok(detail()),
      },
    })

    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Amount' }), '125000.50')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/finance/payments') as Record<string, unknown>

    expect(typeof body.amount).toBe('string')
    expect(body).toEqual({
      payment_type: 'PAYMENT',
      party_type: 'VENDOR',
      party_id: VENDOR.id,
      payment_date: expect.any(String),
      amount: '125000.50',
      payment_method: 'BANK_TRANSFER',
      reference_number: null,
      bank_name: null,
      instrument_date: null,
      notes: null,
      // Blank means "the company's own currency", defaulted server-side so it
      // matches the invoices this payment will later be allowed to settle.
      currency: null,
    })
  })

  it('puts a server field error on the field that caused it', async () => {
    // The mutation cache suppresses the toast for a 422 that carries field
    // errors, on the understanding that the form shows them inline. If that
    // mapping breaks, the submit button appears to do nothing at all and the
    // user is left guessing which figure the server objected to.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.VENDOR_VIEW],
      routes: {
        'GET /finance/payments': page([payment()]),
        'GET /vendors': page([VENDOR]),
        'POST /finance/payments': fail(422, 'VALIDATION_ERROR', 'Invalid payment', {
          fields: { amount: ['exceeds the vendor credit note balance'] },
        }),
      },
    })

    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Amount' }), '125000.50')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(await screen.findByText('exceeds the vendor credit note balance')).toBeInTheDocument()
  })

  it('shows a refusal of the whole body above the footer', async () => {
    // `PaymentCreate._party_matches_direction` refuses the body as a whole, so
    // the 422 arrives under `_root` rather than a field. No control owns that
    // key: the shared mapping puts it on the form's root slot, which this
    // dialog renders at the foot of the form, so the sentence is on screen.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.VENDOR_VIEW],
      routes: {
        'GET /finance/payments': page([payment()]),
        'GET /vendors': page([VENDOR]),
        'POST /finance/payments': fail(422, 'VALIDATION_ERROR', 'Invalid payment', {
          fields: { _root: ['A PAYMENT is recorded against a vendor, not a customer.'] },
        }),
      },
    })

    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Amount' }), '125000.50')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(
      await within(dialog).findByText('A PAYMENT is recorded against a vendor, not a customer.'),
    ).toBeVisible()
  })

  it('moves the party side with the direction, so a receipt starts on a customer', async () => {
    // `PaymentCreate._party_matches_direction`: a payment goes to a vendor and
    // a receipt comes from a customer. Choosing "Receipt" therefore swaps the
    // picker over rather than leaving a vendor box under a direction the API
    // would refuse it on.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.VENDOR_VIEW, P.CUSTOMER_VIEW],
      routes: { 'GET /finance/payments': page([payment()]) },
    })

    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('combobox', { name: 'Vendor' })).toBeInTheDocument()

    await user.click(within(dialog).getByRole('combobox', { name: 'Direction' }))
    await user.click(await screen.findByRole('option', { name: 'Receipt - money in' }))

    expect(await within(dialog).findByRole('combobox', { name: 'Customer' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('combobox', { name: 'Vendor' })).not.toBeInTheDocument()
  })

  it('refuses a payment against a customer before the round trip', async () => {
    // The API refuses the inverse pairing as a model-level 422 keyed `_root`,
    // which no field on the form reads, while the global toast stays quiet for
    // a 422 that carries field errors - so sent, the refusal would vanish. The
    // rule is stated here, on the party side, and nothing is posted.
    const { user, fetch } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.VENDOR_VIEW, P.CUSTOMER_VIEW],
      routes: {
        'GET /finance/payments': page([payment()]),
        'GET /customers': page([CUSTOMER]),
        'POST /finance/payments': ok(detail()),
      },
    })

    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Party' }))
    await user.click(await screen.findByRole('option', { name: 'Customer' }))
    await user.click(await within(dialog).findByRole('combobox', { name: 'Customer' }))
    await user.click(await screen.findByRole('option', { name: /CUS-0003/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Amount' }), '5000.00')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(
      await within(dialog).findByText('A payment is recorded against a vendor, not a customer.'),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('refuses a third decimal place rather than letting the server round it away', async () => {
    // `to_money` quantises to two places, so a third would validate against the
    // contract's own four-place pattern and then be silently rounded off - the
    // clerk's figure and the posted figure would differ with nothing said.
    const { user, fetch } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD, P.VENDOR_VIEW],
      routes: {
        'GET /finance/payments': page([payment()]),
        'GET /vendors': page([VENDOR]),
      },
    })

    await screen.findByText(CODE)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Amount' }), '100.005')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(
      await within(dialog).findByText('An amount with up to two decimal places'),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
})

// ------------------------------------------------------------ settlement -----

describe('settling an approved payment', () => {
  /** Three real bills that add up to the payment exactly - and not as doubles. */
  const ALLOCATIONS = [
    {
      id: 'al1',
      vendor_invoice_id: 'vi1',
      amount: '6408.52',
      notes: null,
      invoice_code: 'VINV-1',
      vendor_invoice_number: 'SBW/4411',
    },
    {
      id: 'al2',
      vendor_invoice_id: 'vi2',
      amount: '6534.59',
      notes: null,
      invoice_code: 'VINV-2',
      vendor_invoice_number: 'SBW/4412',
    },
    {
      id: 'al3',
      vendor_invoice_id: 'vi3',
      amount: '3594.81',
      notes: 'part settlement',
      invoice_code: 'VINV-3',
      vendor_invoice_number: 'SBW/4413',
    },
  ]

  const settled = (overrides: Partial<PaymentDetail> = {}) =>
    detail({
      status: 'ALLOCATED',
      amount: '16537.92',
      allocated_amount: '16537.92',
      unallocated_amount: '0.00',
      allocations: ALLOCATIONS,
      ...overrides,
    })

  const settledRow = payment({
    status: 'ALLOCATED',
    amount: '16537.92',
    allocated_amount: '16537.92',
    unallocated_amount: '0.00',
  })

  /**
   * Open the settlement editor the way the reader offers it.
   *
   * The detail dialog closes as the editor opens, so this leaves exactly one
   * dialog on screen - and the button only exists for someone who may record.
   */
  async function openSettlement(
    user: ReturnType<typeof renderScreen>['user'],
  ): Promise<HTMLElement> {
    await user.click(await screen.findByText(CODE))
    const reader = await screen.findByRole('dialog')
    await user.click(await within(reader).findByRole('button', { name: 'Settle invoices' }))
    return screen.findByRole('dialog')
  }

  it('leaves nothing unallocated when the parts add up in decimal, not in floats', async () => {
    // 6408.52 + 6534.59 + 3594.81 is 16537.92 exactly. As IEEE-754 doubles the
    // sum lands a hair *above* the payment, so `amount - total` comes out
    // negative and the dialog would announce that this settles more than left
    // the bank - blocking a correct, fully matched settlement over a rounding
    // artefact nobody can see. decimal.js gets zero.
    const { user } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: {
        'GET /finance/payments': page([settledRow]),
        [`GET ${DETAIL_PATH}`]: ok(settled()),
      },
    })

    const dialog = await openSettlement(user)

    // Twice: what the payment is worth, and what this settlement covers. They
    // agree to the paisa, which is the whole assertion.
    expect(await within(dialog).findAllByText('₹16,537.92')).toHaveLength(2)
    expect(within(dialog).getByText('₹0.00')).toBeInTheDocument()
    expect(within(dialog).queryByText('Not ready to save')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Save settlement' })).toBeEnabled()
  })

  it('sends the whole set with replace, as decimal strings, and drops a released line', async () => {
    // The endpoint takes an allocation *set*, not a delta, and this dialog is
    // seeded from the payment's complete allocation list - which is the only
    // thing that makes `replace: true` safe. Sent additively there would be no
    // way to say "and nothing else", so a mis-keyed settlement could only ever
    // be added to. A released line therefore has to vanish from the request,
    // and the notes have to travel with it: `_apply_allocation_plan` rewrites
    // `notes` from the request on every amended row, so omitting one wipes what
    // somebody wrote against the settlement.
    const { user, fetch } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: {
        'GET /finance/payments': page([settledRow]),
        [`GET ${DETAIL_PATH}`]: ok(settled()),
        [`POST ${DETAIL_PATH}/allocate`]: ok(settled()),
      },
    })

    const dialog = await openSettlement(user)
    await within(dialog).findByRole('textbox', { name: 'Amount settled against SBW/4411' })
    await user.click(
      within(dialog).getByRole('button', { name: 'Release SBW/4412 from this payment' }),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Save settlement' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', `${DETAIL_PATH}/allocate`) as {
      allocations: { vendor_invoice_id: string; amount: unknown; notes: string | null }[]
      replace: boolean
    }

    expect(body.replace).toBe(true)
    expect(body.allocations).toEqual([
      { vendor_invoice_id: 'vi1', amount: '6408.52', notes: null },
      { vendor_invoice_id: 'vi3', amount: '3594.81', notes: 'part settlement' },
    ])
    for (const line of body.allocations) expect(typeof line.amount).toBe('string')
  })

  it('will not send a settlement worth more than the payment', async () => {
    // The API refuses this under a row lock. Saying so before the round trip is
    // the difference between a corrected figure and a rejected request, and the
    // figure quoted back has to be the settlement's own total.
    const { user, fetch } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: {
        'GET /finance/payments': page([settledRow]),
        [`GET ${DETAIL_PATH}`]: ok(settled()),
      },
    })

    const dialog = await openSettlement(user)
    const line = await within(dialog).findByRole('textbox', {
      name: 'Amount settled against SBW/4411',
    })
    await user.clear(line)
    await user.type(line, '9999999.00')

    expect(await within(dialog).findByText('Not ready to save')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Save settlement' })).toBeDisabled()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('tells a settler without the invoice register why the candidate list is missing', async () => {
    // Browsing the vendor's open bills is `vendor_invoice.view`, a different
    // right from the `payment.record` that opened this dialog. Firing the
    // search anyway would 403 and read as a vendor with no open invoices.
    const { user, fetch } = renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: {
        'GET /finance/payments': page([payment({ status: 'APPROVED' })]),
        [`GET ${DETAIL_PATH}`]: ok(detail({ status: 'APPROVED' })),
      },
    })

    const dialog = await openSettlement(user)
    expect(
      await within(dialog).findByText('The invoice register is not open to you'),
    ).toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path === '/finance/vendor-invoices')).toBe(false)
  })
})

// -------------------------------------------------------------- commands -----

/**
 * The three lifecycle commands, driven at the dialog rather than through the
 * register's row menu.
 *
 * Each of these differs in *where* the reason travels, and the three spellings
 * are exactly the sort of thing that is right once and wrong after a refactor.
 * The register's own menu cannot currently reach them (see the file header), so
 * pinning the request shapes here keeps that contract under test while the
 * entry path is broken - and these are the assertions that matter, because a
 * wrong spelling is a 422 at the moment somebody releases money.
 */
describe('the lifecycle commands', () => {
  it('approves with no body at all when no reason is given', async () => {
    // `approve` takes an OPTIONAL `ReasonRequest`, and a reason inside one must
    // clear three characters - so an empty box means "send no body", never
    // `{reason: ""}`, which comes back a 422 at the moment of use.
    const { user, fetch } = renderScreen(
      <PaymentCommandDialog command="approve" payment={payment()} onClose={() => {}} />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
        routes: { [`POST ${DETAIL_PATH}/approve`]: ok(detail({ status: 'APPROVED' })) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Approve payment' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `${DETAIL_PATH}/approve`)).toBeUndefined()
  })

  it('sends an approval reason inside the body when one is written', async () => {
    const { user, fetch } = renderScreen(
      <PaymentCommandDialog command="approve" payment={payment()} onClose={() => {}} />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
        routes: { [`POST ${DETAIL_PATH}/approve`]: ok(detail({ status: 'APPROVED' })) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason (optional)' }),
      'Cleared by the works manager',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Approve payment' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `${DETAIL_PATH}/approve`)).toEqual({
      reason: 'Cleared by the works manager',
    })
  })

  it('refuses a written approval reason shorter than the API accepts, and sends nothing', async () => {
    // The reason is optional, but `ReasonRequest.reason` is 3..500 whenever a
    // body is sent. A two-character note would go out as `{reason: 'ok'}`,
    // come back a 422 with a field error, and vanish: the mutation cache stays
    // quiet for field errors on the understanding that a form shows them, and
    // this dialog has no form. The rule is applied here, before the click is
    // spent.
    const { user, fetch } = renderScreen(
      <PaymentCommandDialog command="approve" payment={payment()} onClose={() => {}} />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
        routes: { [`POST ${DETAIL_PATH}/approve`]: ok(detail({ status: 'APPROVED' })) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason (optional)' }), 'ok')
    await user.click(within(dialog).getByRole('button', { name: 'Approve payment' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Record at least 3 characters of reason',
    )
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('refuses to send a cancellation until the reason is long enough', async () => {
    // Cancelling posts the mirror image of a real journal, and the reason is
    // required in the body and bounded at three characters. A click with an
    // empty box has to be stopped here rather than spent on a 422.
    const { user, fetch } = renderScreen(
      <PaymentCommandDialog
        command="cancel"
        payment={payment({ status: 'APPROVED', allocated_amount: '0.00' })}
        onClose={() => {}}
      />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_APPROVE],
        routes: { [`POST ${DETAIL_PATH}/cancel`]: ok(detail({ status: 'CANCELLED' })) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel payment' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Record at least 3 characters of reason',
    )
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)

    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Paid against the wrong vendor',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Cancel payment' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    expect(fetch.requestFor('POST', `${DETAIL_PATH}/cancel`)).toEqual({
      reason: 'Paid against the wrong vendor',
    })
  })

  it('carries the delete reason as a query parameter, which is this route alone', async () => {
    // The finance router spells this one `ReasonQuery`, not a body. Putting it
    // in the body is a 422 that only surfaces the day somebody deletes a draft.
    let seen: URL | null = null
    const { user } = renderScreen(
      <PaymentCommandDialog command="delete" payment={payment()} onClose={() => {}} />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
        routes: {
          'DELETE /finance/payments': ({ url }) => {
            seen = url
            return ok(null)
          },
        },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Keyed twice by mistake',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Delete draft' }))

    await expect.poll(() => seen !== null).toBe(true)
    const url = seen as unknown as URL
    expect(url.pathname).toBe(`/api/v1${DETAIL_PATH}`)
    expect(url.searchParams.get('reason')).toBe('Keyed twice by mistake')
  })
})

describe('amending a draft', () => {
  it('requires an explicit real method before repairing a legacy adjustment draft', async () => {
    const legacy = detail({ payment_method: 'ADJUSTMENT' })
    const { user, fetch } = renderScreen(
      <PaymentFormDialog open onOpenChange={() => {}} paymentId={PAYMENT_ID} />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
        routes: {
          [`GET ${DETAIL_PATH}`]: ok(legacy),
          [`PATCH ${DETAIL_PATH}`]: ok(detail({ payment_method: 'BANK_TRANSFER' })),
        },
      },
    )
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByDisplayValue('125000.50')
    expect(within(dialog).getByText(/This legacy draft uses a noncash adjustment/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))
    expect(
      await within(dialog).findByText(/noncash adjustments cannot use this form/),
    ).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
    await user.click(within(dialog).getByRole('combobox', { name: 'Method' }))
    expect(screen.queryByRole('option', { name: 'Adjustment' })).toBeNull()
    await user.click(await screen.findByRole('option', { name: 'Bank transfer' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))
    await expect.poll(() => fetch.calls.some((call) => call.method === 'PATCH')).toBe(true)
    expect(fetch.requestFor('PATCH', DETAIL_PATH)).toMatchObject({
      payment_method: 'BANK_TRANSFER',
    })
  })

  it('sends only the fields that are still free to change', async () => {
    // `PaymentUpdate` has no party, no direction and no currency: changing the
    // party of a payment is a different payment. The form still carries all
    // three so its own type stays whole, which is exactly how they creep back
    // into the request.
    const { user, fetch } = renderScreen(
      <PaymentFormDialog open onOpenChange={() => {}} paymentId={PAYMENT_ID} />,
      {
        permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
        routes: {
          [`GET ${DETAIL_PATH}`]: ok(detail()),
          [`PATCH ${DETAIL_PATH}`]: ok(detail({ amount: '99000.00' })),
        },
      },
    )

    const dialog = await screen.findByRole('dialog')
    const amount = await within(dialog).findByDisplayValue('125000.50')
    await user.clear(amount)
    await user.type(amount, '99000.00')
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'PATCH')).toBe(true)
    const body = fetch.requestFor('PATCH', DETAIL_PATH) as Record<string, unknown>

    expect(body.amount).toBe('99000.00')
    expect(typeof body.amount).toBe('string')
    expect(Object.keys(body).sort()).toEqual([
      'amount',
      'bank_name',
      'instrument_date',
      'notes',
      'payment_date',
      'payment_method',
      'reference_number',
    ])
  })
})

// ------------------------------------------------- empty, error and loading --

describe('the states with no rows in them', () => {
  it('offers the way in from an empty register', async () => {
    // An empty queue is the first thing a new company sees. Without the action
    // here, the only route to recording a payment is the header button, which
    // is exactly where a first-time user is not looking.
    renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW, P.PAYMENT_RECORD],
      routes: { 'GET /finance/payments': page([]) },
    })

    expect(await screen.findByText('No payments')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Record payment' })).toHaveLength(2)
  })

  it('shows the server message when the register cannot be read', async () => {
    // A 403 on the list has to say something. A silent empty table reads as a
    // company with no payments, which is a very different answer.
    renderScreen(<PaymentsPage />, {
      permissions: [P.PAYMENT_VIEW],
      routes: {
        'GET /finance/payments': fail(403, 'PERMISSION_DENIED', 'You may not read payments'),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeInTheDocument()
    expect(screen.getByText('You may not read payments')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})

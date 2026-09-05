/**
 * The quality screen.
 *
 * Three properties of this screen are worth more than everything else on it,
 * and all three are invisible to the compiler.
 *
 * **1. The landing tab is the PENDING QUEUE, and that is a different endpoint.**
 * `GET /quality/pending` is ordered oldest-first by the route and takes only
 * `qc_stage` and `warehouse_id`; `GET /quality/inspections` is the searchable,
 * sortable history. A screen that opened on the history instead would look
 * entirely correct - same columns, same rows - while the inspector no longer
 * sees the ageing queue that says which lot is blocking a delivery. And because
 * FastAPI silently drops query parameters a route does not declare, sending the
 * queue a `search` or a `sort_by` produces the same page back rather than an
 * error, so nothing downstream would report the mistake either.
 *
 * **2. The Inspect action is gated on `qc.inspect` OR `qc.decide`.** Six
 * authorisation defects have shipped in this codebase, every one of them a
 * control gated on the wrong right.
 *
 * **3. `isInspectable` includes REWORK and HOLD.** Those are decided statuses
 * whose quantity is still sitting in the QC hold pool, so the API will still
 * accept a decision on them (`decide_inspection` gates on OPEN + REOPENABLE).
 * Listing only the open two hid the action on exactly the inspections that most
 * need it: held stock with no path forward.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import QualityPage from './quality-page'
import type { QcInspectionRow } from './quality-types'

/** One row of `GET /quality/pending` / `GET /quality/inspections`. */
function inspection(overrides: Partial<QcInspectionRow> = {}): QcInspectionRow {
  return {
    id: '01a00000-0000-7000-8000-0000000000q1',
    code: 'QC-0001',
    qc_stage: 'INCOMING',
    status: 'PENDING',
    result: 'PENDING',
    item_id: '01a00000-0000-7000-8000-0000000000i1',
    batch_id: null,
    vendor_id: null,
    warehouse_id: '01a00000-0000-7000-8000-0000000000w1',
    reference_type: 'goods_receipt',
    reference_id: '01a00000-0000-7000-8000-0000000000g1',
    reference_code: 'GRN-0001',
    inspected_quantity: '100.0000',
    sample_size: '8.0000',
    accepted_quantity: '0.0000',
    rejected_quantity: '0.0000',
    rework_quantity: '0.0000',
    hold_quantity: '0.0000',
    scrap_quantity: '0.0000',
    return_required: false,
    inspected_at: null,
    inspector_user_id: null,
    template_id: null,
    template_version: null,
    created_at: '2026-09-01T08:00:00Z',
    ...overrides,
  }
}

/** Records the query string of every request to a path, which `fetch.calls` drops. */
function urlRecorder() {
  const urls: URL[] = []
  return {
    urls,
    /** The parameters of the nth request, as a plain object. */
    paramsAt(index: number): Record<string, string> {
      const url = urls[index]
      expect(url, `expected at least ${index + 1} request(s)`).toBeDefined()
      return Object.fromEntries(url!.searchParams)
    },
  }
}

describe('the landing tab', () => {
  it('queries the pending queue, not the full inspection history', async () => {
    const { fetch } = renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/pending': page([inspection()]) },
    })

    expect(await screen.findByText('QC-0001')).toBeVisible()
    // The assertion that matters is the negative one. Both endpoints return the
    // same row shape, so a screen that landed on the history would render
    // identically and silently drop the oldest-first ageing queue.
    expect(fetch.calls.map((call) => call.path)).toContain('/quality/pending')
    expect(fetch.calls.map((call) => call.path)).not.toContain('/quality/inspections')
  })

  it('sends the queue no search or sort parameters, because the route declares none', async () => {
    // FastAPI drops undeclared query parameters rather than rejecting them, so a
    // `search` sent here comes back as the same unfiltered page - the inspector
    // would conclude the lot was not in the queue. The screen therefore offers
    // no search box and no sortable header, and this pins that it sends neither.
    const recorder = urlRecorder()
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      routes: {
        'GET /quality/pending': ({ url }) => {
          recorder.urls.push(url)
          return page([inspection()])
        },
      },
    })

    expect(await screen.findByText('QC-0001')).toBeVisible()
    const params = recorder.paramsAt(0)
    expect(params.sort_by).toBeUndefined()
    expect(params.sort_dir).toBeUndefined()
    expect(params.search).toBeUndefined()
    expect(params.page).toBe('1')
  })

  it('narrows the queue with qc_stage, the parameter the route actually reads', async () => {
    // The URL key the screen bookmarks under is `stage`; the parameter the API
    // answers is `qc_stage`. Forwarding the former would filter nothing at all -
    // and FastAPI would answer the unfiltered page rather than complain, so the
    // inspector would see every stage and believe the filter had been applied.
    const recorder = urlRecorder()
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      route: '/quality?stage=JOB_WORK',
      routes: {
        'GET /quality/pending': ({ url }) => {
          recorder.urls.push(url)
          return page([inspection({ qc_stage: 'JOB_WORK' })])
        },
      },
    })

    expect(await screen.findByText('QC-0001')).toBeVisible()
    expect(recorder.paramsAt(0).qc_stage).toBe('JOB_WORK')
    expect(recorder.paramsAt(0).stage).toBeUndefined()
  })

  it('says what an empty queue means rather than showing a blank table', async () => {
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/pending': page([]) },
    })

    expect(await screen.findByText('Nothing waiting for inspection')).toBeVisible()
  })

  it('surfaces a failed load with a way back, not an empty list', async () => {
    // An empty table and a failed request look identical to a user, and one of
    // them means "stop looking for the lot" while the other means "try again".
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      routes: {
        'GET /quality/pending': fail(
          503,
          'SERVICE_UNAVAILABLE',
          'Quality is temporarily unavailable',
        ),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeVisible()
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })
})

describe('who may inspect', () => {
  it('offers Inspect to a user holding qc.inspect', async () => {
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW, P.QC_INSPECT],
      routes: { 'GET /quality/pending': page([inspection()]) },
    })

    expect(await screen.findByRole('button', { name: 'Inspect' })).toBeVisible()
  })

  it('offers Inspect to a user holding only qc.decide', async () => {
    // `canAny(QC_INSPECT, QC_DECIDE)`: the person who dispositions a lot is not
    // always the person who measures it, and the API authorises the two posts
    // separately. A gate on `inspect` alone would lock the decider out of the
    // only screen that reaches the decision.
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW, P.QC_DECIDE],
      routes: { 'GET /quality/pending': page([inspection()]) },
    })

    expect(await screen.findByRole('button', { name: 'Inspect' })).toBeVisible()
  })

  it('withholds Inspect from a read-only account', async () => {
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/pending': page([inspection()]) },
    })

    expect(await screen.findByText('QC-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Inspect' })).toBeNull()
  })
})

describe('the inspection history', () => {
  /** Open the second tab, whose list is a different endpoint from the queue. */
  async function openHistory(rows: QcInspectionRow[], permissions: string[]) {
    const rendered = renderScreen(<QualityPage />, {
      permissions,
      routes: {
        'GET /quality/pending': page([]),
        'GET /quality/inspections': page(rows),
      },
    })
    await screen.findByText('Nothing waiting for inspection')
    await rendered.user.click(screen.getByRole('tab', { name: /all inspections/i }))
    return rendered
  }

  it('is a different endpoint from the queue, and is the one that searches', async () => {
    const { fetch } = await openHistory([inspection({ code: 'QC-0009' })], [P.QC_VIEW])

    expect(await screen.findByText('QC-0009')).toBeVisible()
    expect(fetch.calls.map((call) => call.path)).toContain('/quality/inspections')
    expect(screen.getByRole('textbox', { name: /search inspection/i })).toBeVisible()
  })

  it.each([
    ['PENDING' as const],
    ['IN_PROGRESS' as const],
    // The two that shipped hidden. Stock put on hold or sent for rework is
    // still sitting in the QC hold pool, and the API will re-decide it - so
    // withholding the button leaves that material with no path forward.
    ['REWORK' as const],
    ['HOLD' as const],
  ])('offers Inspect on a %s inspection, which the API will still decide', async (status) => {
    await openHistory(
      [
        inspection({
          code: `QC-${status}`,
          status,
          result: status === 'HOLD' ? 'HOLD' : 'PENDING',
        }),
      ],
      [P.QC_VIEW, P.QC_DECIDE],
    )

    expect(await screen.findByText(`QC-${status}`)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Inspect' })).toBeVisible()
  })

  it.each([['ACCEPTED' as const], ['REJECTED' as const], ['SCRAP' as const], ['CLOSED' as const]])(
    'withholds Inspect on a %s inspection, which the API would refuse',
    async (status) => {
      // Offering an action the state machine will reject puts the inspector
      // through a full disposition form for a 409.
      await openHistory(
        [
          inspection({
            code: 'QC-SETTLED',
            status,
            result: 'ACCEPTED',
            inspected_at: '2026-08-30T09:00:00Z',
          }),
        ],
        [P.QC_VIEW, P.QC_DECIDE],
      )

      expect(await screen.findByText('QC-SETTLED')).toBeVisible()
      expect(screen.queryByRole('button', { name: 'Inspect' })).toBeNull()
    },
  )

  it('shows the decided result rather than the status once a verdict exists', async () => {
    // `result` is never null - an undecided inspection carries PENDING - so a
    // cell that read `status` unconditionally would report every finished
    // inspection as CLOSED and hide whether the lot passed.
    await openHistory(
      [inspection({ code: 'QC-0100', status: 'CLOSED', result: 'REJECTED' })],
      [P.QC_VIEW],
    )

    const row = (await screen.findByText('QC-0100')).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('Rejected')).toBeVisible()
  })
})

// ---------------------------------------------------------------- dialog ----

/**
 * The inspection sheet, reached from the queue.
 *
 * What this dialog SENDS is the part worth pinning. The API derives the verdict
 * from how the quantity was split, so the decision payload carries no `result`
 * field at all; the five quantities are decimal strings, because a measured
 * split that round-trips through a JS number stops adding up; and a reading goes
 * into one typed column per parameter type rather than a single text value.
 */

const INSPECTION_ID = '01a00000-0000-7000-8000-0000000000q1'
const PARAMETER_ID = '01a00000-0000-7000-8000-0000000000p1'

/** `GET /quality/inspections/{id}` - the summary plus its check sheet. */
function detail(overrides: Record<string, unknown> = {}) {
  return {
    ...inspection(),
    parameters: [
      {
        id: PARAMETER_ID,
        template_id: '01a00000-0000-7000-8000-0000000000t1',
        sequence: 1,
        parameter_name: 'Overall length',
        parameter_type: 'NUMERIC',
        unit: 'mm',
        min_value: '12.5000',
        max_value: '13.5000',
        target_value: '13.0000',
        allowed_values: null,
        is_critical: false,
        is_mandatory: true,
      },
    ],
    results: [],
    remarks: null,
    rejection_reason: null,
    closed_at: null,
    ...overrides,
  }
}

const EVALUATION = {
  recommended_result: 'ACCEPTED',
  is_conclusive: true,
  has_critical_failure: false,
  measured_count: 1,
  in_spec_count: 1,
  failures: [],
  critical_failures: [],
  missing_mandatory: [],
}

/** `POST .../decide` - what the decision actually moved in stock. */
function decisionResponse() {
  return {
    inspection: { ...detail(), status: 'ACCEPTED', result: 'ACCEPTED' },
    evaluation: EVALUATION,
    released_quantity: '100.0000',
    damaged_quantity: '0.0000',
    scrapped_quantity: '0.0000',
    still_on_hold_quantity: '0.0000',
    return_required: false,
  }
}

/** Open the sheet from the queue, the way an inspector reaches it. */
async function openInspection(
  routes: Record<string, unknown>,
  permissions: string[],
): Promise<ReturnType<typeof renderScreen>> {
  const rendered = renderScreen(<QualityPage />, {
    permissions,
    routes: {
      'GET /quality/pending': page([inspection()]),
      [`GET /quality/inspections/${INSPECTION_ID}`]: ok(detail()),
      ...routes,
    },
  })
  await rendered.user.click(await screen.findByRole('button', { name: 'Inspect' }))
  return rendered
}

describe('the inspection sheet', () => {
  it('posts a reading into the typed column its parameter belongs in', async () => {
    const { user, fetch } = await openInspection(
      {
        [`POST /quality/inspections/${INSPECTION_ID}/results`]: ok({
          inspection: detail(),
          evaluation: EVALUATION,
        }),
        [`POST /quality/inspections/${INSPECTION_ID}/decide`]: ok(decisionResponse()),
      },
      [P.QC_VIEW, P.QC_INSPECT, P.QC_DECIDE],
    )

    await user.type(await screen.findByLabelText(/measured value/i), '13.05')
    await user.type(screen.getByLabelText('Accepted'), '100')
    await user.type(screen.getByLabelText(/reason for this decision/i), 'Within tolerance')
    await user.click(screen.getByRole('button', { name: /submit inspection/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/quality/inspections/${INSPECTION_ID}/results`),
    )) as { measurements: Record<string, unknown>[]; remarks: unknown }

    // The API keeps a reading in one of three typed columns according to the
    // parameter's type. Sending a measurement as text would store a number
    // where every range check becomes a string comparison - which is how "10"
    // comes out smaller than "9".
    expect(body.measurements).toEqual([
      {
        parameter_id: PARAMETER_ID,
        measured_value_numeric: '13.05',
        measured_value_bool: null,
        measured_value_text: null,
        remark: null,
        photo_document_id: null,
      },
    ])
    // An untouched optional field is omitted as null rather than sent as an
    // empty string, which the API bounds and would refuse.
    expect(body.remarks).toBeNull()
  })

  it('decides with decimal strings and no verdict of its own', async () => {
    const { user, fetch } = await openInspection(
      {
        [`POST /quality/inspections/${INSPECTION_ID}/results`]: ok({
          inspection: detail(),
          evaluation: EVALUATION,
        }),
        [`POST /quality/inspections/${INSPECTION_ID}/decide`]: ok(decisionResponse()),
      },
      // Typing a reading is the `qc.inspect` half; deciding is the other.
      [P.QC_VIEW, P.QC_INSPECT, P.QC_DECIDE],
    )

    await user.type(await screen.findByLabelText(/measured value/i), '13.05')
    await user.type(screen.getByLabelText('Accepted'), '99.5')
    await user.type(screen.getByLabelText('Hold'), '0.5')
    await user.type(screen.getByLabelText(/reason for this decision/i), 'Half a piece held back')
    await user.click(screen.getByRole('button', { name: /submit inspection/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/quality/inspections/${INSPECTION_ID}/decide`),
    )) as Record<string, unknown>

    // Quantities stay strings end to end: 99.5 + 0.5 read back through a JS
    // number is not reliably 100, and a lot that is a fraction short in the
    // system is unexplained stock on the floor.
    expect(body.accepted_quantity).toBe('99.5')
    expect(body.hold_quantity).toBe('0.5')
    // An empty bucket is an explicit zero, not an omission - the API splits the
    // whole inspected quantity and a missing field is not the same as none.
    expect(body.rejected_quantity).toBe('0')
    expect(body.rework_quantity).toBe('0')
    expect(body.scrap_quantity).toBe('0')
    expect(body.reason).toBe('Half a piece held back')
    // The verdict is the server's - it derives the result from the split. A
    // `result` sent from here would be a second opinion the API does not read.
    expect(body).not.toHaveProperty('result')
    expect(body.rejection_reason).toBeNull()
  })

  it('refuses a split that does not add up to the inspected quantity', async () => {
    const { user, fetch } = await openInspection({}, [P.QC_VIEW, P.QC_INSPECT, P.QC_DECIDE])

    await user.type(await screen.findByLabelText(/measured value/i), '13.05')
    await user.type(screen.getByLabelText('Accepted'), '80')
    await user.type(screen.getByLabelText(/reason for this decision/i), 'Most of it passed')
    await user.click(screen.getByRole('button', { name: /submit inspection/i }))

    // 20 pieces unaccounted for is 20 pieces of stock the system cannot explain,
    // so nothing is posted at all - not even the readings.
    expect(await screen.findByText(/must add up to the 100 inspected/i)).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('skips the readings post when the inspection has no check sheet', async () => {
    // `RecordResultsRequest.measurements` is minItems: 1. Posting an empty list
    // earns a 422 whose field path no control on this form renders, and which
    // the query client suppresses - the decision then never happens and the
    // screen says nothing at all.
    const { user, fetch } = await openInspection(
      {
        [`GET /quality/inspections/${INSPECTION_ID}`]: ok(detail({ parameters: [] })),
        [`POST /quality/inspections/${INSPECTION_ID}/decide`]: ok(decisionResponse()),
      },
      [P.QC_VIEW, P.QC_DECIDE],
    )

    await user.type(await screen.findByLabelText('Accepted'), '100')
    await user.type(screen.getByLabelText(/reason for this decision/i), 'No sheet, judged on sight')
    await user.click(screen.getByRole('button', { name: /submit inspection/i }))

    await waitFor(() => fetch.requestFor('POST', `/quality/inspections/${INSPECTION_ID}/decide`))
    expect(
      fetch.calls.some((call) => call.path.endsWith('/results')),
      'an inspection with no parameters must not post an empty measurement set',
    ).toBe(false)
  })

  it('sends a decide-only account straight to the decision, never to the readings post', async () => {
    // `POST .../results` is `qc.inspect`; `POST .../decide` is `qc.decide`. An
    // account holding only the second used to be shown every reading control,
    // made to fill the mandatory ones, and then refused with a 403 on the
    // readings post - so the decision it was entitled to record never happened.
    const { user, fetch } = await openInspection(
      {
        [`GET /quality/inspections/${INSPECTION_ID}`]: ok(
          detail({
            results: [
              {
                id: '01a00000-0000-7000-8000-0000000000r1',
                inspection_id: INSPECTION_ID,
                parameter_id: PARAMETER_ID,
                measured_value_numeric: '13.05',
                measured_value_bool: null,
                measured_value_text: null,
                is_within_spec: true,
                remark: null,
                photo_document_id: null,
              },
            ],
          }),
        ),
        [`POST /quality/inspections/${INSPECTION_ID}/decide`]: ok(decisionResponse()),
      },
      [P.QC_VIEW, P.QC_DECIDE],
    )

    // The inspector's reading is shown as recorded and cannot be changed here.
    const reading = await screen.findByLabelText(/measured value/i)
    expect(reading).toHaveValue('13.05')
    expect(reading).toHaveAttribute('readonly')
    expect(screen.getByText('Readings are recorded by an inspector')).toBeVisible()

    await user.type(screen.getByLabelText('Accepted'), '100')
    await user.type(screen.getByLabelText(/reason for this decision/i), 'Reading already on file')
    await user.click(screen.getByRole('button', { name: /submit inspection/i }))

    await waitFor(() => fetch.requestFor('POST', `/quality/inspections/${INSPECTION_ID}/decide`))
    expect(
      fetch.calls.some((call) => call.path.endsWith('/results')),
      'a decide-only account must never be sent to the qc.inspect endpoint',
    ).toBe(false)
  })

  it('puts a refused decision back on the field that caused it', async () => {
    const { user } = await openInspection(
      {
        [`GET /quality/inspections/${INSPECTION_ID}`]: ok(detail({ parameters: [] })),
        [`POST /quality/inspections/${INSPECTION_ID}/decide`]: fail(
          422,
          'VALIDATION_ERROR',
          'Invalid decision',
          { fields: { reason: ['This inspection is already closed'] } },
        ),
      },
      [P.QC_VIEW, P.QC_DECIDE],
    )

    await user.type(await screen.findByLabelText('Accepted'), '100')
    await user.type(screen.getByLabelText(/reason for this decision/i), 'Passed on sight')
    await user.click(screen.getByRole('button', { name: /submit inspection/i }))

    // The query client stays quiet for a 422 that carries field errors, on the
    // understanding that the form shows them inline. If it does not, the submit
    // looks to the inspector like it did nothing at all.
    expect(await screen.findByText('This inspection is already closed')).toBeVisible()
  })

  it('offers only the readings to an inspector who may not decide', async () => {
    await openInspection({}, [P.QC_VIEW, P.QC_INSPECT])

    expect(await screen.findByText('Readings only')).toBeVisible()
    // The disposition is where stock actually moves, so it is absent rather
    // than merely disabled, and the button says what the form will do.
    expect(screen.queryByLabelText('Accepted')).toBeNull()
    expect(screen.getByRole('button', { name: /save readings/i })).toBeVisible()
  })
})

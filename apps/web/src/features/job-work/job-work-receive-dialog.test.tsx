/**
 * The receive dialog request body (E7b/P10.M6, P10.M8).
 *
 * The backend has carried `is_final`, `approve_excess_loss` and
 * `rejection_action` for a while, but the dialog never sent them: a short
 * return could not be closed from the screen, excess-loss approval was
 * API-only, and every rejection arrived as RETURN_TO_STOCK. What is asserted
 * here is the exact body the dialog posts, plus the two gates around it - the
 * excess approval is offered only to `job_work.close` holders, and the vendor
 * challan date rides along with the vendor challan number.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { JobWorkIssueDialog, JobWorkReceiveDialog } from './job-work-dialogs'
import { receiveSchema } from './job-work-receive-schema'
import type { JobWorkOrderDetail, JobWorkOrderLine } from './job-work-model'

const LINE: JobWorkOrderLine = {
  id: 'line-1',
  job_work_order_id: 'jw-1',
  line_number: 1,
  item_id: 'item-raw-1',
  is_batch_tracked: false,
  is_serial_tracked: false,
  serial_ids: [],
  batch_id: null,
  uom_id: 'uom-1',
  quantity_to_issue: '100.0000',
  issued_quantity: '100.0000',
  received_quantity: '0.0000',
  expected_output_item_id: 'item-coated-1',
  expected_output_quantity: '100.0000',
  received_output_quantity: '0.0000',
  rejected_quantity: '0.0000',
  scrap_quantity: '0.0000',
  expected_loss_percentage: '5.0000',
  reservation_id: null,
  notes: null,
}

const DETAIL: JobWorkOrderDetail = {
  id: 'jw-1',
  code: 'JW-0001',
  status: 'ISSUED',
  version: 1,
  vendor_id: 'vendor-aaaa-0001',
  warehouse_id: 'wh-1',
  return_warehouse_id: 'wh-1',
  cost_centre_id: null,
  process_type: 'PVD',
  process_description: 'PVD, rose gold',
  rate_basis: 'PER_PIECE',
  rate_per_unit: '12.500000',
  tax_rate: '18.0000',
  sac_code: '9988',
  estimated_charges: '1250.0000',
  total_charges: '0.0000',
  vendor_debit_amount: '0.0000',
  receipt_count: 0,
  requires_qc: false,
  expected_return_date: '2026-09-20',
  issued_at: '2026-09-05T08:00:00Z',
  approved_at: '2026-09-04T08:00:00Z',
  approved_by_id: null,
  closed_at: null,
  cancellation_reason: null,
  source_type: 'MANUAL',
  source_id: null,
  notes: null,
  lines: [LINE],
}

function renderReceive(permissions: string[], detail = DETAIL, extraRoutes: RouteTable = {}) {
  return renderScreen(
    <JobWorkReceiveDialog open onOpenChange={() => {}} orderId="jw-1" orderCode="JW-0001" />,
    {
      permissions: [P.JOB_WORK_VIEW, P.JOB_WORK_RECEIVE, ...permissions],
      routes: {
        'GET /job-work/jw-1': ok(detail),
        'POST /job-work/jw-1/receive': ok({ received: true }),
        ...extraRoutes,
      },
    },
  )
}

const SERIAL_LINE = {
  ...LINE,
  is_serial_tracked: true,
  quantity_to_issue: '4',
  issued_quantity: '4',
  expected_output_quantity: '4',
}
const SERIAL_UNITS = [1, 2, 3, 4].map((number) => ({
  id: `unit-${number}`,
  serial_number: `SERIAL-00${number}`,
  status: 'ISSUED',
  item_id: LINE.item_id,
}))

describe('named serial processing', () => {
  it('bounds the whole mixed-outcome receipt at 500 units while leaving nonserial quantity uncapped', () => {
    const line = {
      line_number: 1,
      label: 'Line 1',
      received_quantity: '200',
      rejected_quantity: '100',
      returned_quantity: '100',
      rejection_action: 'RETURN_TO_STOCK',
      rejection_reason: '',
      is_final: true,
      output_serial_ids: Array.from({ length: 200 }, (_, index) => `good-${index}`),
      rejected_serial_ids: Array.from({ length: 100 }, (_, index) => `rejected-${index}`),
      returned_unprocessed_serial_ids: Array.from(
        { length: 100 },
        (_, index) => `unworked-${index}`,
      ),
      scrapped_serial_ids: [],
    }
    const body = {
      receipt_date: '2026-10-25',
      remarks: '',
      reason: 'Physical units counted',
      vendor_challan_number: '',
      vendor_challan_date: '',
      approve_excess_loss: false,
      lines: [
        line,
        {
          ...line,
          line_number: 2,
          output_serial_ids: [],
          rejected_serial_ids: [],
          returned_unprocessed_serial_ids: [],
          scrapped_serial_ids: Array.from({ length: 100 }, (_, index) => `loss-${index}`),
        },
      ],
    }
    expect(receiveSchema.safeParse(body).success).toBe(true)
    const over = receiveSchema.safeParse({
      ...body,
      lines: [
        body.lines[0],
        {
          ...body.lines[1],
          scrapped_serial_ids: [...body.lines[1]!.scrapped_serial_ids, 'loss-101'],
        },
      ],
    })
    expect(over.success).toBe(false)
    expect(over.error?.issues[0]?.message).toContain('at most 500 serial units across all lines')
    expect(
      receiveSchema.safeParse({
        ...body,
        lines: [
          {
            ...line,
            received_quantity: '100000',
            output_serial_ids: [],
            rejected_serial_ids: [],
            returned_unprocessed_serial_ids: [],
          },
        ],
      }).success,
    ).toBe(true)
  })
  it('requires the exact issue count and sends the selected stock identities', async () => {
    const urls: URL[] = []
    const { user, fetch } = renderScreen(
      <JobWorkIssueDialog open onOpenChange={() => {}} orderId="jw-1" orderCode="JW-0001" />,
      {
        permissions: [P.JOB_WORK_VIEW, P.JOB_WORK_ISSUE, P.INVENTORY_VIEW],
        routes: {
          'GET /job-work/jw-1': ok({
            ...DETAIL,
            lines: [{ ...SERIAL_LINE, issued_quantity: '0', quantity_to_issue: '2' }],
          }),
          'GET /transporters/options': ok([]),
          'GET /inventory/serials': ({ url }) => {
            urls.push(url)
            return page(SERIAL_UNITS.slice(0, 2).map((unit) => ({ ...unit, status: 'IN_STOCK' })))
          },
          'POST /job-work/jw-1/issue': ok({}),
        },
      },
    )
    await user.type(
      await screen.findByRole('textbox', { name: 'Reason' }),
      'Send named units for coating',
    )
    await user.click(screen.getByRole('button', { name: 'Issue and create challan' }))
    expect(
      await screen.findByText('Choose one distinct serial unit for every issued unit.'),
    ).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    await user.click(await screen.findByRole('button', { name: 'SERIAL-001' }))
    await user.click(screen.getByRole('button', { name: 'SERIAL-002' }))
    await user.click(screen.getByRole('button', { name: 'Issue and create challan' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/job-work/jw-1/issue')).toMatchObject({
        lines: [{ issued_quantity: '2', serial_ids: ['unit-1', 'unit-2'] }],
      }),
    )
    expect(urls[0]?.searchParams.get('warehouse_id')).toBe('wh-1')
  })

  it('names output, rejected, untouched and lost units separately, using only outstanding vendor units', async () => {
    const urls: URL[] = []
    const { user, fetch } = renderReceive(
      [P.JOB_WORK_CLOSE],
      { ...DETAIL, lines: [SERIAL_LINE] },
      {
        'GET /inventory/serials': ({ url }) => {
          urls.push(url)
          return page(SERIAL_UNITS)
        },
      },
    )
    await user.clear(await screen.findByLabelText('Received'))
    await user.type(screen.getByLabelText('Received'), '1')
    await user.type(screen.getByLabelText('Rejected'), '1')
    await user.type(screen.getByLabelText('Returned unworked'), '1')
    await user.click(screen.getByLabelText('Close line - write off shortfall'))
    await user.click(screen.getByLabelText('Rejection reason'))
    await user.paste('Coating did not adhere')
    await user.click(screen.getByRole('textbox', { name: 'Reason' }))
    await user.paste('Four units individually reconciled')
    await user.click(screen.getByRole('button', { name: 'Record receipt' }))
    expect(await screen.findByText(/Name each serial unit once/)).toBeVisible()
    const buckets = [
      'Processed output serials',
      'Rejected serials',
      'Unworked return serials',
      'Process loss serials',
    ]
    for (const [index, label] of buckets.entries()) {
      const search = screen.getByLabelText(label)
      const picker = search.parentElement!.closest('div.space-y-2')!
      await user.click(
        within(picker as HTMLElement).getByRole('button', { name: `SERIAL-00${index + 1}` }),
      )
      if (index === 0) {
        const otherPicker = screen
          .getByLabelText('Rejected serials')
          .parentElement!.closest('div.space-y-2')!
        expect(
          within(otherPicker as HTMLElement).getByRole('button', { name: /SERIAL-001/ }),
        ).toBeDisabled()
      }
    }
    await user.click(screen.getByRole('button', { name: 'Record receipt' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/job-work/jw-1/receive')).toMatchObject({
        lines: [
          {
            output_serial_ids: ['unit-1'],
            rejected_serial_ids: ['unit-2'],
            returned_unprocessed_serial_ids: ['unit-3'],
            scrapped_serial_ids: ['unit-4'],
            is_final: true,
          },
        ],
      }),
    )
    expect(urls.length).toBeGreaterThan(0)
    for (const url of urls) {
      expect(url.searchParams.get('job_work_order_line_id')).toBe('line-1')
      expect(url.searchParams.get('job_work_outstanding_only')).toBe('true')
      expect(url.searchParams.get('status')).toBe('ISSUED')
      expect(url.searchParams.has('warehouse_id')).toBe(false)
    }
  })
})

describe('the receive dialog request body', () => {
  it.each([
    ['50', '0', '50'],
    ['200', '30', '140'],
  ])(
    'converts outstanding input to output units for expected yield %s',
    async (expected, received, output) => {
      const { user, fetch } = renderReceive([], {
        ...DETAIL,
        lines: [{ ...LINE, expected_output_quantity: expected, received_quantity: received }],
      })

      await waitFor(() => expect(screen.getByLabelText('Received')).toHaveValue(output))
      await user.type(
        screen.getByRole('textbox', { name: 'Reason' }),
        'Full remaining output returned',
      )
      await user.click(screen.getByRole('button', { name: 'Record receipt' }))

      await waitFor(() =>
        expect(fetch.requestFor('POST', '/job-work/jw-1/receive')).toMatchObject({
          lines: [{ output_quantity: output }],
        }),
      )
    },
  )

  it('sends the close-line flag, the rejection action with its reason, and the vendor challan date', async () => {
    const { user, fetch } = renderReceive([P.JOB_WORK_CLOSE])

    // A short lot: 90 good, 2 rejected, nothing unworked.
    await user.clear(await screen.findByLabelText('Received'))
    await user.type(screen.getByLabelText('Received'), '90')
    await user.type(screen.getByLabelText('Rejected'), '2')

    // The rejection controls appear once something is rejected.
    expect(await screen.findByLabelText('Rejection action')).toBeVisible()
    await user.type(screen.getByLabelText('Rejection reason'), '  Plating peeled  ')

    fireEvent.change(screen.getByLabelText('Vendor challan date'), {
      target: { value: '2026-09-09' },
    })

    await user.click(screen.getByLabelText('Close line - write off shortfall'))
    await user.type(screen.getByRole('textbox', { name: 'Reason' }), '  Short lot closed  ')
    await user.click(screen.getByRole('button', { name: 'Record receipt' }))

    await waitFor(() => fetch.requestFor('POST', '/job-work/jw-1/receive'))
    const body = fetch.requestFor('POST', '/job-work/jw-1/receive') as {
      vendor_challan_date?: string | null
      approve_excess_loss?: boolean
      lines?: unknown
    }
    expect(body?.vendor_challan_date).toBe('2026-09-09')
    expect(body?.approve_excess_loss).toBe(false)
    expect(body?.lines).toEqual([
      {
        line_number: 1,
        batch_id: null,
        output_quantity: '90',
        rejected_quantity: '2',
        rejection_action: 'RETURN_TO_STOCK',
        returned_unprocessed_quantity: '0',
        is_final: true,
        notes: 'Plating peeled',
      },
    ])
  })

  it('sends the excess-loss approval only from a job_work.close holder', async () => {
    const closer = renderReceive([P.JOB_WORK_CLOSE])
    expect(await screen.findByLabelText('Approve excess loss (debit vendor)')).toBeVisible()
    closer.unmount()

    renderReceive([])
    await screen.findByLabelText('Received')
    expect(screen.queryByLabelText('Approve excess loss (debit vendor)')).toBeNull()
  })

  it('shows the computed write-off against the allowance when a line is closed', async () => {
    const { user } = renderReceive([P.JOB_WORK_CLOSE])

    await user.clear(await screen.findByLabelText('Received'))
    await user.type(screen.getByLabelText('Received'), '90')
    await user.click(screen.getByLabelText('Close line - write off shortfall'))

    // 100 out, 90 back good: 10 becomes process loss, 5 of it within the 5%
    // allowance and 5 beyond it.
    expect(await screen.findByText(/10 still out becomes/)).toBeVisible()
    expect(screen.getByText(/5 beyond it/)).toBeVisible()
  })
})

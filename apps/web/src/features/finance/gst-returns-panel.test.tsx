import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, renderScreen } from '@/test/harness'

import { GstReturnsPanel } from './gst-returns-panel'

const JOB_ID = 'jb000000-0000-7000-8000-000000000003'
const JOB_PATH = `/reports/jobs/${JOB_ID}`
const ACCEPTED = { job_id: JOB_ID, status: 'queued', status_url: `/api/v1${JOB_PATH}` }

const AMOUNTS = {
  taxable_amount: '1000.00',
  cgst_amount: '90.00',
  sgst_amount: '90.00',
  igst_amount: '0.00',
  tax_amount: '180.00',
  unallocated_tax_amount: '0.00',
}

const SUMMARY = {
  date_from: '2026-09-01',
  date_to: '2026-09-30',
  outward: { documents: AMOUNTS, notes: AMOUNTS, net: AMOUNTS },
  itc: { documents: AMOUNTS, notes: AMOUNTS, net: AMOUNTS },
  net_cgst: '0.00',
  net_sgst: '0.00',
  net_igst: '0.00',
  net_tax_payable: '0.00',
  unallocated_tax_amount: '0.00',
}

describe('GST return preparation', () => {
  it('shows the GSTR-3B summary and exports the selected month through a report job', async () => {
    const exportUrls: URL[] = []
    const { user } = renderScreen(<GstReturnsPanel period="2026-09" />, {
      permissions: [P.GST_REPORT_VIEW, P.REPORT_EXPORT],
      routes: {
        'GET /finance/gstr-3b': ok(SUMMARY),
        'POST /finance/gstr-1/export': ({ url }) => {
          exportUrls.push(url)
          return ok(ACCEPTED)
        },
        [`GET ${JOB_PATH}`]: ok({
          id: JOB_ID,
          report_key: 'GSTR1',
          status: 'COMPLETED',
          parameters: null,
          row_count: 2,
          generated_at: '2026-09-30T09:00:00Z',
          expires_at: '2026-10-01T09:00:00Z',
          document_id: null,
          download_url: '/api/v1/documents/gstr1/download',
          error_message: null,
          created_at: '2026-09-30T08:59:00Z',
        }),
      },
    })

    expect(await screen.findByText('3.1 Outward supplies, net')).toBeVisible()
    expect(screen.getByText('4 Input tax credit, net')).toBeVisible()
    expect(screen.getByText('Net position')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Export GSTR-1 XLSX' }))

    expect(await screen.findByText('GSTR-1 workbook is ready')).toBeVisible()
    expect(exportUrls).toHaveLength(1)
    expect(exportUrls[0]!.searchParams.get('date_from')).toBe('2026-09-01')
    expect(exportUrls[0]!.searchParams.get('date_to')).toBe('2026-09-30')
  })

  it('does not offer workbook export without report.export', async () => {
    renderScreen(<GstReturnsPanel period="2026-09" />, {
      permissions: [P.GST_REPORT_VIEW],
      routes: { 'GET /finance/gstr-3b': ok(SUMMARY) },
    })

    expect(await screen.findByText('3.1 Outward supplies, net')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Export GSTR-1 XLSX' })).toBeNull()
  })
})

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ReportJobPanel } from './job-panel'
import type { ReportJob, ReportJobController, ReportJobPhase } from './report-jobs'

function panel(overrides: Partial<ReportJob> = {}, phase: ReportJobPhase = 'ready') {
  const job: ReportJob = {
    id: 'job-profitability',
    report_key: 'PRODUCT_PROFITABILITY',
    status: 'COMPLETED',
    parameters: null,
    row_count: 12,
    generated_at: '2026-09-30T12:00:00Z',
    expires_at: '2026-10-01T12:00:00Z',
    document_id: 'document-profitability',
    download_url: '/api/v1/reports/jobs/job-profitability/download',
    error_message: null,
    created_at: '2026-09-30T11:59:00Z',
    ...overrides,
  }
  const controller: ReportJobController = {
    phase,
    job,
    error: null,
    start: vi.fn(),
    reset: vi.fn(),
    retry: vi.fn(),
    isRetrying: false,
    download: vi.fn(),
    isDownloading: false,
  }
  render(<ReportJobPanel controller={controller} label="Product profitability" />)
  return controller
}

describe('profitability export calculation basis', () => {
  it.each([null, {}, { calculation_version: 1 }])(
    'labels a completed older calculation and preserves its download (%j)',
    async (parameters) => {
      const user = userEvent.setup()
      const controller = panel({ parameters })

      expect(screen.getByText('Historical standard-cost calculation')).toBeVisible()
      expect(
        screen.getByText(/Run Product profitability again for invoice and credit dates/),
      ).toBeVisible()
      expect(screen.getByText('12 rows.')).toBeVisible()
      await user.click(screen.getByRole('button', { name: 'Download' }))
      expect(controller.download).toHaveBeenCalledOnce()
      await user.click(screen.getByRole('button', { name: 'Dismiss' }))
      expect(controller.reset).toHaveBeenCalledOnce()
    },
  )

  it('keeps a current actual-profitability export in the normal ready flow', () => {
    panel({ parameters: { calculation_version: 2 } })

    expect(screen.getByText('Product profitability is ready')).toBeVisible()
    expect(screen.queryByText('Historical standard-cost calculation')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled()
  })

  it('does not label other report exports as old profitability calculations', () => {
    panel({ report_key: 'STOCK_AVAILABILITY' })

    expect(screen.getByText('Product profitability is ready')).toBeVisible()
    expect(screen.queryByText('Historical standard-cost calculation')).not.toBeInTheDocument()
  })

  it('does not present an old queued job as a completed historical file', () => {
    panel({ status: 'QUEUED', document_id: null, download_url: null }, 'queued')

    expect(screen.getByText('Product profitability is queued')).toBeVisible()
    expect(screen.queryByText('Historical standard-cost calculation')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
  })
})

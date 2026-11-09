import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import type { ReportJob } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import { P } from '@/lib/permissions'
import { fail, ok, renderScreen, type RouteTable } from '@/test/harness'

import { ChallanDownload } from './job-work-challan-download'

/** What `POST /job-work/challans/{id}/pdf` answers: the job and where to poll it. */
const accepted = (jobId: string) => ({
  job_id: jobId,
  status: 'queued',
  status_url: `/api/v1/reports/jobs/${jobId}`,
})

const COMPLETED: ReportJob = {
  id: 'job-1',
  report_key: 'JOB_WORK_CHALLAN',
  status: 'COMPLETED',
  parameters: null,
  row_count: 1,
  generated_at: '2026-10-16T08:00:05Z',
  expires_at: '2026-10-17T08:00:05Z',
  document_id: 'doc-1',
  download_url: '/api/v1/reports/jobs/job-1/download',
  error_message: null,
  created_at: '2026-10-16T08:00:00Z',
}

function renderChallan(routes: RouteTable) {
  return renderScreen(<ChallanDownload challanId="ch-1" challanCode="JWC-0001" />, {
    permissions: [P.JOB_WORK_VIEW],
    routes,
  })
}

describe('the job-work challan download', () => {
  it('downloads the finished PDF under the challan number', async () => {
    const download = vi.spyOn(api, 'download').mockResolvedValue()
    const { user } = renderChallan({
      'POST /job-work/challans/ch-1/pdf': ok(accepted('job-1')),
      'GET /reports/jobs/job-1': ok(COMPLETED),
    })

    await user.click(await screen.findByRole('button', { name: 'PDF' }))
    await user.click(await screen.findByRole('button', { name: 'Download PDF' }))

    // The job's own link, saved under the challan's number rather than the
    // name the stored document carries.
    await waitFor(() =>
      expect(download).toHaveBeenCalledWith('/reports/jobs/job-1/download', 'JWC-0001.pdf'),
    )
    download.mockRestore()
  })

  it('recovers a failed status read without queueing another PDF', async () => {
    let unavailable = true
    const { user, fetch } = renderChallan({
      'POST /job-work/challans/ch-1/pdf': ok(accepted('job-1')),
      'GET /reports/jobs/job-1': () =>
        unavailable
          ? fail(503, 'SERVICE_UNAVAILABLE', 'Worker status is unavailable')
          : ok(COMPLETED),
    })

    await user.click(await screen.findByRole('button', { name: 'PDF' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Worker status is unavailable')
    expect(screen.queryByRole('button', { name: /Generating/ })).toBeNull()

    unavailable = false
    await user.click(screen.getByRole('button', { name: 'Retry status' }))
    expect(await screen.findByRole('button', { name: 'Download PDF' })).toBeEnabled()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })

  it('says a failed status read once, under the button', async () => {
    const { user } = renderScreen(
      <>
        <ChallanDownload challanId="ch-1" challanCode="JWC-0001" />
        <Toaster />
      </>,
      {
        permissions: [P.JOB_WORK_VIEW],
        errorNets: true,
        routes: {
          'POST /job-work/challans/ch-1/pdf': ok(accepted('job-1')),
          'GET /reports/jobs/job-1': fail(
            503,
            'SERVICE_UNAVAILABLE',
            'Worker status is unavailable',
          ),
        },
      },
    )

    await user.click(await screen.findByRole('button', { name: 'PDF' }))

    expect(await screen.findByText(/Could not check the challan/)).toHaveTextContent(
      'Worker status is unavailable',
    )
    const toasts = within(screen.getByRole('region', { name: 'Notifications' }))
    expect(toasts.queryAllByRole('alert')).toHaveLength(0)
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('shows a render failure and allows a fresh generation attempt', async () => {
    let attempt = 0
    const { user, fetch } = renderChallan({
      'POST /job-work/challans/ch-1/pdf': () => ok(accepted(`job-${++attempt}`)),
      'GET /reports/jobs/job-1': ok({
        ...COMPLETED,
        status: 'FAILED',
        document_id: null,
        download_url: null,
        error_message: 'PDF renderer unavailable',
      }),
      'GET /reports/jobs/job-2': ok({ ...COMPLETED, id: 'job-2' }),
    })

    await user.click(await screen.findByRole('button', { name: 'PDF' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('PDF renderer unavailable')
    await user.click(screen.getByRole('button', { name: 'Retry PDF' }))
    expect(await screen.findByRole('button', { name: 'Download PDF' })).toBeEnabled()
    await waitFor(() =>
      expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(2),
    )
  })

  it('says a refused queue under the button and polls nothing until it is queued', async () => {
    let refuse = true
    const { user, fetch } = renderChallan({
      'POST /job-work/challans/ch-1/pdf': () =>
        refuse ? fail(409, 'CONFLICT', 'This challan has been cancelled') : ok(accepted('job-1')),
      'GET /reports/jobs/job-1': ok(COMPLETED),
    })

    await user.click(await screen.findByRole('button', { name: 'PDF' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('This challan has been cancelled')
    expect(fetch.calls.some((call) => call.path.startsWith('/reports/jobs/'))).toBe(false)

    refuse = false
    await user.click(screen.getByRole('button', { name: 'Retry PDF' }))
    expect(await screen.findByRole('button', { name: 'Download PDF' })).toBeEnabled()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

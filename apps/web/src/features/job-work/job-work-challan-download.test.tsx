import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, renderScreen } from '@/test/harness'

import { ChallanDownload } from './job-work-challan-download'
import type { ReportJob } from './job-work-model'

const COMPLETED: ReportJob = {
  id: 'job-1',
  report_key: 'JOB_WORK_CHALLAN',
  status: 'COMPLETED',
  row_count: 1,
  created_at: '2026-10-16T08:00:00Z',
}

describe('the job-work challan download', () => {
  it('recovers a failed status read without queueing another PDF', async () => {
    let unavailable = true
    const { user, fetch } = renderScreen(
      <ChallanDownload challanId="ch-1" challanCode="JWC-0001" />,
      {
        permissions: [P.JOB_WORK_VIEW],
        routes: {
          'POST /job-work/challans/ch-1/pdf': ok({ job_id: 'job-1', status: 'QUEUED' }),
          'GET /reports/jobs/job-1': () =>
            unavailable
              ? fail(503, 'SERVICE_UNAVAILABLE', 'Worker status is unavailable')
              : ok(COMPLETED),
        },
      },
    )

    await user.click(await screen.findByRole('button', { name: 'PDF' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Worker status is unavailable')
    expect(screen.queryByRole('button', { name: /Generating/ })).toBeNull()

    unavailable = false
    await user.click(screen.getByRole('button', { name: 'Retry status' }))
    expect(await screen.findByRole('button', { name: 'Download PDF' })).toBeEnabled()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })

  it('shows a render failure and allows a fresh generation attempt', async () => {
    let attempt = 0
    const { user, fetch } = renderScreen(
      <ChallanDownload challanId="ch-1" challanCode="JWC-0001" />,
      {
        permissions: [P.JOB_WORK_VIEW],
        routes: {
          'POST /job-work/challans/ch-1/pdf': () =>
            ok({ job_id: `job-${++attempt}`, status: 'QUEUED' }),
          'GET /reports/jobs/job-1': ok({
            ...COMPLETED,
            status: 'FAILED',
            error_message: 'PDF renderer unavailable',
          }),
          'GET /reports/jobs/job-2': ok({ ...COMPLETED, id: 'job-2' }),
        },
      },
    )

    await user.click(await screen.findByRole('button', { name: 'PDF' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('PDF renderer unavailable')
    await user.click(screen.getByRole('button', { name: 'Retry PDF' }))
    expect(await screen.findByRole('button', { name: 'Download PDF' })).toBeEnabled()
    await waitFor(() =>
      expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(2),
    )
  })
})

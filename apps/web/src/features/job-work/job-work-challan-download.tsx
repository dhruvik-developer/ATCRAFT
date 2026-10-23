/**
 * Queue a delivery-challan PDF and download it once the worker finishes.
 *
 * The render runs on Celery, so the button queues
 * `POST /job-work/challans/{id}/pdf`, polls the job row, and then pulls the
 * bytes through the authenticated client - an `<a href>` would fetch without
 * the bearer token. Shared by the order detail and the challan register.
 */

import { useQueryClient } from '@tanstack/react-query'
import { FileDown } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { api } from '@/lib/api-client'
import { describeError, fileMessages, success } from '@/lib/messages'
import { toast } from '@/lib/toast'

import { jobWorkKeys, useReportJob, type QueuedJobData } from './job-work-model'

/** The `data` half of an `AcceptedJobResponse`: what the queue endpoint hands back. */

export function ChallanDownload({
  challanId,
  challanCode,
}: {
  challanId: string
  challanCode: string
}) {
  const queryClient = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const [queued, setQueued] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const job = useReportJob(queued ? jobId : null)

  const queue = async () => {
    setJobId(null)
    setQueued(true)
    setFailed(null)
    try {
      const queuedJob = await api.post<QueuedJobData>(`/job-work/challans/${challanId}/pdf`, {})
      setJobId(queuedJob.job_id)
      toast.show(success.queued('Challan'), fileMessages.generating)
    } catch (error) {
      setQueued(false)
      const message = describeError(error)
      setFailed(message)
      toast.failure(error, 'Could not queue the challan')
    } finally {
      void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all })
    }
  }

  const download = async () => {
    if (!jobId) return
    setDownloading(true)
    try {
      await api.download(`/reports/jobs/${jobId}/download`, `${challanCode}.pdf`)
    } catch (error) {
      toast.failure(error, 'Could not download the challan')
    } finally {
      setDownloading(false)
    }
  }

  const status = job.data?.status
  if (job.error) {
    return (
      <div className="max-w-xs space-y-1">
        <Button
          variant="outline"
          size="sm"
          onClick={() => void job.refetch()}
          loading={job.isFetching}
        >
          Retry status
        </Button>
        <p role="alert" className="break-words text-xs text-destructive">
          Could not check the challan: {describeError(job.error)}
        </p>
      </div>
    )
  }
  if (status === 'COMPLETED') {
    return (
      <Button variant="outline" size="sm" onClick={() => void download()} loading={downloading}>
        <FileDown />
        Download PDF
      </Button>
    )
  }

  const failedMessage =
    failed ??
    (status === 'FAILED' ? (job.data?.error_message ?? fileMessages.generationFailed) : null)

  return (
    <div className="max-w-xs space-y-1">
      <Button
        variant="outline"
        size="sm"
        onClick={() => void queue()}
        loading={queued && status !== 'FAILED'}
        title={failedMessage ?? `Queue the PDF for ${challanCode}`}
      >
        <FileDown />
        {failedMessage ? 'Retry PDF' : queued ? 'Generating...' : 'PDF'}
      </Button>
      {failedMessage && (
        <p role="alert" className="break-words text-xs text-destructive">
          {failedMessage}
        </p>
      )}
    </div>
  )
}

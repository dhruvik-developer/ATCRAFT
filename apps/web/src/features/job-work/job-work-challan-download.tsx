/**
 * Queue a delivery-challan PDF and download it once the worker finishes.
 *
 * The render runs on Celery, so the button queues
 * `POST /job-work/challans/{id}/pdf` and hands the 202 to the shared report-job
 * controller (`features/reports/report-jobs.ts`), which polls the status URL the
 * 202 names and pulls the bytes through the authenticated client - an `<a href>`
 * would fetch without the bearer token. Shared by the order detail and the
 * challan register, where a row has room for one button rather than the
 * controller's strip, so the job's states are drawn on that button.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FileDown } from 'lucide-react'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, fileMessages } from '@/lib/messages'

import { jobWorkKeys } from './job-work-model'

export function ChallanDownload({
  challanId,
  challanCode,
}: {
  challanId: string
  challanCode: string
}) {
  const queryClient = useQueryClient()
  // Saved under the challan's own number rather than the stored document's
  // name, which is not the challan's.
  const job = useReportJob({ fileName: `${challanCode}.pdf` })
  const queue = useMutation({
    mutationFn: () => api.post<AcceptedJobData>(`/job-work/challans/${challanId}/pdf`, {}),
    // A fresh attempt stops watching the job it replaces, so that job's
    // failure no longer shows while the new one is queued.
    onMutate: () => job.reset(),
    onSuccess: (accepted) => job.start(accepted),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: jobWorkKeys.all }),
    // Said under the button, where the next press retries it; a toast would
    // say it twice.
    meta: { suppressErrorToast: true },
  })

  // The status URL itself could not be read: retry reading it rather than
  // queueing a second PDF.
  if (job.error) {
    return (
      <div className="max-w-xs space-y-1">
        <Button variant="outline" size="sm" onClick={job.retry} loading={job.isRetrying}>
          Retry status
        </Button>
        <FieldError
          className="break-words"
          message={`Could not check the challan: ${describeError(job.error)}`}
        />
      </div>
    )
  }
  if (job.phase === 'ready') {
    return (
      <Button variant="outline" size="sm" onClick={job.download} loading={job.isDownloading}>
        <FileDown />
        Download PDF
      </Button>
    )
  }

  const generating = queue.isPending || job.phase === 'queued' || job.phase === 'running'
  const failedMessage = queue.error
    ? (fieldErrorSummary(queue.error) ?? describeError(queue.error))
    : job.phase === 'failed'
      ? (job.job?.error_message ?? fileMessages.generationFailed)
      : null

  return (
    <div className="max-w-xs space-y-1">
      <Button
        variant="outline"
        size="sm"
        onClick={() => queue.mutate()}
        loading={generating}
        title={failedMessage ?? `Queue the PDF for ${challanCode}`}
      >
        <FileDown />
        {failedMessage ? 'Retry PDF' : generating ? 'Generating...' : 'PDF'}
      </Button>
      <FieldError className="break-words" message={failedMessage ?? undefined} />
    </div>
  )
}

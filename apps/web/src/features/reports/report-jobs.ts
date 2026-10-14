/**
 * The background report/export job flow.
 *
 * Anything that can outlive a request answers 202 with a job id and a status URL
 * (docs/api.md, "Long-running operations"). This hook owns the whole cycle:
 * queue -> poll -> download, so the finance valuation export and the report
 * catalogue behave identically instead of each inventing its own spinner.
 *
 * Polling stops dead the moment the job reaches a terminal state, and also the
 * moment the status URL itself fails: a report that failed at 09:00 must not
 * keep a tab requesting it until lunchtime, and neither must a status URL the
 * API cannot serve.
 *
 * The interval belongs to the query observer, so unmounting the screen tears it
 * down as well - `QueryObserver.onUnsubscribe` destroys the observer, which
 * clears the interval. That is why there is no `useEffect` cleanup here, and why
 * adding one would be a second, unsynchronised owner of the same timer.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useCallback, useState } from 'react'

import { BASE_URL, api } from '@/lib/api-client'
import { toast } from '@/lib/toast'
import type { AcceptedJob } from '@/types/api'

const POLL_INTERVAL_MS = 2000

/**
 * `GET` on the job's `status_url` - `ReportJobRead`.
 *
 * There is no progress percentage and no file name in this contract: the job
 * reports the rows it produced and, once it has one, the document to download.
 */
export interface ReportJob {
  id: string
  report_key: string
  /** `QUEUED | RUNNING | COMPLETED | FAILED`. */
  status: string
  parameters: Record<string, unknown> | null
  row_count: number
  generated_at: string | null
  expires_at: string | null
  document_id: string | null
  /** The API's own link to the artefact. Null until the job completes. */
  download_url: string | null
  error_message: string | null
  created_at: string
}

export type ReportJobPhase = 'idle' | 'queued' | 'running' | 'ready' | 'failed'

/**
 * The `data` block of a 202 - `{job_id, status, status_url}`.
 *
 * This, not the envelope, is what a caller actually holds: `api.post` unwraps
 * `ApiEnvelope` and returns `data`, and `useListQuery` reads `envelope.data`
 * too, so neither caller ever sees an `AcceptedJobResponse` at runtime.
 */
export type AcceptedJobData = AcceptedJob['data']

/**
 * The 202 envelope reports `queued` in lower case while the job table uses
 * `QUEUED`; normalising here keeps the caller from caring which it got.
 */
function normalise(status: string | null | undefined): string {
  return (status ?? '').toUpperCase()
}

export function isJobInFlight(status: string | null | undefined): boolean {
  const state = normalise(status)
  return state === 'QUEUED' || state === 'RUNNING' || state === ''
}

/**
 * The path `api` expects, taken from the URL the API told us to poll.
 *
 * `status_url` is the API's authoritative statement of where the job lives, so
 * it is used rather than a path guessed from the queueing endpoint. The client
 * prepends its own base, which has to come back off first. The same conversion
 * applies to `download_url`.
 */
export function toApiPath(statusUrl: string): string {
  let path = statusUrl
  if (/^https?:\/\//i.test(path)) {
    try {
      path = new URL(path).pathname
    } catch {
      // A malformed absolute URL is used as-is and fails loudly on the request.
    }
  }
  return path.startsWith(BASE_URL) ? path.slice(BASE_URL.length) || '/' : path
}

export interface ReportJobController {
  phase: ReportJobPhase
  job: ReportJob | null
  /** Why the watch failed, when the status URL itself could not be read. */
  error: Error | null
  /** Begin watching the job an endpoint just handed back with its 202. */
  start: (accepted: AcceptedJobData) => void
  reset: () => void
  /** Retry reading the existing job; never queues another artifact. */
  retry: () => void
  isRetrying: boolean
  download: () => void
  isDownloading: boolean
}

export function useReportJob(): ReportJobController {
  const [statusPath, setStatusPath] = useState<string | null>(null)

  const job = useQuery({
    queryKey: ['reports', 'job', statusPath],
    queryFn: () => api.get<ReportJob>(statusPath ?? ''),
    enabled: statusPath !== null,
    staleTime: 0,
    // One failure stops the poll. Retrying a status URL the API will not serve
    // turns a broken link into a request every two seconds for as long as the
    // tab is open.
    retry: false,
    refetchInterval: (query) =>
      query.state.status === 'error' || !isJobInFlight(query.state.data?.status)
        ? false
        : POLL_INTERVAL_MS,
  })

  const data = job.data ?? null
  const error = (job.error as Error | null) ?? null

  const download = useMutation({
    mutationFn: async () => {
      // `download_url` is the API's own statement of where the artefact is;
      // the document id is only a fallback for a job that reports one without
      // a link.
      const target = data?.download_url
        ? toApiPath(data.download_url)
        : data?.document_id
          ? `/documents/${data.document_id}/download`
          : null
      if (!target) {
        // Said here rather than thrown: the mutation cache replaces anything
        // that is not an `ApiError` with the generic "something went wrong",
        // and the one thing worth telling the user would be lost.
        toast.error('Nothing to download', 'This job finished without producing a file.')
        return
      }
      await api.download(target)
      // After the await, so it can only follow a download that actually began.
      // A failed one throws an `ApiError`, which the mutation cache toasts.
      toast.success('Download started', 'The file is saving to your device.')
    },
  })

  const start = useCallback((accepted: AcceptedJobData) => {
    // `AcceptedJobResponse.create()` builds
    // `data={"job_id", "status", "status_url"}` (api/src/core/responses.py), and
    // the client strips the envelope before this is reached - `api.post` returns
    // `envelope.data`, `useListQuery` returns `envelope.data`. So `status_url`
    // is read off the top level of what the caller holds.
    //
    // Reaching for `accepted.data?.status_url` type-checked perfectly against
    // the generated envelope schema and was `undefined` in every browser, which
    // is why a queued export sat there with no strip, no poll and no download.
    // The shape a caller holds is the shape to read.
    const statusUrl = accepted.status_url
    if (typeof statusUrl !== 'string') return
    setStatusPath(toApiPath(statusUrl))
    toast.show('Queued', 'The file is being generated in the background.')
  }, [])

  const reset = useCallback(() => setStatusPath(null), [])
  const retry = () => {
    if (statusPath !== null && error) void job.refetch()
  }

  let phase: ReportJobPhase = 'idle'
  if (statusPath !== null) {
    const state = normalise(data?.status)
    if (error) phase = 'failed'
    else if (state === 'COMPLETED') phase = 'ready'
    else if (state === 'FAILED') phase = 'failed'
    else if (state === 'RUNNING') phase = 'running'
    else phase = 'queued'
  }

  return {
    phase,
    job: data,
    error,
    start,
    reset,
    retry,
    isRetrying: Boolean(error) && job.isFetching,
    download: () => download.mutate(),
    isDownloading: download.isPending,
  }
}

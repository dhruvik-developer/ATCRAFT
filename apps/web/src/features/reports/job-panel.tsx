/**
 * The queued -> generating -> download strip for a background report job.
 *
 * Shared by the finance valuation export and the report catalogue so a queued
 * export looks the same wherever it was started, and so the "you can keep
 * working" promise is made in exactly one place.
 *
 * The job contract carries no progress percentage and no file name, so neither
 * is claimed here. What it does carry - the row count once the file exists, and
 * the reason when it does not - is what the strip reports.
 */

import { CheckCircle2, Download, Loader2, TriangleAlert, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/primitives'
import { formatNumber } from '@/lib/format'

import type { ReportJobController } from './report-jobs'

export function ReportJobPanel({
  controller,
  label = 'Report',
}: {
  controller: ReportJobController
  /** What is being generated, e.g. "Full inventory valuation". */
  label?: string
}) {
  const { phase, job, error } = controller

  if (phase === 'idle') return null

  if (phase === 'failed') {
    return (
      <Alert variant="destructive" className="flex items-start gap-3">
        <TriangleAlert className="mt-0.5 shrink-0 text-destructive" aria-hidden />
        <div className="min-w-0 flex-1">
          <AlertTitle>{label} could not be generated</AlertTitle>
          <AlertDescription>
            {job?.error_message ??
              error?.message ??
              'The job stopped before it produced a file. Nothing was saved.'}
          </AlertDescription>
        </div>
        <Button variant="ghost" size="icon-sm" onClick={controller.reset} aria-label="Dismiss">
          <X />
        </Button>
      </Alert>
    )
  }

  if (phase === 'ready') {
    return (
      <Alert variant="info" className="flex items-start gap-3">
        <CheckCircle2 className="mt-0.5 shrink-0 text-success" aria-hidden />
        <div className="min-w-0 flex-1">
          <AlertTitle>{label} is ready</AlertTitle>
          <AlertDescription>
            {job ? `${formatNumber(job.row_count)} rows.` : 'The file is ready to download.'}
          </AlertDescription>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button size="sm" onClick={controller.download} loading={controller.isDownloading}>
            <Download />
            Download
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={controller.reset} aria-label="Dismiss">
            <X />
          </Button>
        </div>
      </Alert>
    )
  }

  return (
    <Alert variant="info" className="flex items-start gap-3" aria-live="polite">
      <Loader2 className="mt-0.5 shrink-0 animate-spin text-muted-foreground" aria-hidden />
      <div className="min-w-0 flex-1">
        <AlertTitle>
          {phase === 'queued' ? `${label} is queued` : `${label} is being generated`}
        </AlertTitle>
        <AlertDescription>
          You can keep working - the download appears here when it is done.
        </AlertDescription>
      </div>
      <Button variant="ghost" size="icon-sm" onClick={controller.reset} aria-label="Stop watching">
        <X />
      </Button>
    </Alert>
  )
}

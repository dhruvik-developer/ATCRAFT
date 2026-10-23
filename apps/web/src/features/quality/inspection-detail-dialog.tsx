import { describeError } from '@/lib/messages'
/**
 * Read-only inspection detail, for any status.
 *
 * Decided inspections used to be unopenable - the Inspect action was offered
 * only for PENDING / IN_PROGRESS / REWORK / HOLD - so a photo or a rejection
 * reason was reachable only through the generic Documents page. Every row now
 * opens here on click: the readings per parameter with their photo thumbnails,
 * the rejection reason, the decided quantities, the verdict history and the
 * rework cycles. Inspectable lots keep their Inspect action beside it.
 *
 * Lazy-loaded so the readings, the history and the bench UI stay out of the
 * quality page's first-load chunk; the rework panel lazy-loads one level
 * further down for the same reason.
 */

import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Microscope } from 'lucide-react'
import { Suspense, lazy } from 'react'
import { Link } from 'react-router-dom'

import { DescriptionList, PanelLoader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Skeleton } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { formatDateTime, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { InspectionAttachments, PhotoThumb } from './inspection-attachments'
import {
  isInspectable,
  referenceLink,
  type QcInspectionDetail,
  type QcInspectionRow,
} from './quality-types'

const ReworkPanel = lazy(() => import('./rework-panel').then((m) => ({ default: m.ReworkPanel })))

function ReadingValue({ result }: { result: NonNullable<QcInspectionDetail['results']>[number] }) {
  if (result.measured_value_numeric !== null) return <span>{result.measured_value_numeric}</span>
  if (result.measured_value_bool !== null)
    return <span>{result.measured_value_bool ? 'Pass' : 'Fail'}</span>
  return <span>{result.measured_value_text ?? '-'}</span>
}

export function InspectionDetailDialog({
  inspection,
  open,
  onOpenChange,
  onInspect,
}: {
  inspection: QcInspectionRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onInspect: (inspection: QcInspectionRow) => void
}) {
  const { canAny } = useAuth()
  const inspectionId = inspection?.id ?? ''

  const detail = useQuery({
    queryKey: queryKeys.quality.detail(inspectionId),
    queryFn: () => api.get<QcInspectionDetail>(`/quality/inspections/${inspectionId}`),
    enabled: open && Boolean(inspectionId),
    staleTime: staleTimes.transactional,
  })

  const row = detail.data
  const referenceTo = row
    ? referenceLink(row.reference_type, row.reference_id, row.reference_code)
    : null
  const parameters = row?.parameters ?? []
  const results = row?.results ?? []
  const decisions = row?.decisions ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Inspection {row?.code ?? inspection?.code ?? ''}</DialogTitle>
          <DialogDescription>
            Readings, verdict history and rework - decided lots open here too.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {detail.isLoading || !row ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={row.qc_stage} />
                <StatusBadge status={row.result === 'PENDING' ? row.status : row.result} />
                {row.return_required && <StatusBadge status="RETURN" />}
                {isInspectable(row.status) && canAny(P.QC_INSPECT, P.QC_DECIDE) && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="ml-auto"
                    onClick={() => {
                      onOpenChange(false)
                      if (inspection) onInspect(inspection)
                    }}
                  >
                    <Microscope />
                    Inspect
                  </Button>
                )}
              </div>

              <DescriptionList
                columns={3}
                items={[
                  { label: 'Item', value: row.item_id.slice(0, 8) },
                  { label: 'Warehouse', value: row.warehouse_id.slice(0, 8) },
                  {
                    label: 'Against',
                    value: row.reference_code ?? humanise(row.reference_type),
                  },
                  { label: 'Inspected', value: formatQuantity(row.inspected_quantity) },
                  { label: 'Sample', value: formatQuantity(row.sample_size) },
                  {
                    label: 'Decided',
                    value: row.inspected_at ? formatDateTime(row.inspected_at) : 'Undecided',
                  },
                  { label: 'Accepted', value: formatQuantity(row.accepted_quantity) },
                  { label: 'Rejected', value: formatQuantity(row.rejected_quantity) },
                  { label: 'Rework', value: formatQuantity(row.rework_quantity) },
                  { label: 'Hold', value: formatQuantity(row.hold_quantity) },
                  { label: 'Scrap', value: formatQuantity(row.scrap_quantity) },
                ]}
              />
              {referenceTo && (
                <p className="text-xs">
                  <Link to={referenceTo} className="inline-flex items-center gap-1 hover:underline">
                    Open the {humanise(row.reference_type).toLowerCase()} {row.reference_code ?? ''}
                    <ExternalLink className="h-3 w-3 shrink-0 opacity-60" aria-hidden />
                  </Link>
                </p>
              )}

              {row.rejection_reason && (
                <div className="rounded border border-destructive/40 px-3 py-2">
                  <h3 className="text-sm font-medium">Rejection reason</h3>
                  <p className="mt-0.5 text-sm">{row.rejection_reason}</p>
                </div>
              )}
              {row.remarks && <p className="text-sm text-muted-foreground">{row.remarks}</p>}

              {parameters.length > 0 && (
                <section aria-label="Readings">
                  <h3 className="mb-2 text-sm font-medium">Readings</h3>
                  <ul className="space-y-2">
                    {parameters.map((parameter) => {
                      const reading = results.find((result) => result.parameter_id === parameter.id)
                      return (
                        <li key={parameter.id} className="rounded border px-3 py-2">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-sm font-medium">{parameter.parameter_name}</span>
                            {reading && (
                              <StatusBadge status={reading.is_within_spec ? 'PASSED' : 'FAILED'} />
                            )}
                            {reading && (
                              <span className="ml-auto text-sm">
                                <ReadingValue result={reading} />
                              </span>
                            )}
                          </div>
                          {reading?.remark && (
                            <p className="mt-0.5 text-xs text-muted-foreground">{reading.remark}</p>
                          )}
                          {reading?.photo_document_id && (
                            <div className="mt-1.5">
                              <PhotoThumb
                                documentId={reading.photo_document_id}
                                label={`${row.code} ${parameter.parameter_name}`}
                              />
                            </div>
                          )}
                          {!reading && (
                            <p className="mt-0.5 text-xs text-muted-foreground">No reading</p>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </section>
              )}

              <InspectionAttachments inspectionId={row.id} />

              {decisions.length > 0 && (
                <section aria-label="Verdict history">
                  <h3 className="mb-2 text-sm font-medium">Verdict history</h3>
                  <ol className="space-y-2">
                    {decisions.map((verdict) => (
                      <li key={verdict.id} className="rounded border px-3 py-2 text-xs">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">Cycle {verdict.sequence}</span>
                          <StatusBadge status={verdict.result} />
                          <span className="ml-auto text-muted-foreground">
                            {formatDateTime(verdict.inspected_at)}
                          </span>
                        </div>
                        <p className="mt-1">
                          Accepted {formatQuantity(verdict.accepted_quantity)}, rejected{' '}
                          {formatQuantity(verdict.rejected_quantity)}, rework{' '}
                          {formatQuantity(verdict.rework_quantity)}, hold{' '}
                          {formatQuantity(verdict.hold_quantity)}, scrap{' '}
                          {formatQuantity(verdict.scrap_quantity)}
                        </p>
                        <p className="mt-0.5 text-muted-foreground">{verdict.reason}</p>
                      </li>
                    ))}
                  </ol>
                </section>
              )}

              <Suspense fallback={<PanelLoader />}>
                <ReworkPanel inspection={row} />
              </Suspense>
            </>
          )}
          {detail.error && (
            <p className="text-xs text-destructive">{describeError(detail.error)}</p>
          )}
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

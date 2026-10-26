import { ImageIcon } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'

import { PanelLoader, StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { formatMoney } from '@/lib/format'
import { describeError, errors } from '@/lib/messages'
import type { OffsetPagination, SchemaOf } from '@/types/api'

export function DealerResult({
  loading,
  error,
  children,
}: {
  loading: boolean
  error?: unknown
  children: ReactNode
}) {
  if (loading) return <PanelLoader />
  if (error)
    return (
      <Alert variant="destructive">
        <AlertTitle>{errors.loadFailed}</AlertTitle>
        <AlertDescription>{describeError(error)}</AlertDescription>
      </Alert>
    )
  return children
}

/** Each authenticated image URL has one owner and is released on route changes. */
export function DealerImage({ path, label }: { path: string | null; label: string }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    let objectUrl: string | null = null
    let live = true
    void api.blobUrl(path, { signal: controller.signal }).then(
      (created) => {
        if (!live) return URL.revokeObjectURL(created)
        objectUrl = created
        setUrl(created)
      },
      () => {
        if (live) setUrl(null)
      },
    )
    return () => {
      live = false
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      setUrl(null)
    }
  }, [path])
  return (
    <div className="grid aspect-[4/3] place-items-center overflow-hidden rounded-lg bg-muted">
      {url ? (
        <img src={url} alt={label} loading="lazy" className="h-full w-full object-contain" />
      ) : (
        <ImageIcon className="h-10 w-10 text-muted-foreground" aria-hidden />
      )}
    </div>
  )
}

export function DealerCredit({ credit }: { credit: SchemaOf<'DealerCreditPosition'> }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatTile
        label="Credit limit"
        value={credit.is_unlimited ? 'Unlimited' : formatMoney(credit.credit_limit)}
      />
      <StatTile label="Outstanding" value={formatMoney(credit.outstanding_amount)} />
      <StatTile label="Open order value" value={formatMoney(credit.order_value)} />
      <StatTile
        label="Available credit"
        value={credit.headroom === null ? 'Unlimited' : formatMoney(credit.headroom)}
      />
      {credit.failure_reason && (
        <p className="text-sm text-destructive sm:col-span-2 lg:col-span-4">
          {credit.failure_reason}
        </p>
      )}
    </div>
  )
}

export function DealerPagination({
  pagination,
  page,
  onPage,
}: {
  pagination?: OffsetPagination
  page: number
  onPage: (page: number) => void
}) {
  return (
    <nav aria-label="Pagination" className="mt-5 flex items-center justify-between gap-3">
      <Button
        variant="outline"
        className="min-h-11"
        disabled={page <= 1}
        onClick={() => onPage(page - 1)}
      >
        Previous
      </Button>
      <span className="text-sm text-muted-foreground">Page {page}</span>
      <Button
        variant="outline"
        className="min-h-11"
        disabled={!pagination?.has_next}
        onClick={() => onPage(page + 1)}
      >
        Next
      </Button>
    </nav>
  )
}

import { Undo2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { formatMoney } from '@/lib/format'

/** One invoice a document settled when it was read, which its draft has taken out. */
export interface ReleasedAllocation {
  invoiceId: string
  /** How the editor names the invoice on its own lines. */
  label: string
  /** What the document had put against it. */
  amount: string
}

/**
 * The invoices a document settled that its draft has taken out, each with a
 * way to put it back as it was saved.
 *
 * The candidates beside every allocation editor are the API's open invoices,
 * and one this document settled in full owes nothing else, so it is never
 * among them (`releasedAllocations`). Saving still gives back whatever is left
 * here; this only stops a removal being a one-way door.
 */
export function ReleasedAllocations({
  rows,
  currency,
  document,
  onPutBack,
}: {
  rows: readonly ReleasedAllocation[]
  currency: string | undefined
  /** What carries the settlement: "payment", "receipt", "debit note". */
  document: string
  onPutBack: (invoiceId: string) => void
}) {
  if (rows.length === 0) return null
  return (
    <section
      aria-label={`Taken out of this ${document}`}
      className="space-y-2 rounded-md border border-dashed p-3"
    >
      <p className="text-xs text-muted-foreground">
        Taken out of this {document}. Saving gives these back to their invoices; put one back to
        keep it as it was.
      </p>
      <ul className="space-y-1.5">
        {rows.map((row) => (
          <li
            key={row.invoiceId}
            className="flex flex-wrap items-center justify-between gap-2 text-sm"
          >
            <span className="min-w-0 break-words">
              {row.label}{' '}
              <span className="tabular text-muted-foreground">
                {formatMoney(row.amount, { currency })}
              </span>
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={`Put back ${row.label}`}
              onClick={() => onPutBack(row.invoiceId)}
            >
              <Undo2 />
              Put back
            </Button>
          </li>
        ))}
      </ul>
    </section>
  )
}

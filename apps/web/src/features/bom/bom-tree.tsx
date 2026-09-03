/**
 * The bill's own components, as a table.
 *
 * This was an ARIA tree over a nested structure. There is no nested structure to
 * render: the API returns a bill's own lines (`GET /bom/{id}` -> `lines`) and,
 * separately, an explosion that is **consolidated by item** - one row per part,
 * merged across every branch that reaches it. Neither can be turned back into a
 * faithful parent/child tree without inventing parentage the engine deliberately
 * dissolved, so this renders exactly what the bill states: one level, in line
 * order. The multi-level view is the explosion tab, where the merge is visible
 * rather than hidden.
 *
 * A line names `component_item_id` and no part number, so the codes and names
 * come from the explosion's level-1 rows, passed in as a lookup. It is an
 * enhancement: a line with no match still renders, with a link to the item
 * master in place of a code.
 */

import { Factory, Package } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/primitives'
import { formatPercent, formatQuantity } from '@/lib/format'
import { cn } from '@/lib/utils'

import type { BomLine, ExplodedComponent } from './bom-model'

export function BomStructure({
  lines,
  names,
  label,
  className,
}: {
  lines: BomLine[]
  /** Item id -> the explosion's row for it, when the explosion resolved. */
  names: Map<string, ExplodedComponent>
  /** Accessible name for the table, normally the BOM code. */
  label: string
  className?: string
}) {
  return (
    <div className={cn('space-y-2', className)}>
      <p className="text-xs text-muted-foreground">
        {lines.length} component line{lines.length === 1 ? '' : 's'} on this bill.
      </p>

      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full min-w-[46rem] text-sm">
          <caption className="sr-only">Components of {label}</caption>
          <thead>
            <tr className="border-b bg-muted/50 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="w-14 px-3 py-2 text-right">
                Line
              </th>
              <th scope="col" className="px-3 py-2 text-left">
                Component
              </th>
              <th scope="col" className="w-28 px-3 py-2 text-right">
                Qty per
              </th>
              <th scope="col" className="w-20 px-3 py-2 text-right">
                Scrap
              </th>
              <th scope="col" className="w-20 px-3 py-2 text-right">
                Operation
              </th>
              <th scope="col" className="w-32 px-3 py-2 text-left">
                Source
              </th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line, index) => {
              const named = names.get(line.component_item_id)
              return (
                <tr
                  key={line.id}
                  className={cn('border-b last:border-b-0', index % 2 === 1 && 'bg-muted/20')}
                >
                  <td className="px-3 py-2 text-right tabular text-muted-foreground">
                    {line.line_number}
                  </td>
                  <td className="px-3 py-2">
                    <span className="flex flex-wrap items-center gap-1.5">
                      <Link
                        to={`/items/${line.component_item_id}`}
                        className="font-medium hover:underline"
                      >
                        {named?.item_code ?? 'Open item'}
                      </Link>
                      {line.is_optional && <Badge variant="muted">Optional</Badge>}
                      {line.substitute_for_line_id && (
                        <Badge variant="secondary">Substitute</Badge>
                      )}
                    </span>
                    {named?.item_name && (
                      <span className="block max-w-[24rem] truncate text-xs text-muted-foreground">
                        {named.item_name}
                      </span>
                    )}
                    {line.notes && (
                      <span className="block max-w-[24rem] truncate text-2xs text-muted-foreground">
                        {line.notes}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular">
                    {formatQuantity(line.quantity_per)}
                  </td>
                  <td className="px-3 py-2 text-right tabular text-muted-foreground">
                    {formatPercent(line.scrap_percentage, 2)}
                  </td>
                  <td className="px-3 py-2 text-right tabular text-muted-foreground">
                    {line.operation_sequence ?? '-'}
                  </td>
                  <td className="px-3 py-2">
                    {/* Purchased and manufactured are independent flags on the
                        item master, so an item that is both is shown as both. */}
                    <span className="flex flex-wrap items-center gap-1">
                      {named?.is_manufactured && (
                        <Badge variant="default">
                          <Factory className="h-3 w-3" aria-hidden />
                          Made
                        </Badge>
                      )}
                      {named?.is_purchased && (
                        <Badge variant="secondary">
                          <Package className="h-3 w-3" aria-hidden />
                          Bought
                        </Badge>
                      )}
                      {!named && <span className="text-muted-foreground">-</span>}
                      {named && !named.is_manufactured && !named.is_purchased && (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/**
 * The chrome every chart shares: the axis tick, a tooltip drawn with the popover
 * tokens, and the chart's figures as a table for a screen reader.
 *
 * The receivables ageing chart and the quality reports each wrote their own.
 * The ageing tooltip was Recharts' default with an inline style, so its items
 * took the series colour while the quality tooltip put a dot beside a muted
 * name; and the next chart would have copied one of the two. This module is
 * imported only by chart screens, so it stays in their lazy chunks with
 * Recharts rather than in the shared bundle.
 */

/** Axis labels: small, and muted against the bars they label. */
export const AXIS_TICK = { fontSize: 11, fill: 'hsl(var(--muted-foreground))' } as const

interface TooltipEntry {
  dataKey?: string | number
  name?: string | number
  value?: number | string
  color?: string
}

/**
 * A chart's tooltip, for Recharts' `content` prop:
 * `<Tooltip content={<ChartTooltip formatValue={(value) => formatMoney(value)} />} />`.
 *
 * Recharts' default paints an inline white background, which is unreadable in
 * dark mode; this one wears the popover tokens. Recharts supplies `active`,
 * `payload` and `label` when it clones the element.
 */
export function ChartTooltip({
  active,
  payload,
  label,
  formatValue,
}: {
  active?: boolean
  payload?: TooltipEntry[]
  label?: string | number
  formatValue: (value: number, key: string) => string
}) {
  if (!active || !payload?.length) return null

  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-medium text-popover-foreground">
        {label === undefined ? '' : String(label)}
      </p>
      <ul className="space-y-0.5">
        {payload.map((entry) => (
          <li key={String(entry.dataKey)} className="flex items-center gap-2">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: entry.color }}
              aria-hidden
            />
            <span className="text-muted-foreground">{String(entry.name ?? entry.dataKey)}</span>
            <span className="ml-auto tabular text-popover-foreground">
              {formatValue(Number(entry.value ?? 0), String(entry.dataKey))}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The chart's figures as a real table, for a screen reader and for copying: a
 * coloured bar is not information anybody can act on if they cannot see it.
 * Each row's first cell heads its row, as the first column heads the chart's
 * categories.
 *
 * `sr-only` sits on a wrapping div, not on the table: a table is never narrower
 * than its content, whatever its width, and `overflow` does not clip it, so a
 * visually hidden table still widened a 320px page by its full width
 * (tests/e2e/specs/ui-layout.spec.ts, the ageing chart).
 */
export function ChartDataTable({
  caption,
  headers,
  rows,
}: {
  caption: string
  headers: string[]
  rows: string[][]
}) {
  return (
    <div className="sr-only">
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            {headers.map((header) => (
              <th key={header} scope="col">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([first, ...rest], index) => (
            // Two parties can share a name, so the position keeps the keys apart.
            <tr key={`${index}-${first}`}>
              <th scope="row">{first}</th>
              {rest.map((cell, cellIndex) => (
                <td key={cellIndex}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

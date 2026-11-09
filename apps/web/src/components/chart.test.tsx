/**
 * The chart chrome both chart screens share: one tooltip in the popover tokens,
 * and the figures as a table a screen reader can walk.
 */

import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { formatMoney } from '@/lib/format'

import { AXIS_TICK, ChartDataTable, ChartTooltip } from './chart'

describe('ChartTooltip', () => {
  it('names the category and each series with its value formatted by the screen', () => {
    render(
      <ChartTooltip
        active
        label="Northline Distributors"
        payload={[
          { dataKey: 'current', name: 'Not yet due', value: 125000, color: 'hsl(var(--primary))' },
          { dataKey: 'over_90', name: 'Over 90 days', value: 4000.5 },
        ]}
        formatValue={(value) => formatMoney(value, { currency: 'INR' })}
      />,
    )

    expect(screen.getByText('Northline Distributors')).toHaveClass('font-medium')
    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]!).getByText('Not yet due')).toHaveClass('text-muted-foreground')
    expect(rows[0]).toHaveTextContent(formatMoney(125000, { currency: 'INR' }))
    expect(rows[1]).toHaveTextContent(formatMoney(4000.5, { currency: 'INR' }))
    // Drawn with the popover tokens, never Recharts' inline white box.
    expect(screen.getByText('Northline Distributors').parentElement).toHaveClass('bg-popover')
  })

  it('draws nothing while no bar is hovered', () => {
    const { container } = render(<ChartTooltip active={false} payload={[]} formatValue={String} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('ChartDataTable', () => {
  it('reads the chart as a captioned table whose first column heads each row', () => {
    render(
      <ChartDataTable
        caption="Outstanding amount by party and age bucket, in INR"
        headers={['Party', 'Not yet due', 'Over 90 days']}
        rows={[
          ['Northline Distributors', '1,25,000.00', '4,000.50'],
          // Two parties may share a name; both rows are still drawn.
          ['Northline Distributors', '10.00', '0.00'],
        ]}
      />,
    )

    const table = screen.getByRole('table', {
      name: 'Outstanding amount by party and age bucket, in INR',
    })
    expect(table).toHaveClass('sr-only')
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((cell) => cell.textContent),
    ).toEqual(['Party', 'Not yet due', 'Over 90 days'])
    expect(
      within(table).getAllByRole('rowheader', { name: 'Northline Distributors' }),
    ).toHaveLength(2)
    expect(
      within(table)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(['1,25,000.00', '4,000.50', '10.00', '0.00'])
  })

  it('shares one muted axis tick between the charts', () => {
    expect(AXIS_TICK).toEqual({ fontSize: 11, fill: 'hsl(var(--muted-foreground))' })
  })
})

/**
 * The table parts carry DataTable's classes, so a screen's own table reads the
 * way every list does, and a right-aligned column lines its digits up.
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroller,
  tableCellClassName,
  tableHeadClassName,
} from './table'

function Statement() {
  return (
    <TableScroller label="Account statement">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Reference</TableHead>
            <TableHead align="right">Debit</TableHead>
            <TableHead align="center">Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>INV-0001</TableCell>
            <TableCell align="right">1,25,000.00</TableCell>
            <TableCell align="center">Open</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </TableScroller>
  )
}

describe('the table parts', () => {
  it('right-align a figure in tabular digits, under a right-aligned heading', () => {
    render(<Statement />)

    expect(screen.getByRole('columnheader', { name: 'Debit' })).toHaveClass('text-right')
    expect(screen.getByRole('cell', { name: '1,25,000.00' })).toHaveClass('text-right', 'tabular')
    expect(screen.getByRole('cell', { name: 'INV-0001' })).not.toHaveClass('tabular')
    expect(screen.getByRole('cell', { name: 'Open' })).toHaveClass('text-center')
  })

  it('wear the header and cell classes DataTable uses, with column scope', () => {
    render(<Statement />)

    const head = screen.getByRole('columnheader', { name: 'Reference' })
    expect(head).toHaveClass(...tableHeadClassName.split(' '), 'text-left')
    expect(head).toHaveAttribute('scope', 'col')
    expect(screen.getByRole('cell', { name: 'INV-0001' })).toHaveClass(
      ...tableCellClassName.split(' '),
    )
  })

  it('scroll inside a named region a keyboard can reach', async () => {
    render(
      <>
        <button type="button">Before</button>
        <Statement />
      </>,
    )

    const region = screen.getByRole('region', { name: 'Account statement' })
    expect(within(region).getByRole('table')).toBeVisible()
    expect(region).toHaveClass('overflow-x-auto')
    await userEvent.click(screen.getByRole('button', { name: 'Before' }))
    await userEvent.tab()
    expect(region).toHaveFocus()
  })
})

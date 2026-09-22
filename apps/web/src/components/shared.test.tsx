import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DateRangeFilter, StatusBadge } from './shared'

describe('StatusBadge', () => {
  it('colours a lifecycle status instead of falling through to the unmapped grey', () => {
    const { rerender } = render(<StatusBadge status="INACTIVE" />)
    const inactive = screen.getByText('Inactive')
    expect(inactive.className).toContain('bg-muted')
    expect(inactive.className).not.toContain('bg-secondary')

    rerender(<StatusBadge status="PACKED" />)
    expect(screen.getByText('Packed').className).toContain('text-success')

    rerender(<StatusBadge status="UNDER_REVIEW" />)
    expect(screen.getByText('Under Review').className).toContain('text-warning')

    rerender(<StatusBadge status="BREAKDOWN" />)
    expect(screen.getByText('Breakdown').className).toContain('text-destructive')

    rerender(<StatusBadge status="NOT_A_REAL_STATUS" />)
    expect(screen.getByText('Not A Real Status').className).toContain('bg-secondary')
  })
})

describe('DateRangeFilter', () => {
  it('labels each input, names them from the id prefix and reports each edge on its own', () => {
    const onFromChange = vi.fn()
    const onToChange = vi.fn()
    render(
      <DateRangeFilter
        id="orders-required"
        from="2026-09-01"
        to="2026-09-30"
        onFromChange={onFromChange}
        onToChange={onToChange}
      />,
    )

    const from = screen.getByLabelText('From')
    const to = screen.getByLabelText('To')
    expect(from).toHaveAttribute('id', 'orders-required-from')
    expect(from).toHaveAttribute('type', 'date')
    expect(from).toHaveValue('2026-09-01')
    expect(to).toHaveAttribute('id', 'orders-required-to')
    expect(to).toHaveValue('2026-09-30')

    fireEvent.change(from, { target: { value: '2026-08-15' } })
    expect(onFromChange).toHaveBeenCalledWith('2026-08-15')
    expect(onToChange).not.toHaveBeenCalled()

    // Clearing the field hands back the empty string, so a caller can map it to
    // "no bound" (`value || undefined`) exactly as the hand-rolled inputs did.
    fireEvent.change(to, { target: { value: '' } })
    expect(onToChange).toHaveBeenCalledWith('')
  })

  it('takes custom labels and passes bounds through to the inputs', () => {
    render(
      <DateRangeFilter
        id="audit"
        from="2026-09-01"
        to="2026-09-04"
        onFromChange={() => undefined}
        onToChange={() => undefined}
        fromLabel="Expected from"
        toLabel="Until"
        fromInputProps={{ max: '2026-09-04', required: true }}
        toInputProps={{ min: '2026-09-01', max: '2026-09-05' }}
      />,
    )

    const from = screen.getByLabelText('Expected from')
    expect(from).toHaveAttribute('max', '2026-09-04')
    expect(from).toBeRequired()

    const to = screen.getByLabelText('Until')
    expect(to).toHaveAttribute('min', '2026-09-01')
    expect(to).toHaveAttribute('max', '2026-09-05')
  })

  it('keeps a label beside its own input so the pairs wrap as units', () => {
    render(
      <DateRangeFilter
        id="grn-received"
        from=""
        to=""
        onFromChange={() => undefined}
        onToChange={() => undefined}
      />,
    )

    const from = screen.getByLabelText('From')
    const to = screen.getByLabelText('To')
    const fromPair = from.parentElement
    const toPair = to.parentElement
    expect(fromPair).not.toBe(toPair)
    expect(fromPair).toContainElement(screen.getByText('From'))
    expect(toPair).toContainElement(screen.getByText('To'))
    // Both pairs sit in the same wrapping row.
    expect(fromPair?.parentElement).toBe(toPair?.parentElement)
    expect(fromPair?.parentElement).toHaveClass('flex-wrap')
  })
})

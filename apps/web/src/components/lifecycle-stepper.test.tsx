import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { LifecycleStepper } from './lifecycle-stepper'

describe('LifecycleStepper', () => {
  const stages = ['Draft', 'Approved', 'Sent', 'Received'] as const

  it('marks the stage the document is at as the current step, and ticks the ones behind it', () => {
    render(<LifecycleStepper stages={stages} current={2} label="Purchase order lifecycle" />)
    const list = screen.getByRole('list', { name: 'Purchase order lifecycle' })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(4)

    expect(within(list).getByText('Sent')).toHaveAttribute('aria-current', 'step')
    expect(list.querySelectorAll('[aria-current]')).toHaveLength(1)
    // Passed stages carry the tick; the current and later ones do not.
    expect(items.map((item) => item.querySelector('svg') !== null)).toEqual([
      true,
      true,
      false,
      false,
    ])
  })

  it('draws a stopped document as having reached no stage', () => {
    render(<LifecycleStepper stages={stages} current={2} stopped label="RFQ lifecycle" />)
    const list = screen.getByRole('list', { name: 'RFQ lifecycle' })
    expect(list.querySelectorAll('[aria-current]')).toHaveLength(0)
    expect(list.querySelectorAll('svg')).toHaveLength(0)
    expect(within(list).getByText('Draft')).toBeVisible()
  })

  it('marks no stage when the status is outside the stages', () => {
    render(<LifecycleStepper stages={stages} current={-1} label="Order progress" />)
    expect(
      screen.getByRole('list', { name: 'Order progress' }).querySelectorAll('[aria-current]'),
    ).toHaveLength(0)
  })
})

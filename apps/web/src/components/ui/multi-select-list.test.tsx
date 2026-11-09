/**
 * The several-choice list's keyboard model: one Tab stop, the arrow keys, Space
 * or Enter to tick, a reachable row that cannot be ticked, and the status said
 * beside the list rather than inside it.
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { MultiSelectList, type MultiSelectListHandle } from './multi-select-list'

interface Unit {
  id: string
  label: string
  held?: boolean
}

const UNITS: Unit[] = [
  { id: 'u1', label: 'SN-001' },
  { id: 'u2', label: 'SN-002', held: true },
  { id: 'u3', label: 'SN-003' },
  { id: 'u4', label: 'SN-004' },
]

function Units({
  initial = [],
  status = '4 units',
  onLeaveTop,
  onType,
}: {
  initial?: string[]
  status?: string
  onLeaveTop?: () => void
  onType?: (character: string) => void
}) {
  const [chosen, setChosen] = useState(initial)
  const list = useRef<MultiSelectListHandle>(null)
  return (
    <>
      <button type="button">Before</button>
      <button type="button" onClick={() => list.current?.focus()}>
        Into the list
      </button>
      <MultiSelectList
        handleRef={list}
        label="Serial units"
        rows={UNITS}
        getKey={(unit) => unit.id}
        isSelected={(unit) => chosen.includes(unit.id)}
        isDisabled={(unit) => unit.held === true}
        onToggle={(unit) =>
          setChosen((current) =>
            current.includes(unit.id)
              ? current.filter((id) => id !== unit.id)
              : [...current, unit.id],
          )
        }
        renderOption={(unit) => (
          <>
            {unit.label}
            {unit.held && ' On QC hold'}
          </>
        )}
        status={status}
        statusClassName="sr-only"
        onLeaveTop={onLeaveTop}
        onType={onType}
      />
      <button type="button">After</button>
    </>
  )
}

describe('MultiSelectList', () => {
  it('is one Tab stop, however many rows it lists', async () => {
    const user = userEvent.setup()
    render(<Units />)

    await user.click(screen.getByRole('button', { name: 'Into the list' }))
    expect(screen.getByRole('option', { name: 'SN-001' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByRole('option', { name: 'SN-001' })).toHaveFocus()

    const listbox = screen.getByRole('listbox', { name: 'Serial units' })
    expect(listbox).toHaveAttribute('aria-multiselectable', 'true')
    const stops = within(listbox)
      .getAllByRole('option')
      .filter((option) => option.tabIndex === 0)
    expect(stops).toHaveLength(1)
  })

  it('moves with the arrow keys, Home and End, and ticks with Space or Enter', async () => {
    const user = userEvent.setup()
    render(<Units />)
    await user.click(screen.getByRole('button', { name: 'Into the list' }))

    await user.keyboard('{ArrowDown}{ArrowDown}')
    expect(screen.getByRole('option', { name: 'SN-003' })).toHaveFocus()
    await user.keyboard(' ')
    expect(screen.getByRole('option', { name: 'SN-003' })).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{End}')
    expect(screen.getByRole('option', { name: 'SN-004' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('option', { name: 'SN-004' })).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{Home}')
    expect(screen.getByRole('option', { name: 'SN-001' })).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    // No row above the first: focus stays rather than wrapping.
    expect(screen.getByRole('option', { name: 'SN-001' })).toHaveFocus()

    // Ticking again unticks.
    await user.click(screen.getByRole('option', { name: 'SN-003' }))
    expect(screen.getByRole('option', { name: 'SN-003' })).toHaveAttribute('aria-selected', 'false')
  })

  it('reads a row that cannot be ticked, with its reason, without ticking it', async () => {
    const user = userEvent.setup()
    render(<Units />)
    await user.click(screen.getByRole('button', { name: 'Into the list' }))

    await user.keyboard('{ArrowDown}')
    const held = screen.getByRole('option', { name: 'SN-002 On QC hold' })
    expect(held).toHaveFocus()
    expect(held).toHaveAttribute('aria-disabled', 'true')
    await user.keyboard(' ')
    await user.click(held)
    expect(held).toHaveAttribute('aria-selected', 'false')
  })

  it('enters at the first ticked row and gives every row a 44px target', async () => {
    const user = userEvent.setup()
    render(<Units initial={['u3']} />)
    await user.click(screen.getByRole('button', { name: 'Into the list' }))

    expect(screen.getByRole('option', { name: 'SN-003' })).toHaveFocus()
    for (const option of screen.getAllByRole('option')) {
      expect(option).toHaveClass('min-h-11')
    }
  })

  it('hands the top and typed characters back to the search box above it', async () => {
    const user = userEvent.setup()
    const onLeaveTop = vi.fn()
    const onType = vi.fn()
    render(<Units onLeaveTop={onLeaveTop} onType={onType} />)
    await user.click(screen.getByRole('button', { name: 'Into the list' }))

    await user.keyboard('{ArrowUp}')
    expect(onLeaveTop).toHaveBeenCalledTimes(1)
    await user.keyboard('7')
    expect(onType).toHaveBeenCalledWith('7')
    // Space ticks; it is not a character for the search.
    await user.keyboard(' ')
    expect(onType).toHaveBeenCalledTimes(1)
  })

  it('says its status beside the list, never inside it, and keeps the region mounted', () => {
    const { rerender } = render(<Units status="4 units" />)
    const region = screen.getByRole('status')
    expect(region).toHaveTextContent('4 units')
    expect(screen.getByRole('listbox')).not.toContainElement(region)

    rerender(<Units status="Searching..." />)
    expect(screen.getByRole('status')).toBe(region)
    expect(region).toHaveTextContent('Searching...')
  })
})

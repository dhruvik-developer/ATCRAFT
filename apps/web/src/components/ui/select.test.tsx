/**
 * A select must keep a saved value whose option has not loaded yet.
 *
 * Inside a `<form>`, Radix renders a hidden native `<select>` and re-dispatches
 * every change of the controlled value through it. A form reset to a saved
 * record sets the value while the picker's list is often still in flight, and a
 * native select with no matching `<option>` can only hold "" - so its change
 * event reached `onValueChange('')`, and a form wired to that cleared the field.
 * The record's warehouse, plant or cost centre became "none", and the next save
 * wrote it. Nothing on screen said so: by the time the list arrived the value
 * was already gone, and the select simply showed its placeholder.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select'

function SavedRecordForm({ onValueChange }: { onValueChange: (value: string) => void }) {
  const [value, setValue] = useState('')
  const [options, setOptions] = useState<string[]>([])

  return (
    <form>
      <button type="button" onClick={() => setValue('wh-main')}>
        Reset to the saved record
      </button>
      <button type="button" onClick={() => setOptions(['wh-main', 'wh-fg'])}>
        The list arrives
      </button>
      <Select
        value={value}
        onValueChange={(next) => {
          onValueChange(next)
          setValue(next)
        }}
      >
        <SelectTrigger aria-label="Warehouse">
          <SelectValue placeholder="No warehouse" />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </form>
  )
}

describe('a value set before its option exists', () => {
  it('is kept, and nobody is told it changed to nothing', async () => {
    const user = userEvent.setup()
    const changed = vi.fn()
    render(<SavedRecordForm onValueChange={changed} />)

    await user.click(screen.getByRole('button', { name: 'Reset to the saved record' }))
    await user.click(screen.getByRole('button', { name: 'The list arrives' }))

    expect(changed).not.toHaveBeenCalled()
    expect(screen.getByRole('combobox', { name: 'Warehouse' })).toHaveTextContent('wh-main')
  })

  it('still reports a choice a person actually makes', async () => {
    const user = userEvent.setup()
    const changed = vi.fn()
    render(<SavedRecordForm onValueChange={changed} />)

    await user.click(screen.getByRole('button', { name: 'The list arrives' }))
    await user.click(screen.getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: 'wh-fg' }))

    expect(changed).toHaveBeenCalledWith('wh-fg')
    expect(screen.getByRole('combobox', { name: 'Warehouse' })).toHaveTextContent('wh-fg')
  })
})

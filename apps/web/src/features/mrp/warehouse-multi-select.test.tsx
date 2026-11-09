/**
 * The MRP warehouse choice: a combobox that opens on the arrow keys, over the
 * shared several-choice list, with its loading and empty states said in a live
 * region rather than drawn where no screen reader is told about them.
 */

import { screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { Label } from '@/components/ui/primitives'
import { P } from '@/lib/permissions'
import { page, renderScreen } from '@/test/harness'

import { WarehouseMultiSelect } from './warehouse-multi-select'

const WAREHOUSES = [
  { id: 'wh-1', code: 'MAIN', name: 'Main store' },
  { id: 'wh-2', code: 'QC', name: 'Quality store' },
  { id: 'wh-3', code: 'FG', name: 'Finished goods' },
]

function Scope({ maxSelections }: { maxSelections?: number }) {
  const [value, setValue] = useState<string[]>([])
  return (
    <>
      <Label htmlFor="scope">Warehouses</Label>
      <WarehouseMultiSelect
        id="scope"
        value={value}
        onChange={setValue}
        maxSelections={maxSelections}
      />
      <span data-testid="chosen">{value.join(',')}</span>
    </>
  )
}

function render(rows = WAREHOUSES, maxSelections?: number) {
  return renderScreen(<Scope maxSelections={maxSelections} />, {
    permissions: [P.MRP_VIEW, P.WAREHOUSE_VIEW],
    routes: { 'GET /warehouses': page(rows) },
  })
}

describe('the MRP warehouse choice', () => {
  it('opens on ArrowDown and ticks warehouses from the keyboard in one Tab stop', async () => {
    const { user } = render()
    const trigger = await screen.findByRole('combobox', { name: 'Warehouses' })
    await screen.findByText('All warehouses')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    trigger.focus()
    await user.keyboard('{ArrowDown}')
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    const list = await screen.findByRole('listbox', { name: 'Warehouses' })
    const options = within(list).getAllByRole('option')
    expect(options.filter((option) => option.tabIndex === 0)).toHaveLength(1)
    // The popover hands focus to the list's Tab stop.
    expect(within(list).getByRole('option', { name: /MAIN/ })).toHaveFocus()

    await user.keyboard('{ArrowDown} ')
    expect(within(list).getByRole('option', { name: /QC/ })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await user.keyboard('{End}{Enter}')
    expect(screen.getByRole('status')).toHaveTextContent('3 warehouses')
    expect(screen.getByTestId('chosen')).toHaveTextContent('wh-2,wh-3')
  })

  it('lists a warehouse past the limit, with the limit stated, but does not tick it', async () => {
    const { user } = render(WAREHOUSES, 1)
    await user.click(await screen.findByRole('combobox', { name: 'Warehouses' }))
    expect(await screen.findByText(/Select up to 1\./)).toBeVisible()

    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    const past = screen.getByRole('option', { name: /QC/ })
    expect(past).toHaveAttribute('aria-disabled', 'true')
    await user.click(past)
    expect(screen.getByTestId('chosen')).toHaveTextContent('wh-1')
  })

  it('says an empty warehouse list in the live region', async () => {
    const { user } = render([])
    await user.click(await screen.findByRole('combobox', { name: 'Warehouses' }))

    expect(await screen.findByRole('status')).toHaveTextContent('No warehouses are configured yet.')
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})

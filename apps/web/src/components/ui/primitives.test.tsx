/**
 * NativeSelect: the browser's select, drawn the way Input is.
 *
 * Thirty-three selects were hand-styled ten ways - 40 and 44px beside the 36px
 * Input, the panel border rather than the input one, 16px text - and none showed
 * aria-invalid or a disabled look, so a refused field stayed grey while the
 * Input beside it turned red, and a disabled select looked editable.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { useForm } from 'react-hook-form'
import { describe, expect, it, vi } from 'vitest'

import { Input, NativeSelect } from './primitives'

function KindForm({ onSubmit }: { onSubmit: (values: { kind: string }) => void }) {
  const { register, handleSubmit } = useForm({ defaultValues: { kind: 'OTHER' } })
  return (
    <form onSubmit={handleSubmit(onSubmit)}>
      <label htmlFor="kind">Kind</label>
      <NativeSelect id="kind" {...register('kind')}>
        <option value="OTHER">Other</option>
        <option value="INVOICE">Invoice</option>
      </NativeSelect>
      <button type="submit">Save</button>
    </form>
  )
}

describe('NativeSelect', () => {
  it('takes a register() field like an Input, ref included', async () => {
    const onSubmit = vi.fn()
    render(<KindForm onSubmit={onSubmit} />)

    await userEvent.selectOptions(screen.getByLabelText('Kind'), 'INVOICE')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(onSubmit).toHaveBeenCalledWith({ kind: 'INVOICE' }, expect.anything())
  })

  it('forwards its ref to the select element', () => {
    const ref = createRef<HTMLSelectElement>()
    render(
      <NativeSelect ref={ref} aria-label="Channel">
        <option value="SMS">SMS</option>
      </NativeSelect>,
    )
    expect(ref.current).toBe(screen.getByRole('combobox', { name: 'Channel' }))
  })

  it('is drawn with the Input tokens, including the refused and disabled looks', () => {
    render(
      <>
        <Input aria-label="Reference" />
        <NativeSelect aria-label="Relation" aria-invalid disabled>
          <option value="OPTIONAL">Optional add-on</option>
        </NativeSelect>
      </>,
    )
    const select = screen.getByRole('combobox', { name: 'Relation' })
    const input = screen.getByRole('textbox', { name: 'Reference' })

    for (const shared of ['h-9', 'border-input', 'text-sm', 'shadow-sm']) {
      expect(select).toHaveClass(shared)
      expect(input).toHaveClass(shared)
    }
    expect(select).toHaveClass(
      'aria-[invalid=true]:border-destructive',
      'disabled:cursor-not-allowed',
      'disabled:opacity-50',
    )
    expect(select).toBeDisabled()
    expect(select).toHaveAttribute('aria-invalid', 'true')
  })

  it('sizes for a thumb or for a table footer, and a caller can still adjust it', () => {
    render(
      <>
        <NativeSelect aria-label="Portal" size="touch" className="mt-1">
          <option>One</option>
        </NativeSelect>
        <NativeSelect aria-label="Rows" size="sm" className="w-auto">
          <option>25</option>
        </NativeSelect>
      </>,
    )
    const portal = screen.getByRole('combobox', { name: 'Portal' })
    expect(portal).toHaveClass('min-h-11', 'mt-1')
    expect(portal).not.toHaveClass('h-9')

    const rows = screen.getByRole('combobox', { name: 'Rows' })
    expect(rows).toHaveClass('h-8', 'w-auto')
    expect(rows).not.toHaveClass('w-full')
  })

  it('takes its natural height when it allows several choices', async () => {
    const onChange = vi.fn()
    render(
      <NativeSelect
        aria-label="Operators"
        multiple
        value={[]}
        onChange={(event) =>
          onChange(Array.from(event.target.selectedOptions).map((option) => option.value))
        }
      >
        <option value="op-1">Asha</option>
        <option value="op-2">Ravi</option>
      </NativeSelect>,
    )
    const select = screen.getByRole('listbox', { name: 'Operators' })
    expect(select).toHaveClass('h-auto')
    expect(select).not.toHaveClass('h-9')

    await userEvent.selectOptions(select, ['op-1', 'op-2'])
    expect(onChange).toHaveBeenCalled()
  })
})

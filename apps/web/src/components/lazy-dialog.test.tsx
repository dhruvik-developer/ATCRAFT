import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { lazyDialog } from './lazy-dialog'

vi.mock('@/lib/error-reporting', () => ({ reportError: vi.fn(), errorReportingEnabled: false }))

function form(mounted = vi.fn()) {
  return function Form({ open }: { open: boolean; onOpenChange: (open: boolean) => void }) {
    const [value, setValue] = useState('')
    useEffect(() => mounted(), [])
    return open ? (
      <label>
        Item name
        <input value={value} onChange={(event) => setValue(event.target.value)} />
      </label>
    ) : null
  }
}

describe('forms loaded on first use', () => {
  it('leaves the page usable without downloading a closed form', async () => {
    let finish!: (module: { default: ReturnType<typeof form> }) => void
    const loader = vi.fn(
      () =>
        new Promise<{ default: ReturnType<typeof form> }>((resolve) => {
          finish = resolve
        }),
    )
    const Form = lazyDialog(loader)
    const onOpenChange = vi.fn()
    const view = render(
      <>
        <h1>Items</h1>
        <Form open={false} onOpenChange={onOpenChange} />
      </>,
    )
    expect(screen.getByText('Items')).toBeVisible()
    expect(loader).not.toHaveBeenCalled()

    view.rerender(
      <>
        <h1>Items</h1>
        <Form open onOpenChange={onOpenChange} />
      </>,
    )
    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Loading form' })).toBeVisible()
    expect(loader).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)

    view.rerender(
      <>
        <h1>Items</h1>
        <Form open={false} onOpenChange={onOpenChange} />
      </>,
    )
    await act(async () => finish({ default: form() }))
    expect(screen.queryByLabelText('Item name')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()

    view.rerender(
      <>
        <h1>Items</h1>
        <Form open onOpenChange={onOpenChange} />
      </>,
    )
    expect(await screen.findByLabelText('Item name')).toBeVisible()
  })

  it('retains the loaded form and its state when closed and reopened', async () => {
    const mounted = vi.fn()
    const loader = vi.fn(async () => ({ default: form(mounted) }))
    const Form = lazyDialog(loader)
    const onOpenChange = vi.fn()
    const view = render(<Form open onOpenChange={onOpenChange} />)
    await userEvent.type(await screen.findByLabelText('Item name'), 'Draft item')
    view.rerender(<Form open={false} onOpenChange={onOpenChange} />)
    expect(screen.queryByLabelText('Item name')).toBeNull()
    view.rerender(<Form open onOpenChange={onOpenChange} />)
    expect(await screen.findByLabelText('Item name')).toHaveValue('Draft item')
    expect(loader).toHaveBeenCalledTimes(1)
    expect(mounted).toHaveBeenCalledTimes(1)
  })

  it('retries a failed form download without replacing the page', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const loader = vi
      .fn<() => Promise<{ default: ReturnType<typeof form> }>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ default: form() })
    const Form = lazyDialog(loader)
    render(
      <>
        <h1>Items</h1>
        <Form open onOpenChange={vi.fn()} />
      </>,
    )
    expect(await screen.findByRole('heading', { name: 'Could not open this form' })).toBeVisible()
    expect(screen.getByText('Items')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByLabelText('Item name')).toBeVisible()
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2))
  })
})

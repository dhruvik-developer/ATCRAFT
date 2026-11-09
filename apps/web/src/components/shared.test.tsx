import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/lib/api-client'
import { messages } from '@/lib/messages'

import {
  BadgeDismiss,
  DateRangeFilter,
  FieldError,
  LoadError,
  PanelDenied,
  ReasonDialog,
  StatusBadge,
} from './shared'
import { CardTitle } from './ui/primitives'

describe('ReasonDialog', () => {
  function Harness({
    onConfirm,
    error,
    destructive,
  }: {
    onConfirm: (reason: string) => void
    error?: unknown
    destructive?: boolean
  }) {
    const [open, setOpen] = useState(true)
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Reopen
        </button>
        <ReasonDialog
          open={open}
          onOpenChange={setOpen}
          title="Cancel SR-001?"
          confirmLabel="Cancel return"
          destructive={destructive}
          error={error}
          onConfirm={onConfirm}
        />
      </>
    )
  }

  it('opens empty and refuses a blank or two-character reason without confirming', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(<Harness onConfirm={onConfirm} />)

    const dialog = screen.getByRole('dialog', { name: 'Cancel SR-001?' })
    const box = within(dialog).getByRole('textbox', { name: 'Reason' })
    // Never pre-filled: whatever a hurried OK sends is what the person typed.
    expect(box).toHaveValue('')
    expect(within(dialog).getByText(messages.prompts.reasonRequired)).toBeVisible()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel return' }))
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    expect(box).toHaveAttribute('aria-invalid', 'true')

    await user.type(box, ' ab ')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel return' }))
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('confirms with the trimmed reason and stops at the API maximum', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(<Harness onConfirm={onConfirm} />)

    const box = screen.getByRole('textbox', { name: 'Reason' })
    expect(box).toHaveAttribute('maxlength', '255')
    await user.type(box, '  Customer changed the order  ')
    await user.click(screen.getByRole('button', { name: 'Cancel return' }))

    expect(onConfirm).toHaveBeenCalledExactlyOnceWith('Customer changed the order')
  })

  it('forgets the reason and the complaint when it closes', async () => {
    const user = userEvent.setup()
    render(<Harness onConfirm={vi.fn()} />)

    await user.type(screen.getByRole('textbox', { name: 'Reason' }), 'ab')
    await user.click(screen.getByRole('button', { name: 'Cancel return' }))
    expect(screen.getByText(messages.errors.reasonTooShort)).toBeVisible()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reopen' }))

    expect(screen.getByRole('textbox', { name: 'Reason' })).toHaveValue('')
    expect(screen.queryByText(messages.errors.reasonTooShort)).not.toBeInTheDocument()
  })

  it('uses the destructive confirmation only when asked', () => {
    const { unmount } = render(<Harness onConfirm={vi.fn()} destructive />)
    expect(screen.getByRole('button', { name: 'Cancel return' })).toHaveClass('bg-destructive')
    unmount()

    render(<Harness onConfirm={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Cancel return' })).toHaveClass('bg-primary')
  })

  it("shows the API's refusal inside the dialog, preferring the field message", () => {
    const refused = new ApiError(422, 'VALIDATION_ERROR', 'Request validation failed', {
      fields: { reason: ['String should have at most 255 characters'] },
    })
    const { unmount } = render(<Harness onConfirm={vi.fn()} error={refused} />)
    expect(within(screen.getByRole('dialog')).getByText(/at most 255 characters/)).toBeVisible()
    unmount()

    const conflict = new ApiError(409, 'CONFLICT', 'The return has already been received.')
    render(<Harness onConfirm={vi.fn()} error={conflict} />)
    expect(
      within(screen.getByRole('dialog')).getByText('The return has already been received.'),
    ).toBeVisible()
  })
})

describe('CardTitle', () => {
  it('follows the page heading and preserves an explicit heading level', () => {
    const { rerender } = render(<CardTitle>Section</CardTitle>)
    expect(screen.getByRole('heading', { name: 'Section', level: 2 })).toBeVisible()
    rerender(
      <CardTitle asChild>
        <h1>Sign in</h1>
      </CardTitle>,
    )
    expect(screen.getByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible()
  })
})

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

describe('BadgeDismiss', () => {
  it('is a named button that removes its chip, and never submits a form around it', () => {
    const onClick = vi.fn()
    const onSubmit = vi.fn((event: React.FormEvent) => event.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <span>
          One sales order
          <BadgeDismiss label="Show every sales order" onClick={onClick} />
        </span>
      </form>,
    )

    const dismiss = screen.getByRole('button', { name: 'Show every sales order' })
    expect(dismiss).toHaveAttribute('type', 'button')
    fireEvent.click(dismiss)
    expect(onClick).toHaveBeenCalledOnce()
    expect(onSubmit).not.toHaveBeenCalled()
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

describe('FieldError', () => {
  it('renders nothing without a message, and an announced line with one', () => {
    const { container, rerender } = render(<FieldError message={undefined} />)
    expect(container).toBeEmptyDOMElement()

    rerender(<FieldError id="email-error" message="Email is required." />)
    const line = screen.getByRole('alert')
    expect(line).toHaveTextContent('Email is required.')
    expect(line).toHaveAttribute('id', 'email-error')
  })

  it('can be an input description, joins a list and takes a spacing override', () => {
    render(
      <>
        <input aria-label="Quantity" aria-describedby="quantity-error" />
        <FieldError
          id="quantity-error"
          className="mt-2"
          message={['Quantity is required.', 'Quantity must be a whole number.']}
        />
      </>,
    )
    expect(screen.getByRole('textbox', { name: 'Quantity' })).toHaveAccessibleDescription(
      messages.joinValidationMessages([
        'Quantity is required.',
        'Quantity must be a whole number.',
      ]),
    )
    expect(screen.getByRole('alert')).toHaveClass('mt-2', 'text-xs', 'text-destructive')
    expect(screen.getByRole('alert')).not.toHaveClass('mt-1')
  })
})

describe('LoadError', () => {
  it('heads the failure with the shared headline and shows the API reason verbatim', () => {
    render(<LoadError error={new ApiError(404, 'NOT_FOUND', 'Purchase order not found')} />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(messages.errors.loadFailed)
    expect(alert).toHaveTextContent('Purchase order not found')
    // Without a way to ask again it offers none, rather than a button that does nothing.
    expect(within(alert).queryByRole('button')).not.toBeInTheDocument()
  })

  it('names what failed and asks again without submitting the form around it', async () => {
    const onRetry = vi.fn()
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => event.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <LoadError
          title="Could not load this quotation"
          error={new ApiError(503, 'SERVICE_UNAVAILABLE', 'Try later')}
          onRetry={onRetry}
        />
      </form>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this quotation')
    await userEvent.click(screen.getByRole('button', { name: messages.empty.loadFailedAction }))
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('holds the retry while one is already in flight', () => {
    render(<LoadError error={new Error('x')} onRetry={() => undefined} retrying />)
    expect(screen.getByRole('button', { name: /Try again/ })).toBeDisabled()
  })

  it('says its headline as text, so a failed read never adds a level to the page outline', () => {
    // As an h5 it jumped every page's outline from h1 to h5 (axe heading-order).
    render(<LoadError title="Could not load the templates" error={null} />)
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
    const headline = screen.getByText('Could not load the templates')
    expect(headline.tagName).toBe('P')
    expect(headline).toHaveClass('font-medium')
  })
})

describe('PanelDenied', () => {
  it('says which panel the role cannot read, in the shared words', () => {
    render(<PanelDenied what="Prices" />)
    expect(screen.getByText(messages.empty.panelDenied('Prices'))).toBeInTheDocument()
    expect(screen.getByText(messages.empty.panelDeniedDetail)).toBeInTheDocument()
  })
})

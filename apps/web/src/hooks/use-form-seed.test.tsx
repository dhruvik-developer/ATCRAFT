import { render, renderHook, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useForm } from 'react-hook-form'
import { describe, expect, it, vi } from 'vitest'

import { useFormSeed, type FormSeedKey } from './use-form-seed'

interface Props {
  when: boolean
  seedKey: FormSeedKey
  seed: () => void
}

function renderSeed(initial: Props) {
  return renderHook(({ when, seedKey, seed }: Props) => useFormSeed(when, seedKey, seed), {
    initialProps: initial,
  })
}

describe('useFormSeed', () => {
  it('seeds when the dialog opens, and not while it is closed', () => {
    const seed = vi.fn()
    const { rerender } = renderSeed({ when: false, seedKey: 'a', seed })
    expect(seed).not.toHaveBeenCalled()

    rerender({ when: true, seedKey: 'a', seed })
    expect(seed).toHaveBeenCalledTimes(1)
  })

  it('seeds again on every opening', () => {
    const seed = vi.fn()
    const { rerender } = renderSeed({ when: true, seedKey: 'a', seed })
    rerender({ when: false, seedKey: 'a', seed })
    rerender({ when: true, seedKey: 'a', seed })
    expect(seed).toHaveBeenCalledTimes(2)
  })

  it('seeds again when a different record is opened', () => {
    const seed = vi.fn()
    const { rerender } = renderSeed({ when: true, seedKey: 'a', seed })
    rerender({ when: true, seedKey: 'b', seed })
    expect(seed).toHaveBeenCalledTimes(2)
  })

  it('does not seed again for a new seed closure or a refetched record object', () => {
    const first = vi.fn()
    const { rerender } = renderSeed({ when: true, seedKey: 'a', seed: first })
    // A background refetch: a new record object, so a new closure, same id.
    const second = vi.fn()
    rerender({ when: true, seedKey: 'a', seed: second })
    rerender({ when: true, seedKey: 'a', seed: second })
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).not.toHaveBeenCalled()
  })

  it('runs the closure of the render that triggered it, never a stale one', () => {
    const seen: string[] = []
    const { rerender } = renderSeed({ when: false, seedKey: null, seed: () => seen.push('old') })
    rerender({ when: false, seedKey: null, seed: () => seen.push('stale') })
    rerender({ when: true, seedKey: null, seed: () => seen.push('current') })
    expect(seen).toEqual(['current'])
  })

  it('compares a list key by its contents', () => {
    const seed = vi.fn()
    const { rerender } = renderSeed({ when: true, seedKey: ['v1', null], seed })
    rerender({ when: true, seedKey: ['v1', null], seed })
    expect(seed).toHaveBeenCalledTimes(1)

    rerender({ when: true, seedKey: ['v1', 'v1'], seed })
    expect(seed).toHaveBeenCalledTimes(2)
  })

  it('ignores a key change while closed, and seeds once on the next opening', () => {
    const seed = vi.fn()
    const { rerender } = renderSeed({ when: false, seedKey: 'a', seed })
    rerender({ when: false, seedKey: 'b', seed })
    expect(seed).not.toHaveBeenCalled()
    rerender({ when: true, seedKey: 'b', seed })
    expect(seed).toHaveBeenCalledTimes(1)
  })
})

// The shape every edit dialog that reads its own record now has: blank while
// the read is in flight, the record once it lands, and the person's typing
// left alone by a background refetch of that same record.
interface Vendor {
  id: string
  name: string
}

function VendorForm({ open, loaded }: { open: boolean; loaded: Vendor | undefined }) {
  const { register, reset } = useForm<{ name: string }>({ defaultValues: { name: '' } })
  useFormSeed(open, loaded?.id ?? null, () => reset({ name: loaded?.name ?? '' }))
  return <input aria-label="Name" {...register('name')} />
}

describe('useFormSeed on a form that reads its record', () => {
  it('seeds blank while the record loads, then the record, then keeps the typing', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<VendorForm open loaded={undefined} />)
    const name = screen.getByLabelText('Name')
    expect(name).toHaveValue('')

    rerender(<VendorForm open loaded={{ id: 'v1', name: 'Acme' }} />)
    expect(name).toHaveValue('Acme')

    await user.clear(name)
    await user.type(name, 'Acme Hardware')
    // A background refetch: a new object for the same vendor.
    rerender(<VendorForm open loaded={{ id: 'v1', name: 'Acme' }} />)
    expect(name).toHaveValue('Acme Hardware')
  })

  it('never carries the record edited last into the next opening', () => {
    const { rerender } = render(<VendorForm open loaded={{ id: 'v1', name: 'Acme' }} />)
    rerender(<VendorForm open={false} loaded={{ id: 'v1', name: 'Acme' }} />)
    // The next vendor's read has not answered yet.
    rerender(<VendorForm open loaded={undefined} />)
    expect(screen.getByLabelText('Name')).toHaveValue('')
  })
})

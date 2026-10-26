import { act, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody } from '@/test/harness'
import { itemAccessory } from '@/test/product-description-fixtures'

import { AccessoryFormDialog, ItemAccessoriesPanel } from './item-accessories-panel'

const accessory = itemAccessory()
const pickerItem = {
  id: accessory.accessory_item_id,
  code: accessory.accessory_code,
  name: accessory.accessory_name,
  is_active: true,
  base_uom_id: 'uom',
  is_batch_tracked: false,
  is_serial_tracked: false,
}

describe('item accessories', () => {
  it('shows included items without stock, pricing or management dependencies', async () => {
    const { fetch } = renderScreen(<ItemAccessoriesPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items/item-1/accessories': page([accessory]) },
    })
    expect(await screen.findByText('Fixing screw kit')).toBeVisible()
    expect(screen.getByText('In the box')).toBeVisible()
    expect(screen.getByText('1.25')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add accessory' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit Fixing screw kit' })).toBeNull()
    expect(
      fetch.calls.some((call) => call.path.includes('inventory') || call.path.includes('prices')),
    ).toBe(false)
  })

  it.each(['OPTIONAL', 'INCLUDED'] as const)(
    'adds a decimal %s link using the correct item-picker scope',
    async (relation) => {
      let pickerScope: string | null | undefined
      const close = vi.fn()
      const { user, fetch } = renderScreen(
        <AccessoryFormDialog itemId="item-1" open onOpenChange={close} />,
        {
          permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
          routes: {
            'GET /products/items': ({ url }) => {
              pickerScope = url.searchParams.get('can_sell')
              return page([pickerItem])
            },
            'POST /products/items/item-1/accessories': ok({ ...accessory, relation }),
          },
        },
      )
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Relation' })).toBeEnabled())
      await user.selectOptions(screen.getByRole('combobox', { name: 'Relation' }), relation)
      await user.click(screen.getByRole('combobox', { name: 'Accessory item' }))
      await user.click(await screen.findByRole('option', { name: /SCREW-KIT/ }))
      const quantity = screen.getByRole('textbox', { name: 'Quantity' })
      await user.clear(quantity)
      await user.paste('1.2500')
      await user.click(
        within(screen.getByRole('dialog')).getByRole('button', { name: 'Add accessory' }),
      )
      await waitFor(() =>
        expect(fetch.requestFor('POST', '/products/items/item-1/accessories')).toEqual({
          accessory_item_id: accessory.accessory_item_id,
          quantity: '1.2500',
          relation,
          sort_order: 0,
          notes: null,
        }),
      )
      expect(pickerScope).toBe(relation === 'OPTIONAL' ? 'true' : null)
      expect(close).toHaveBeenCalledWith(false)
    },
  )

  it('edits the position to reorder and blocks duplicate saves or dismissal while pending', async () => {
    let complete!: (body: RouteBody) => void
    const pending = new Promise<RouteBody>((resolve) => {
      complete = resolve
    })
    const close = vi.fn()
    const { user, fetch } = renderScreen(
      <AccessoryFormDialog itemId="item-1" accessory={accessory} open onOpenChange={close} />,
      {
        permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
        routes: { 'PATCH /products/item-accessories/accessory-1': () => pending },
      },
    )
    const position = screen.getByRole('textbox', { name: 'Position in the list' })
    await waitFor(() => expect(position).toBeEnabled())
    await user.clear(position)
    await user.paste('2')
    await user.click(screen.getByRole('button', { name: 'Save accessory' }))
    await waitFor(() =>
      expect(fetch.requestFor('PATCH', '/products/item-accessories/accessory-1')).toEqual({
        quantity: '1.25',
        relation: 'INCLUDED',
        sort_order: 2,
        notes: accessory.notes,
      }),
    )
    expect(position).toBeDisabled()
    expect(screen.getByRole('button', { name: /Save accessory/ })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(close).not.toHaveBeenCalled()
    expect(fetch.calls.filter((call) => call.method === 'PATCH')).toHaveLength(1)
    await act(async () => {
      complete(ok(accessory))
    })
    await waitFor(() => expect(close).toHaveBeenCalledWith(false))
  })

  it('rejects zero quantity locally and preserves a backend-refused edit', async () => {
    const { user, fetch } = renderScreen(
      <AccessoryFormDialog itemId="item-1" accessory={accessory} open onOpenChange={() => {}} />,
      {
        permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
        routes: {
          'PATCH /products/item-accessories/accessory-1': fail(
            422,
            'VALIDATION_ERROR',
            'Invalid accessory',
            { fields: { relation: ['An optional accessory must be sellable.'] } },
          ),
        },
      },
    )
    const quantity = screen.getByRole('textbox', { name: 'Quantity' })
    await waitFor(() => expect(quantity).toBeEnabled())
    await user.clear(quantity)
    await user.paste('0')
    await user.click(screen.getByRole('button', { name: 'Save accessory' }))
    expect(
      await screen.findByText('Enter a positive quantity with up to 4 decimal places.'),
    ).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
    await user.clear(quantity)
    await user.paste('2.5')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Relation' }), 'OPTIONAL')
    await user.click(screen.getByRole('button', { name: 'Save accessory' }))
    expect((await screen.findAllByText('An optional accessory must be sellable.'))[0]).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Relation' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(quantity).toHaveValue('2.5')
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('requires an audit reason for removal and shows a refusal without losing it', async () => {
    let reason: string | null = null
    const { user, fetch } = renderScreen(<ItemAccessoriesPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
      routes: {
        'GET /products/items/item-1/accessories': page([accessory]),
        'DELETE /products/item-accessories/accessory-1': ({ url }) => {
          reason = url.searchParams.get('reason')
          return fail(409, 'CONFLICT', 'The accessory was changed by another user.')
        },
      },
    })
    await user.click(await screen.findByRole('button', { name: 'Remove Fixing screw kit' }))
    await user.click(screen.getByRole('button', { name: 'Remove accessory' }))
    expect(fetch.calls.some((call) => call.method === 'DELETE')).toBe(false)
    await user.click(screen.getByRole('textbox', { name: 'Reason' }))
    await user.paste('No longer supplied in the box')
    await user.click(screen.getByRole('button', { name: 'Remove accessory' }))
    expect(await screen.findByText('The accessory was changed by another user.')).toBeVisible()
    expect(reason).toBe('No longer supplied in the box')
    expect(screen.getByRole('textbox', { name: 'Reason' })).toHaveValue(reason)
  })
})

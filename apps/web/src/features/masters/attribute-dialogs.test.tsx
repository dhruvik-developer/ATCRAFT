/**
 * The screen where a client adds a finish, a size or a colour.
 *
 * Two properties decide whether this screen is safe to hand to an
 * administrator, and neither is visible in a screenshot.
 *
 * **The kind is fixed once created.** `ItemAttributeUpdate` does not carry
 * `attribute`, so moving a value from Finish to Colour is not something the API
 * offers - it would silently retarget every item pointing at the row, and the
 * code is unique per kind, so the move could collide as well. The control must
 * therefore be locked on an edit rather than offering a change the server can
 * only refuse.
 *
 * **The picker caches must be invalidated on both branches.** The tab reads the
 * `masters` branch; every item and variant form reads the shared attribute
 * options branch. Invalidating only the first is the classic failure here: the
 * value appears on this screen immediately and stays missing from the form that
 * needed it for the length of the reference-data stale window.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { fail, ok, renderScreen, type RouteTable } from '@/test/harness'

import { ItemAttributeFormDialog } from './attribute-dialogs'

const FINISH = {
  id: 'att-1',
  attribute: 'FINISH' as const,
  code: 'ANTIQUE-BRASS',
  name: 'Antique Brass',
  sort_order: 0,
  is_active: true,
}

function renderDialog(attribute: typeof FINISH | null, routes: RouteTable = {}) {
  return renderScreen(
    <ItemAttributeFormDialog open onOpenChange={() => {}} attribute={attribute} />,
    { permissions: [], routes },
  )
}

describe('creating a value', () => {
  it('posts the kind, the code and the name', async () => {
    const { user, fetch } = renderDialog(null, {
      'POST /products/attributes': ok(FINISH),
    })

    await user.type(screen.getByLabelText('Code'), 'ANTIQUE-BRASS')
    await user.type(screen.getByLabelText('Name'), 'Antique Brass')
    await user.click(screen.getByRole('button', { name: /create value/i }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/products/attributes'))) as Record<
      string,
      unknown
    >

    expect(body.attribute).toBe('FINISH')
    expect(body.code).toBe('ANTIQUE-BRASS')
    expect(body.name).toBe('Antique Brass')
    expect(body.is_active).toBe(true)
  })

  it('refuses a value with no name before anything reaches the network', async () => {
    const { user, fetch } = renderDialog(null)

    await user.type(screen.getByLabelText('Code'), 'ANTIQUE-BRASS')
    await user.click(screen.getByRole('button', { name: /create value/i }))

    expect(await screen.findByText('Name is required.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('offers all three kinds, because one screen governs all three', async () => {
    renderDialog(null)

    const trigger = screen.getByRole('combobox', { name: /attribute/i })

    expect(trigger).toBeEnabled()
  })
})

describe('editing a value', () => {
  it('sends the name without the code, because the code identifies the row', async () => {
    const { user, fetch } = renderDialog(FINISH, {
      'PATCH /products/attributes/att-1': ok({ ...FINISH, name: 'Antique Brass Satin' }),
    })

    const name = screen.getByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Antique Brass Satin')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/products/attributes/att-1'),
    )) as Record<string, unknown>

    expect(body.name).toBe('Antique Brass Satin')
    expect(body).not.toHaveProperty('code')
    expect(body).not.toHaveProperty('attribute')
  })

  it('locks the kind rather than offering a change the API will refuse', async () => {
    renderDialog(FINISH)

    expect(screen.getByRole('combobox', { name: /attribute/i })).toBeDisabled()
    expect(screen.getByLabelText('Code')).toBeDisabled()
  })

  it('says what retiring does, because it is not a delete', async () => {
    renderDialog(FINISH)

    expect(screen.getByText(/items already using it keep it/i)).toBeVisible()
  })
})

describe('the server has the last word', () => {
  it('puts a duplicate-code refusal on the field that caused it', async () => {
    const { user, fetch } = renderDialog(null, {
      'POST /products/attributes': fail(
        409,
        'DUPLICATE',
        'A finish with code ANTIQUE-BRASS already exists.',
        { fields: { code: ['A finish with that code already exists.'] } },
      ),
    })

    await user.type(screen.getByLabelText('Code'), 'ANTIQUE-BRASS')
    await user.type(screen.getByLabelText('Name'), 'Antique Brass')
    await user.click(screen.getByRole('button', { name: /create value/i }))

    expect(await screen.findByText('A finish with that code already exists.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(true)
  })
})

describe('what the screen tells the operator', () => {
  it('explains that renaming is safe, which is the reason this is a table', async () => {
    renderDialog(FINISH)

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/renaming is safe/i)).toBeVisible()
  })
})
